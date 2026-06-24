from __future__ import annotations

from typing import Any

from .cos_actions import record_action_audit, revert_action
from .cos_policy import demote_policy_version
from .db import connection, loads
from .paths import BrainPaths


def run_sampled_audit(
    paths: BrainPaths,
    *,
    limit: int = 25,
    auto_revert_bad: bool = False,
) -> dict[str, Any]:
    with connection(paths.sqlite_path) as conn:
        actions = [
            dict(row)
            for row in conn.execute(
                """
                SELECT *
                FROM cos_actions
                WHERE status IN ('applied', 'auto_applied')
                  AND audit_status = 'unaudited'
                ORDER BY
                  CASE COALESCE(risk_tier, '')
                    WHEN 'high' THEN 0
                    WHEN 'medium' THEN 1
                    ELSE 2
                  END,
                  applied_at DESC
                LIMIT ?
                """,
                (limit,),
            )
        ]
    audited: list[dict[str, Any]] = []
    bad_action_ids: list[str] = []
    for action in actions:
        features = loads(action["action_features"], {})
        audit_status = "sampled_bad" if features.get("audit_expected_bad") else "sampled_ok"
        audited.append(
            record_action_audit(
                paths,
                action["id"],
                audit_status,
                metadata={"source": "sampled_audit"},
            )
        )
        if audit_status == "sampled_bad":
            bad_action_ids.append(action["id"])
    demoted_version = None
    if bad_action_ids:
        with connection(paths.sqlite_path) as conn:
            demoted_version = demote_policy_version(
                conn, reason=f"{len(bad_action_ids)} sampled actions were bad"
            )
    reverted: list[dict[str, Any]] = []
    if auto_revert_bad:
        for action_id in bad_action_ids:
            reverted.append(revert_action(paths, action_id))
    return {
        "status": "ok",
        "sampled": len(actions),
        "audited": audited,
        "bad_action_ids": bad_action_ids,
        "demoted_policy_version": demoted_version,
        "reverted": reverted,
    }
