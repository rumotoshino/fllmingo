"""Parameter sanitizer — strips unsupported params per provider/model."""

from __future__ import annotations

import copy
import logging
from typing import Any

logger = logging.getLogger("llm-router.sanitizer")


def strip_params(payload: dict[str, Any], params_to_strip: list[str]) -> tuple[dict[str, Any], list[str]]:
    """Remove disallowed params from the request payload.
    
    Handles top-level keys (reasoning_effort, service_tier) and nested
    extra_body sub-keys. Returns (sanitized_payload, list_of_stripped).
    """
    if not params_to_strip:
        return payload, []

    body = copy.deepcopy(payload)
    stripped: list[str] = []

    for param in params_to_strip:
        # Top-level removal
        if param in body:
            val = body.pop(param)
            stripped.append(f"{param}={val!r}")
            logger.debug("Stripped top-level param: %s", param)

        # Nested in extra_body
        if "extra_body" in body and isinstance(body["extra_body"], dict):
            if param in body["extra_body"]:
                val = body["extra_body"].pop(param)
                stripped.append(f"extra_body.{param}={val!r}")
                logger.debug("Stripped extra_body.%s", param)
            # Clean up empty extra_body
            if not body["extra_body"]:
                body.pop("extra_body", None)

    return body, stripped


def sanitize_for_model(
    payload: dict[str, Any], provider_name: str, model: str
) -> tuple[dict[str, Any], list[str]]:
    """Apply per-model strip_params from config."""
    from .config import get_model_config

    model_cfg = get_model_config(provider_name, model)
    strip_list = model_cfg.get("strip_params", [])
    return strip_params(payload, strip_list)


def auto_strip_on_400(payload: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Aggressive strip for retry after a 400 — removes all suspicious params."""
    from .config import get_config

    cfg = get_config()
    auto_strip = cfg.get("routing", {}).get("auto_strip_on_400", [
        "reasoning_effort", "service_tier"
    ])
    return strip_params(payload, auto_strip)
