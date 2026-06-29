"""Core routing engine — resolves tiers, manages fallbacks, streams responses."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from typing import Any, AsyncGenerator

import httpx

from . import database as db
from .config import get_config, resolve_tier, resolve_direct_alias, get_provider_config, get_passthrough_config
from .sanitizer import sanitize_for_model, auto_strip_on_400

logger = logging.getLogger("llm-router.engine")


async def _forward_to_provider(
    client: httpx.AsyncClient,
    provider_name: str,
    provider_cfg: dict[str, Any],
    model: str,
    payload: dict[str, Any],
    *,
    strip_applied: list[str] | None = None,
) -> tuple[int, dict[str, Any] | bytes, str]:
    """Send request to a provider. Returns (status_code, response_data, error_msg).
    
    For streaming requests, returns (200, raw_bytes, "") and the caller handles SSE.
    For non-streaming, returns parsed JSON.
    """
    endpoint = provider_cfg["endpoint"].rstrip("/")
    url = f"{endpoint}/chat/completions"
    api_key = provider_cfg.get("key", "")
    timeout = provider_cfg.get("timeout", 60)

    headers = {
        "Content-Type": "application/json",
    }
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    # Sanitize params for this specific model
    payload, stripped = sanitize_for_model(payload, provider_name, model)
    if strip_applied is not None:
        strip_applied.extend(stripped)

    # Override the model name in the payload
    body = {**payload, "model": model}
    is_stream_req = body.get("stream", False)

    try:
        if is_stream_req:
            # Streaming: use client.send(stream=True) to avoid buffering the
            # full SSE response before forwarding (which drops chunks on
            # providers that send many small deltas like kiro/claude).
            timeout_obj = httpx.Timeout(timeout)
            request = client.build_request(
                "POST", url, json=body, headers=headers, timeout=timeout_obj,
            )
            resp = await client.send(request, stream=True)
            if resp.status_code == 200:
                return 200, resp, ""
            # Non-200: drain body, close stream, return error text
            await resp.aread()
            text = resp.text
            await resp.aclose()
            return resp.status_code, text, f"HTTP {resp.status_code}: {text[:200]}"
        resp = await client.post(url, json=body, headers=headers, timeout=timeout)
        if resp.status_code == 200:
            return 200, resp, ""
        return resp.status_code, resp.text, f"HTTP {resp.status_code}: {resp.text[:200]}"
    except httpx.TimeoutException:
        return 504, b"", f"Timeout after {timeout}s"
    except httpx.ConnectError as e:
        return 503, b"", f"Connection error: {e}"
    except Exception as e:
        logger.exception("Unexpected error forwarding to %s", provider_name)
        return 500, b"", str(e)




async def _retry_with_backoff(
    client: httpx.AsyncClient,
    prov_name: str,
    prov_cfg: dict,
    model_name: str,
    payload: dict,
    strip_applied: list,
    max_attempts: int = 3,
) -> tuple[int, Any, str]:
    """Retry a provider call with exponential backoff on 5xx/429."""
    last = (0, None, "")
    delay = 1.0
    for attempt in range(max_attempts):
        status, response, error = await _forward_to_provider(
            client, prov_name, prov_cfg, model_name, payload, strip_applied=strip_applied,
        )
        if status not in (429, 500, 502, 503, 504):
            return status, response, error
        last = (status, response, error)
        if attempt < max_attempts - 1:
            import asyncio as _asyncio
            await _asyncio.sleep(delay)
            delay *= 2
    return last


async def route_request(
    incoming_payload: dict[str, Any],
    client: httpx.AsyncClient,
) -> AsyncGenerator[tuple[str, Any], None]:
    """Main routing logic. Yields SSE chunks for streaming, or final response.
    
    Yields tuples of (event_type, data):
        ("status", {"phase": "routing", "tier": "...", "provider": "..."})
        ("chunk", b"...sse data...")
        ("error", {"code": 400, "message": "..."})
        ("done", {"provider": "...", "model": "...", "latency_ms": ...})
    """
    config = get_config()
    request_id = str(uuid.uuid4())[:8]
    incoming_model = incoming_payload.get("model", "")
    is_stream = incoming_payload.get("stream", False)
    start_time = time.monotonic()

    # ── Resolve: direct alias FIRST (highest priority) ───────────
    direct = resolve_direct_alias(incoming_model)
    if direct:
        tier_name = "direct"
        candidates = [{"provider": direct["provider"], "model": direct["model"]}]
        # Override candidates list — direct aliases never fall back to others
        max_direct_retries = direct["max_retries"]
        yield ("status", {
            "phase": "resolved",
            "tier": "direct",
            "alias": incoming_model,
            "provider": direct["provider"],
            "model": direct["model"],
            "max_retries": max_direct_retries,
            "candidates": 1,
        })
    else:
        max_direct_retries = None  # signal: not a direct alias
        # ── Resolve tier (existing logic) ─────────────────────────
        tier_result = resolve_tier(incoming_model)
        if tier_result:
            tier_name, tier_cfg = tier_result
            candidates = tier_cfg.get("models", [])
            strategy = tier_cfg.get("strategy", "fallback")
            yield ("status", {"phase": "resolved", "tier": tier_name, "candidates": len(candidates)})
        else:
            # Passthrough — model is not a tier name or alias
            tier_name = "passthrough"
            pt = get_passthrough_config()
            if not pt["enabled"]:
                yield ("error", {"code": 404, "message": f"No tier or alias matched '{incoming_model}', and passthrough is disabled"})
                return

            allowed = set(pt["providers"])
            candidates = []

            # 1. Try live catalog cache (has all models the provider actually serves)
            #    Imported lazily to avoid circular import with main.py
            from .main import _catalog_cache
            catalog = _catalog_cache.get("data", [])
            for m in catalog:
                if m["id"] == incoming_model and m["provider"] in allowed:
                    candidates.append({"provider": m["provider"], "model": incoming_model})

            # 2. Cold-start fallback: catalog empty → check static config models map
            if not candidates:
                for prov_name, prov_cfg in config.get("providers", {}).items():
                    if prov_name not in allowed:
                        continue
                    models = prov_cfg.get("models", {})
                    if incoming_model in models or "*" in models:
                        candidates.append({"provider": prov_name, "model": incoming_model})

            if not candidates:
                yield ("error", {"code": 404, "message": f"Model '{incoming_model}' not found in passthrough providers: {sorted(allowed)}"})
                return
            yield ("status", {"phase": "resolved", "tier": "passthrough", "candidates": len(candidates)})

    # ── Try each candidate (fallback chain) ───────────────────────
    last_error = ""
    attempted = []

    for i, candidate in enumerate(candidates):
        prov_name = candidate["provider"]
        model_name = candidate["model"]

        # Check circuit breaker
        if await db.is_quarantined(prov_name):
            yield ("status", {"phase": "skip", "provider": prov_name, "reason": "quarantined"})
            attempted.append({"provider": prov_name, "model": model_name, "skipped": True})
            continue

        yield ("status", {"phase": "attempt", "provider": prov_name, "model": model_name, "attempt": i + 1})

        prov_cfg = get_provider_config(prov_name)
        if not prov_cfg:
            continue

        strip_log: list[str] = []
        # Direct aliases retry on transient failures (5xx/429); tiers fall back.
        if max_direct_retries is not None:
            status, response, error = await _retry_with_backoff(
                client, prov_name, prov_cfg, model_name,
                incoming_payload, strip_applied=strip_log,
                max_attempts=max_direct_retries + 1,
            )
        else:
            status, response, error = await _forward_to_provider(
                client, prov_name, prov_cfg, model_name,
                incoming_payload, strip_applied=strip_log,
            )

        latency_ms = int((time.monotonic() - start_time) * 1000)

        if status == 200:
            # ── Success ────────────────────────────────────────────
            await db.update_provider_health(prov_name, success=True)

            if is_stream and isinstance(response, httpx.Response):
                # Stream SSE chunks back to client using aiter_bytes for raw
                # byte-perfect forwarding. aiter_lines() drops empty lines
                # (SSE event delimiters) and decodes inconsistently, which
                # caused chunks to be lost on providers like kiro that emit
                # many small reasoning_content deltas.
                collected_prompt_tokens = 0
                collected_completion_tokens = 0
                buffer = ""
                try:
                    async for raw in response.aiter_bytes():
                        if not raw:
                            continue
                        chunk_str = raw.decode("utf-8", errors="replace")
                        # Forward bytes as-is to preserve SSE framing exactly
                        yield ("chunk", chunk_str)
                        # Accumulate for usage parsing (split on event boundaries)
                        buffer += chunk_str
                        while "\n\n" in buffer:
                            event, buffer = buffer.split("\n\n", 1)
                            for line in event.split("\n"):
                                if not line.startswith("data: "):
                                    continue
                                data_str = line[6:]
                                if "[DONE]" in data_str:
                                    continue
                                try:
                                    chunk_data = json.loads(data_str)
                                    usage = chunk_data.get("usage") or {}
                                    pt = usage.get("prompt_tokens")
                                    ct = usage.get("completion_tokens")
                                    if pt is not None:
                                        collected_prompt_tokens = pt
                                    if ct is not None:
                                        collected_completion_tokens = ct
                                except json.JSONDecodeError:
                                    pass
                finally:
                    # Ensure the streaming response is always closed, even on
                    # client disconnect (GeneratorExit) or exceptions.
                    await response.aclose()

                # Log to DB BEFORE yielding "done" so the frontend refresh sees fresh data
                model_cfg = prov_cfg.get("models", {}).get(model_name, {})
                cost = (collected_prompt_tokens / 1000) * model_cfg.get("cost_per_1k_input", 0) + \
                       (collected_completion_tokens / 1000) * model_cfg.get("cost_per_1k_output", 0)
                await db.log_request(
                    request_id=request_id,
                    incoming_model=incoming_model,
                    resolved_provider=prov_name,
                    resolved_model=model_name,
                    tier=tier_name,
                    status_code=200,
                    latency_ms=latency_ms,
                    prompt_tokens=collected_prompt_tokens,
                    completion_tokens=collected_completion_tokens,
                    cost=round(cost, 6),
                    request_body=json.dumps(incoming_payload)[:5000],
                    stripped_params=",".join(strip_log) if strip_log else None,
                )

                yield ("done", {
                    "request_id": request_id,
                    "provider": prov_name,
                    "model": model_name,
                    "tier": tier_name,
                    "latency_ms": latency_ms,
                    "cost": round(cost, 6),
                    "prompt_tokens": collected_prompt_tokens,
                    "completion_tokens": collected_completion_tokens,
                    "stripped_params": strip_log,
                })
            elif isinstance(response, httpx.Response):
                # Non-streaming JSON response
                data = response.json()
                usage = data.get("usage", {})
                prompt_tokens = usage.get("prompt_tokens", 0)
                completion_tokens = usage.get("completion_tokens", 0)
                model_cfg = prov_cfg.get("models", {}).get(model_name, {})
                cost = (prompt_tokens / 1000) * model_cfg.get("cost_per_1k_input", 0) + \
                       (completion_tokens / 1000) * model_cfg.get("cost_per_1k_output", 0)

                yield ("response", data)

                await db.log_request(
                    request_id=request_id,
                    incoming_model=incoming_model,
                    resolved_provider=prov_name,
                    resolved_model=model_name,
                    tier=tier_name,
                    status_code=200,
                    latency_ms=latency_ms,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    cost=round(cost, 6),
                    request_body=json.dumps(incoming_payload)[:5000],
                    stripped_params=",".join(strip_log) if strip_log else None,
                )

                yield ("done", {
                    "request_id": request_id,
                    "provider": prov_name,
                    "model": model_name,
                    "tier": tier_name,
                    "latency_ms": latency_ms,
                    "cost": round(cost, 6),
                    "stripped_params": strip_log,
                })
            return  # Success — stop trying

        # ── Failure ────────────────────────────────────────────────
        last_error = error
        attempted.append({
            "provider": prov_name,
            "model": model_name,
            "status": status,
            "error": error[:200],
            "stripped": strip_log,
        })
        await db.update_provider_health(prov_name, success=False, error=error)
        yield ("status", {"phase": "failed", "provider": prov_name, "status": status, "error": error[:200]})
        # Fire webhook if provider just got quarantined
        try:
            if await db.is_quarantined(prov_name):
                from .main import send_webhook_alert
                await send_webhook_alert(
                    "provider_quarantined",
                    f"Provider `{prov_name}` quarantined after consecutive failures. Last error: {error[:100]}",
                    "critical",
                )
        except Exception:
            pass

        # ── Auto-strip retry on 400 ───────────────────────────────
        if status == 400:
            stripped_payload, auto_stripped = auto_strip_on_400(incoming_payload)
            if auto_stripped:
                yield ("status", {"phase": "retry_strip", "stripped": auto_stripped, "provider": prov_name})
                strip_log_2: list[str] = []
                status2, response2, error2 = await _forward_to_provider(
                    client, prov_name, prov_cfg, model_name,
                    stripped_payload, strip_applied=strip_log_2,
                )
                if status2 == 200:
                    await db.update_provider_health(prov_name, success=True)
                    latency_ms2 = int((time.monotonic() - start_time) * 1000)
                    if is_stream and isinstance(response2, httpx.Response):
                        async for line in response2.aiter_lines():
                            if line.strip():
                                yield ("chunk", line + "\n\n")
                    elif isinstance(response2, httpx.Response):
                        yield ("response", response2.json())

                    yield ("done", {
                        "request_id": request_id,
                        "provider": prov_name,
                        "model": model_name,
                        "tier": tier_name,
                        "latency_ms": latency_ms2,
                        "retried": True,
                        "stripped_params": strip_log_2,
                    })
                    await db.log_request(
                        request_id=request_id,
                        incoming_model=incoming_model,
                        resolved_provider=prov_name,
                        resolved_model=model_name,
                        tier=tier_name,
                        status_code=200,
                        latency_ms=latency_ms2,
                        retried=True,
                        stripped_params=",".join(strip_log_2) if strip_log_2 else None,
                    )
                    return
                else:
                    yield ("status", {"phase": "retry_failed", "status": status2, "error": error2[:200]})
                # Propagate stripped payload to remaining candidates
                incoming_payload = stripped_payload

    # ── All providers exhausted ────────────────────────────────────
    yield ("error", {
        "code": 502,
        "message": f"All providers failed. Last error: {last_error}",
        "attempted": attempted,
    })
    await db.log_request(
        request_id=request_id,
        incoming_model=incoming_model,
        tier=tier_name,
        status_code=502,
        error=last_error[:500],
    )
