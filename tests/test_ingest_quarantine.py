from __future__ import annotations

import json
from pathlib import Path

from pkm_brain.db import connection
from pkm_brain.paths import BrainPaths
from pkm_brain.service import BrainService


def test_external_ingest_quarantines_failed_file_and_continues(tmp_path: Path, monkeypatch) -> None:
    paths = BrainPaths.from_value(tmp_path / "primary")
    svc = BrainService(paths, prefer_model_embeddings=False)
    svc.init_workspace()
    external = paths.inbox / "external" / "secondary"
    good = external / "notes" / "good.md"
    bad = external / "notes" / "bad.md"
    good.parent.mkdir(parents=True, exist_ok=True)
    good.write_text("# Good\n\nThis should ingest.\n", encoding="utf-8")
    bad.write_text("# Bad\n\nThis should quarantine.\n", encoding="utf-8")

    def flaky_detect(path: Path) -> str:
        if path.name == "bad.md":
            raise RuntimeError("synthetic parse failure")
        return "markdown_note"

    monkeypatch.setattr("pkm_brain.service.detect_source_type", flaky_detect)

    result = svc.ingest(external)

    quarantined = external / "_quarantine" / "notes" / "bad.md"
    error = external / "_quarantine" / "notes" / "bad.md.error.json"
    assert result.changed == 1
    assert result.errors
    assert quarantined.exists()
    assert not bad.exists()
    payload = json.loads(error.read_text(encoding="utf-8"))
    assert payload["error"] == "synthetic parse failure"
    with connection(paths.sqlite_path) as conn:
        rows = [dict(row) for row in conn.execute("SELECT origin_node_id, logical_source_key FROM documents")]
    assert rows == [{"origin_node_id": "secondary", "logical_source_key": "notes/good.md"}]


def test_retry_quarantine_restores_and_reingests_files(tmp_path: Path, monkeypatch) -> None:
    paths = BrainPaths.from_value(tmp_path / "primary")
    svc = BrainService(paths, prefer_model_embeddings=False)
    svc.init_workspace()
    external = paths.inbox / "external" / "secondary"
    bad = external / "notes" / "bad.md"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("# Bad\n\nFirst pass fails.\n", encoding="utf-8")

    def failing_detect(path: Path) -> str:
        if path.name == "bad.md":
            raise RuntimeError("synthetic parse failure")
        return "markdown_note"

    monkeypatch.setattr("pkm_brain.service.detect_source_type", failing_detect)
    svc.ingest(external)

    monkeypatch.setattr("pkm_brain.service.detect_source_type", lambda path: "markdown_note")
    retry = svc.ingest(external, retry_quarantine=True)

    assert retry.changed == 1
    assert retry.errors == []
    assert bad.exists()
    assert not (external / "_quarantine" / "notes" / "bad.md").exists()
    with connection(paths.sqlite_path) as conn:
        row = conn.execute("SELECT logical_source_key FROM documents").fetchone()
    assert row["logical_source_key"] == "notes/bad.md"
