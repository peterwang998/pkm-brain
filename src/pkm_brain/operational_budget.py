from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from pathlib import Path
from typing import Any

from .operational_db import operational_connection
from .util import new_id, now_iso


BUDGET_METRICS = {
    "api_requests",
    "api_quota_units",
    "detector_calls",
    "detector_input_tokens",
    "detector_total_tokens",
}
BUDGET_SOURCES = {"calendar", "gmail"}


class DailyBudgetExceeded(RuntimeError):
    """A durable per-local-day source budget has no remaining capacity."""


def reserve_daily_budget(
    db_path: Path,
    *,
    source_type: str,
    metric: str,
    amount: int,
    limit: int | None,
    local_day: str,
    policy_version: str,
    run_id: str | None,
    created_at: str | None = None,
) -> dict[str, Any]:
    results = reserve_daily_budgets(
        db_path,
        source_type=source_type,
        reservations={metric: (amount, limit)},
        local_day=local_day,
        policy_version=policy_version,
        run_id=run_id,
        created_at=created_at,
    )
    return results[metric]


def reserve_daily_budgets(
    db_path: Path,
    *,
    source_type: str,
    reservations: Mapping[str, tuple[int, int | None]],
    local_day: str,
    policy_version: str,
    run_id: str | None,
    created_at: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Reserve several counters atomically before an external operation."""

    if source_type not in BUDGET_SOURCES:
        raise ValueError("unsupported budget source")
    if not reservations:
        raise ValueError("at least one budget reservation is required")
    normalized: dict[str, tuple[int, int | None]] = {}
    for metric, (amount, limit) in reservations.items():
        if metric not in BUDGET_METRICS:
            raise ValueError("unsupported budget metric")
        if isinstance(amount, bool) or int(amount) <= 0:
            raise ValueError("budget reservation amount must be positive")
        if limit is not None and (isinstance(limit, bool) or int(limit) <= 0):
            raise ValueError("budget limit must be positive when supplied")
        normalized[metric] = (
            int(amount),
            int(limit) if limit is not None else None,
        )
    try:
        date.fromisoformat(local_day)
    except ValueError:
        raise ValueError("budget local_day must be YYYY-MM-DD")
    if len(local_day) != 10:
        raise ValueError("budget local_day must be YYYY-MM-DD")
    if not policy_version or len(policy_version) > 128:
        raise ValueError("budget policy_version is invalid")
    timestamp = created_at or now_iso()
    output: dict[str, dict[str, Any]] = {}
    with operational_connection(db_path, write=True) as conn:
        used_by_metric: dict[str, int] = {}
        for metric, (amount, limit) in normalized.items():
            used = int(
                conn.execute(
                """
                SELECT COALESCE(SUM(amount), 0)
                FROM ops_budget_reservations
                WHERE local_day = ? AND source_type = ? AND metric = ?
                """,
                (local_day, source_type, metric),
                ).fetchone()[0]
            )
            used_by_metric[metric] = used
            if limit is not None and used + amount > limit:
                raise DailyBudgetExceeded(
                    f"{source_type} {metric} daily budget exhausted "
                    f"({used}/{limit} already reserved)"
                )
        for metric, (amount, limit) in normalized.items():
            reservation_id = new_id("opsbudget")
            conn.execute(
                """
                INSERT INTO ops_budget_reservations(
                  id, run_id, source_type, metric, amount, local_day,
                  policy_version, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    reservation_id,
                    run_id,
                    source_type,
                    metric,
                    amount,
                    local_day,
                    policy_version,
                    timestamp,
                ),
            )
            total = used_by_metric[metric] + amount
            output[metric] = {
                "id": reservation_id,
                "source_type": source_type,
                "metric": metric,
                "amount": amount,
                "used": total,
                "limit": limit,
                "remaining": max(0, limit - total) if limit is not None else None,
                "local_day": local_day,
            }
    return output


def daily_budget_usage(
    db_path: Path,
    *,
    local_day: str,
) -> dict[str, dict[str, int]]:
    with operational_connection(db_path, write=False) as conn:
        rows = conn.execute(
            """
            SELECT source_type, metric, SUM(amount) AS amount
            FROM ops_budget_reservations
            WHERE local_day = ?
            GROUP BY source_type, metric
            ORDER BY source_type, metric
            """,
            (local_day,),
        ).fetchall()
    output: dict[str, dict[str, int]] = {}
    for row in rows:
        output.setdefault(str(row["source_type"]), {})[
            str(row["metric"])
        ] = int(row["amount"])
    return output
