from pathlib import Path

import pytest

from pkm_brain.operational_budget import (
    DailyBudgetExceeded,
    daily_budget_usage,
    reserve_daily_budgets,
)
from pkm_brain.operational_db import init_operational_db


def test_daily_budget_reservations_are_durable_and_multi_metric_atomic(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "ops.sqlite"
    init_operational_db(db_path)

    reserved = reserve_daily_budgets(
        db_path,
        source_type="gmail",
        reservations={
            "detector_calls": (1, 2),
            "detector_input_tokens": (100, 150),
        },
        local_day="2026-07-13",
        policy_version="shadow@1",
        run_id=None,
    )
    assert reserved["detector_calls"]["remaining"] == 1
    assert daily_budget_usage(db_path, local_day="2026-07-13") == {
        "gmail": {"detector_calls": 1, "detector_input_tokens": 100}
    }

    with pytest.raises(DailyBudgetExceeded, match="detector_input_tokens"):
        reserve_daily_budgets(
            db_path,
            source_type="gmail",
            reservations={
                "detector_calls": (1, 2),
                "detector_input_tokens": (100, 150),
            },
            local_day="2026-07-13",
            policy_version="shadow@1",
            run_id=None,
        )

    assert daily_budget_usage(db_path, local_day="2026-07-13") == {
        "gmail": {"detector_calls": 1, "detector_input_tokens": 100}
    }
