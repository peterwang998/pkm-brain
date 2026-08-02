from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pkm_brain.cos_audit as cos_audit
from pkm_brain.db import connection
from pkm_brain.paths import BrainPaths
from pkm_brain.service import BrainService


def initialized_paths(tmp_path: Path) -> BrainPaths:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    return paths


def insert_applied_actions(conn: Any, count: int) -> list[str]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    action_ids = [f"cosact_historical_{index:05d}" for index in range(count)]
    conn.executemany(
        """
        INSERT INTO cos_actions(
          id, action_type, status, target_page_paths, risk_tier,
          audit_status, created_at, applied_at
        ) VALUES (?, 'canonicalize_page', 'applied', ?, 'high',
                  'unaudited', ?, ?)
        """,
        [
            (
                action_id,
                json.dumps([f"concepts/historical-{index:05d}.md"]),
                (start + timedelta(seconds=index)).isoformat(),
                (start + timedelta(seconds=index)).isoformat(),
            )
            for index, action_id in enumerate(action_ids)
        ],
    )
    return action_ids


class RecordingConnection:
    def __init__(self, conn: Any) -> None:
        self.conn = conn
        self.candidate_batch_limits: list[int] = []
        self.frozen_window_bind_counts: list[int] = []
        self.fact_bind_counts: list[int] = []
        self.lineage_bind_counts: list[int] = []

    def execute(self, sql: str, params: Any = ()) -> Any:
        normalized = " ".join(sql.split())
        values = list(params)
        if (
            "FROM cos_actions" in normalized
            and "ORDER BY COALESCE(applied_at, created_at) DESC, id DESC" in normalized
        ):
            self.candidate_batch_limits.append(int(values[-1]))
        elif "FROM cos_actions" in normalized and "AND id IN" in normalized:
            self.frozen_window_bind_counts.append(len(values))
        elif "SELECT id, status FROM facts WHERE id IN" in normalized:
            self.fact_bind_counts.append(len(values))
        elif (
            "FROM context_lineage_events" in normalized and "target_id IN" in normalized
        ):
            self.lineage_bind_counts.append(len(values))
        return self.conn.execute(sql, params)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.conn, name)


def test_large_historical_backlog_scans_a_hard_bounded_keyset_window(
    tmp_path: Path, monkeypatch
) -> None:
    paths = initialized_paths(tmp_path)
    backlog_size = cos_audit.HISTORICAL_AUDIT_CANDIDATE_SCAN_CAP * 3
    with connection(paths.sqlite_path) as conn:
        action_ids = insert_applied_actions(conn, backlog_size)

    decoded_action_ids: list[str] = []
    real_row_to_action = cos_audit.row_to_action

    def recording_row_to_action(row: Any) -> dict[str, Any]:
        action = real_row_to_action(row)
        decoded_action_ids.append(str(action["id"]))
        return action

    monkeypatch.setattr(cos_audit, "row_to_action", recording_row_to_action)
    with connection(paths.sqlite_path) as conn:
        recording_conn = RecordingConnection(conn)
        first = cos_audit.load_audit_sample(recording_conn, 5, historical=True)
        repeated = cos_audit.load_audit_sample(recording_conn, 5, historical=True)

        assert [action["id"] for action in first] == list(reversed(action_ids[-5:]))
        assert [action["id"] for action in repeated] == [
            action["id"] for action in first
        ]
        assert len(decoded_action_ids) == (
            cos_audit.HISTORICAL_AUDIT_CANDIDATE_SCAN_CAP * 2
        )
        assert max(recording_conn.candidate_batch_limits) <= (
            cos_audit.HISTORICAL_AUDIT_CANDIDATE_BATCH_SIZE
        )

        selected_ids = [str(action["id"]) for action in first]
        placeholders = ",".join("?" for _ in selected_ids)
        conn.execute(
            f"UPDATE cos_actions SET audit_status = 'sampled_ok' "
            f"WHERE id IN ({placeholders})",
            selected_ids,
        )
        decoded_action_ids.clear()
        advanced = cos_audit.load_audit_sample(recording_conn, 5, historical=True)

    assert [action["id"] for action in advanced] == list(reversed(action_ids[-10:-5]))
    assert len(decoded_action_ids) == cos_audit.HISTORICAL_AUDIT_CANDIDATE_SCAN_CAP


def test_frozen_historical_window_excludes_all_later_inserts_and_chunks_binds(
    tmp_path: Path,
) -> None:
    paths = initialized_paths(tmp_path)
    with connection(paths.sqlite_path) as conn:
        original_action_ids = insert_applied_actions(
            conn,
            cos_audit.HISTORICAL_AUDIT_CANDIDATE_SCAN_CAP,
        )
        initial = cos_audit.select_historical_audit_cohort(conn, 5)
        window_action_ids = list(initial["window_action_ids"])
        conn.executemany(
            """
            INSERT INTO cos_actions(
              id, action_type, status, target_page_paths, risk_tier,
              audit_status, created_at, applied_at
            ) VALUES (?, 'canonicalize_page', 'applied', '[]', 'high',
                      'unaudited', ?, ?)
            """,
            [
                (
                    "cosact_later_newer",
                    "2027-01-01T00:00:00+00:00",
                    "2027-01-01T00:00:00+00:00",
                ),
                (
                    "cosact_later_backdated",
                    "2025-01-01T00:00:00+00:00",
                    "2025-01-01T00:00:00+00:00",
                ),
            ],
        )
        recording_conn = RecordingConnection(conn)
        frozen = cos_audit.select_historical_audit_cohort(
            recording_conn,
            5,
            window_action_ids=window_action_ids,
        )

    assert len(window_action_ids) == cos_audit.HISTORICAL_AUDIT_CANDIDATE_SCAN_CAP
    assert set(window_action_ids) == set(original_action_ids)
    assert [action["id"] for action in frozen["actions"]] == [
        action["id"] for action in initial["actions"]
    ]
    assert sum(recording_conn.frozen_window_bind_counts) == len(window_action_ids)
    assert max(recording_conn.frozen_window_bind_counts) <= (
        cos_audit.SQLITE_SAFE_BIND_BATCH_SIZE
    )


def test_historical_priority_fact_lookups_are_fair_capped_and_bind_chunked(
    tmp_path: Path,
) -> None:
    paths = initialized_paths(tmp_path)
    actions = [
        {
            "id": f"action_{action_index:03d}",
            "target_fact_ids": [
                f"fact_{action_index:03d}_{fact_index:03d}"
                for fact_index in range(
                    cos_audit.HISTORICAL_PRIORITY_FACT_IDS_PER_ACTION + 20
                )
            ],
        }
        for action_index in range(100)
    ]
    first_fact_id = str(actions[0]["target_fact_ids"][0])
    last_action_first_fact_id = str(actions[-1]["target_fact_ids"][0])
    with connection(paths.sqlite_path) as conn:
        conn.execute(
            """
            INSERT INTO facts(
              id, statement, entity_key, confidence, status, created_at
            ) VALUES (?, 'Conflicting historical state.', 'concept:history',
                      1.0, 'contested', '2026-01-01T00:00:00+00:00')
            """,
            (first_fact_id,),
        )
        recording_conn = RecordingConnection(conn)
        bounded_ids = cos_audit.bounded_historical_priority_fact_ids(actions)
        priorities = cos_audit.historical_priority_index(recording_conn, actions)

    assert len(bounded_ids) == cos_audit.HISTORICAL_PRIORITY_FACT_ID_SCAN_CAP
    assert first_fact_id in bounded_ids
    assert last_action_first_fact_id in bounded_ids
    assert priorities["action_000"]["direct_contradiction"] == 1
    assert sum(recording_conn.fact_bind_counts) == len(bounded_ids)
    assert sum(recording_conn.lineage_bind_counts) == len(bounded_ids)
    assert max(recording_conn.fact_bind_counts) <= cos_audit.SQLITE_SAFE_BIND_BATCH_SIZE
    assert max(recording_conn.lineage_bind_counts) <= (
        cos_audit.SQLITE_SAFE_BIND_BATCH_SIZE
    )


def test_historical_semantic_priority_is_preserved_inside_bounded_window(
    tmp_path: Path,
) -> None:
    paths = initialized_paths(tmp_path)
    with connection(paths.sqlite_path) as conn:
        action_ids = insert_applied_actions(conn, 20)
        contested_action_id = action_ids[0]
        conn.execute(
            """
            INSERT INTO facts(
              id, statement, entity_key, confidence, status, created_at
            ) VALUES ('fact_contested_history', 'A contested historical state.',
                      'concept:history', 1.0, 'contested',
                      '2026-01-01T00:00:00+00:00')
            """
        )
        conn.execute(
            """
            UPDATE cos_actions
            SET target_fact_ids = '["fact_contested_history"]'
            WHERE id = ?
            """,
            (contested_action_id,),
        )
        selected = cos_audit.load_audit_sample(conn, 5, historical=True)

    assert selected[0]["id"] == contested_action_id
    assert selected[0]["historical_priority"]["direct_contradiction"] == 1


def test_historical_keyset_uses_legacy_effective_chronology(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = initialized_paths(tmp_path)
    with connection(paths.sqlite_path) as conn:
        conn.executemany(
            """
            INSERT INTO cos_actions(
              id, action_type, status, target_page_paths, risk_tier,
              audit_status, created_at, applied_at
            ) VALUES (?, 'canonicalize_page', 'applied', ?, 'high',
                      'unaudited', ?, ?)
            """,
            [
                (
                    "cosact_applied_old",
                    '["concepts/applied-old.md"]',
                    "2026-03-01T00:00:00+00:00",
                    "2026-01-01T00:00:00+00:00",
                ),
                (
                    "cosact_legacy_a",
                    '["concepts/legacy-a.md"]',
                    "2026-02-01T00:00:00+00:00",
                    None,
                ),
                (
                    "cosact_legacy_z",
                    '["concepts/legacy-z.md"]',
                    "2026-02-01T00:00:00+00:00",
                    None,
                ),
            ],
        )
        monkeypatch.setattr(cos_audit, "HISTORICAL_AUDIT_CANDIDATE_SCAN_CAP", 2)
        monkeypatch.setattr(cos_audit, "HISTORICAL_AUDIT_CANDIDATE_BATCH_SIZE", 1)
        first, cursor, reached_end = cos_audit.load_bounded_historical_candidates(
            conn,
            [
                "status IN ('applied', 'auto_applied')",
                "audit_status = 'unaudited'",
            ],
            [],
        )
        second, _, final_reached_end = cos_audit.load_bounded_historical_candidates(
            conn,
            [
                "status IN ('applied', 'auto_applied')",
                "audit_status = 'unaudited'",
            ],
            [],
            scan_after=cursor,
        )

    assert [action["id"] for action in first] == [
        "cosact_legacy_z",
        "cosact_legacy_a",
    ]
    assert cursor == ("2026-02-01T00:00:00+00:00", "cosact_legacy_a")
    assert reached_end is False
    assert [action["id"] for action in second] == ["cosact_applied_old"]
    assert final_reached_end is True


def test_historical_keyset_preserves_blank_applied_at_across_batches(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = initialized_paths(tmp_path)
    action_ids = [f"cosact_blank_applied_{index:03d}" for index in range(130)]
    with connection(paths.sqlite_path) as conn:
        conn.executemany(
            """
            INSERT INTO cos_actions(
              id, action_type, status, target_page_paths, risk_tier,
              audit_status, created_at, applied_at
            ) VALUES (?, 'canonicalize_page', 'applied', '[]', 'high',
                      'unaudited', ?, '')
            """,
            [
                (
                    action_id,
                    f"2026-01-01T00:{index // 60:02d}:{index % 60:02d}+00:00",
                )
                for index, action_id in enumerate(action_ids)
            ],
        )
        monkeypatch.setattr(cos_audit, "HISTORICAL_AUDIT_CANDIDATE_SCAN_CAP", 128)
        monkeypatch.setattr(cos_audit, "HISTORICAL_AUDIT_CANDIDATE_BATCH_SIZE", 64)
        where = [
            "status IN ('applied', 'auto_applied')",
            "audit_status = 'unaudited'",
        ]

        first, cursor, reached_end = cos_audit.load_bounded_historical_candidates(
            conn,
            where,
            [],
        )
        second, final_cursor, final_reached_end = (
            cos_audit.load_bounded_historical_candidates(
                conn,
                where,
                [],
                scan_after=cursor,
            )
        )

    expected_descending = list(reversed(action_ids))
    assert [action["id"] for action in first] == expected_descending[:128]
    assert len({action["id"] for action in first}) == 128
    assert cursor == ("", expected_descending[127])
    assert reached_end is False
    assert [action["id"] for action in second] == expected_descending[128:]
    assert final_cursor == ("", expected_descending[-1])
    assert final_reached_end is True
