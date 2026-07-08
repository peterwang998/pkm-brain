from __future__ import annotations

import json
from pathlib import Path

from pkm_brain.capture import CaptureResult
from pkm_brain.paths import BrainPaths
from pkm_brain.service import BrainService


def test_service_result_surfaces_are_json_serializable(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    svc = BrainService(paths)
    svc.init_workspace()
    paths.sync_config_file.write_text(
        f"""
node_id: primary-laptop
role: primary
brain_home: {paths.home}
peers: []
""",
        encoding="utf-8",
    )
    note = paths.inbox / "json.md"
    note.write_text("# JSON\n\nService surfaces serialize.\n", encoding="utf-8")

    ingest = svc.ingest().as_dict()
    doctor = svc.doctor()
    sync_doctor = svc.sync_doctor()
    memories = svc.list_memories()
    capture = CaptureResult(discovered=1, captured=0, skipped=1).as_dict()

    json.dumps(ingest)
    json.dumps(doctor)
    json.dumps(sync_doctor)
    json.dumps(memories)
    json.dumps(capture)
    assert doctor["nightly"]["status"] == "warning"
    assert doctor["nightly"]["warning"] == "no successful nightly run recorded"
