# Changelog

All notable changes to FLLMingo are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [1.3.0b1] — 2026-06-27 (beta)

### Added

- **Public model aliases** — define rich, client-facing model names that route to tiers under the hood. Clients see `GPT5`, `fast-claude`, or any name you pick instead of internal tier names like `standard` or `complex`. Aliases now appear as first-class entries in `/v1/models` with custom `display_name`, `owned_by`, and `description` fields. Perfect for harness/SDK selection where the public model name matters.
- **Aliases tab on Tiers page** — full CRUD UI (create, edit, rename, delete) with tier dropdown and live `/v1/models` integration.
- New endpoints: `GET /api/aliases`, `POST /api/aliases`, `PUT /api/aliases/{name}`, `DELETE /api/aliases/{name}`.

### Changed

- `resolve_tier()` now accepts both legacy string aliases (`{auto: standard}`) and rich dict aliases (`{GPT5: {tier: complex, display_name: "GPT-5", ...}}`). Backward compatible — existing configs keep working.

## [1.2.1b1] — 2026-06-27 (beta)

Bugfix-only release. Squashes silent failures in request logging and the live dashboard.

### Fixed

- **`is_quarantined()` TypeError**: SQLite's `datetime('now', ...)` returned naive ISO strings; comparing them against a timezone-aware `datetime.now()` raised `TypeError: can't compare offset-naive and offset-aware datetimes` on every single chat completion. The exception bubbled past `log_request()`, so **successful requests never reached the database**. Naive timestamps are now coerced to UTC before comparison.
- **Token explosion in streaming**: `prompt_tokens` / `completion_tokens` were accumulated with `+=` on every SSE chunk, but providers send *cumulative* usage. Result: tokens reported as 273k (or 0 if a stream ended without a usage chunk). Now assigns the latest non-zero value.
- **Log row written after `done` yield**: dashboard's `loadStatus()` fired before the DB write completed → stale totals. Reordered so the row is logged *before* `done` is emitted.
- **Live feed died after switching tabs**: `handleWSMessage` looked up `liveStream` element unconditionally; on non-STATUS pages it was `null`, throwing silently. WS handler now buffers events into a global ring and renders only when the element exists; `rehydrateLiveFeed()` replays the buffer when STATUS re-mounts.
- **Usage Summary blank on initial mount**: `initApp()` only invoked `loadStatus()`. Now also runs `loadUsage()` and the 5s refresh covers both.

### Changed

- Live feed shows completion-token count next to each successful response (e.g. `✓ ... — 123ms $0.0042 (45 tok)`).
- `loadStatus()` / `loadUsage()` errors are wrapped in try/catch inside the WS handler so a transient API hiccup can't kill the feed.

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
