from __future__ import annotations

from pathlib import Path

from fake_transport import LocalRsyncTransport
from pkm_brain.paths import BrainPaths
from pkm_brain.sync_setup import add_peer, init_primary
from pkm_brain.sync_transfer import sync_push


def test_push_mirrors_canonical_subdirs_and_preserves_local_only_remote_paths(tmp_path: Path) -> None:
    primary = BrainPaths.from_value(tmp_path / "primary")
    secondary_home = tmp_path / "secondary"
    init_primary(primary, "primary")
    add_peer(primary, "secondary", "secondary.local", "peter", secondary_home)

    (primary.raw / "keep.md").parent.mkdir(parents=True, exist_ok=True)
    (primary.raw / "keep.md").write_text("# Raw\n", encoding="utf-8")
    (primary.raw / "cache" / "skip.md").parent.mkdir(parents=True, exist_ok=True)
    (primary.raw / "cache" / "skip.md").write_text("skip\n", encoding="utf-8")
    (primary.raw / "tmp" / "skip.md").parent.mkdir(parents=True, exist_ok=True)
    (primary.raw / "tmp" / "skip.md").write_text("skip\n", encoding="utf-8")
    (primary.raw / "brain.sqlite").write_text("skip\n", encoding="utf-8")
    (primary.raw / "brain.sqlite-wal").write_text("skip\n", encoding="utf-8")
    (primary.raw / "brain.sqlite-shm").write_text("skip\n", encoding="utf-8")
    (primary.raw / ".DS_Store").write_text("skip\n", encoding="utf-8")
    (primary.wiki / "index.md").parent.mkdir(parents=True, exist_ok=True)
    (primary.wiki / "index.md").write_text("# Wiki\n", encoding="utf-8")
    (primary.memory / "global" / "mem.md").parent.mkdir(parents=True, exist_ok=True)
    (primary.memory / "global" / "mem.md").write_text("# Memory\n", encoding="utf-8")
    (primary.config_shared / "shared.yaml").parent.mkdir(parents=True, exist_ok=True)
    (primary.config_shared / "shared.yaml").write_text("shared: true\n", encoding="utf-8")
    (secondary_home / "config").mkdir(parents=True, exist_ok=True)
    (secondary_home / "config" / "sync.yaml").write_text("remote-sync: keep\n", encoding="utf-8")
    (secondary_home / "config" / "local").mkdir(parents=True, exist_ok=True)
    (secondary_home / "config" / "local" / "config.yaml").write_text("local: keep\n", encoding="utf-8")
    (secondary_home / "outbox" / "secondary").mkdir(parents=True, exist_ok=True)
    (secondary_home / "outbox" / "secondary" / "manifest.jsonl").write_text("{}\n", encoding="utf-8")

    result = sync_push(primary, "secondary", transport=LocalRsyncTransport(remote_home=secondary_home))

    assert result.status == "ok"
    assert result.pushed == ["raw/", "wiki/", "memory/", "config/shared/"]
    assert (secondary_home / "raw" / "keep.md").exists()
    assert not (secondary_home / "raw" / "cache" / "skip.md").exists()
    assert not (secondary_home / "raw" / "tmp" / "skip.md").exists()
    assert not (secondary_home / "raw" / "brain.sqlite").exists()
    assert not (secondary_home / "raw" / "brain.sqlite-wal").exists()
    assert not (secondary_home / "raw" / "brain.sqlite-shm").exists()
    assert not (secondary_home / "raw" / ".DS_Store").exists()
    assert (secondary_home / "wiki" / "index.md").exists()
    assert (secondary_home / "memory" / "global" / "mem.md").exists()
    assert (secondary_home / "config" / "shared" / "shared.yaml").exists()
    assert (secondary_home / "config" / "sync.yaml").read_text(encoding="utf-8") == "remote-sync: keep\n"
    assert (secondary_home / "config" / "local" / "config.yaml").read_text(encoding="utf-8") == "local: keep\n"
    assert (secondary_home / "outbox" / "secondary" / "manifest.jsonl").exists()
