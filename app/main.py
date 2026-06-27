"""FastAPI server — OpenAI-compatible proxy + dashboard API."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Request, HTTPException, Depends, WebSocket, WebSocketDisconnect
from fastapi.responses import (
    StreamingResponse,
    JSONResponse,
    FileResponse,
    HTMLResponse,
)
from fastapi.staticfiles import StaticFiles

from .config import load_config, get_config, watch_config
from . import database as db
from .engine import route_request

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("llm-router")

# Shared HTTP client
_http_client: httpx.AsyncClient | None = None

# WebSocket subscribers for live feed
_ws_subscribers: set[WebSocket] = set()

STATIC_DIR = Path(__file__).resolve().parent / "static"


def get_auth_token() -> str | None:
    """Return the configured auth token (or None = no auth)."""
    cfg = get_config()
    return cfg.get("server", {}).get("auth_token")


def verify_auth(request: Request):
    """Dependency: validate Bearer token for /v1/ endpoints.

    Checks in order:
      1. Server auth token (from config server.auth_token)
      2. Generated API keys (from config api_keys[])
    """
    # First: check server auth token
    token = get_auth_token()
    if token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer ") and auth_header[7:] == token:
            return

    # Second: check API keys
    if verify_api_key(request):
        return

    # If neither is configured, allow (open mode)
    if not token and not get_config().get("api_keys"):
        return

    raise HTTPException(status_code=401, detail="Invalid or missing auth token")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _http_client
    load_config()
    await db.init_db()
    _http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=10, read=120, write=10, pool=5),
        limits=httpx.Limits(max_connections=50, max_keepalive_connections=20),
    )
    # Start config watcher
    watcher = asyncio.create_task(watch_config())
    probe = asyncio.create_task(health_probe_loop())
    logger.info("╔══════════════════════════════════════════╗")
    logger.info("║  FLLMingo v1.3.0b3 — [SYSTEM ACTIVE]     ║")
    logger.info("╚══════════════════════════════════════════╝")
    yield
    watcher.cancel()
    probe.cancel()
    await _http_client.aclose()


app = FastAPI(title="FLLMingo", version="1.3.0b4", lifespan=lifespan)


@app.middleware("http")
async def dashboard_auth_middleware(request: Request, call_next):
    """Protect /api/* and /docs routes with dashboard token (except /api/health and /api/auth/status)."""
    path = request.url.path
    _docs_paths = ("/docs", "/redoc", "/openapi.json")
    _is_docs = path in _docs_paths or path.startswith("/docs/")
    _is_api = path.startswith("/api/") and not path.startswith("/api/health") and not path.startswith("/api/auth/status")
    if _is_api or _is_docs:
        token = get_auth_token()
        if token:
            # Check X-Dashboard-Token header first, then fallback to Bearer
            provided = request.headers.get("X-Dashboard-Token", "")
            if not provided:
                auth = request.headers.get("Authorization", "")
                if auth.startswith("Bearer "):
                    provided = auth[7:]
            if provided != token:
                return JSONResponse(
                    status_code=401,
                    content={"detail": "Dashboard authentication required"},
                )
    return await call_next(request)




# ═══════════════════════════════════════════════════════════════════
#  Rate Limiting (toggle-able via config.rate_limit.enabled)
# ═══════════════════════════════════════════════════════════════════

import time as _time
from collections import defaultdict, deque
_rate_buckets: dict[str, deque] = defaultdict(deque)


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    """Token-bucket rate limiter for /v1/* endpoints (configurable)."""
    if not request.url.path.startswith("/v1/"):
        return await call_next(request)
    cfg = get_config().get("rate_limit", {})
    if not cfg.get("enabled", False):
        return await call_next(request)

    rpm = int(cfg.get("requests_per_minute", 60))
    mode = cfg.get("per", "ip")  # "ip" or "api_key"

    # Identify bucket key
    if mode == "api_key":
        auth = request.headers.get("Authorization", "")
        key = auth[7:] if auth.startswith("Bearer ") else "anon"
    else:
        key = request.client.host if request.client else "unknown"

    now = _time.monotonic()
    bucket = _rate_buckets[key]
    # Drop entries older than 60s
    while bucket and bucket[0] < now - 60:
        bucket.popleft()
    if len(bucket) >= rpm:
        retry_after = int(60 - (now - bucket[0]))
        return JSONResponse(
            status_code=429,
            content={"detail": f"Rate limit exceeded: {rpm}/min", "retry_after": retry_after},
            headers={"Retry-After": str(max(1, retry_after))},
        )
    bucket.append(now)
    return await call_next(request)


# ═══════════════════════════════════════════════════════════════════
#  Webhook Alerts (Discord, Slack, custom)
# ═══════════════════════════════════════════════════════════════════

async def send_webhook_alert(event: str, message: str, level: str = "info"):
    """POST an alert to the configured webhook URL.

    Event types: provider_down, budget_exceeded, key_revoked, server_error
    Level: info | warning | critical
    """
    cfg = get_config().get("webhook", {})
    if not cfg.get("enabled", False):
        return
    url = cfg.get("url", "")
    if not url:
        return
    icon = {"info": "ℹ️", "warning": "⚠️", "critical": "🚨"}.get(level, "•")
    payload_type = cfg.get("type", "discord")
    if payload_type == "discord":
        payload = {"content": f"{icon} **[{event}]** {message}"}
    elif payload_type == "slack":
        payload = {"text": f"{icon} *[{event}]* {message}"}
    else:
        payload = {"event": event, "message": message, "level": level}
    try:
        if _http_client:
            await _http_client.post(url, json=payload, timeout=10)
    except Exception as e:
        logger.warning(f"Webhook delivery failed: {e}")


@app.post("/api/webhook/test")
async def api_webhook_test():
    """Send a test webhook to verify configuration."""
    cfg = get_config().get("webhook", {})
    if not cfg.get("enabled", False):
        raise HTTPException(400, "Webhook is not enabled in config")
    if not cfg.get("url"):
        raise HTTPException(400, "Webhook URL is not set")
    await send_webhook_alert("test", "Webhook is working correctly! ð¦© FLLMingo says hi.", "info")
    return {"status": "ok", "message": "Test webhook sent"}


@app.get("/api/settings/integrations")
async def api_get_integrations():
    """Return current rate_limit + webhook config (URL redacted)."""
    cfg = get_config()
    rl = cfg.get("rate_limit", {})
    wh = cfg.get("webhook", {})
    url = wh.get("url", "")
    return {
        "rate_limit": {
            "enabled": bool(rl.get("enabled", False)),
            "requests_per_minute": int(rl.get("requests_per_minute", 60)),
            "per": rl.get("per", "ip"),
        },
        "webhook": {
            "enabled": bool(wh.get("enabled", False)),
            "type": wh.get("type", "discord"),
            "url_set": bool(url),
            "url_preview": (url[:30] + "..." + url[-10:]) if len(url) > 50 else (url and "***"),
        },
    }


@app.put("/api/settings/integrations")
async def api_update_integrations(request: Request):
    """Update rate_limit + webhook config."""
    body = await request.json()
    cfg = get_config()
    if "rate_limit" in body:
        rl = body["rate_limit"]
        cfg["rate_limit"] = {
            "enabled": bool(rl.get("enabled", False)),
            "requests_per_minute": int(rl.get("requests_per_minute", 60)),
            "per": rl.get("per", "ip") if rl.get("per") in ("ip", "api_key") else "ip",
        }
    if "webhook" in body:
        wh = body["webhook"]
        new_wh = {
            "enabled": bool(wh.get("enabled", False)),
            "type": wh.get("type", "discord") if wh.get("type") in ("discord", "slack", "custom") else "discord",
        }
        # Only update URL if explicitly provided
        if "url" in wh and wh["url"]:
            new_wh["url"] = wh["url"]
        elif cfg.get("webhook", {}).get("url"):
            new_wh["url"] = cfg["webhook"]["url"]
        cfg["webhook"] = new_wh
    _save_config_yaml(cfg)
    return {"status": "ok"}


# ═══════════════════════════════════════════════════════════════════


@app.get("/api/settings/circuit-breaker")
async def api_get_circuit_breaker():
    """Return current circuit_breaker config."""
    cb = get_config().get("circuit_breaker", {})
    return {
        "enabled": cb.get("enabled", True),
        "failure_threshold": int(cb.get("failure_threshold", 3)),
        "recovery_timeout": int(cb.get("recovery_timeout", 60)),
    }


@app.put("/api/settings/circuit-breaker")
async def api_update_circuit_breaker(request: Request):
    """Update circuit_breaker config."""
    body = await request.json()
    cfg = get_config()
    cfg["circuit_breaker"] = {
        "enabled": bool(body.get("enabled", True)),
        "failure_threshold": int(body.get("failure_threshold", 3)),
        "recovery_timeout": int(body.get("recovery_timeout", 60)),
    }
    _save_config_yaml(cfg)
    return {"status": "ok"}


#  OpenAI-compatible API
# ═══════════════════════════════════════════════════════════════════

@app.post("/v1/chat/completions")
async def chat_completions(request: Request, _=Depends(verify_auth)):
    """Main proxy endpoint — OpenAI-compatible."""
    if _http_client is None:
        raise HTTPException(503, "Server not ready")

    body = await request.json()
    is_stream = body.get("stream", False)

    if is_stream:
        async def stream_generator():
            async for event_type, data in route_request(body, _http_client):
                # Broadcast to WebSocket subscribers
                await _broadcast_ws(event_type, data)
                if event_type == "chunk":
                    yield data
                elif event_type == "done":
                    yield "data: [DONE]\n\n"
                elif event_type == "error":
                    yield f"data: {json.dumps({'error': data})}\n\n"

        return StreamingResponse(
            stream_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
            },
        )
    else:
        async def collect_response():
            final_data = None
            async for event_type, data in route_request(body, _http_client):
                await _broadcast_ws(event_type, data)
                if event_type == "response":
                    final_data = data
                elif event_type == "error":
                    raise HTTPException(data.get("code", 502), data.get("message", "Unknown error"))
            return final_data

        result = await collect_response()
        return JSONResponse(content=result)


@app.get("/v1/models")
async def list_models(_=Depends(verify_auth)):
    """Return available models (OpenAI-compatible).

    Exposes ONLY what the user explicitly chose to publish:
      - Direct aliases (named models with specific provider+model targets)
      - Tier names (standard, complex, etc.) for tier-based selection

    Provider-registered model entries in config.yaml are for cost tracking
    and routing only — they are NOT exposed here. Use aliases to publish them.
    """
    cfg = get_config()
    models_list = []
    seen_ids = set()

    # 1. Direct aliases — first-class published models
    aliases = cfg.get("routing", {}).get("aliases", {}) or {}
    for alias_name, alias_def in aliases.items():
        if not isinstance(alias_def, dict) or alias_def.get("type") != "direct":
            continue
        model_id = alias_def.get("display_name") or alias_name
        if model_id in seen_ids:
            continue
        seen_ids.add(model_id)
        entry = {
            "id": model_id,
            "object": "model",
            "owned_by": alias_def.get("owned_by", "fllmingo"),
            "fllmingo_direct": True,
            "fllmingo_target": f"{alias_def.get('provider', '')}/{alias_def.get('model', '')}",
        }
        if alias_def.get("description"):
            entry["description"] = alias_def["description"]
        models_list.append(entry)

    # 2. Tier names — for clients that want tier-based routing
    for tier_name in cfg.get("tiers", {}):
        if tier_name in seen_ids:
            continue
        seen_ids.add(tier_name)
        models_list.append({
            "id": tier_name,
            "object": "model",
            "owned_by": "fllmingo",
            "fllmingo_tier": True,
        })

    return {"object": "list", "data": models_list}


# ═══════════════════════════════════════════════════════════════════
#  Dashboard API
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/status")
async def api_status():
    """Dashboard overview stats."""
    stats = await db.get_daily_stats()
    providers = await db.get_provider_stats()
    cfg = get_config()
    return {
        "stats": stats,
        "providers": providers,
        "tiers": list(cfg.get("tiers", {}).keys()),
    }


@app.get("/api/requests")
async def api_requests(limit: int = 50):
    """Recent request log."""
    return await db.get_recent_requests(limit)


@app.get("/api/requests/{req_id}")
async def api_request_detail(req_id: int):
    """Full request detail (payload inspector)."""
    row = await db.get_request_by_id(req_id)
    if not row:
        raise HTTPException(404, "Request not found")
    return row


@app.get("/api/config")
async def api_get_config():
    """Return current config (keys redacted)."""
    cfg = get_config()
    # Redact keys
    safe = json.loads(json.dumps(cfg))
    for prov in safe.get("providers", {}).values():
        if "key" in prov:
            k = prov["key"]
            prov["key"] = k[:4] + "..." + k[-4:] if len(k) > 8 else "***"
    if "server" in safe and "auth_token" in safe["server"]:
        safe["server"]["auth_token"] = "***"
    return safe


@app.put("/api/config")
async def api_update_config(request: Request):
    """Update config.yaml (saves and hot-reloads)."""
    from .config import _config_path
    if _config_path is None:
        raise HTTPException(500, "Config path not set")
    body = await request.body()
    # Validate YAML before writing
    import yaml
    try:
        parsed = yaml.safe_load(body.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("Config must be a YAML mapping")
    except yaml.YAMLError as e:
        raise HTTPException(400, f"Invalid YAML: {e}")
    except ValueError as e:
        raise HTTPException(400, str(e))
    # Write new config
    _config_path.write_text(body.decode("utf-8"), encoding="utf-8")
    # Force reload
    load_config()
    return {"status": "ok", "message": "Config updated and reloaded"}


@app.get("/api/providers")
async def api_providers():
    """Provider list with health + config."""
    cfg = get_config()
    health_data = await db.get_provider_stats()
    health_map = {h["provider"]: h for h in health_data}

    # Count actual available models per provider from catalog cache
    model_counts: dict[str, int] = {}
    if _catalog_cache["data"]:
        for m in _catalog_cache["data"]:
            model_counts[m["provider"]] = model_counts.get(m["provider"], 0) + 1

    result = []
    for name, pcfg in cfg.get("providers", {}).items():
        key = pcfg.get("key", "")
        masked = key[:4] + "..." + key[-4:] if len(key) > 8 else "***" if key else ""
        health = health_map.get(name, {})
        result.append({
            "name": name,
            "endpoint": pcfg.get("endpoint", ""),
            "key_masked": masked,
            "key_env": pcfg.get("key_env", ""),
            "type": pcfg.get("type", "openai"),
            "timeout": pcfg.get("timeout", 60),
            "max_retries": pcfg.get("max_retries", 2),
            "model_count": model_counts.get(name, 0),
            "overrides": len(pcfg.get("models", {})),
            "status": health.get("status", "unknown"),
            "total_requests": health.get("total_requests", 0),
            "total_failures": health.get("total_failures", 0),
        })
    return result


@app.post("/api/providers/{provider_name}")
async def api_create_provider(provider_name: str, request: Request):
    """Add a new provider."""
    cfg = get_config()
    providers = cfg.setdefault("providers", {})
    if provider_name in providers:
        raise HTTPException(409, f"Provider '{provider_name}' already exists")
    body = await request.json()

    # Build provider config
    new_provider = {
        "endpoint": body.get("endpoint", ""),
        "type": body.get("type", "openai"),
        "timeout": body.get("timeout", 60),
        "max_retries": body.get("max_retries", 2),
        "models": body.get("models", {}),
    }

    # Handle API key — prefer key_env (env var reference), fallback to raw key
    if body.get("key_env"):
        new_provider["key_env"] = body["key_env"]
    elif body.get("key"):
        # Store in .env file and reference by env var
        env_var = f"{provider_name.upper()}_API_KEY"
        _append_env_var(env_var, body["key"])
        new_provider["key_env"] = env_var

    providers[provider_name] = new_provider
    _save_config_yaml(cfg)
    # Reload to pick up new .env vars
    from .config import load_config as _reload
    _reload()
    return {"status": "ok", "provider": provider_name}


@app.put("/api/providers/{provider_name}")
async def api_update_provider(provider_name: str, request: Request):
    """Update a provider's settings."""
    cfg = get_config()
    providers = cfg.get("providers", {})
    if provider_name not in providers:
        raise HTTPException(404, f"Provider '{provider_name}' not found")

    body = await request.json()
    prov = providers[provider_name]

    # Handle rename
    new_name = body.get("name", provider_name)
    if new_name != provider_name:
        providers[new_name] = providers.pop(provider_name)
        prov = providers[new_name]
        # Update any tier references
        for tier_cfg in cfg.get("tiers", {}).values():
            for m in tier_cfg.get("models", []):
                if m["provider"] == provider_name:
                    m["provider"] = new_name

    if "endpoint" in body:
        prov["endpoint"] = body["endpoint"]
    if "type" in body:
        prov["type"] = body["type"]
    if "timeout" in body:
        prov["timeout"] = body["timeout"]
    if "max_retries" in body:
        prov["max_retries"] = body["max_retries"]
    if "models" in body:
        prov["models"] = body["models"]

    # Handle key update
    if body.get("key"):
        env_var = prov.get("key_env", f"{new_name.upper()}_API_KEY")
        _append_env_var(env_var, body["key"])
        prov["key_env"] = env_var

    _save_config_yaml(cfg)
    from .config import load_config as _reload
    _reload()
    return {"status": "ok", "provider": new_name}


@app.delete("/api/providers/{provider_name}")
async def api_delete_provider(provider_name: str):
    """Remove a provider. Also cleans up tier references."""
    cfg = get_config()
    providers = cfg.get("providers", {})
    if provider_name not in providers:
        raise HTTPException(404, f"Provider '{provider_name}' not found")

    # Check if any tier uses this provider
    tier_refs = []
    for tier_name, tier_cfg in cfg.get("tiers", {}).items():
        for m in tier_cfg.get("models", []):
            if m["provider"] == provider_name:
                tier_refs.append(f"{tier_name}:{m['model']}")

    del providers[provider_name]

    # Remove from tiers too
    for tier_cfg in cfg.get("tiers", {}).values():
        tier_cfg["models"] = [
            m for m in tier_cfg.get("models", [])
            if m["provider"] != provider_name
        ]

    _save_config_yaml(cfg)
    return {
        "status": "ok",
        "deleted": provider_name,
        "tier_refs_removed": tier_refs,
    }


def _append_env_var(key: str, value: str):
    """Add or update a key=value in the .env file."""
    from .config import _config_path
    if _config_path is None:
        return
    env_path = _config_path.parent / ".env"
    lines = []
    found = False
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{key}="):
                lines.append(f"{key}={value}")
                found = True
            else:
                lines.append(line)
    if not found:
        lines.append(f"{key}={value}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ═══════════════════════════════════════════════════════════════════
#  Model Catalog — fetch all available models from providers
# ═══════════════════════════════════════════════════════════════════

_catalog_cache: dict = {"data": [], "ts": 0}

@app.get("/api/catalog")
async def api_catalog(
    refresh: bool = False,
    sort: str = "az",
    provider: str | None = None,
):
    """Fetch available models from all providers' /v1/models endpoints.

    Query params:
      - sort: 'az' (default) or 'za' for reverse alphabetical
      - provider: comma-separated provider names to filter by
    """
    import time
    if not refresh and _catalog_cache["data"] and (time.time() - _catalog_cache["ts"]) < 300:
        data = _catalog_cache["data"]
    else:
        cfg = get_config()
        all_models: list[dict[str, Any]] = []
        tier_models = set()
        for tier_cfg in cfg.get("tiers", {}).values():
            for m in tier_cfg.get("models", []):
                tier_models.add(f'{m["provider"]}:{m["model"]}')

        async def fetch_provider(name, pcfg):
            endpoint = pcfg.get("endpoint", "").rstrip("/")
            key = pcfg.get("key", "")
            # Build local pricing lookup for this provider
            local_models = (cfg.get("providers", {}).get(name, {}).get("models", {}) or {})
            pricing_lookup = {}
            for mkey, mcfg in local_models.items():
                if isinstance(mcfg, dict):
                    pricing_lookup[mkey] = {
                        "input_price": mcfg.get("cost_per_1k_input"),
                        "output_price": mcfg.get("cost_per_1k_output"),
                    }
            try:
                resp = await _http_client.get(
                    f"{endpoint}/models",
                    headers={"Authorization": f"Bearer {key}"},
                    timeout=15,
                )
                if resp.status_code == 200:
                    raw = resp.json()
                    models = raw.get("data", []) if isinstance(raw, dict) else []
                    for m in models:
                        mid = m.get("id", "")
                        # Extract extended metadata
                        max_ctx = (
                            m.get("max_context_length")
                            or m.get("context_length")
                            or m.get("max_tokens")
                        )
                        max_out = (
                            m.get("max_output_tokens")
                            or m.get("max_completion_tokens")
                        )
                        local_pricing = pricing_lookup.get(mid, {})
                        # Pricing: prefer provider-supplied, fall back to local config
                        provider_pricing = m.get("pricing") or {}
                        input_price = local_pricing.get("input_price")
                        output_price = local_pricing.get("output_price")
                        # OpenRouter exposes per-token prices as strings
                        if input_price is None and provider_pricing.get("prompt"):
                            try:
                                input_price = float(provider_pricing["prompt"]) * 1000
                            except (ValueError, TypeError):
                                pass
                        if output_price is None and provider_pricing.get("completion"):
                            try:
                                output_price = float(provider_pricing["completion"]) * 1000
                            except (ValueError, TypeError):
                                pass
                        all_models.append({
                            "id": mid,
                            "provider": name,
                            "in_tier": f"{name}:{mid}" in tier_models,
                            "object": m.get("object", "model"),
                            "created": m.get("created"),
                            "owned_by": m.get("owned_by", name),
                            "name": m.get("name"),
                            "description": m.get("description"),
                            "max_context_length": max_ctx,
                            "max_output_tokens": max_out,
                            "supported_parameters": m.get("supported_parameters", []),
                            "input_price": input_price,
                            "output_price": output_price,
                            "architecture": m.get("architecture"),
                            "top_provider": m.get("top_provider"),
                            "modality": (m.get("architecture") or {}).get("modality") if isinstance(m.get("architecture"), dict) else None,
                            "input_modalities": (m.get("architecture") or {}).get("input_modalities") if isinstance(m.get("architecture"), dict) else None,
                            "tokenizer": (m.get("architecture") or {}).get("tokenizer") if isinstance(m.get("architecture"), dict) else None,
                            "instruct_type": (m.get("architecture") or {}).get("instruct_type") if isinstance(m.get("architecture"), dict) else None,
                            "pricing_raw": provider_pricing or None,
                            "hugging_face_id": m.get("hugging_face_id"),
                            "per_request_limits": m.get("per_request_limits"),
                            "canonical_slug": m.get("canonical_slug"),
                            "deprecated": m.get("deprecated"),
                        })
            except Exception as e:
                logger.warning("Catalog fetch failed for %s: %s", name, e)

        tasks = [fetch_provider(n, p) for n, p in cfg.get("providers", {}).items()]
        await asyncio.gather(*tasks)

        all_models.sort(key=lambda x: (x["provider"], x["id"]))
        _catalog_cache["data"] = all_models
        _catalog_cache["ts"] = time.time()
        data = all_models

    # Apply server-side filters and sorting on cached data
    filtered = list(data)
    if provider:
        allowed = {p.strip().lower() for p in provider.split(",")}
        filtered = [m for m in filtered if m["provider"].lower() in allowed]
    if sort == "za":
        filtered.sort(key=lambda x: x["id"].lower(), reverse=True)
    else:
        filtered.sort(key=lambda x: x["id"].lower())
    return filtered


# ═══════════════════════════════════════════════════════════════════
#  Token & Leaderboard stats
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/health")
async def api_health():
    return {"status": "ok"}

@app.get("/api/stats/tokens")
async def api_stats_tokens():
    """Token usage aggregated by time periods (today, 7d, 30d, all)."""
    return await db.get_token_stats()


@app.get("/api/stats/leaderboard")
async def api_stats_leaderboard(
    sort: str = "requests",
    period: str = "all",
    dir: str = "desc",
):
    """Model ranking by usage.

    Query params:
    - sort: requests | prompt_tokens | completion_tokens | total_tokens
    - period: today | 7d | 30d | all
    - dir: asc | desc
    """
    return await db.get_leaderboard(sort=sort, period=period, direction=dir)


# ═══════════════════════════════════════════════════════════════════
#  Tier CRUD — edit tiers from the dashboard
# ═══════════════════════════════════════════════════════════════════

def _save_config_yaml(cfg: dict):
    """Write config dict back to YAML and reload."""
    import yaml
    from .config import _config_path, load_config as _reload
    if _config_path is None:
        raise HTTPException(500, "Config path not set")
    # Remove resolved keys before saving
    save_cfg = json.loads(json.dumps(cfg))
    for prov in save_cfg.get("providers", {}).values():
        prov.pop("key", None)
    save_cfg.get("server", {}).pop("auth_token", None)
    _config_path.write_text(yaml.dump(save_cfg, default_flow_style=False, sort_keys=False), encoding="utf-8")
    _reload()


@app.get("/api/tiers")
async def api_tiers():
    """Get all tiers with their models."""
    cfg = get_config()
    return cfg.get("tiers", {})


@app.post("/api/tiers/{tier_name}")
async def api_create_tier(tier_name: str, request: Request):
    """Create a new tier."""
    cfg = get_config()
    tiers = cfg.setdefault("tiers", {})
    if tier_name in tiers:
        raise HTTPException(409, f"Tier '{tier_name}' already exists")
    body = await request.json()
    tiers[tier_name] = {
        "models": body.get("models", []),
        "strategy": body.get("strategy", "fallback"),
    }
    _save_config_yaml(cfg)
    return {"status": "ok", "tier": tier_name}


@app.put("/api/tiers/{tier_name}")
async def api_update_tier(tier_name: str, request: Request):
    """Update a tier (rename, change models, reorder)."""
    cfg = get_config()
    tiers = cfg.get("tiers", {})
    if tier_name not in tiers:
        raise HTTPException(404, f"Tier '{tier_name}' not found")
    body = await request.json()

    # Handle rename
    new_name = body.get("name", tier_name)
    if new_name != tier_name:
        tiers[new_name] = tiers.pop(tier_name)
        # Update aliases
        aliases = cfg.get("routing", {}).get("aliases", {})
        for alias, target in list(aliases.items()):
            if target == tier_name:
                aliases[alias] = new_name

    target = tiers.get(new_name, tiers.get(tier_name))
    if "models" in body:
        target["models"] = body["models"]
    if "strategy" in body:
        target["strategy"] = body["strategy"]

    _save_config_yaml(cfg)
    return {"status": "ok", "tier": new_name}


@app.delete("/api/tiers/{tier_name}")
async def api_delete_tier(tier_name: str):
    """Delete a tier."""
    cfg = get_config()
    tiers = cfg.get("tiers", {})
    if tier_name not in tiers:
        raise HTTPException(404, f"Tier '{tier_name}' not found")
    del tiers[tier_name]
    # Clean up aliases pointing to this tier
    aliases = cfg.get("routing", {}).get("aliases", {})
    for alias, target in list(aliases.items()):
        if target == tier_name:
            del aliases[alias]
    _save_config_yaml(cfg)
    return {"status": "ok", "deleted": tier_name}


# ═══════════════════════════════════════════════════════════════════
#  Tier model reorder / move helpers
# ═══════════════════════════════════════════════════════════════════

@app.put("/api/tiers/{tier_name}/models/reorder")
async def api_reorder_tier_models(tier_name: str, request: Request):
    """Persist the full ordered models array for a tier."""
    cfg = get_config()
    tiers = cfg.setdefault("tiers", {})
    if tier_name not in tiers:
        raise HTTPException(404, f"Tier '{tier_name}' not found")
    body = await request.json()
    if "models" not in body:
        raise HTTPException(400, "Missing 'models' in body")
    tiers[tier_name]["models"] = body["models"]
    _save_config_yaml(cfg)
    return {"status": "ok", "tier": tier_name, "count": len(body["models"])}


@app.post("/api/tiers/{tier_name}/models/move")
async def api_move_model(tier_name: str, request: Request):
    """Move a model item from (source_tier, source_index) -> (target_tier, target_index).

    This covers:
      - Same-tier reorder (source_tier == tier_name)
      - Cross-tier move (tier_name is the target; source is in body)
    """
    body = await request.json()
    src_tier = body.get("source_tier", tier_name)
    src_index: int = body.get("source_index", -1)
    tgt_index: int | None = body.get("target_index")
    # Allow body to override the URL's tier_name for cross-tier moves
    tgt_tier: str = body.get("target_tier", tier_name)

    cfg = get_config()
    tiers = cfg.setdefault("tiers", {})

    # Validate source
    src_tier_cfg = tiers.get(src_tier)
    if src_tier_cfg is None:
        raise HTTPException(404, f"Source tier '{src_tier}' not found")
    src_models = src_tier_cfg.get("models", [])
    if not (0 <= src_index < len(src_models)):
        raise HTTPException(400, f"Invalid source_index {src_index}")

    moved = src_models.pop(src_index)

    # Validate target
    tgt_tier_cfg = tiers.get(tgt_tier)
    if tgt_tier_cfg is None:
        raise HTTPException(404, f"Target tier '{tgt_tier}' not found")
    tgt_models = tgt_tier_cfg.setdefault("models", [])

    if tgt_index is None:
        tgt_index = len(tgt_models)

    # Adjust for same-tier index shift FIRST (before clamping):
    # after popping at src_index, any tgt_index > src_index is shifted down by 1
    if src_tier == tgt_tier and src_index < tgt_index:
        tgt_index -= 1

    # Now clamp to valid range
    if tgt_index < 0:
        tgt_index = 0
    if tgt_index > len(tgt_models):
        tgt_index = len(tgt_models)

    tgt_models.insert(tgt_index, moved)

    _save_config_yaml(cfg)
    return {
        "status": "ok",
        "moved": moved,
        "source": f"{src_tier}:{src_index}",
        "target": f"{tgt_tier}:{tgt_index}",
    }


# ═══════════════════════════════════════════════════════════════════
#  API Key Management — generate, expiry, quota, revocation
# ═══════════════════════════════════════════════════════════════════

import secrets
import os
import hashlib
from datetime import datetime, timezone


def _generate_api_key() -> str:
    """Generate a new API key."""
    return "llm-" + secrets.token_urlsafe(24)


def _hash_key(key: str, salt: bytes | None = None) -> str:
    """HMAC-SHA256 hash of a key for storage.

    Returns a string of the form `b64salt$hexhmac`.
    """
    salt = salt or os.urandom(16)
    hmac = hashlib.pbkdf2_hmac(
        "sha256",
        key.encode(),
        salt,
        100_000,
    ).hex()
    return f"{__import__('base64').b64encode(salt).decode()}${hmac}"


def _verify_key_hash(key: str, stored_hash: str) -> bool:
    """Verify a key against a stored hash (salted or legacy unsalted)."""
    if "$" in stored_hash:
        salt_b64, expected = stored_hash.split("$", 1)
        salt = __import__("base64").b64decode(salt_b64)
        _, hmac = _hash_key(key, salt).split("$", 1)
        return hmac == expected
    # Legacy fallback: plain SHA-256
    return hashlib.sha256(key.encode()).hexdigest() == stored_hash


def _get_api_keys(cfg: dict) -> list[dict]:
    """Return the api_keys list from config, creating if absent."""
    return cfg.setdefault("api_keys", [])


@app.get("/api/keys")
async def api_list_keys():
    """List all API keys (masked)."""
    cfg = get_config()
    keys = cfg.get("api_keys", [])
    result = []
    for i, k in enumerate(keys):
        result.append({
            "id": i,
            "name": k.get("name", ""),
            "key_prefix": k.get("key_prefix", ""),
            "expires_at": k.get("expires_at"),
            "quota_requests": k.get("quota_requests"),
            "quota_period": k.get("quota_period"),
            "usage_count": k.get("usage_count", 0),
            "is_active": k.get("is_active", True),
            "created_at": k.get("created_at"),
            "last_used_at": k.get("last_used_at"),
            "allowed_tiers": k.get("allowed_tiers", []),
        })
    return result


@app.post("/api/keys")
async def api_create_key(request: Request):
    """Generate a new API key."""
    cfg = get_config()
    keys = _get_api_keys(cfg)
    body = await request.json()

    raw_key = _generate_api_key()
    scopes = body.get("allowed_tiers", [])
    if not isinstance(scopes, list):
        scopes = []
    entry = {
        "name": body.get("name", "Unnamed"),
        "key_hash": _hash_key(raw_key),
        "key_prefix": raw_key[:12],
        "expires_at": body.get("expires_at"),
        "quota_requests": body.get("quota_requests"),
        "quota_period": body.get("quota_period"),
        "allowed_tiers": scopes,
        "usage_count": 0,
        "is_active": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "last_used_at": None,
    }
    keys.append(entry)
    _save_config_yaml(cfg)
    return {"status": "ok", "key": raw_key, "id": len(keys) - 1}


@app.delete("/api/keys/{key_id}")
async def api_revoke_key(key_id: int):
    """Revoke an API key."""
    cfg = get_config()
    keys = cfg.get("api_keys", [])
    if key_id < 0 or key_id >= len(keys):
        raise HTTPException(404, "Key not found")
    keys[key_id]["is_active"] = False
    _save_config_yaml(cfg)
    return {"status": "ok", "revoked": key_id}


@app.post("/api/keys/{key_id}/renew")
async def api_renew_key(key_id: int):
    """Rotate an API key — generates new token, keeps settings."""
    cfg = get_config()
    keys = cfg.get("api_keys", [])
    if key_id < 0 or key_id >= len(keys):
        raise HTTPException(404, "Key not found")
    raw_key = _generate_api_key()
    keys[key_id]["key_hash"] = _hash_key(raw_key)
    keys[key_id]["key_prefix"] = raw_key[:12]
    keys[key_id]["is_active"] = True
    keys[key_id]["usage_count"] = 0
    _save_config_yaml(cfg)
    return {"status": "ok", "key": raw_key, "id": key_id}


def verify_api_key(request: Request) -> bool:
    """Check if the request has a valid API key in Authorization header.

    Returns True if the key is valid OR if no keys are configured (open mode).
    """
    cfg = get_config()
    keys = cfg.get("api_keys", [])
    if not keys:
        return True  # No keys configured = open access

    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return False
    token = auth[7:]

    now = datetime.now(timezone.utc)
    for k in keys:
        if not k.get("is_active", True):
            continue
        if not _verify_key_hash(token, k.get("key_hash", "")):
            continue
        # Check expiry
        exp = k.get("expires_at")
        if exp:
            try:
                exp_dt = datetime.fromisoformat(exp.replace("Z", "+00:00"))
                if now > exp_dt:
                    return False
            except Exception:
                pass
        # Check quota
        quota = k.get("quota_requests")
        if quota and k.get("usage_count", 0) >= quota:
            return False
        # Update usage
        k["usage_count"] = k.get("usage_count", 0) + 1
        k["last_used_at"] = now.isoformat()
        return True
    return False


# ═══════════════════════════════════════════════════════════════════
#  Settings — password change + auth status
# ═══════════════════════════════════════════════════════════════════

# Known weak/default tokens that trigger first-time password change
_DEFAULT_TOKENS = {"llm-ro...2026", "changeme", "admin", "password", ""}


@app.get("/api/auth/status")
async def api_auth_status(request: Request):
    """Check if the current token is a default/weak token requiring change.

    This endpoint is NOT behind the dashboard auth middleware (returns info
    needed for the first-time flow). It only reveals whether the token needs
    changing, not the token itself.
    """
    token = get_auth_token() or ""
    needs_change = token in _DEFAULT_TOKENS
    return {"needs_password_change": needs_change, "has_auth": bool(token)}


@app.post("/api/settings/password")
async def api_change_password(request: Request):
    """Change the dashboard auth token (writes to .env).

    Requires the current token to authenticate. Accepts:
      { "current": "...", "new": "..." }
    """
    token = get_auth_token()
    if not token:
        raise HTTPException(400, "No auth token configured")

    # Verify current token
    provided = request.headers.get("X-Dashboard-Token", "")
    if not provided:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            provided = auth[7:]
    if provided != token:
        raise HTTPException(401, "Current password incorrect")

    body = await request.json()
    current = body.get("current", "")
    new = body.get("new", "")

    # Double-check current matches
    if current != token:
        raise HTTPException(401, "Current password incorrect")

    if len(new) < 6:
        raise HTTPException(400, "New password must be at least 6 characters")

    # Update .env file
    from .config import _config_path
    if _config_path is None:
        raise HTTPException(500, "Config path not set")
    env_path = _config_path.parent / ".env"
    env_key = "ROUTER_AUTH_TOKEN"
    lines = []
    found = False
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            if line.startswith(env_key + "="):
                lines.append(f"{env_key}={new}")
                found = True
            else:
                lines.append(line)
    if not found:
        lines.append(f"{env_key}={new}")
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # Reload config to pick up new token
    load_config()
    return {"status": "ok", "message": "Password updated. Use new token on next login."}




# ═══════════════════════════════════════════════════════════════════
#  Cost Budgets (daily/monthly caps with auto-pause)
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/budget/status")
async def api_budget_status():
    """Current budget usage vs caps."""
    cfg = get_config().get("budget", {})
    if not cfg.get("enabled", False):
        return {"enabled": False}
    today_cost = (await db.get_daily_stats()).get("total_cost", 0)
    monthly_cost = await db.get_monthly_cost()
    daily_cap = float(cfg.get("daily_limit", 0))
    monthly_cap = float(cfg.get("monthly_limit", 0))
    return {
        "enabled": True,
        "daily": {"used": today_cost, "limit": daily_cap, "exceeded": daily_cap > 0 and today_cost >= daily_cap},
        "monthly": {"used": monthly_cost, "limit": monthly_cap, "exceeded": monthly_cap > 0 and monthly_cost >= monthly_cap},
        "alert_at_percent": int(cfg.get("alert_at_percent", 80)),
    }


@app.put("/api/budget")
async def api_update_budget(request: Request):
    """Update budget config."""
    body = await request.json()
    cfg = get_config()
    cfg["budget"] = {
        "enabled": bool(body.get("enabled", False)),
        "daily_limit": float(body.get("daily_limit", 0)),
        "monthly_limit": float(body.get("monthly_limit", 0)),
        "alert_at_percent": int(body.get("alert_at_percent", 80)),
        "auto_pause": bool(body.get("auto_pause", True)),
    }
    _save_config_yaml(cfg)
    return {"status": "ok"}


# ═══════════════════════════════════════════════════════════════════
#  Latency Stats (p50/p95/p99)
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/stats/latency")
async def api_latency_stats(period: str = "7d"):
    """Per-provider latency percentiles."""
    return await db.get_latency_stats(period)


# ═══════════════════════════════════════════════════════════════════
#  Export Logs (CSV/JSON)
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/export/logs")
async def api_export_logs(format: str = "json", days: int = 7):
    """Export request logs."""
    rows = await db.export_logs(days=days)
    if format == "csv":
        import csv, io
        if not rows:
            return JSONResponse(content="", media_type="text/csv")
        out = io.StringIO()
        writer = csv.DictWriter(out, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
        return JSONResponse(
            content=out.getvalue(),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=fllmingo-logs.csv"},
        )
    return rows


# ═══════════════════════════════════════════════════════════════════
#  Backup / Restore Config
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/backup")
async def api_backup():
    """Download full config as YAML backup."""
    from .config import _config_path
    if _config_path is None or not _config_path.exists():
        raise HTTPException(404, "Config not found")
    content = _config_path.read_text(encoding="utf-8")
    from datetime import datetime as _dt
    timestamp = _dt.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return JSONResponse(
        content={"timestamp": timestamp, "config": content},
        headers={"Content-Disposition": f"attachment; filename=fllmingo-backup-{timestamp}.json"},
    )


@app.post("/api/restore")
async def api_restore(request: Request):
    """Restore config from a backup YAML payload."""
    from .config import _config_path
    if _config_path is None:
        raise HTTPException(500, "Config path not set")
    body = await request.json()
    yaml_text = body.get("config", "")
    if not yaml_text:
        raise HTTPException(400, "Missing 'config' field")
    import yaml as _yaml
    try:
        parsed = _yaml.safe_load(yaml_text)
        if not isinstance(parsed, dict):
            raise ValueError("Config must be a YAML mapping")
    except Exception as e:
        raise HTTPException(400, f"Invalid YAML: {e}")
    _config_path.write_text(yaml_text, encoding="utf-8")
    load_config()
    return {"status": "ok", "message": "Config restored and reloaded"}


# ═══════════════════════════════════════════════════════════════════
#  Prompt Templates CRUD
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/templates")
async def api_list_templates():
    """List all prompt templates."""
    cfg = get_config()
    templates = cfg.get("prompt_templates", [])
    return templates


@app.post("/api/templates")
async def api_create_template(request: Request):
    """Create a new prompt template."""
    body = await request.json()
    name = body.get("name", "").strip()
    if not name:
        raise HTTPException(400, "Name is required")
    cfg = get_config()
    templates = cfg.setdefault("prompt_templates", [])
    if any(t.get("name") == name for t in templates):
        raise HTTPException(409, f"Template '{name}' already exists")
    template = {
        "name": name,
        "description": body.get("description", ""),
        "system_prompt": body.get("system_prompt", ""),
        "model": body.get("model", ""),
        "temperature": body.get("temperature"),
        "max_tokens": body.get("max_tokens"),
    }
    templates.append(template)
    _save_config_yaml(cfg)
    return {"status": "ok", "name": name}


@app.put("/api/templates/{name}")
async def api_update_template(name: str, request: Request):
    """Update a prompt template."""
    body = await request.json()
    cfg = get_config()
    templates = cfg.get("prompt_templates", [])
    for t in templates:
        if t.get("name") == name:
            if "description" in body:
                t["description"] = body["description"]
            if "system_prompt" in body:
                t["system_prompt"] = body["system_prompt"]
            if "model" in body:
                t["model"] = body["model"]
            if "temperature" in body:
                t["temperature"] = body["temperature"]
            if "max_tokens" in body:
                t["max_tokens"] = body["max_tokens"]
            _save_config_yaml(cfg)
            return {"status": "ok"}
    raise HTTPException(404, f"Template '{name}' not found")


@app.delete("/api/templates/{name}")
async def api_delete_template(name: str):
    """Delete a prompt template."""
    cfg = get_config()
    templates = cfg.get("prompt_templates", [])
    new_templates = [t for t in templates if t.get("name") != name]
    if len(new_templates) == len(templates):
        raise HTTPException(404, f"Template '{name}' not found")
    cfg["prompt_templates"] = new_templates
    _save_config_yaml(cfg)
    return {"status": "ok"}


# ═══════════════════════════════════════════════════════════════════
#  Health Probes (periodic background pings)
# ═══════════════════════════════════════════════════════════════════

async def health_probe_loop():
    """Background task: ping each provider's /models endpoint periodically."""
    while True:
        try:
            cfg = get_config()
            probe_cfg = cfg.get("health_probe", {})
            if not probe_cfg.get("enabled", False):
                await asyncio.sleep(60)
                continue
            interval = int(probe_cfg.get("interval_seconds", 300))
            for name, pcfg in cfg.get("providers", {}).items():
                endpoint = pcfg.get("endpoint", "").rstrip("/")
                key = pcfg.get("key", "")
                if not endpoint:
                    continue
                try:
                    if _http_client:
                        resp = await _http_client.get(
                            f"{endpoint}/models",
                            headers={"Authorization": f"Bearer {key}"},
                            timeout=10,
                        )
                        if resp.status_code == 200:
                            await db.update_provider_health(name, success=True)
                        else:
                            await db.update_provider_health(name, success=False, error=f"probe {resp.status_code}")
                except Exception as e:
                    await db.update_provider_health(name, success=False, error=f"probe error: {str(e)[:100]}")
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"Health probe loop error: {e}")
            await asyncio.sleep(60)




# ═══════════════════════════════════════════════════════════════════
#  Public Model Aliases (rich names like "GPT5" routing to tiers)
# ═══════════════════════════════════════════════════════════════════

@app.get("/api/aliases")
async def api_list_aliases():
    """List all aliases, normalized to rich shape."""
    cfg = get_config()
    aliases = cfg.get("routing", {}).get("aliases", {}) or {}
    result = []
    # Only return DIRECT aliases. Legacy tier aliases (string or type='tier')
    # are kept as routing shortcuts but hidden from the public-alias UI.
    for name, val in aliases.items():
        if not isinstance(val, dict):
            continue
        if val.get("type") != "direct":
            continue
        result.append({
            "name": name,
            "type": "direct",
            "provider": val.get("provider", ""),
            "model": val.get("model", ""),
            "max_retries": int(val.get("max_retries", 2)),
            "display_name": val.get("display_name", name),
            "description": val.get("description", ""),
            "owned_by": val.get("owned_by", "fllmingo"),
        })
    return result


@app.post("/api/aliases")
async def api_create_alias(request: Request):
    """Create a new public model alias."""
    body = await request.json()
    name = (body.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "Name is required")
    cfg = get_config()
    routing = cfg.setdefault("routing", {})
    aliases = routing.setdefault("aliases", {})
    if name in aliases:
        raise HTTPException(409, f"Alias '{name}' already exists")

    # Public aliases are always direct (specific provider+model targets).
    provider = (body.get("provider") or "").strip()
    model = (body.get("model") or "").strip()
    if not provider or not model:
        raise HTTPException(400, "provider and model are required")
    if provider not in cfg.get("providers", {}):
        raise HTTPException(404, f"Provider '{provider}' not found")
    try:
        max_retries = int(body.get("max_retries", 2))
    except (TypeError, ValueError):
        max_retries = 2
    max_retries = max(0, min(max_retries, 10))
    aliases[name] = {
        "type": "direct",
        "provider": provider,
        "model": model,
        "max_retries": max_retries,
        "display_name": (body.get("display_name") or name).strip(),
        "description": (body.get("description") or "").strip(),
        "owned_by": (body.get("owned_by") or "fllmingo").strip(),
    }
    _save_config_yaml(cfg)
    return {"status": "ok", "name": name}


@app.put("/api/aliases/{name}")
async def api_update_alias(name: str, request: Request):
    """Update an existing alias."""
    body = await request.json()
    cfg = get_config()
    routing = cfg.setdefault("routing", {})
    aliases = routing.setdefault("aliases", {})
    if name not in aliases:
        raise HTTPException(404, f"Alias '{name}' not found")

    # Normalize the entry to dict shape on update
    cur = aliases[name]
    if not isinstance(cur, dict):
        cur = {"tier": str(cur), "display_name": name, "description": "", "owned_by": "fllmingo"}

    # All public aliases are direct now.
    cur["type"] = "direct"
    if "provider" in body:
        prov = (body.get("provider") or "").strip()
        if prov and prov not in cfg.get("providers", {}):
            raise HTTPException(404, f"Provider '{prov}' not found")
        cur["provider"] = prov
    if "model" in body:
        cur["model"] = (body.get("model") or "").strip()
    if "max_retries" in body:
        try:
            mr = int(body.get("max_retries", 2))
        except (TypeError, ValueError):
            mr = 2
        cur["max_retries"] = max(0, min(mr, 10))
    cur.pop("tier", None)

    if "display_name" in body:
        cur["display_name"] = (body["display_name"] or name).strip()
    if "description" in body:
        cur["description"] = (body["description"] or "").strip()
    if "owned_by" in body:
        cur["owned_by"] = (body["owned_by"] or "fllmingo").strip()

    new_name = (body.get("rename") or "").strip()
    if new_name and new_name != name:
        if new_name in aliases:
            raise HTTPException(409, f"Alias '{new_name}' already exists")
        del aliases[name]
        aliases[new_name] = cur
        name = new_name
    else:
        aliases[name] = cur

    _save_config_yaml(cfg)
    return {"status": "ok", "name": name}


@app.delete("/api/aliases/{name}")
async def api_delete_alias(name: str):
    """Remove an alias."""
    cfg = get_config()
    aliases = cfg.get("routing", {}).get("aliases", {})
    if name not in aliases:
        raise HTTPException(404, f"Alias '{name}' not found")
    del aliases[name]
    _save_config_yaml(cfg)
    return {"status": "ok", "deleted": name}


# ═══════════════════════════════════════════════════════════════════
#  WebSocket — live request feed
# ═══════════════════════════════════════════════════════════════════

@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    _ws_subscribers.add(ws)
    try:
        while True:
            await ws.receive_text()  # keep alive
    except WebSocketDisconnect:
        _ws_subscribers.discard(ws)


async def _broadcast_ws(event_type: str, data: Any):
    """Broadcast an event to all WebSocket subscribers."""
    if not _ws_subscribers:
        return
    msg = json.dumps({"type": event_type, "data": data}, default=str)
    dead = set()
    for ws in _ws_subscribers:
        try:
            await ws.send_text(msg)
        except Exception:
            dead.add(ws)
    _ws_subscribers.difference_update(dead) if dead else None


# ══════════════════════════════════════════════════════════════════
#  Dashboard frontend
# ═══════════════════════════════════════════════════════════════════

@app.get("/", response_class=HTMLResponse)
async def dashboard():
    index = STATIC_DIR / "index.html"
    if index.exists():
        html = index.read_text()
        # Inject version as cache-busting query param so browsers fetch fresh assets on every deploy
        import re
        ver = app.version
        html = re.sub(r'style\.css(\?v=[^"]*)?', f'style.css?v={ver}', html)
        html = re.sub(r'app\.js(\?v=[^"]*)?', f'app.js?v={ver}', html)
        return HTMLResponse(html)
    return HTMLResponse("<h1>FLLMingo</h1><p>Static files not found</p>")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


def _ensure_config():
    from .config import load_config
    try:
        import app.config as _cfg
        if not _cfg._config:
            load_config()
    except Exception:
        load_config()


def run():
    """Entry point for `llm-router` CLI command."""
    _ensure_config()
    import app.config as _cfg
    server_cfg = _cfg.get_config().get("server", {})
    uvicorn.run(
        "app.main:app",
        host=server_cfg.get("host", "0.0.0.0"),
        port=int(server_cfg.get("port", 8000)),
        reload=False,
    )
