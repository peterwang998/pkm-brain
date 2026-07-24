from __future__ import annotations

import importlib.util
import json
import stat
import sys
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).parents[1]
RUNNER_PATH = ROOT / "scripts" / "run_gmail_fact_parity.py"
EVALUATOR_PATH = ROOT / "scripts" / "evaluate_gmail_fact_parity.py"


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


runner = _load("test_run_gmail_fact_parity", RUNNER_PATH)
evaluator = _load("test_run_gmail_fact_parity_evaluator", EVALUATOR_PATH)


PACKET_ID = f"gfp_p_{'1' * 32}"
THREAD_ID = f"gfp_t_{'2' * 32}"
REVISION_ID = f"gfp_r_{'3' * 32}"
MESSAGE_ID = f"gfp_m_{'4' * 32}"


def _private_write(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n",
        encoding="utf-8",
    )
    path.chmod(0o600)


def _fake_adapter(
    path: Path,
    *,
    emit_stdout: bool = False,
    duplicate_deferred_candidate: bool = False,
) -> None:
    source = f"""#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
from datetime import datetime, timezone

parser = argparse.ArgumentParser()
parser.add_argument("--request", required=True)
parser.add_argument("--response", required=True)
args = parser.parse_args()
request = json.load(open(args.request, encoding="utf-8"))
packet = request["packet"]
message_id = packet["messages"][0]["message_id"]
now = datetime.now(timezone.utc).isoformat()
candidate = {{
    "statement": "Project Atlas is scheduled for launch.",
    "metadata": {{"gmail_message_ids": [message_id]}},
}}
response = {{
    "version": "gmail_fact_parity_adapter_response_v1",
    "packet_id": packet["packet_id"],
    "thread_id": packet["thread_id"],
    "production_api": "pkm_brain.extraction.extract_recent_documents",
    "prompt_version": request["runtime_config"]["prompt_version"],
    "members": [{{
        "candidate": candidate,
        "evidence_message_ids": [message_id],
        "actions": [],
        "persisted_facts": [],
    }}],
    "invocations": [{{
        "invocation_id": request["run_id"] + ":" + packet["packet_id"],
        "window_index": 0,
        "window_count": 1,
        "request_sha256": hashlib.sha256(b"request").hexdigest(),
        "response_sha256": hashlib.sha256(b"response").hexdigest(),
        "provider": "external-codex",
        "model": request["runtime_config"]["model"],
        "reasoning_effort": request["runtime_config"]["reasoning_effort"],
        "started_at": now,
        "completed_at": now,
    }}],
}}
{'''response["members"].append(dict(response["members"][0]))''' if duplicate_deferred_candidate else '''pass'''}
descriptor = os.open(args.response, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
    json.dump(response, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    handle.write("\\n")
{'''print("adapter output")''' if emit_stdout else '''pass'''}
"""
    path.write_text(source, encoding="utf-8")
    path.chmod(0o700)


def _manifest(tmp_path: Path, adapter: Path, **updates: Any) -> Path:
    runtime = {
        "production_api": runner.PRODUCTION_API,
        "shadow": False,
        "isolated_disposable_home": True,
        "packet_policy": "identical_sealed_packet",
        "model": evaluator.EXPECTED_V2_MODEL,
        "reasoning_effort": evaluator.EXPECTED_V2_REASONING_EFFORT,
        "prompt_version": evaluator.EXPECTED_V2_PROMPT_VERSION,
    }
    value = {
        "version": runner.ADAPTER_MANIFEST_VERSION,
        "adapter_kind": "test",
        "arm": "v2",
        "python_executable": sys.executable,
        "adapter_path": str(adapter),
        "production_root": str(ROOT),
        "production_api": runner.PRODUCTION_API,
        "commit": runner._git_head(ROOT),
        "prompt_version": evaluator.EXPECTED_V2_PROMPT_VERSION,
        "prompt_files": [
            "src/pkm_brain/extraction.py",
            "src/pkm_brain/extraction_contract.py",
        ],
        "model": evaluator.EXPECTED_V2_MODEL,
        "reasoning_effort": evaluator.EXPECTED_V2_REASONING_EFFORT,
        "runtime_config": runtime,
    }
    value.update(updates)
    path = tmp_path / "adapter-manifest.json"
    _private_write(path, value)
    return path


def _evidence() -> dict[str, Any]:
    packet = {
        "version": evaluator.PACKET_VERSION,
        "packet_id": PACKET_ID,
        "thread_id": THREAD_ID,
        "revision_id": REVISION_ID,
        "projection_version": 8,
        "classifier_version": 5,
        "messages": [
            {
                "message_id": MESSAGE_ID,
                "internal_date": "2026-07-01T12:00:00+00:00",
                "text": "## Message 1 — 2026-07-01T12:00:00+00:00 — "
                + MESSAGE_ID
                + "\n\nSubject: Atlas\n\nLaunch is scheduled.",
            }
        ],
    }
    return {
        "packets": {PACKET_ID: packet},
        "cohort_sha256": "a" * 64,
        "packet_sha256": "b" * 64,
    }


def test_runner_derives_contract_stages_and_emits_v2_evidence(tmp_path: Path) -> None:
    adapter = tmp_path / "adapter.py"
    _fake_adapter(adapter)
    manifest = _manifest(tmp_path, adapter)
    output_root = tmp_path / "private-output"
    output_root.mkdir(mode=0o700)
    output = output_root / "v2-a.jsonl"
    receipt = output_root / "v2-a.receipt.json"

    result = runner.execute_gmail_fact_parity_run(
        evidence=_evidence(),
        adapter_manifest_path=manifest,
        output_path=output,
        receipt_path=receipt,
        run_id="v2-a",
        allow_test_adapter=True,
    )

    assert result["packets"] == 1
    assert result["members"] == 1
    assert result["private_content_printed"] is False
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert stat.S_IMODE(receipt.stat().st_mode) == 0o600
    parsed = evaluator._load_run_output(
        output, expected_run_id="v2-a", evidence=_evidence()
    )
    member = next(iter(parsed["members"].values()))
    assert member["stages"] == {
        "candidate": True,
        "review": False,
        "persisted": False,
    }
    assert member["stage_record"]["disposition"] == "deferred"
    assert member["stage_record"]["candidate_sha256"]
    assert member["stage_record"]["action_id"] is None
    parsed_receipt = evaluator._load_receipt(
        receipt, expected_run_id="v2-a", run=parsed
    )
    assert len(parsed_receipt["invocations"]) == 1
    assert parsed_receipt["adapter_sha256"] == parsed["adapter_sha256"]
    assert (
        parsed_receipt["adapter_executable_sha256"]
        == parsed["adapter_executable_sha256"]
    )


def test_test_adapter_requires_explicit_test_only_authority(tmp_path: Path) -> None:
    adapter = tmp_path / "adapter.py"
    _fake_adapter(adapter)
    manifest = _manifest(tmp_path, adapter)

    with pytest.raises(runner.GmailFactParityRunnerError, match="production adapter"):
        runner.load_adapter_manifest(manifest)


def test_manifest_fails_closed_on_prompt_or_commit_mismatch(tmp_path: Path) -> None:
    adapter = tmp_path / "adapter.py"
    _fake_adapter(adapter)
    wrong_prompt = _manifest(
        tmp_path,
        adapter,
        prompt_version="extractor-evidence-units-v999",
    )
    with pytest.raises(runner.GmailFactParityRunnerError, match="prompt version"):
        runner.load_adapter_manifest(wrong_prompt, allow_test_adapter=True)

    wrong_prompt.unlink()
    wrong_commit = _manifest(tmp_path, adapter, commit="0" * 40)
    with pytest.raises(runner.GmailFactParityRunnerError, match="commit"):
        runner.load_adapter_manifest(wrong_commit, allow_test_adapter=True)


def test_manifest_preserves_declared_venv_python_symlink(tmp_path: Path) -> None:
    adapter = tmp_path / "adapter.py"
    _fake_adapter(adapter)
    venv_python = tmp_path / ".venv" / "bin" / "python"
    venv_python.parent.mkdir(parents=True)
    venv_python.symlink_to(Path(sys.executable).resolve())
    manifest = _manifest(
        tmp_path,
        adapter,
        python_executable=str(venv_python),
    )

    loaded = runner.load_adapter_manifest(manifest, allow_test_adapter=True)

    assert loaded["python_executable"] == venv_python.absolute()
    assert loaded["python_executable_target"] == Path(sys.executable).resolve()

    venv_python.unlink()
    venv_python.symlink_to("/bin/false")
    with pytest.raises(runner.GmailFactParityRunnerError, match="changed after"):
        runner._verify_manifest_executable(loaded)


def test_adapter_code_authority_is_stable_across_distinct_declared_launchers(
    tmp_path: Path,
) -> None:
    adapter = tmp_path / "adapter.py"
    _fake_adapter(adapter)
    launchers = []
    loaded_manifests = []
    for arm in ("original", "v2"):
        launcher = tmp_path / arm / ".venv" / "bin" / "python"
        launcher.parent.mkdir(parents=True)
        launcher.symlink_to(Path(sys.executable).resolve())
        launchers.append(launcher)
        manifest = _manifest(
            tmp_path,
            adapter,
            python_executable=str(launcher),
        )
        loaded_manifests.append(
            runner.load_adapter_manifest(manifest, allow_test_adapter=True)
        )

    expected_code_sha256 = runner.sha256_bytes(
        runner.canonical_json(
            {
                "runner": runner.sha256_bytes(RUNNER_PATH.read_bytes()),
                "adapter": runner.sha256_bytes(adapter.read_bytes()),
            }
        )
    )

    assert {item["adapter_sha256"] for item in loaded_manifests} == {
        expected_code_sha256
    }
    assert len({item["adapter_executable_sha256"] for item in loaded_manifests}) == len(
        launchers
    )


def test_manifest_revalidation_rejects_adapter_code_mutation(tmp_path: Path) -> None:
    adapter = tmp_path / "adapter.py"
    _fake_adapter(adapter)
    manifest = _manifest(tmp_path, adapter)
    loaded = runner.load_adapter_manifest(manifest, allow_test_adapter=True)

    adapter.write_text("raise SystemExit(0)\n", encoding="utf-8")

    with pytest.raises(runner.GmailFactParityRunnerError, match="adapter code changed"):
        runner._verify_manifest_executable(loaded)


def test_production_manifest_rejects_a_dirty_source_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    adapter = tmp_path / "adapter.py"
    _fake_adapter(adapter)
    manifest = _manifest(
        tmp_path,
        adapter,
        adapter_kind="production",
        adapter_path=str(runner.CANONICAL_PRODUCTION_ADAPTER),
    )
    monkeypatch.setattr(runner, "_git_tree_is_clean", lambda _root: False)

    with pytest.raises(
        runner.GmailFactParityRunnerError,
        match="must be clean and immutable",
    ):
        runner.load_adapter_manifest(manifest)


def test_adapter_console_output_is_rejected_without_replaying_it(
    tmp_path: Path,
) -> None:
    adapter = tmp_path / "adapter.py"
    _fake_adapter(adapter, emit_stdout=True)
    manifest = _manifest(tmp_path, adapter)
    output_root = tmp_path / "private-output"
    output_root.mkdir(mode=0o700)

    with pytest.raises(runner.GmailFactParityRunnerError) as exc_info:
        runner.execute_gmail_fact_parity_run(
            evidence=_evidence(),
            adapter_manifest_path=manifest,
            output_path=output_root / "v2-a.jsonl",
            receipt_path=output_root / "v2-a.receipt.json",
            run_id="v2-a",
            allow_test_adapter=True,
        )
    assert "adapter output" not in str(exc_info.value)
    assert not (output_root / "v2-a.jsonl").exists()


def test_duplicate_deferred_candidate_is_rejected_before_publication(
    tmp_path: Path,
) -> None:
    adapter = tmp_path / "adapter.py"
    _fake_adapter(adapter, duplicate_deferred_candidate=True)
    manifest = _manifest(tmp_path, adapter)
    output_root = tmp_path / "private-output"
    output_root.mkdir(mode=0o700)
    output = output_root / "v2-a.jsonl"
    receipt = output_root / "v2-a.receipt.json"

    with pytest.raises(
        runner.GmailFactParityRunnerError,
        match="reused one candidate across run records",
    ):
        runner.execute_gmail_fact_parity_run(
            evidence=_evidence(),
            adapter_manifest_path=manifest,
            output_path=output,
            receipt_path=receipt,
            run_id="v2-a",
            allow_test_adapter=True,
        )

    assert not output.exists()
    assert not receipt.exists()


def test_runner_refuses_adapter_supplied_stage_fields() -> None:
    raw = {
        "candidate": {
            "statement": "Atlas is scheduled.",
            "metadata": {"gmail_message_ids": [MESSAGE_ID]},
        },
        "evidence_message_ids": [MESSAGE_ID],
        "actions": [],
        "persisted_facts": [],
        "stages": {"candidate": True, "review": True, "persisted": True},
    }
    with pytest.raises(runner.GmailFactParityRunnerError, match="member schema"):
        runner._stage_member(raw, allowed_ids={MESSAGE_ID})


def test_global_stale_install_cannot_masquerade_as_frozen_source_tree() -> None:
    stale_root = Path("/Users/Peter/.local/share/uv/tools/pkm-brain")
    if not stale_root.exists():
        pytest.skip("host stale install is not present")
    with pytest.raises(runner.GmailFactParityRunnerError, match="package tree"):
        runner.production_tree_sha256(stale_root)
