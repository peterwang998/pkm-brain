from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from pkm_brain.chunking import prepare_text_for_indexing
from pkm_brain.db import connection
from pkm_brain.extraction import EXTRACTION_PROMPT_VERSION, recent_source_cards
from pkm_brain.paths import BrainPaths
from pkm_brain.service import BrainService
from pkm_brain.source_dates import (
    source_frontmatter_with_path,
    trusted_gmail_message_policies,
)


ROOT = Path(__file__).parents[1]
MODULE_PATH = ROOT / "scripts" / "gmail_fact_parity_production_adapter.py"
SPEC = importlib.util.spec_from_file_location(
    "test_gmail_fact_parity_production_adapter", MODULE_PATH
)
assert SPEC is not None and SPEC.loader is not None
adapter = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = adapter
SPEC.loader.exec_module(adapter)
RUNNER_SPEC = importlib.util.spec_from_file_location(
    "test_gmail_fact_parity_production_adapter_runner",
    ROOT / "scripts" / "run_gmail_fact_parity.py",
)
assert RUNNER_SPEC is not None and RUNNER_SPEC.loader is not None
runner = importlib.util.module_from_spec(RUNNER_SPEC)
sys.modules[RUNNER_SPEC.name] = runner
RUNNER_SPEC.loader.exec_module(runner)

PACKET_ID = f"gfp_p_{'1' * 32}"
THREAD_ID = f"gfp_t_{'2' * 32}"
REVISION_ID = f"gfp_r_{'3' * 32}"
MESSAGE_1 = f"gfp_m_{'4' * 32}"
MESSAGE_2 = f"gfp_m_{'5' * 32}"
DATE_1 = "2026-07-01T16:00:00+00:00"
DATE_2 = "2026-07-02T17:00:00+00:00"
MODEL = "gpt-5.6-sol"
EFFORT = "medium"


def _packet() -> dict[str, Any]:
    return {
        "version": adapter.PACKET_VERSION,
        "packet_id": PACKET_ID,
        "thread_id": THREAD_ID,
        "revision_id": REVISION_ID,
        "projection_version": 8,
        "classifier_version": 5,
        "messages": [
            {
                "message_id": MESSAGE_1,
                "internal_date": DATE_1,
                "text": (
                    f"## Message 2 — {DATE_1} — {MESSAGE_1}\n\n"
                    "From: collaborator@example.test\n"
                    "To: owner@example.test\n"
                    "Subject: Project Atlas\n\n"
                    "Project Atlas remains limited to pilot customers."
                ),
            },
            {
                "message_id": MESSAGE_2,
                "internal_date": DATE_2,
                "text": (
                    f"## Message 8 — {DATE_2} — {MESSAGE_2}\n\n"
                    "From: owner@example.test\n"
                    "To: collaborator@example.test\n"
                    "Subject: Re: Project Atlas\n\n"
                    "The pilot boundary remains unchanged."
                ),
            },
        ],
    }


def _request() -> dict[str, Any]:
    return {
        "version": adapter.REQUEST_VERSION,
        "run_id": "v2-public-synthetic",
        "arm": "v2",
        "packet": _packet(),
        "runtime_config": {
            "production_api": adapter.PRODUCTION_API,
            "shadow": False,
            "isolated_disposable_home": True,
            "packet_policy": "identical_sealed_packet",
            "model": MODEL,
            "reasoning_effort": EFFORT,
            "prompt_version": EXTRACTION_PROMPT_VERSION,
        },
    }


def _write_fake_codex(path: Path, *, extraction: bool) -> Path:
    source = f"""#!{Path(sys.executable).resolve()}
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


arguments = sys.argv[1:]
if arguments == ["login", "status"]:
    print("Logged in")
    raise SystemExit(0)
if "exec" not in arguments:
    print("fake-codex")
    raise SystemExit(0)
prompt = sys.stdin.buffer.read()
output_path = Path(arguments[arguments.index("--output-last-message") + 1])
counter_path = Path(os.environ["FAKE_CODEX_COUNTER"])
try:
    counter = int(counter_path.read_text(encoding="utf-8")) + 1
except (OSError, ValueError):
    counter = 1
counter_path.write_text(str(counter), encoding="utf-8")
if {extraction!r} and b"Source window JSON:" in prompt:
    source_window = json.loads(
        prompt.decode("utf-8").rsplit("Source window JSON:", 1)[1]
    )
    selected = None
    for chunk in source_window["window"]["chunks"]:
        for unit in chunk.get("units") or []:
            if "Project Atlas remains limited to pilot customers." in unit["text"]:
                selected = (chunk["chunk_id"], unit["unit_id"])
                break
        if selected:
            break
    if selected is None:
        response = {{"facts": []}}
    else:
        response = {{
            "facts": [{{
                "statement": "Project Atlas remains limited to pilot customers.",
                "chunk_id": selected[0],
                "evidence_unit_ids": [selected[1]],
                "page_hint": "projects/project-atlas.md",
                "section_hint": "Summary",
                "claim_class": "project_state",
                "entities": [{{
                    "surface": "Project Atlas",
                    "type": "project",
                    "mention_kind": "named",
                    "is_primary": True,
                }}],
                "entity_key": "Project Atlas",
                "extraction_confidence": 0.99,
                "routing_confidence": 0.99,
                "truth_confidence": 0.99,
            }}]
        }}
else:
    response = {{"decision": "agree", "rationale": "Synthetic evidence agrees."}}
payload = json.dumps(response, sort_keys=True, separators=(",", ":")).encode("utf-8")
output_path.write_bytes(payload)
print(json.dumps({{
    "type": "thread.started",
    "thread_id": f"fake_session_{{counter}}",
}}))
"""
    path.write_text(source, encoding="utf-8")
    path.chmod(0o700)
    return path


def test_packet_body_is_identical_and_canonically_renumbered() -> None:
    request = adapter.validate_request(_request())

    body, ranges = adapter.render_packet_body(request["packet"])

    assert body.startswith(
        f"# Email thread: {THREAD_ID}\n\n## Message 1 — {DATE_1} — {MESSAGE_1}"
    )
    assert f"## Message 2 — {DATE_2} — {MESSAGE_2}" in body
    assert "## Message 8" not in body
    assert body[ranges[0]["start_offset"] : ranges[0]["end_offset"]].startswith(
        "## Message 1"
    )
    assert ranges[-1]["end_offset"] == len(body)


def test_both_arm_sources_expose_the_exact_same_indexed_body(tmp_path: Path) -> None:
    packet = _packet()
    body, ranges = adapter.render_packet_body(packet)
    home = tmp_path / "brain"

    original = adapter.write_original_source(home, body, packet)
    v2 = adapter.write_v2_source(home, body, ranges, packet)

    assert original.stem == THREAD_ID
    assert (
        prepare_text_for_indexing(original.read_text(encoding="utf-8"), "markdown_note")
        == body
    )
    assert (
        prepare_text_for_indexing(v2.read_text(encoding="utf-8"), "gmail_thread")
        == body
    )


def test_v2_source_rejects_a_packet_from_another_renderer_version(
    tmp_path: Path,
) -> None:
    packet = _packet()
    packet["projection_version"] += 1
    body, ranges = adapter.render_packet_body(packet)

    with pytest.raises(
        adapter.GmailFactParityAdapterError,
        match="renderer version",
    ):
        adapter.write_v2_source(tmp_path / "brain", body, ranges, packet)


def test_v2_projection_passes_real_timestamp_and_policy_authority(
    tmp_path: Path,
) -> None:
    packet = _packet()
    body, ranges = adapter.render_packet_body(packet)
    paths = BrainPaths.from_value(tmp_path / "brain")
    service = BrainService(paths)
    service.init_workspace()
    (paths.config_local / "cos_llm.yaml").write_text(
        "extraction:\n  source_types:\n    gmail_thread:\n      extract: true\n",
        encoding="utf-8",
    )

    source = adapter.write_v2_source(paths.home, body, ranges, packet)
    result = service.ingest(source=source)
    documents = recent_source_cards(paths, limit=10, changed_only=False)

    assert result.errors == []
    assert len(documents) == 1
    document = documents[0]
    assert document["structured_gmail_message_metadata_trusted"] is True
    assert [
        item["message_id"] for item in document["trusted_gmail_message_timestamps"]
    ] == [MESSAGE_1, MESSAGE_2]
    with connection(paths.sqlite_path) as conn:
        stored_document = dict(conn.execute("SELECT * FROM documents").fetchone())
    frontmatter, frontmatter_path = source_frontmatter_with_path(stored_document)
    policies = trusted_gmail_message_policies(
        stored_document, frontmatter, frontmatter_path
    )
    assert policies is not None
    assert [item["fact_admission_basis"] for item in policies] == [
        "durable_human_candidate",
        "durable_human_candidate",
    ]
    assert frontmatter["parity_admission_convention"] == (
        adapter.CONDITIONAL_ADMISSION_VERSION
    )


def test_evidence_message_ids_are_derived_from_chunk_offsets() -> None:
    packet = _packet()
    body, ranges = adapter.render_packet_body(packet)
    first = body.index("Project Atlas remains limited")
    second = body.index("The pilot boundary remains unchanged")
    chunks = {
        "chunk-public": {
            "chunk_id": "chunk-public",
            "start_offset": 0,
            "end_offset": len(body),
            "text": body,
        }
    }
    candidate = {
        "statement": "Two source-backed facts.",
        "source_spans": [
            {"chunk_id": "chunk-public", "start": first, "end": first + 13},
            {"chunk_id": "chunk-public", "start": second, "end": second + 9},
        ],
        "metadata": {"gmail_message_ids": [MESSAGE_1, MESSAGE_2]},
    }

    resolved = adapter.evidence_message_ids(
        candidate,
        chunks=chunks,
        body=body,
        message_ranges=ranges,
        packet_message_ids=[MESSAGE_1, MESSAGE_2],
    )

    assert resolved == [MESSAGE_1, MESSAGE_2]

    boundary = ranges[0]["end_offset"] - 2
    candidate["source_spans"] = [
        {"chunk_id": "chunk-public", "start": boundary, "end": boundary + 6}
    ]
    with pytest.raises(adapter.GmailFactParityAdapterError, match="ambiguous"):
        adapter.evidence_message_ids(
            candidate,
            chunks=chunks,
            body=body,
            message_ranges=ranges,
            packet_message_ids=[MESSAGE_1, MESSAGE_2],
        )


def test_codex_shim_hashes_exact_request_and_response_without_logging_login(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _write_fake_codex(tmp_path / "fake-codex", extraction=False)
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    counter = tmp_path / "counter"
    monkeypatch.setenv("PKM_BRAIN_FACT_PARITY_CODEX_BIN", str(fake))
    monkeypatch.setenv("FAKE_CODEX_COUNTER", str(counter))
    shim, ledger, real = adapter.write_codex_shim(home)
    environment = dict(os.environ)
    environment.update(
        {
            "PKM_BRAIN_FACT_PARITY_REAL_CODEX_BIN": str(real),
            "PKM_BRAIN_FACT_PARITY_LEDGER": str(ledger),
            "PKM_BRAIN_FACT_PARITY_EXPECTED_MODEL": MODEL,
            "PKM_BRAIN_FACT_PARITY_EXPECTED_EFFORT": EFFORT,
        }
    )

    login = subprocess.run(
        [str(shim), "login", "status"],
        capture_output=True,
        check=False,
        env=environment,
    )
    assert login.returncode == 0
    assert b"Logged in" in login.stdout
    assert not ledger.exists()

    output = tmp_path / "last-message.json"
    prompt = b"public synthetic prompt\n"
    completed = subprocess.run(
        [
            str(shim),
            "-c",
            'model_reasoning_effort="medium"',
            "exec",
            "--model",
            MODEL,
            "--output-last-message",
            str(output),
            "-",
        ],
        input=prompt,
        capture_output=True,
        check=False,
        env=environment,
    )

    assert completed.returncode == 0
    row = json.loads(ledger.read_text(encoding="utf-8"))
    assert row["request_sha256"] == hashlib.sha256(prompt).hexdigest()
    assert row["response_sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert row["invocation_id"] == "fake_session_1"
    assert stat.S_IMODE(ledger.stat().st_mode) == 0o600


def test_codex_shim_poison_row_prevents_a_swallowed_failure_from_attesting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _write_fake_codex(tmp_path / "fake-codex", extraction=False)
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    counter = tmp_path / "counter"
    monkeypatch.setenv("PKM_BRAIN_FACT_PARITY_CODEX_BIN", str(fake))
    monkeypatch.setenv("FAKE_CODEX_COUNTER", str(counter))
    shim, ledger, real = adapter.write_codex_shim(home)
    environment = dict(os.environ)
    environment.update(
        {
            "PKM_BRAIN_FACT_PARITY_REAL_CODEX_BIN": str(real),
            "PKM_BRAIN_FACT_PARITY_LEDGER": str(ledger),
            "PKM_BRAIN_FACT_PARITY_EXPECTED_MODEL": MODEL,
            "PKM_BRAIN_FACT_PARITY_EXPECTED_EFFORT": EFFORT,
        }
    )
    command = [
        str(shim),
        "-c",
        'model_reasoning_effort="medium"',
        "exec",
        "--model",
        MODEL,
        "--output-last-message",
        str(tmp_path / "last-message.json"),
        "-",
    ]
    successful = subprocess.run(
        command,
        input=b"public synthetic prompt\n",
        capture_output=True,
        check=False,
        env=environment,
    )
    assert successful.returncode == 0

    # Repoint only the shim's real executable after one valid invocation.  If a
    # later production stage swallows this provider failure, the invalid row
    # must still prevent the partial ledger from being accepted.
    environment["PKM_BRAIN_FACT_PARITY_REAL_CODEX_BIN"] = "/bin/false"
    failed = subprocess.run(
        command,
        input=b"public synthetic critic prompt\n",
        capture_output=True,
        check=False,
        env=environment,
    )
    assert failed.returncode != 0
    with pytest.raises(
        adapter.GmailFactParityAdapterError,
        match="invocation schema",
    ):
        adapter._read_invocations(
            ledger,
            model=MODEL,
            reasoning_effort=EFFORT,
        )


def test_adapter_runs_real_v2_api_and_reads_run_scoped_persistence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = _write_fake_codex(tmp_path / "fake-codex", extraction=True)
    counter = tmp_path / "counter"
    monkeypatch.setenv("PKM_BRAIN_FACT_PARITY_CODEX_BIN", str(fake))
    monkeypatch.setenv("FAKE_CODEX_COUNTER", str(counter))
    request = tmp_path / "request.json"
    response = tmp_path / "response.json"
    request.write_bytes(adapter.canonical_json(_request()) + b"\n")
    request.chmod(0o600)

    completed = subprocess.run(
        [
            sys.executable,
            str(MODULE_PATH),
            "--request",
            str(request),
            "--response",
            str(response),
        ],
        cwd=ROOT,
        capture_output=True,
        check=False,
        env=dict(os.environ),
    )

    assert completed.returncode == 0
    assert completed.stdout == b""
    assert completed.stderr == b""
    assert stat.S_IMODE(response.stat().st_mode) == 0o600
    value = json.loads(response.read_text(encoding="utf-8"))
    assert set(value) == adapter._RESPONSE_KEYS
    assert value["packet_id"] == PACKET_ID
    assert value["thread_id"] == THREAD_ID
    assert value["production_api"] == adapter.PRODUCTION_API
    assert [item["candidate"]["statement"] for item in value["members"]] == [
        "Project Atlas remains limited to pilot customers."
    ]
    assert value["members"][0]["evidence_message_ids"] == [MESSAGE_1]
    assert value["members"][0]["actions"]
    assert all(
        item["run_id"].startswith("gfp_") for item in value["members"][0]["actions"]
    )
    assert [item["window_index"] for item in value["invocations"]] == list(
        range(len(value["invocations"]))
    )
    assert {item["window_count"] for item in value["invocations"]} == {
        len(value["invocations"])
    }
    assert {item["provider"] for item in value["invocations"]} == {"external-codex"}
    assert int(counter.read_text(encoding="utf-8")) == len(value["invocations"])
    staged = runner._stage_member(
        value["members"][0], allowed_ids={MESSAGE_1, MESSAGE_2}
    )
    assert staged["stages"]["candidate"] is True
    assert (
        staged["stage_record"]["action_id"] == value["members"][0]["actions"][0]["id"]
    )


def test_adapter_rejects_non_private_request(tmp_path: Path) -> None:
    request = tmp_path / "request.json"
    request.write_bytes(adapter.canonical_json(_request()) + b"\n")
    request.chmod(0o644)

    with pytest.raises(adapter.GmailFactParityAdapterError, match="0600"):
        adapter.run_adapter(request, tmp_path / "response.json")


def test_adapter_failure_never_replays_private_request_content(
    tmp_path: Path,
) -> None:
    request_value = _request()
    private_marker = "PRIVATE_PACKET_MARKER_DO_NOT_PRINT"
    request_value["packet"]["messages"][0]["text"] += "\n" + private_marker
    request_value["runtime_config"]["prompt_version"] = "invalid-production-prompt"
    request = tmp_path / "request.json"
    response = tmp_path / "response.json"
    request.write_bytes(adapter.canonical_json(request_value) + b"\n")
    request.chmod(0o600)

    completed = subprocess.run(
        [
            sys.executable,
            str(MODULE_PATH),
            "--request",
            str(request),
            "--response",
            str(response),
        ],
        cwd=ROOT,
        capture_output=True,
        check=False,
        env=dict(os.environ),
    )

    assert completed.returncode == 1
    assert completed.stdout == b""
    assert completed.stderr == b""
    assert private_marker.encode("utf-8") not in completed.stdout + completed.stderr
    assert not response.exists()


def test_end_to_end_fixture_does_not_access_private_brain(
    tmp_path: Path,
) -> None:
    """Keep a visible guard that this suite uses only its disposable database."""

    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    with connection(paths.sqlite_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0] == 0
