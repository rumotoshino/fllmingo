# 🦩 FLLMingo

> **F**astAPI + **LLM** routing proxy with a terminal-themed dashboard. Self-hosted. Provider-agnostic. Pink as a flamingo (well, green).

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Python 3.11+](https://img.shields.io/badge/python-3.11+-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)

FLLMingo is an OpenAI-compatible proxy that routes your `/v1/chat/completions` requests across multiple LLM providers (OpenRouter, NVIDIA, Google, custom OpenAI-compatible endpoints, etc.) with automatic fallback, circuit breaking, and a slick terminal-aesthetic dashboard for live observability.

---

## ✨ Features

| | |
|---|---|
| 🔀 **Tiered routing** | Define tiers (`fast`, `standard`, `complex`) and FLLMingo picks the best healthy provider |
| 🔁 **Automatic fallback** | When a provider 5xx's, the next one in the tier picks up instantly |
| 🛡️ **Circuit breaker** | Quarantines failing providers, recovers after a cooldown |
| 🧪 **Auto-strip retry** | Removes incompatible params (`reasoning_effort`, `service_tier`, ...) on 400 errors |
| 🔁 **Exponential backoff** | Retries on 5xx/429 with growing delay |
| 📊 **Live dashboard** | WebSocket stream of every request, p50/p95/p99 latency, cost tracking |
| 🔑 **Scoped API keys** | Generate keys with quotas, expiry, and per-tier scopes |
| 🎨 **6 themes** | Dark, Light, VSCode, AMOLED, Solarized, Nord |
| 💰 **Cost budgets** | Daily / monthly caps with optional auto-pause |
| 🚨 **Webhook alerts** | Discord, Slack, or custom JSON on provider quarantine |
| 📤 **Export & backup** | One-click YAML config backup, CSV/JSON log export |
| 🏥 **Health probes** | Periodic background pings to detect outages early |
| 📝 **Prompt templates** | Save reusable system prompts with model + temperature defaults |
| 🔐 **Auth-gated** | Single shared token, optional dashboard password-change UX, first-run flow |

---

## 🚀 Quick Start

### 1. Clone & install

```bash
git clone https://github.com/rumotoshino/fllmingo.git
cd fllmingo
python3.11 -m venv .venv
source .venv/bin/activate
pip install -e .
```

### 2. Configure

```bash
cp .env.example .env
cp config.example.yaml config.yaml
```

Edit `.env` and set at minimum:

```
ROUTER_AUTH_TOKEN=pick-a-strong-token-here
OPENROUTER_API_KEY=sk-or-v1-...
```

Then edit `config.yaml` to add the tiers and models you want.

### 3. Run

```bash
fllmingo
```

The dashboard is at `http://localhost:8100`. Log in with your `ROUTER_AUTH_TOKEN`.

### 4. Use it as an OpenAI-compatible proxy

```bash
curl http://localhost:8100/v1/chat/completions \
  -H "Authorization: Bearer $ROUTER_AUTH_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "standard",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

Any tier name (`fast`, `standard`, `complex`) works as the `model`. FLLMingo picks the first healthy provider in that tier.

---

## 📦 Running as a systemd service (Linux)

```ini
# ~/.config/systemd/user/fllmingo.service
[Unit]
Description=FLLMingo LLM Router
After=network.target

[Service]
Type=simple
WorkingDirectory=/path/to/fllmingo
EnvironmentFile=/path/to/fllmingo/.env
ExecStart=/path/to/fllmingo/.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8100
Restart=on-failure

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now fllmingo.service
```

---

## 🔌 OpenAI Client Compatibility

FLLMingo speaks the OpenAI Chat Completions API. Drop it into anything that supports `OPENAI_BASE_URL`:

```python
from openai import OpenAI
client = OpenAI(
    base_url="http://localhost:8100/v1",
    api_key="your-ROUTER_AUTH_TOKEN-or-generated-key",
)
response = client.chat.completions.create(
    model="standard",   # tier name
    messages=[{"role": "user", "content": "Hi"}],
)
```

---

## 🎛️ Dashboard

| Tab | What's inside |
|-----|---------------|
| **STATUS** | Total requests, cost, error rate, live activity feed |
| **PROVIDERS** | Health per provider, edit endpoints/keys, manage models |
| **TIERS** | Drag-and-drop model ordering, cross-tier moves |
| **CATALOG** | Browse every model from every provider with full metadata |
| **≡ Menu** | Settings · Logs · Inspect · Leaderboard · Config |

The **Settings** tab is the hub for everything else: themes, password change, rate limiting, webhooks, budgets, backups, exports, latency stats, prompt templates, API keys.

---

## 🛠️ Configuration Reference

See [`config.example.yaml`](./config.example.yaml) for the full schema. All optional features (rate limiting, webhooks, budgets, health probes) are disabled by default and can be toggled from the Settings tab — no manual YAML editing required after setup.

---

## 📜 API Endpoints

| Endpoint | Purpose |
|---------|---------|
| `POST /v1/chat/completions` | OpenAI-compatible inference |
| `GET /v1/models` | List tier names + provider models |
| `GET /api/health` | Public health check |
| `GET /docs` | Interactive Swagger (auth-gated) |
| `GET /api/status` | Dashboard overview |
| `GET /api/catalog` | Aggregated model catalog |
| `GET /api/stats/latency` | p50/p95/p99 percentiles |
| `WS /ws` | Live request stream |

Full schema available at `/docs` once you authenticate.

---

## 🏗️ Architecture

```
              ┌──────────────┐
client ──────►│  FLLMingo    │ tier "standard"
              │              │   ├─ try provider A → 200 ✓ stream
              │  /v1/chat/   │   │
              │  completions │   ├─ try provider B (if A fails)
              │              │   └─ try provider C (if B fails)
              └──────┬───────┘
                     │ WS stream
              ┌──────▼───────┐
              │  Dashboard   │   request log, latency, cost, health
              └──────────────┘
```

- **Engine** (`app/engine.py`) — routing, fallback, retry, streaming
- **Database** (`app/database.py`) — SQLite for request log, provider health, latency
- **Config** (`app/config.py`) — YAML with hot-reload via file watcher
- **Sanitizer** (`app/sanitizer.py`) — strips incompatible params per-provider

---

## 🐛 Reporting Issues

If you hit a bug, please open an issue with:
- FLLMingo version (`pyproject.toml` `[project] version`)
- Python version
- Provider name + model name (if provider-specific)
- Stack trace from `journalctl --user -u fllmingo` or your runner's log

---

## 📋 License

[MIT](./LICENSE) — do whatever you want, just keep the copyright notice.

---

## 🦩 Why "FLLMingo"?

**F**astAPI + **LLM** + the green-on-black terminal vibe of a neon flamingo. Pronounced "flamingo." That's all.
