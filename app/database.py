"""Async SQLite database for request logging, cost tracking, and health."""

from __future__ import annotations

import aiosqlite
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "router.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT NOT NULL DEFAULT (datetime('now')),
    request_id TEXT,
    incoming_model TEXT,
    resolved_provider TEXT,
    resolved_model TEXT,
    tier TEXT,
    status_code INTEGER,
    latency_ms INTEGER,
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    cost REAL DEFAULT 0,
    error TEXT,
    request_body TEXT,
    response_excerpt TEXT,
    retried INTEGER DEFAULT 0,
    stripped_params TEXT
);

CREATE TABLE IF NOT EXISTS provider_health (
    provider TEXT PRIMARY KEY,
    status TEXT DEFAULT 'healthy',
    consecutive_failures INTEGER DEFAULT 0,
    last_failure TEXT,
    last_success TEXT,
    quarantined_until TEXT,
    total_requests INTEGER DEFAULT 0,
    total_failures INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_requests_ts ON requests(timestamp);
CREATE INDEX IF NOT EXISTS idx_requests_provider ON requests(resolved_provider);
CREATE INDEX IF NOT EXISTS idx_requests_status ON requests(status_code);
"""


def _ensure_dir():
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)


async def init_db():
    _ensure_dir()
    async with aiosqlite.connect(_DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()


async def log_request(
    *,
    request_id: str | None = None,
    incoming_model: str = "",
    resolved_provider: str = "",
    resolved_model: str = "",
    tier: str = "",
    status_code: int = 0,
    latency_ms: int = 0,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    cost: float = 0.0,
    error: str | None = None,
    request_body: str | None = None,
    response_excerpt: str | None = None,
    retried: bool = False,
    stripped_params: str | None = None,
):
    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute(
            """INSERT INTO requests
               (request_id, incoming_model, resolved_provider, resolved_model,
                tier, status_code, latency_ms, prompt_tokens, completion_tokens,
                cost, error, request_body, response_excerpt, retried, stripped_params)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                request_id, incoming_model, resolved_provider, resolved_model,
                tier, status_code, latency_ms, prompt_tokens, completion_tokens,
                cost, error, request_body[:5000] if request_body else None,
                response_excerpt[:1000] if response_excerpt else None,
                1 if retried else 0, stripped_params,
            ),
        )
        await db.commit()


async def get_recent_requests(limit: int = 50) -> list[dict[str, Any]]:
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM requests ORDER BY id DESC LIMIT ?", (limit,)
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def get_request_by_id(req_id: int) -> dict[str, Any] | None:
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute("SELECT * FROM requests WHERE id = ?", (req_id,))
        row = await cursor.fetchone()
        return dict(row) if row else None


async def get_daily_stats() -> dict[str, Any]:
    """Aggregate stats for today."""
    async with aiosqlite.connect(_DB_PATH) as db:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        cursor = await db.execute(
            """SELECT
                COUNT(*) as total,
                COALESCE(SUM(cost), 0) as total_cost,
                SUM(CASE WHEN status_code >= 400 THEN 1 ELSE 0 END) as errors,
                COALESCE(SUM(prompt_tokens), 0) as prompt_tokens,
                COALESCE(SUM(completion_tokens), 0) as completion_tokens
               FROM requests WHERE date(timestamp) = ?""",
            (today,),
        )
        row = await cursor.fetchone()
        return {
            "total": row[0] or 0,
            "total_cost": round(row[1] or 0, 4),
            "errors": row[2] or 0,
            "error_rate": round(((row[2] or 0) / row[0] * 100), 1) if row[0] else 0,
            "prompt_tokens": row[3] or 0,
            "completion_tokens": row[4] or 0,
        }


async def get_provider_stats() -> list[dict[str, Any]]:
    """Per-provider health from the provider_health table."""
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            """SELECT provider, status, consecutive_failures,
                      last_failure, last_success, quarantined_until,
                      total_requests, total_failures
               FROM provider_health"""
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]


async def update_provider_health(provider: str, success: bool, error: str = ""):
    now = datetime.now(timezone.utc).isoformat()
    # Read circuit breaker config from config.yaml
    from .config import get_config
    _cfg = get_config()
    _cb = _cfg.get("circuit_breaker", {})
    _threshold = int(_cb.get("failure_threshold", 3))
    _recovery = int(_cb.get("recovery_timeout", 60))
    async with aiosqlite.connect(_DB_PATH) as db:
        # Upsert
        if success:
            await db.execute(
                """INSERT INTO provider_health (provider, status, consecutive_failures,
                    last_success, total_requests, total_failures, quarantined_until)
                   VALUES (?, 'healthy', 0, ?, 1, 0, NULL)
                   ON CONFLICT(provider) DO UPDATE SET
                    status='healthy',
                    consecutive_failures=0,
                    last_success=excluded.last_success,
                    total_requests=provider_health.total_requests+1""",
                (provider, now),
            )
        else:
            await db.execute(
                """INSERT INTO provider_health (provider, status, consecutive_failures,
                    last_failure, total_requests, total_failures)
                   VALUES (?, 'degraded', 1, ?, 1, 1)
                   ON CONFLICT(provider) DO UPDATE SET
                    consecutive_failures=provider_health.consecutive_failures+1,
                    last_failure=excluded.last_failure,
                    total_requests=provider_health.total_requests+1,
                    total_failures=provider_health.total_failures+1,
                    status=CASE WHEN provider_health.consecutive_failures+1 >= ?
                        THEN 'quarantined' ELSE 'degraded' END,
                    quarantined_until=CASE WHEN provider_health.consecutive_failures+1 >= ?
                        THEN datetime('now', '+' || ? || ' seconds') ELSE NULL END""",
                (provider, now, _threshold, _threshold, _recovery),
            )
        await db.commit()


async def get_token_stats() -> dict[str, Any]:
    """Token usage aggregated by time periods."""
    async with aiosqlite.connect(_DB_PATH) as conn:
        periods = {
            "today": "date(timestamp) = date('now')",
            "7d": "timestamp >= datetime('now', '-7 days')",
            "30d": "timestamp >= datetime('now', '-30 days')",
            "all": "1=1",
        }
        result: dict[str, Any] = {"periods": {}}
        for label, where in periods.items():
            cursor = await conn.execute(
                f"""SELECT
                        COALESCE(SUM(prompt_tokens), 0),
                        COALESCE(SUM(completion_tokens), 0)
                    FROM requests WHERE {where}"""
            )
            row = await cursor.fetchone()
            prompt, completion = int(row[0]), int(row[1])
            result["periods"][label] = {
                "prompt": prompt,
                "completion": completion,
                "total": prompt + completion,
            }
        all_p = result["periods"]["all"]
        result["total_prompt"] = all_p["prompt"]
        result["total_completion"] = all_p["completion"]
        result["total_all"] = all_p["total"]
        return result


async def get_leaderboard(
    sort: str = "requests",
    period: str = "all",
    direction: str = "desc",
) -> list[dict[str, Any]]:
    """Model ranking by usage, grouped by resolved_model + resolved_provider."""
    period_map = {
        "today": "date(timestamp) = date('now')",
        "7d": "timestamp >= datetime('now', '-7 days')",
        "30d": "timestamp >= datetime('now', '-30 days')",
        "all": "1=1",
    }
    where = period_map.get(period, "1=1")

    sort_col_map = {
        "requests": "req_count",
        "prompt_tokens": "sum_prompt",
        "completion_tokens": "sum_completion",
        "total_tokens": "sum_total",
    }
    sort_col = sort_col_map.get(sort, "req_count")
    dir_sql = "ASC" if direction == "asc" else "DESC"

    async with aiosqlite.connect(_DB_PATH) as conn:
        cursor = await conn.execute(
            f"""SELECT
                    resolved_model,
                    resolved_provider,
                    COUNT(*) as req_count,
                    COALESCE(SUM(prompt_tokens), 0) as sum_prompt,
                    COALESCE(SUM(completion_tokens), 0) as sum_completion,
                    COALESCE(SUM(prompt_tokens), 0) + COALESCE(SUM(completion_tokens), 0) as sum_total
                FROM requests
                WHERE {where}
                  AND resolved_model != ''
                GROUP BY resolved_model, resolved_provider
                ORDER BY {sort_col} {dir_sql}"""
        )
        rows = await cursor.fetchall()
        result = []
        for rank, row in enumerate(rows, 1):
            result.append({
                "rank": rank,
                "model": row[0],
                "provider": row[1],
                "requests": row[2],
                "prompt_tokens": int(row[3]),
                "completion_tokens": int(row[4]),
                "total_tokens": int(row[5]),
            })
        return result


async def is_quarantined(provider: str, threshold: int = 3) -> bool:
    async with aiosqlite.connect(_DB_PATH) as db:
        cursor = await db.execute(
            "SELECT quarantined_until FROM provider_health WHERE provider = ?",
            (provider,),
        )
        row = await cursor.fetchone()
        if row and row[0]:
            # SQLite's datetime('now', '+N seconds') returns a naive UTC string.
            # Treat both sides as naive UTC to avoid offset-naive-vs-aware errors.
            try:
                qu = datetime.fromisoformat(row[0])
                if qu.tzinfo is None:
                    qu = qu.replace(tzinfo=timezone.utc)
                return datetime.now(timezone.utc) < qu
            except (ValueError, TypeError):
                return False
        return False


async def get_monthly_cost() -> float:
    """Sum of costs from the start of the current month."""
    async with aiosqlite.connect(_DB_PATH) as db:
        cursor = await db.execute(
            "SELECT COALESCE(SUM(cost), 0) FROM requests "
            "WHERE strftime('%Y-%m', timestamp) = strftime('%Y-%m', 'now')"
        )
        row = await cursor.fetchone()
        return round(row[0] or 0, 4)


async def get_latency_stats(period: str = "7d") -> dict[str, Any]:
    """Per-provider latency percentiles (p50/p95/p99)."""
    period_clause = {
        "today": "date(timestamp) = date('now')",
        "7d": "timestamp >= datetime('now', '-7 days')",
        "30d": "timestamp >= datetime('now', '-30 days')",
        "all": "1=1",
    }.get(period, "timestamp >= datetime('now', '-7 days')")

    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            f"""SELECT resolved_provider, latency_ms FROM requests
                WHERE {period_clause} AND latency_ms IS NOT NULL AND status_code = 200
                ORDER BY resolved_provider, latency_ms"""
        )
        rows = await cursor.fetchall()

    # Bucket by provider
    by_provider: dict[str, list[int]] = {}
    for r in rows:
        prov = r["resolved_provider"] or "unknown"
        by_provider.setdefault(prov, []).append(r["latency_ms"])

    def percentile(sorted_data, p):
        if not sorted_data:
            return 0
        k = int(len(sorted_data) * p / 100)
        return sorted_data[min(k, len(sorted_data) - 1)]

    result = []
    for prov, latencies in by_provider.items():
        latencies.sort()
        result.append({
            "provider": prov,
            "count": len(latencies),
            "p50": percentile(latencies, 50),
            "p95": percentile(latencies, 95),
            "p99": percentile(latencies, 99),
            "min": latencies[0],
            "max": latencies[-1],
            "avg": round(sum(latencies) / len(latencies), 1),
        })
    return {"period": period, "providers": sorted(result, key=lambda x: x["p50"])}


async def export_logs(days: int = 7) -> list[dict[str, Any]]:
    """Export request logs as list of dicts."""
    async with aiosqlite.connect(_DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT * FROM requests WHERE timestamp >= datetime('now', ?) ORDER BY id DESC",
            (f"-{days} days",),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
