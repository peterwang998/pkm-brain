#!/usr/bin/env python3
"""Run one sealed Gmail fact-parity arm through a production adapter.

The runner owns evidence authentication, provenance, stage derivation, and the
private output boundary.  The adapter may execute production extraction and
return production-shaped candidates/actions/facts, but it cannot assert stage
membership.  This command never prints packet text or fact statements.

An adapter is intentionally a separate process: original Brain and Brain V2
install the same Python package name and therefore cannot safely coexist in one
interpreter.  Production mode fails closed unless the selected source tree
contains the advertised production extraction API and prompt version.  Test
adapters are accepted only through the Python API's explicit test-only flag.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import runpy
import secrets
import stat
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
EVALUATOR = runpy.run_path(
    str(Path(__file__).with_name("evaluate_gmail_fact_parity.py"))
)
CONTRACT = runpy.run_path(
    str(Path(__file__).with_name("gmail_fact_parity_contract.py"))
)

VERSION = "gmail_fact_parity_runner_v2"
ADAPTER_MANIFEST_VERSION = "gmail_fact_parity_production_adapter_manifest_v2"
ADAPTER_REQUEST_VERSION = "gmail_fact_parity_adapter_request_v1"
ADAPTER_RESPONSE_VERSION = "gmail_fact_parity_adapter_response_v1"
PRODUCTION_API = "pkm_brain.extraction.extract_recent_documents"
CANONICAL_PRODUCTION_ADAPTER = (
    Path(__file__).with_name("gmail_fact_parity_production_adapter.py").resolve()
)
PRIVATE_FILE_MODE = 0o600
PRIVATE_DIRECTORY_MODE = 0o700
DEFAULT_TIMEOUT_SECONDS = 900
EXPECTED_PROMPT_FILES = {
    "original": ["src/pkm_brain/extraction.py"],
    "v2": [
        "src/pkm_brain/extraction.py",
        "src/pkm_brain/extraction_contract.py",
    ],
}

RUN_VERSION = str(EVALUATOR["RUN_VERSION"])
RUN_PACKET_VERSION = str(EVALUATOR["RUN_PACKET_VERSION"])
RECEIPT_VERSION = str(EVALUATOR["RECEIPT_VERSION"])
INVOCATION_ATTESTATION = str(EVALUATOR["INVOCATION_ATTESTATION"])
STAGE_CONTRACT_VERSION = str(CONTRACT["CONTRACT_VERSION"])
STAGE_CONTRACT_SHA256 = str(CONTRACT["CONTRACT_SHA256"])
DERIVE_STAGE_RECORD = CONTRACT["derive_stage_record"]

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_INVOCATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_MANIFEST_KEYS = {
    "version",
    "adapter_kind",
    "arm",
    "python_executable",
    "adapter_path",
    "production_root",
    "production_api",
    "commit",
    "prompt_version",
    "prompt_files",
    "model",
    "reasoning_effort",
    "runtime_config",
}
_RESPONSE_KEYS = {
    "version",
    "packet_id",
    "thread_id",
    "production_api",
    "prompt_version",
    "members",
    "invocations",
}
_MEMBER_KEYS = {"candidate", "evidence_message_ids", "actions", "persisted_facts"}
_INVOCATION_KEYS = {
    "invocation_id",
    "window_index",
    "window_count",
    "request_sha256",
    "response_sha256",
    "provider",
    "model",
    "reasoning_effort",
    "started_at",
    "completed_at",
}


class GmailFactParityRunnerError(ValueError):
    """Raised when a run cannot establish production-grade evidence."""


def canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise GmailFactParityRunnerError("value is not canonical JSON") from exc


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _adapter_code_sha256(*, runner_path: Path, adapter_path: Path) -> str:
    """Bind canonical adapter behavior independently of its arm launcher."""

    return sha256_bytes(
        canonical_json(
            {
                "runner": sha256_bytes(_read_regular_bytes(runner_path)),
                "adapter": sha256_bytes(_read_regular_bytes(adapter_path)),
            }
        )
    )


def _adapter_executable_sha256(
    *,
    declared: Path,
    target: Path,
    target_sha256: str,
) -> str:
    """Bind one arm to its exact declared launcher and resolved executable."""

    return sha256_bytes(
        canonical_json(
            {
                "python_executable": str(declared),
                "python_executable_target": str(target),
                "python_executable_target_sha256": target_sha256,
            }
        )
    )


def _regular_file(path: Path, *, private: bool = False) -> None:
    if path.is_symlink() or not path.is_file():
        raise GmailFactParityRunnerError("required input is not a regular file")
    if private and stat.S_IMODE(path.stat().st_mode) != PRIVATE_FILE_MODE:
        raise GmailFactParityRunnerError("private input must have mode 0600")


def _declared_executable(value: Any) -> tuple[Path, Path]:
    """Keep a venv launcher path while validating its final executable target."""

    raw = str(value or "").strip()
    if not raw:
        raise GmailFactParityRunnerError("adapter Python executable is invalid")
    expanded = Path(raw).expanduser()
    declared = Path(os.path.abspath(expanded))
    try:
        target = declared.resolve(strict=True)
    except OSError as exc:
        raise GmailFactParityRunnerError(
            "adapter Python executable is invalid"
        ) from exc
    _regular_file(target)
    if not os.access(declared, os.X_OK) or not os.access(target, os.X_OK):
        raise GmailFactParityRunnerError("adapter Python executable is invalid")
    return declared, target


def _read_regular_bytes(path: Path, *, private: bool = False) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise GmailFactParityRunnerError("required input is not a regular file")
        if private and stat.S_IMODE(metadata.st_mode) != PRIVATE_FILE_MODE:
            raise GmailFactParityRunnerError("private input must have mode 0600")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            return handle.read()
    except GmailFactParityRunnerError:
        raise
    except OSError as exc:
        raise GmailFactParityRunnerError(
            "required input could not be read safely"
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _private_json(path: Path) -> Any:
    try:
        return json.loads(_read_regular_bytes(path, private=True).decode("utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise GmailFactParityRunnerError("private JSON input is invalid") from exc


def _parse_time(value: Any, *, label: str) -> datetime:
    if not isinstance(value, str) or len(value) > 80:
        raise GmailFactParityRunnerError(f"{label} timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GmailFactParityRunnerError(f"{label} timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise GmailFactParityRunnerError(f"{label} timestamp lacks a timezone")
    return parsed


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _git_head(root: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GmailFactParityRunnerError(
            "production root is not a Git checkout"
        ) from exc
    value = result.stdout.strip()
    if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value) is None:
        raise GmailFactParityRunnerError("production commit is invalid")
    return value


def _git_tree_is_clean(root: Path) -> bool:
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(root),
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GmailFactParityRunnerError(
            "production root Git state cannot be established"
        ) from exc
    return not result.stdout


def _tree_files(root: Path) -> list[Path]:
    selected = [root / "pyproject.toml", root / "uv.lock"]
    package = root / "src" / "pkm_brain"
    if package.is_symlink() or not package.is_dir():
        raise GmailFactParityRunnerError("production package tree is unavailable")
    selected.extend(sorted(package.rglob("*.py")))
    files = []
    for path in selected:
        if path.exists():
            _regular_file(path)
            files.append(path)
    if not files:
        raise GmailFactParityRunnerError("production tree has no bindable files")
    return files


def production_tree_sha256(root: Path) -> str:
    rows = [
        {
            "path": path.relative_to(root).as_posix(),
            "sha256": sha256_bytes(_read_regular_bytes(path)),
            "size": path.stat().st_size,
        }
        for path in _tree_files(root)
    ]
    return sha256_bytes(canonical_json(rows))


def _combined_file_sha256(paths: Sequence[Path], *, root: Path) -> str:
    rows = []
    for path in paths:
        resolved = path.resolve()
        try:
            relative = resolved.relative_to(root)
        except ValueError as exc:
            raise GmailFactParityRunnerError(
                "bound file escapes production root"
            ) from exc
        _regular_file(resolved)
        rows.append(
            {
                "path": relative.as_posix(),
                "sha256": sha256_bytes(_read_regular_bytes(resolved)),
            }
        )
    if not rows:
        raise GmailFactParityRunnerError("prompt binding is empty")
    return sha256_bytes(canonical_json(rows))


def load_adapter_manifest(
    path: Path, *, allow_test_adapter: bool = False
) -> dict[str, Any]:
    value = _private_json(path)
    if not isinstance(value, Mapping) or set(value) != _MANIFEST_KEYS:
        raise GmailFactParityRunnerError("adapter manifest schema is invalid")
    if value.get("version") != ADAPTER_MANIFEST_VERSION:
        raise GmailFactParityRunnerError("adapter manifest version is invalid")
    kind = value.get("adapter_kind")
    if kind not in {"production", "test"} or (
        kind == "test" and not allow_test_adapter
    ):
        raise GmailFactParityRunnerError("a verified production adapter is required")
    arm = value.get("arm")
    if arm not in {"original", "v2"}:
        raise GmailFactParityRunnerError("adapter arm is invalid")
    if value.get("production_api") != PRODUCTION_API:
        raise GmailFactParityRunnerError(
            "adapter does not name the production extraction API"
        )

    root = Path(str(value.get("production_root") or "")).expanduser().resolve()
    if root.is_symlink() or not root.is_dir():
        raise GmailFactParityRunnerError("production root is unsafe")
    extraction_path = root / "src" / "pkm_brain" / "extraction.py"
    _regular_file(extraction_path)
    extraction_source = _read_regular_bytes(extraction_path).decode("utf-8")
    if "def extract_recent_documents(" not in extraction_source:
        raise GmailFactParityRunnerError("production extraction API is unavailable")

    commit = str(value.get("commit") or "")
    if commit != _git_head(root):
        raise GmailFactParityRunnerError(
            "adapter commit does not match its production tree"
        )
    if kind == "production" and not _git_tree_is_clean(root):
        raise GmailFactParityRunnerError(
            "production source checkout must be clean and immutable"
        )
    prompt_version = str(value.get("prompt_version") or "")
    expected_prompt = (
        str(EVALUATOR["EXPECTED_ORIGINAL_PROMPT_VERSION"])
        if arm == "original"
        else str(EVALUATOR["EXPECTED_V2_PROMPT_VERSION"])
    )
    prompt_files = value.get("prompt_files")
    if not isinstance(prompt_files, list) or any(
        not isinstance(item, str) or not item for item in prompt_files
    ):
        raise GmailFactParityRunnerError("adapter prompt files are invalid")
    if prompt_files != EXPECTED_PROMPT_FILES[arm]:
        raise GmailFactParityRunnerError("adapter prompt file authority is not pinned")
    prompt_paths = [root / item for item in prompt_files]
    try:
        prompt_source = "\n".join(
            _read_regular_bytes(path).decode("utf-8") for path in prompt_paths
        )
    except UnicodeError as exc:
        raise GmailFactParityRunnerError("prompt binding is not UTF-8") from exc
    if prompt_version != expected_prompt or prompt_version not in prompt_source:
        raise GmailFactParityRunnerError("production prompt version is not established")
    if arm == "original" and commit != str(EVALUATOR["EXPECTED_ORIGINAL_COMMIT"]):
        raise GmailFactParityRunnerError("original adapter is not the frozen baseline")

    model = str(value.get("model") or "")
    effort = str(value.get("reasoning_effort") or "")
    expected_model = (
        str(EVALUATOR["EXPECTED_ORIGINAL_MODEL"])
        if arm == "original"
        else str(EVALUATOR["EXPECTED_V2_MODEL"])
    )
    expected_effort = (
        str(EVALUATOR["EXPECTED_ORIGINAL_REASONING_EFFORT"])
        if arm == "original"
        else str(EVALUATOR["EXPECTED_V2_REASONING_EFFORT"])
    )
    if (model, effort) != (expected_model, expected_effort):
        raise GmailFactParityRunnerError("adapter model configuration is not pinned")

    python, python_target = _declared_executable(value.get("python_executable"))
    adapter = Path(str(value.get("adapter_path") or "")).expanduser().resolve()
    _regular_file(adapter)
    if kind == "production" and adapter != CANONICAL_PRODUCTION_ADAPTER:
        raise GmailFactParityRunnerError(
            "the canonical production fact-parity adapter is unavailable"
        )
    runtime_config = value.get("runtime_config")
    if not isinstance(runtime_config, Mapping):
        raise GmailFactParityRunnerError("adapter runtime config is invalid")
    required_runtime = {
        "production_api": PRODUCTION_API,
        "shadow": False,
        "isolated_disposable_home": True,
        "packet_policy": "identical_sealed_packet",
        "model": model,
        "reasoning_effort": effort,
        "prompt_version": prompt_version,
    }
    if dict(runtime_config) != required_runtime:
        raise GmailFactParityRunnerError(
            "adapter runtime config is not production-shaped"
        )

    runner_path = Path(__file__).resolve()
    python_target_sha256 = sha256_bytes(_read_regular_bytes(python_target))
    return {
        **dict(value),
        "production_root": root,
        "python_executable": python,
        "python_executable_target": python_target,
        "python_executable_target_sha256": python_target_sha256,
        "adapter_path": adapter,
        "production_tree_sha256": production_tree_sha256(root),
        "prompt_sha256": _combined_file_sha256(prompt_paths, root=root),
        "runtime_config_sha256": sha256_bytes(canonical_json(runtime_config)),
        "adapter_sha256": _adapter_code_sha256(
            runner_path=runner_path,
            adapter_path=adapter,
        ),
        "adapter_executable_sha256": _adapter_executable_sha256(
            declared=python,
            target=python_target,
            target_sha256=python_target_sha256,
        ),
    }


def _verify_manifest_executable(manifest: Mapping[str, Any]) -> None:
    declared = Path(manifest["python_executable"])
    expected_target = Path(manifest["python_executable_target"])
    try:
        current_target = declared.resolve(strict=True)
    except OSError as exc:
        raise GmailFactParityRunnerError(
            "adapter Python executable changed after manifest validation"
        ) from exc
    if (
        current_target != expected_target
        or not os.access(declared, os.X_OK)
        or sha256_bytes(_read_regular_bytes(current_target))
        != manifest["python_executable_target_sha256"]
    ):
        raise GmailFactParityRunnerError(
            "adapter Python executable changed after manifest validation"
        )
    if (
        _adapter_executable_sha256(
            declared=declared,
            target=current_target,
            target_sha256=str(manifest["python_executable_target_sha256"]),
        )
        != manifest["adapter_executable_sha256"]
    ):
        raise GmailFactParityRunnerError(
            "adapter Python executable binding changed after manifest validation"
        )
    if (
        _adapter_code_sha256(
            runner_path=Path(__file__).resolve(),
            adapter_path=Path(manifest["adapter_path"]),
        )
        != manifest["adapter_sha256"]
    ):
        raise GmailFactParityRunnerError(
            "adapter code changed after manifest validation"
        )


def _write_private_new(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags, PRIVATE_FILE_MODE)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        path.chmod(PRIVATE_FILE_MODE)
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise GmailFactParityRunnerError("private artifact write failed") from exc


def _ensure_output_parent(path: Path) -> None:
    if path.exists():
        if path.is_symlink() or not path.is_dir():
            raise GmailFactParityRunnerError("output directory is unsafe")
        if stat.S_IMODE(path.stat().st_mode) != PRIVATE_DIRECTORY_MODE:
            raise GmailFactParityRunnerError("output directory must have mode 0700")
        return
    path.mkdir(parents=True, mode=PRIVATE_DIRECTORY_MODE)
    path.chmod(PRIVATE_DIRECTORY_MODE)


def _adapter_response(
    manifest: Mapping[str, Any],
    *,
    run_id: str,
    packet: Mapping[str, Any],
    scratch: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    _verify_manifest_executable(manifest)
    token = secrets.token_hex(8)
    request_path = scratch / f"request-{token}.json"
    response_path = scratch / f"response-{token}.json"
    request = {
        "version": ADAPTER_REQUEST_VERSION,
        "run_id": run_id,
        "arm": manifest["arm"],
        "packet": dict(packet),
        "runtime_config": dict(manifest["runtime_config"]),
    }
    _write_private_new(request_path, canonical_json(request) + b"\n")
    try:
        completed = subprocess.run(
            [
                str(manifest["python_executable"]),
                str(manifest["adapter_path"]),
                "--request",
                str(request_path),
                "--response",
                str(response_path),
            ],
            cwd=manifest["production_root"],
            check=False,
            capture_output=True,
            timeout=timeout_seconds,
        )
        _verify_manifest_executable(manifest)
        if completed.returncode != 0 or completed.stdout or completed.stderr:
            raise GmailFactParityRunnerError(
                "production adapter failed or emitted console output "
                f"(diagnostic_sha256={sha256_bytes(completed.stdout + completed.stderr)})"
            )
        value = _private_json(response_path)
    except (OSError, subprocess.SubprocessError) as exc:
        raise GmailFactParityRunnerError(
            "production adapter invocation failed"
        ) from exc
    finally:
        for path in (request_path, response_path):
            path.unlink(missing_ok=True)
    if not isinstance(value, Mapping) or set(value) != _RESPONSE_KEYS:
        raise GmailFactParityRunnerError("adapter response schema is invalid")
    if (
        value.get("version") != ADAPTER_RESPONSE_VERSION
        or value.get("packet_id") != packet["packet_id"]
        or value.get("thread_id") != packet["thread_id"]
        or value.get("production_api") != PRODUCTION_API
        or value.get("prompt_version") != manifest["prompt_version"]
    ):
        raise GmailFactParityRunnerError("adapter response provenance is invalid")
    return dict(value)


def _stage_member(raw: Any, *, allowed_ids: set[str]) -> dict[str, Any]:
    if not isinstance(raw, Mapping) or set(raw) != _MEMBER_KEYS:
        raise GmailFactParityRunnerError("adapter member schema is invalid")
    candidate = raw.get("candidate")
    actions = raw.get("actions")
    persisted = raw.get("persisted_facts")
    evidence_ids = raw.get("evidence_message_ids")
    if (
        not isinstance(candidate, Mapping)
        or not isinstance(actions, list)
        or not isinstance(persisted, list)
        or not isinstance(evidence_ids, list)
        or not evidence_ids
        or any(
            not isinstance(item, str) or item not in allowed_ids
            for item in evidence_ids
        )
        or len(evidence_ids) != len(set(evidence_ids))
    ):
        raise GmailFactParityRunnerError("adapter member evidence is invalid")
    try:
        derived = DERIVE_STAGE_RECORD(candidate, actions, persisted)
    except Exception as exc:
        raise GmailFactParityRunnerError("canonical stage derivation failed") from exc
    statement = candidate.get("statement")
    if not isinstance(statement, str) or not statement.strip():
        raise GmailFactParityRunnerError("candidate statement is invalid")
    stage_record = {
        "version": derived["version"],
        "contract_sha256": derived["contract_sha256"],
        "candidate_sha256": derived["candidate_sha256"],
        "action_id": derived["action_id"],
        "stages": dict(derived["stages"]),
        "disposition": derived["disposition"],
        "action_status": derived["action_status"],
        "persisted_fact_ids": list(derived["persisted_fact_ids"]),
    }
    return {
        "statement": statement.strip(),
        "evidence_message_ids": list(evidence_ids),
        "stages": dict(stage_record["stages"]),
        "stage_record": stage_record,
    }


def _invocations(
    raw: Any,
    *,
    packet_id: str,
    manifest: Mapping[str, Any],
    ordinal_start: int,
) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        raise GmailFactParityRunnerError("adapter invocation ledger is empty")
    output = []
    seen: set[str] = set()
    for offset, item in enumerate(raw):
        if not isinstance(item, Mapping) or set(item) != _INVOCATION_KEYS:
            raise GmailFactParityRunnerError("adapter invocation schema is invalid")
        invocation_id = item.get("invocation_id")
        if (
            not isinstance(invocation_id, str)
            or _INVOCATION_ID_RE.fullmatch(invocation_id) is None
            or invocation_id in seen
        ):
            raise GmailFactParityRunnerError("adapter invocation identity is invalid")
        seen.add(invocation_id)
        index = item.get("window_index")
        count = item.get("window_count")
        if (
            isinstance(index, bool)
            or not isinstance(index, int)
            or isinstance(count, bool)
            or not isinstance(count, int)
            or count < 1
            or index < 0
            or index >= count
            or item.get("provider") != str(EVALUATOR["EXPECTED_PROVIDER"])
            or item.get("model") != manifest["model"]
            or item.get("reasoning_effort") != manifest["reasoning_effort"]
            or _SHA256_RE.fullmatch(str(item.get("request_sha256") or "")) is None
            or _SHA256_RE.fullmatch(str(item.get("response_sha256") or "")) is None
        ):
            raise GmailFactParityRunnerError("adapter invocation target is invalid")
        started = _parse_time(item.get("started_at"), label="invocation started_at")
        completed = _parse_time(
            item.get("completed_at"), label="invocation completed_at"
        )
        if completed < started:
            raise GmailFactParityRunnerError("adapter invocation chronology is invalid")
        output.append(
            {
                "invocation_id": invocation_id,
                "ordinal": ordinal_start + offset,
                "packet_id": packet_id,
                "window_index": index,
                "window_count": count,
                "request_sha256": item["request_sha256"],
                "response_sha256": item["response_sha256"],
                "provider": item["provider"],
                "model": item["model"],
                "reasoning_effort": item["reasoning_effort"],
                "started_at": item["started_at"],
                "completed_at": item["completed_at"],
            }
        )
    if {item["window_count"] for item in output} != {len(output)} or {
        item["window_index"] for item in output
    } != set(range(len(output))):
        raise GmailFactParityRunnerError("adapter invocation windows are incomplete")
    return output


def _validate_stage_ownership(packet_rows: Sequence[Mapping[str, Any]]) -> None:
    candidate_owners: set[str] = set()
    action_owners: dict[str, str] = {}
    fact_owners: dict[str, str] = {}
    for packet in packet_rows:
        for member in packet["members"]:
            record = member["stage_record"]
            candidate_digest = str(record["candidate_sha256"])
            if candidate_digest in candidate_owners:
                raise GmailFactParityRunnerError(
                    "adapter reused one candidate across run records"
                )
            candidate_owners.add(candidate_digest)
            action_id = record["action_id"]
            if action_id is not None:
                if action_id in action_owners:
                    raise GmailFactParityRunnerError(
                        "adapter reused one action across candidate records"
                    )
                action_owners[action_id] = candidate_digest
            for fact_id in record["persisted_fact_ids"]:
                if fact_id in fact_owners:
                    raise GmailFactParityRunnerError(
                        "adapter reused one persisted fact across candidate records"
                    )
                fact_owners[fact_id] = candidate_digest


def execute_gmail_fact_parity_run(
    *,
    evidence: Mapping[str, Any],
    adapter_manifest_path: Path,
    output_path: Path,
    receipt_path: Path,
    run_id: str,
    allow_test_adapter: bool = False,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Execute one arm and publish an evaluator-compatible run and receipt."""

    if _RUN_ID_RE.fullmatch(run_id) is None:
        raise GmailFactParityRunnerError("run ID is invalid")
    if output_path == receipt_path or output_path.exists() or receipt_path.exists():
        raise GmailFactParityRunnerError("fresh distinct output paths are required")
    if isinstance(timeout_seconds, bool) or timeout_seconds < 1:
        raise GmailFactParityRunnerError("adapter timeout is invalid")
    manifest = load_adapter_manifest(
        adapter_manifest_path, allow_test_adapter=allow_test_adapter
    )
    parent = output_path.parent.resolve()
    if receipt_path.parent.resolve() != parent:
        raise GmailFactParityRunnerError(
            "run output and receipt must share one directory"
        )
    _ensure_output_parent(parent)
    scratch = Path(tempfile.mkdtemp(prefix=".gmail-fact-parity-", dir=parent))
    scratch.chmod(PRIVATE_DIRECTORY_MODE)
    started_at = _now()
    packet_rows: list[dict[str, Any]] = []
    ledger: list[dict[str, Any]] = []
    try:
        for packet_id, packet in sorted(evidence["packets"].items()):
            response = _adapter_response(
                manifest,
                run_id=run_id,
                packet=packet,
                scratch=scratch,
                timeout_seconds=timeout_seconds,
            )
            raw_members = response.get("members")
            if not isinstance(raw_members, list):
                raise GmailFactParityRunnerError("adapter members are invalid")
            allowed_ids = {item["message_id"] for item in packet["messages"]}
            members = [
                _stage_member(item, allowed_ids=allowed_ids) for item in raw_members
            ]
            invocations = _invocations(
                response.get("invocations"),
                packet_id=packet_id,
                manifest=manifest,
                ordinal_start=len(ledger),
            )
            ledger.extend(invocations)
            packet_rows.append(
                {
                    "version": RUN_PACKET_VERSION,
                    "run_id": run_id,
                    "packet_id": packet_id,
                    "thread_id": packet["thread_id"],
                    "members": members,
                }
            )
    finally:
        for path in scratch.iterdir():
            path.unlink(missing_ok=True)
        scratch.rmdir()

    invocation_ids = [item["invocation_id"] for item in ledger]
    if len(invocation_ids) != len(set(invocation_ids)):
        raise GmailFactParityRunnerError(
            "adapter invocation IDs are not globally unique"
        )
    _validate_stage_ownership(packet_rows)
    ledger_sha = sha256_bytes(canonical_json(ledger))
    bindings = {
        "stage_contract_version": STAGE_CONTRACT_VERSION,
        "stage_contract_sha256": STAGE_CONTRACT_SHA256,
        "adapter_sha256": manifest["adapter_sha256"],
        "adapter_executable_sha256": manifest["adapter_executable_sha256"],
        "production_tree_sha256": manifest["production_tree_sha256"],
        "runtime_config_sha256": manifest["runtime_config_sha256"],
        "prompt_sha256": manifest["prompt_sha256"],
        "invocation_ledger_sha256": ledger_sha,
    }
    header = {
        "version": RUN_VERSION,
        "run_id": run_id,
        "arm": manifest["arm"],
        "commit": manifest["commit"],
        "prompt_version": manifest["prompt_version"],
        "model": manifest["model"],
        "reasoning_effort": manifest["reasoning_effort"],
        "cohort_sha256": evidence["cohort_sha256"],
        "packet_sha256": evidence["packet_sha256"],
        **bindings,
    }
    output_bytes = b"".join(
        canonical_json(row) + b"\n" for row in [header, *packet_rows]
    )
    completed_at = _now()
    receipt_started = _parse_time(started_at, label="receipt started_at")
    receipt_completed = _parse_time(completed_at, label="receipt completed_at")
    previous_started: datetime | None = None
    for invocation in ledger:
        invocation_started = _parse_time(
            invocation["started_at"], label="invocation started_at"
        )
        invocation_completed = _parse_time(
            invocation["completed_at"], label="invocation completed_at"
        )
        if (
            invocation_started < receipt_started
            or invocation_completed > receipt_completed
            or (previous_started is not None and invocation_started < previous_started)
        ):
            raise GmailFactParityRunnerError(
                "adapter invocation chronology falls outside the run receipt"
            )
        previous_started = invocation_started
    receipt = {
        "version": RECEIPT_VERSION,
        "run_id": run_id,
        "provider": str(EVALUATOR["EXPECTED_PROVIDER"]),
        "model": manifest["model"],
        "reasoning_effort": manifest["reasoning_effort"],
        "started_at": started_at,
        "completed_at": completed_at,
        "output_sha256": sha256_bytes(output_bytes),
        "attestation": INVOCATION_ATTESTATION,
        "invocations": ledger,
        **bindings,
    }
    _write_private_new(output_path, output_bytes)
    try:
        _write_private_new(receipt_path, canonical_json(receipt) + b"\n")
    except Exception:
        output_path.unlink(missing_ok=True)
        raise
    return {
        "version": VERSION,
        "run_id": run_id,
        "arm": manifest["arm"],
        "packets": len(packet_rows),
        "members": sum(len(row["members"]) for row in packet_rows),
        "invocations": len(ledger),
        "output_sha256": receipt["output_sha256"],
        "private_content_printed": False,
    }


def run_gmail_fact_parity(
    *,
    packets_path: Path,
    cohort_path: Path,
    admissions_path: Path,
    cohort_manifest_path: Path,
    source_bindings_path: Path,
    hmac_key_path: Path,
    original_inventory_path: Path,
    v2_inventory_path: Path,
    adapter_manifest_path: Path,
    output_path: Path,
    receipt_path: Path,
    run_id: str,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    evidence_loader: Callable[..., dict[str, Any]] = EVALUATOR[
        "load_gmail_fact_parity_bound_evidence"
    ]
    evidence = evidence_loader(
        packets_path,
        cohort_path,
        admissions_path,
        cohort_manifest_path,
        source_bindings_path,
        hmac_key_path,
        original_inventory_path,
        v2_inventory_path,
    )
    return execute_gmail_fact_parity_run(
        evidence=evidence,
        adapter_manifest_path=adapter_manifest_path,
        output_path=output_path,
        receipt_path=receipt_path,
        run_id=run_id,
        timeout_seconds=timeout_seconds,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("packets", type=Path)
    parser.add_argument("cohort", type=Path)
    parser.add_argument("admissions", type=Path)
    parser.add_argument("cohort_manifest", type=Path)
    parser.add_argument("source_bindings", type=Path)
    parser.add_argument("original_inventory", type=Path)
    parser.add_argument("v2_inventory", type=Path)
    parser.add_argument("adapter_manifest", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("receipt", type=Path)
    parser.add_argument("--hmac-key", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--timeout-seconds", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    args = parser.parse_args()
    result = run_gmail_fact_parity(
        packets_path=args.packets,
        cohort_path=args.cohort,
        admissions_path=args.admissions,
        cohort_manifest_path=args.cohort_manifest,
        source_bindings_path=args.source_bindings,
        hmac_key_path=args.hmac_key,
        original_inventory_path=args.original_inventory,
        v2_inventory_path=args.v2_inventory,
        adapter_manifest_path=args.adapter_manifest,
        output_path=args.output,
        receipt_path=args.receipt,
        run_id=args.run_id,
        timeout_seconds=args.timeout_seconds,
    )
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
