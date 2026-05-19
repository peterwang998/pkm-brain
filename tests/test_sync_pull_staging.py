from __future__ import annotations

import json
from pathlib import Path

from fake_transport import LocalRsyncTransport
from pkm_brain.paths import BrainPaths
from pkm_brain.sync_setup import add_peer, init_primary
from pkm_brain.sync_transfer import sync_pull
from pkm_brain.util import file_sha256


def primary_with_secondary(tmp_path: Path) -> tuple[BrainPaths, Path]:
    primary = BrainPaths.from_value(tmp_path / "primary")
    secondary_home = tmp_path / "secondary"
    init_primary(primary, "primary")
    add_peer(primary, "secondary", "secondary.local", "peter", secondary_home)
    return primary, secondary_home


def write_outbox_file(secondary_home: Path, relative_path: str, text: str, content_hash: str | None = None) -> Path:
    outbox = secondary_home / "outbox" / "secondary"
    path = outbox / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    row = {
        "node_id": "secondary",
        "source_kind": "agent_session_log",
        "agent": "codex",
        "session_id": path.stem,
        "relative_path": relative_path,
        "content_hash": content_hash or file_sha256(path),
        "captured_at": "2026-05-18T12:00:00Z",
        "source_path": "/secondary/source.jsonl",
    }
    manifest = outbox / "manifest.jsonl"
    with manifest.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(row, sort_keys=True) + "\n")
    return path


def test_pull_promotes_valid_files_and_removes_empty_staging(tmp_path: Path) -> None:
    primary, secondary_home = primary_with_secondary(tmp_path)
    write_outbox_file(
        secondary_home,
        "agent_logs/codex/session.md",
        "---\nsource_type: \"agent_session_log\"\nsession_id: \"session\"\n---\n\n# Session\n",
    )
    transport = LocalRsyncTransport(remote_home=secondary_home)

    result = sync_pull(primary, "secondary", transport=transport, run_id="run-ok", run_ingest=False)

    live_file = primary.inbox / "external" / "secondary" / "agent_logs" / "codex" / "session.md"
    assert result.status == "ok"
    assert result.promoted == ["agent_logs/codex/session.md"]
    assert live_file.exists()
    assert not (primary.inbox / "external" / "secondary" / "_staging" / "run-ok").exists()


def test_pull_empty_outbox_without_manifest_is_ok(tmp_path: Path) -> None:
    primary, secondary_home = primary_with_secondary(tmp_path)
    (secondary_home / "outbox" / "secondary").mkdir(parents=True)
    transport = LocalRsyncTransport(remote_home=secondary_home)

    result = sync_pull(primary, "secondary", transport=transport, run_id="run-empty", run_ingest=False)

    assert result.status == "ok"
    assert result.promoted == []
    assert result.rejected == []
    assert result.errors == []
    assert not (primary.inbox / "external" / "secondary" / "_staging" / "run-empty").exists()


def test_pull_files_without_manifest_still_fails(tmp_path: Path) -> None:
    primary, secondary_home = primary_with_secondary(tmp_path)
    path = secondary_home / "outbox" / "secondary" / "agent_logs" / "codex" / "session.md"
    path.parent.mkdir(parents=True)
    path.write_text("# Session\n", encoding="utf-8")
    transport = LocalRsyncTransport(remote_home=secondary_home)

    result = sync_pull(primary, "secondary", transport=transport, run_id="run-missing-manifest", run_ingest=False)

    assert result.status == "failed"
    assert any("missing manifest" in error for error in result.errors)


def test_pull_rejects_hash_mismatch_without_touching_live_file(tmp_path: Path) -> None:
    primary, secondary_home = primary_with_secondary(tmp_path)
    live_file = primary.inbox / "external" / "secondary" / "agent_logs" / "codex" / "bad.md"
    live_file.parent.mkdir(parents=True, exist_ok=True)
    live_file.write_text("existing good copy\n", encoding="utf-8")
    write_outbox_file(secondary_home, "agent_logs/codex/good.md", "# Good\n")
    write_outbox_file(secondary_home, "agent_logs/codex/bad.md", "# Bad\n", content_hash="wrong-hash")
    transport = LocalRsyncTransport(remote_home=secondary_home)

    result = sync_pull(primary, "secondary", transport=transport, run_id="run-mismatch", run_ingest=False)

    rejected = primary.inbox / "external" / "secondary" / "_staging" / "run-mismatch" / "_rejected" / "agent_logs" / "codex" / "bad.md"
    error = rejected.with_name("bad.md.error.json")
    assert result.status == "ok_with_rejections"
    assert result.promoted == ["agent_logs/codex/good.md"]
    assert result.rejected == ["agent_logs/codex/bad.md"]
    assert rejected.exists()
    assert json.loads(error.read_text(encoding="utf-8"))["reason"] == "hash_mismatch"
    assert live_file.read_text(encoding="utf-8") == "existing good copy\n"


def test_pull_rsync_failure_preserves_staging_and_live_inbox(tmp_path: Path) -> None:
    primary, secondary_home = primary_with_secondary(tmp_path)
    write_outbox_file(secondary_home, "agent_logs/codex/session.md", "# Session\n")
    transport = LocalRsyncTransport(remote_home=secondary_home, fail_rsync=True)

    result = sync_pull(primary, "secondary", transport=transport, run_id="run-fail", run_ingest=False)

    assert result.status == "failed"
    assert (primary.inbox / "external" / "secondary" / "_staging" / "run-fail").is_dir()
    assert not (primary.inbox / "external" / "secondary" / "agent_logs").exists()
