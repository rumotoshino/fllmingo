# Changelog

All notable changes to FLLMingo are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [1.2.0] — 2026-06-26

First public release. Renamed from `llm-router` to **FLLMingo** 🦩.

### Added

- **Drag-and-drop tier model reordering** — left-side drag handle, works on mouse + touch via Pointer Events. Cross-tier moves supported.
- **Login / logout flow** — terminal-themed overlay with "keep me signed in" (localStorage) toggle and ⏻ logout button in top bar.
- **First-time password change** — forces a token rotation if a default/weak `ROUTER_AUTH_TOKEN` is detected.
- **Settings hub** with sub-panels:
  - Change password
  - Rate limiting (IP or API-key, per-minute)
  - Webhook alerts (Discord / Slack / custom)
  - Cost budgets (daily + monthly caps with auto-pause)
  - Health probes (periodic provider pings)
  - Backup / restore (one-click YAML export + file restore)
  - Export logs (CSV / JSON, configurable date range)
  - Latency stats (p50 / p95 / p99 per provider)
  - Prompt templates (full CRUD)
  - API key management (with tier scopes)
  - 6 themes: Dark, Light, VSCode, AMOLED, Solarized, Nord
- **Hamburger nav menu** — keeps the main bar uncluttered.
- **Catalog row expansion** — click any model to reveal description, modality, tokenizer, supported parameters, full provider pricing, HuggingFace ID, and more.
- **OpenAPI docs** at `/docs` and `/redoc` (auth-gated).
- **Retry on 5xx / 429** with exponential backoff.
- **API key scopes** — restrict generated keys to specific tiers.
- **Auto-strip retry on 400** — propagates stripped payload to subsequent providers.
- Per-provider streaming token tracking and cost computation.
- Salted HMAC-SHA256 hashes for stored API keys.

### Changed

- Dashboard `/api/*` routes are now gated by `X-Dashboard-Token` middleware.
- `verify_auth` accepts either the server token or any active generated API key.
- `PUT /api/config` validates YAML before writing.
- Circuit breaker thresholds + recovery now driven by `config.yaml`.
- Streaming SSE responses use proper `\n\n` chunk delimiters.
- All user-supplied strings in the dashboard are escaped before rendering.
- Theme persistence uses `localStorage["fllmingo-theme"]`.
- Dark theme palette bumped ~10 % lighter for better contrast.

### Fixed

- WebSocket subscriber leak (`difference_update` instead of `discard(set)`).
- Cross-tier model move ignored `target_tier` and clamped target index too early.
- Same-tier downward move was a no-op due to index shift miscalculation.
- Hamburger button was treated as a page tab and blanked the screen.
- Catalog tab threw `ReferenceError` because `loadCatalog` was undefined.
- Status-page period selector used `<div>` instead of `<select>` (completely non-functional).
- Duplicate `toggleTheme` declaration clobbered persistence.
- API keys table never loaded after being merged into the Settings hub.
- Tier drag failed to find a drop position when hovering between rows.
- Many smaller frontend/audit issues from the comprehensive frontend pass.

### Security

- Dashboard endpoints require `X-Dashboard-Token` header.
- API keys are hashed with PBKDF2-HMAC-SHA256 + per-key salt.
- Inline `onclick` handlers escape names safely (apostrophe-safe).
- Config writes validated as YAML mappings before persisting.
