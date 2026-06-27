"""Config loader with hot-reload and .env interpolation."""

from __future__ import annotations

import asyncio
import os
import re
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

_ENV_REF = re.compile(r"\$\{(\w+)\}")

# Module-level singleton
_config: dict[str, Any] = {}
_config_mtime: float = 0.0
_config_path: Path | None = None


def _resolve_env_vars(obj: Any) -> Any:
    """Recursively replace ${VAR} references with env values."""
    if isinstance(obj, str):
        return _ENV_REF.sub(lambda m: os.getenv(m.group(1), ""), obj)
    if isinstance(obj, dict):
        return {k: _resolve_env_vars(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_env_vars(v) for v in obj]
    return obj


def _resolve_key_env(obj: Any) -> Any:
    """Resolve `key_env: VAR_NAME` → `key: <value>` while KEEPING key_env."""
    if isinstance(obj, dict):
        if "key_env" in obj:
            obj = {**obj, "key": os.getenv(obj["key_env"], "")}
        if "auth_token_env" in obj:
            obj = {**obj, "auth_token": os.getenv(obj["auth_token_env"], "")}
        return {k: _resolve_key_env(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_resolve_key_env(v) for v in obj]
    return obj


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    """Load (or reload) the YAML config, interpolating env vars."""
    global _config, _config_mtime, _config_path

    if path is not None:
        _config_path = Path(path)
    if _config_path is None:
        _config_path = Path(__file__).resolve().parent.parent / "config.yaml"

    load_dotenv(_config_path.parent / ".env", override=True)

    mtime = _config_path.stat().st_mtime
    if mtime == _config_mtime and _config:
        return _config  # unchanged

    raw = yaml.safe_load(_config_path.read_text(encoding="utf-8"))
    raw = _resolve_env_vars(raw)
    raw = _resolve_key_env(raw)
    _config = raw or {}
    _config_mtime = mtime
    return _config


def get_config() -> dict[str, Any]:
    """Return the current config (reloads if file changed)."""
    if _config_path is None:
        raise RuntimeError("Config not loaded yet — call load_config() first")
    mtime = _config_path.stat().st_mtime
    if mtime != _config_mtime:
        load_config()
    return _config


def get_provider_config(provider_name: str) -> dict[str, Any] | None:
    cfg = get_config()
    return cfg.get("providers", {}).get(provider_name)


def get_model_config(provider_name: str, model: str) -> dict[str, Any]:
    """Get per-model config (strip_params, cost) for a provider+model."""
    provider = get_provider_config(provider_name)
    if not provider:
        return {"strip_params": [], "cost_per_1k_input": 0, "cost_per_1k_output": 0}
    models = provider.get("models", {})
    # Exact match first, then wildcard
    return models.get(model) or models.get("*") or {
        "strip_params": [],
        "cost_per_1k_input": 0,
        "cost_per_1k_output": 0,
    }


def get_tier_config(tier_name: str) -> dict[str, Any] | None:
    cfg = get_config()
    return cfg.get("tiers", {}).get(tier_name)


def resolve_tier(model_field: str) -> tuple[str, dict[str, Any]] | None:
    """Resolve a model string to (tier_name, tier_config).

    Supports two alias shapes:
      - legacy: aliases[name] = "tier_name"  (plain string redirect)
      - rich:   aliases[name] = {tier, display_name, description, owned_by}

    If model_field is a tier name or alias, return the resolved tier.
    If it's a passthrough model name, return None.
    """
    cfg = get_config()
    aliases = cfg.get("routing", {}).get("aliases", {})
    tiers = cfg.get("tiers", {})

    alias_entry = aliases.get(model_field)
    if alias_entry is None:
        # No alias match — model_field might be a tier name directly
        if model_field in tiers:
            return model_field, tiers[model_field]
        return None

    # Rich alias (dict) — pull tier out of the object
    if isinstance(alias_entry, dict):
        target = alias_entry.get("tier", "")
    else:
        target = str(alias_entry)

    if target in tiers:
        return target, tiers[target]
    return None


async def watch_config(interval: float = 2.0):
    """Poll config file for changes (lightweight alternative to watchfiles)."""
    while True:
        try:
            get_config()  # auto-reloads on mtime change
        except Exception:
            pass
        await asyncio.sleep(interval)
