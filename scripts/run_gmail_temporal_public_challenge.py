#!/usr/bin/env python3
"""Run a sealed public-synthetic Gmail temporal smoke through production V2.

This utility is deliberately separate from the private Gmail holdout authority.
``run`` accepts no gold path, uses the production restricted external-Codex
boundary, and seals every prediction before it mutates the isolated Brain home.
``score`` is a later, separate operation that may open the committed public gold.

The command is diagnostic-only.  It cannot establish private-distribution or
release evidence and every artifact it writes is owner-only.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import importlib.util
import json
import os
import re
import secrets
import stat
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

from pkm_brain.db import connection
from pkm_brain.gmail_temporal_runner import (
    GMAIL_TEMPORAL_COMPONENT_VERSION,
    GMAIL_TEMPORAL_EXTERNAL_PROVIDER,
    GMAIL_TEMPORAL_PIPELINE_SCOPE,
    GMAIL_TEMPORAL_RUNNER_VERSION,
    GMAIL_TEMPORAL_VERIFIER_MODEL,
    GMAIL_TEMPORAL_VERIFIER_REASONING_EFFORT,
    GmailTemporalReviewPreparation,
    GmailTemporalRunnerError,
    gmail_temporal_admission_policy_fingerprint,
    gmail_temporal_runner_policy_fingerprint,
    gmail_temporal_verifier_policy_fingerprint,
    prepare_gmail_temporal_review,
    run_gmail_temporal_review,
)
from pkm_brain.paths import BrainPaths


VERSION = "gmail_temporal_public_challenge_launcher_v2"
CHALLENGE_VERSION = "gmail_temporal_public_challenge_v2"
PLAN_VERSION = "gmail_temporal_public_challenge_plan_v2"
CALL_START_VERSION = "gmail_temporal_public_challenge_call_start_v2"
CALL_RECEIPT_VERSION = "gmail_temporal_public_challenge_call_receipt_v2"
PREDICTION_SEAL_VERSION = "gmail_temporal_public_challenge_prediction_seal_v2"
RESULT_VERSION = "gmail_temporal_public_challenge_result_v2"
SCORE_VERSION = "gmail_temporal_public_challenge_score_v2"
GOLD_VERSION = "public_blind_gmail_temporal_gold_v2"
PUBLIC_ROOT_AUTHORITY_VERSION = "gmail_temporal_public_root_authority_v2"
PUBLIC_ROOT_AUTHORITY_FILENAME = "public_temporal_challenge_authority.json"
PUBLIC_TEST_PROVIDER = "injected-test-double"

PUBLIC_SCOPE = "public_synthetic_non_release"
RUN_COUNT = 3
PRIVATE_FILE_MODE = 0o600
PRIVATE_DIRECTORY_MODE = 0o700
DEFAULT_TIMEOUT_SECONDS = 900
MAX_TIMEOUT_SECONDS = 1800

PLAN_DOMAIN = b"gmail_temporal_public_challenge_plan_v2\0"
CALL_START_DOMAIN = b"gmail_temporal_public_challenge_call_start_v2\0"
CALL_RECEIPT_DOMAIN = b"gmail_temporal_public_challenge_call_receipt_v2\0"
PREDICTION_SEAL_DOMAIN = b"gmail_temporal_public_challenge_prediction_seal_v2\0"
RESULT_DOMAIN = b"gmail_temporal_public_challenge_result_v2\0"
SCORE_DOMAIN = b"gmail_temporal_public_challenge_score_v2\0"
PUBLIC_ROOT_AUTHORITY_DOMAIN = b"gmail_temporal_public_root_authority_v2\0"

_ROOT = Path(__file__).resolve().parents[1]
_HOLDOUT_RUNNER_PATH = _ROOT / "scripts" / "run_gmail_temporal_holdout_external.py"
_RUNNER_PATH = _ROOT / "src" / "pkm_brain" / "gmail_temporal_runner.py"

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,95}$")
_CHALLENGE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_CALL_ID_RE = re.compile(r"^gtpvc_(?:test_)?i_[0-9a-f]{64}$")
_LOGICAL_RUN_ID_RE = re.compile(r"^gtpvc_(?:test_)?r_[0-9a-f]{64}$")

_CHALLENGE_KEYS = {
    "version",
    "challenge_id",
    "scope",
    "created_at",
    "brain_home",
    "gold_sha256",
    "public_synthetic",
    "contains_private_gmail",
    "release_eligible",
    "cases",
}
_CASE_KEYS = {"case_id", "document_id", "gmail_message_id", "source_sha256"}
_PUBLIC_ROOT_AUTHORITY_KEYS = {
    "version",
    "challenge_id",
    "scope",
    "created_at",
    "brain_home",
    "challenge_manifest_sha256",
    "gold_sha256",
    "public_synthetic",
    "contains_private_gmail",
    "release_eligible",
    "cases",
    "authority_hmac_sha256",
}
_NORMALIZED_TEMPORAL_VALUE_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?)?$"
)


class PublicChallengeError(ValueError):
    """Raised without reflecting source or model content."""


ModelInvoker = Callable[
    [Mapping[str, Any], Mapping[str, Any], str, str, int], Mapping[str, Any]
]


@dataclass(frozen=True)
class _Case:
    case_id: str
    document_id: str
    gmail_message_id: str
    preparation: GmailTemporalReviewPreparation


@dataclass(frozen=True)
class _RequestRow:
    case_id: str
    request_fingerprint: str
    payload: Mapping[str, Any]
    batch_fingerprint: str
    frontier_fingerprint: str
    page_plan_fingerprint: str
    page_fingerprint: str
    candidate_ids: tuple[str, ...]


@dataclass(frozen=True)
class _CallUnit:
    unit_ordinal: int
    case_ids: tuple[str, ...]
    rows: tuple[_RequestRow, ...]
    request: Mapping[str, Any]
    request_sha256: str


@dataclass(frozen=True)
class _CompletedCall:
    run_ordinal: int
    unit_ordinal: int
    call_id: str
    logical_run_id: str
    started_at: str
    completed_at: str
    request_sha256: str
    response_sha256: str
    start_sha256: str
    receipt_sha256: str
    response: Mapping[str, Any]
    case_pages: Mapping[str, tuple[Mapping[str, Any], ...]]


@dataclass(frozen=True)
class _ExecutionClaims:
    provider: str
    external_call_started: bool
    restricted_execution: bool
    ephemeral_execution: bool
    local_model_used: bool
    test_invoker_used: bool


def _load_script(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise PublicChallengeError("shared external runner is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        sys.modules.pop(name, None)
        raise PublicChallengeError(
            "shared external runner could not be loaded"
        ) from exc
    return module


external = _load_script("_gmail_temporal_public_shared_external", _HOLDOUT_RUNNER_PATH)


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise PublicChallengeError("artifact is not canonical JSON") from exc


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _aware_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value or len(value) > 80:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _private_directory(path: Path, *, create: bool = False) -> None:
    if create and not path.exists() and not path.is_symlink():
        missing: list[Path] = []
        cursor = path
        while not cursor.exists() and not cursor.is_symlink():
            missing.append(cursor)
            cursor = cursor.parent
        if cursor.is_symlink() or not cursor.is_dir():
            raise PublicChallengeError("owner-only directory parent is unsafe")
        for item in reversed(missing):
            os.mkdir(item, PRIVATE_DIRECTORY_MODE)
            item.chmod(PRIVATE_DIRECTORY_MODE)
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise PublicChallengeError("owner-only directory is unavailable") from exc
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.geteuid()
        or stat.S_IMODE(info.st_mode) != PRIVATE_DIRECTORY_MODE
    ):
        raise PublicChallengeError("directory must be owner-only and non-symlinked")


def _fresh_private_directory(path: Path) -> None:
    if path.exists() or path.is_symlink():
        raise PublicChallengeError("fresh output directory is required")
    parent = path.parent
    if not parent.exists():
        parent.mkdir(parents=True, mode=PRIVATE_DIRECTORY_MODE)
    if parent.is_symlink() or not parent.is_dir():
        raise PublicChallengeError("output parent is unsafe")
    os.mkdir(path, PRIVATE_DIRECTORY_MODE)
    _private_directory(path)


def _private_file(path: Path) -> bytes:
    descriptor: int | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid()
            or stat.S_IMODE(info.st_mode) != PRIVATE_FILE_MODE
            or info.st_nlink != 1
        ):
            raise PublicChallengeError("input must be an owner-only regular file")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            return handle.read()
    except PublicChallengeError:
        raise
    except OSError as exc:
        raise PublicChallengeError("owner-only input is unavailable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _write_private_new(path: Path, payload: bytes) -> None:
    _private_directory(path.parent, create=True)
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
        raise PublicChallengeError("owner-only artifact write failed") from exc


def _strict_json(raw: bytes, *, label: str) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        output: dict[str, Any] = {}
        for key, value in pairs:
            if key in output:
                raise PublicChallengeError(f"{label} contains duplicate keys")
            output[key] = value
        return output

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=reject_duplicates)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise PublicChallengeError(f"{label} is invalid JSON") from exc
    if not isinstance(value, dict) or raw != _canonical_json(value) + b"\n":
        raise PublicChallengeError(f"{label} is not canonical")
    return value


def _key(path: Path) -> bytes:
    raw = _private_file(path)
    if len(raw) < 32:
        raise PublicChallengeError("HMAC key is too short")
    return raw


def _execution_claims(*, test_invoker_used: bool) -> _ExecutionClaims:
    if test_invoker_used:
        # An injected callable has no external-execution attestation. Mark it
        # conservatively as local so test output cannot masquerade as the
        # restricted Codex evidence required by the smoke gate.
        return _ExecutionClaims(
            provider=PUBLIC_TEST_PROVIDER,
            external_call_started=False,
            restricted_execution=False,
            ephemeral_execution=False,
            local_model_used=True,
            test_invoker_used=True,
        )
    return _ExecutionClaims(
        provider=GMAIL_TEMPORAL_EXTERNAL_PROVIDER,
        external_call_started=True,
        restricted_execution=True,
        ephemeral_execution=True,
        local_model_used=False,
        test_invoker_used=False,
    )


def _signed(
    value: Mapping[str, Any],
    *,
    key: bytes,
    domain: bytes,
    signature_field: str,
) -> dict[str, Any]:
    if signature_field in value:
        raise PublicChallengeError("signature field is duplicated")
    signature = hmac.new(
        key, domain + _canonical_json(value), hashlib.sha256
    ).hexdigest()
    return {**dict(value), signature_field: signature}


def _verify_signed(
    value: Mapping[str, Any],
    *,
    key: bytes,
    domain: bytes,
    signature_field: str,
) -> bool:
    supplied = value.get(signature_field)
    if not isinstance(supplied, str) or _SHA256_RE.fullmatch(supplied) is None:
        return False
    unsigned = dict(value)
    unsigned.pop(signature_field, None)
    expected = hmac.new(
        key, domain + _canonical_json(unsigned), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(supplied, expected)


def _load_challenge(path: Path, *, key: bytes) -> tuple[dict[str, Any], bytes]:
    raw = _private_file(path)
    value = _strict_json(raw, label="challenge manifest")
    if set(value) != _CHALLENGE_KEYS:
        raise PublicChallengeError("challenge manifest schema is invalid")
    if (
        value.get("version") != CHALLENGE_VERSION
        or value.get("scope") != PUBLIC_SCOPE
        or value.get("public_synthetic") is not True
        or value.get("contains_private_gmail") is not False
        or value.get("release_eligible") is not False
        or _CHALLENGE_ID_RE.fullmatch(str(value.get("challenge_id") or "")) is None
        or _aware_timestamp(value.get("created_at")) is None
        or _SHA256_RE.fullmatch(str(value.get("gold_sha256") or "")) is None
    ):
        raise PublicChallengeError("public challenge authority is invalid")
    home = value.get("brain_home")
    if not isinstance(home, str) or not home or "\x00" in home:
        raise PublicChallengeError("challenge Brain home is invalid")
    cases = value.get("cases")
    if not isinstance(cases, list) or not cases:
        raise PublicChallengeError("challenge cases are empty")
    seen_cases: set[str] = set()
    seen_targets: set[tuple[str, str]] = set()
    for item in cases:
        if not isinstance(item, Mapping) or set(item) != _CASE_KEYS:
            raise PublicChallengeError("challenge case schema is invalid")
        case_id = item.get("case_id")
        document_id = item.get("document_id")
        message_id = item.get("gmail_message_id")
        source_sha256 = item.get("source_sha256")
        target = (str(document_id or ""), str(message_id or ""))
        if (
            not isinstance(case_id, str)
            or _CASE_ID_RE.fullmatch(case_id) is None
            or case_id in seen_cases
            or not all(target)
            or target in seen_targets
            or not isinstance(source_sha256, str)
            or _SHA256_RE.fullmatch(source_sha256) is None
        ):
            raise PublicChallengeError("challenge case identity is invalid")
        seen_cases.add(case_id)
        seen_targets.add(target)
    _validate_public_root_authority(value, raw, key=key)
    return value, raw


def _known_private_brain_homes() -> set[Path]:
    values = {
        (Path.home() / "brain").resolve(),
        (Path.home() / "brain-v2").resolve(),
    }
    try:
        values.add(BrainPaths.from_value(None).home.expanduser().resolve())
    except Exception:  # noqa: BLE001 - conservative fixed homes remain enforced.
        pass
    return values


def _validate_public_root_authority(
    challenge: Mapping[str, Any], challenge_raw: bytes, *, key: bytes
) -> None:
    raw_home = Path(str(challenge["brain_home"])).expanduser()
    if raw_home.is_symlink() or not raw_home.is_dir():
        raise PublicChallengeError("dedicated public Brain home is unavailable")
    home = raw_home.resolve()
    if home in _known_private_brain_homes():
        raise PublicChallengeError("default or production Brain home is forbidden")
    paths = BrainPaths.from_value(home)
    marker_path = paths.config_local / PUBLIC_ROOT_AUTHORITY_FILENAME
    try:
        marker_raw = _private_file(marker_path)
    except PublicChallengeError as exc:
        raise PublicChallengeError("public root authority is unavailable") from exc
    marker = _strict_json(marker_raw, label="public root authority")
    expected_cases = [
        {
            "case_id": str(item["case_id"]),
            "document_id": str(item["document_id"]),
            "gmail_message_id": str(item["gmail_message_id"]),
            "source_sha256": str(item["source_sha256"]),
        }
        for item in challenge["cases"]
    ]
    if (
        set(marker) != _PUBLIC_ROOT_AUTHORITY_KEYS
        or not _verify_signed(
            marker,
            key=key,
            domain=PUBLIC_ROOT_AUTHORITY_DOMAIN,
            signature_field="authority_hmac_sha256",
        )
        or marker.get("version") != PUBLIC_ROOT_AUTHORITY_VERSION
        or marker.get("challenge_id") != challenge["challenge_id"]
        or marker.get("scope") != PUBLIC_SCOPE
        or _aware_timestamp(marker.get("created_at")) is None
        or marker.get("brain_home") != str(home)
        or marker.get("challenge_manifest_sha256") != _sha256(challenge_raw)
        or marker.get("gold_sha256") != challenge["gold_sha256"]
        or marker.get("public_synthetic") is not True
        or marker.get("contains_private_gmail") is not False
        or marker.get("release_eligible") is not False
        or marker.get("cases") != expected_cases
    ):
        raise PublicChallengeError("public root authority is invalid")


def _prepare_cases(challenge: Mapping[str, Any]) -> tuple[_Case, ...]:
    paths = BrainPaths.from_value(str(challenge["brain_home"]))
    output: list[_Case] = []
    for item in challenge["cases"]:
        try:
            preparation = prepare_gmail_temporal_review(
                paths,
                document_id=str(item["document_id"]),
                gmail_message_id=str(item["gmail_message_id"]),
            )
        except GmailTemporalRunnerError as exc:
            raise PublicChallengeError(
                "production challenge preparation failed"
            ) from exc
        if preparation.source_sha256 != item["source_sha256"]:
            raise PublicChallengeError("public challenge source authority is stale")
        output.append(
            _Case(
                case_id=str(item["case_id"]),
                document_id=str(item["document_id"]),
                gmail_message_id=str(item["gmail_message_id"]),
                preparation=preparation,
            )
        )
    return tuple(output)


def _request_rows(cases: Sequence[_Case]) -> tuple[_RequestRow, ...]:
    rows: list[_RequestRow] = []
    for case in cases:
        for request in case.preparation.requests:
            try:
                payload = json.loads(request.payload)
                clusters = payload["page"]["clusters"]
                candidate_ids = tuple(
                    candidate_id
                    for cluster in clusters
                    for candidate_id in cluster["candidate_ids"]
                )
            except (KeyError, TypeError, json.JSONDecodeError) as exc:
                raise PublicChallengeError(
                    "production verifier request is malformed"
                ) from exc
            if not candidate_ids or len(candidate_ids) != len(set(candidate_ids)):
                raise PublicChallengeError(
                    "production verifier candidate set is invalid"
                )
            rows.append(
                _RequestRow(
                    case_id=case.case_id,
                    request_fingerprint=request.request_fingerprint,
                    payload=payload,
                    batch_fingerprint=request.batch_fingerprint,
                    frontier_fingerprint=request.frontier_fingerprint,
                    page_plan_fingerprint=request.page_plan_fingerprint,
                    page_fingerprint=request.page_fingerprint,
                    candidate_ids=candidate_ids,
                )
            )
    return tuple(rows)


def bounded_public_call_units(rows: Sequence[_RequestRow]) -> tuple[_CallUnit, ...]:
    """Apply the private holdout's exact item and serialized-byte ceilings."""

    shared_rows = [
        {
            "case_id": row.case_id,
            "request_fingerprint": row.request_fingerprint,
            "payload": dict(row.payload),
        }
        for row in rows
    ]
    try:
        batches = external._bounded_batches(  # noqa: SLF001
            shared_rows,
            max_items=external.MAX_VERIFIER_BATCH_SIZE,
            max_request_bytes=external.MAX_VERIFIER_REQUEST_BYTES,
            request_factory=external._verifier_request,  # noqa: SLF001
        )
    except Exception as exc:
        raise PublicChallengeError(
            "public verifier request exceeds safe bounds"
        ) from exc
    by_fingerprint = {row.request_fingerprint: row for row in rows}
    if len(by_fingerprint) != len(rows):
        raise PublicChallengeError("verifier request identity is duplicated")
    output: list[_CallUnit] = []
    case_unit: dict[str, int] = {}
    for unit_ordinal, batch in enumerate(batches, start=1):
        resolved: list[_RequestRow] = []
        for item in batch:
            fingerprint = str(item.get("request_fingerprint") or "")
            row = by_fingerprint.get(fingerprint)
            if row is None:
                raise PublicChallengeError("bounded verifier request is unknown")
            resolved.append(row)
        case_ids = tuple(dict.fromkeys(row.case_id for row in resolved))
        for case_id in case_ids:
            previous = case_unit.setdefault(case_id, unit_ordinal)
            if previous != unit_ordinal:
                raise PublicChallengeError(
                    "one public case cannot span multiple external invocations"
                )
        request = external._verifier_request(batch)  # noqa: SLF001
        request_raw = _canonical_json(request) + b"\n"
        if (
            len(batch) > external.MAX_VERIFIER_BATCH_SIZE
            or len(request_raw) > external.MAX_VERIFIER_REQUEST_BYTES
        ):
            raise PublicChallengeError("bounded verifier unit violates shared ceiling")
        output.append(
            _CallUnit(
                unit_ordinal=unit_ordinal,
                case_ids=case_ids,
                rows=tuple(resolved),
                request=request,
                request_sha256=_sha256(request_raw),
            )
        )
    covered = [row.request_fingerprint for unit in output for row in unit.rows]
    if covered != [row.request_fingerprint for row in rows]:
        raise PublicChallengeError("bounded verifier coverage is incomplete")
    return tuple(output)


def _source_hashes() -> dict[str, str]:
    return {
        "launcher": _sha256(Path(__file__).read_bytes()),
        "production_runner": _sha256(_RUNNER_PATH.read_bytes()),
        "shared_external_runner": _sha256(_HOLDOUT_RUNNER_PATH.read_bytes()),
    }


def _plan_value(
    *,
    challenge: Mapping[str, Any],
    challenge_raw: bytes,
    cases: Sequence[_Case],
    units: Sequence[_CallUnit],
    claims: _ExecutionClaims,
) -> dict[str, Any]:
    run_prefix = "gtpvc_test_r_" if claims.test_invoker_used else "gtpvc_r_"
    call_prefix = "gtpvc_test_i_" if claims.test_invoker_used else "gtpvc_i_"
    logical_runs = [run_prefix + secrets.token_hex(32) for _ in range(RUN_COUNT)]
    calls = []
    for run_ordinal, logical_run_id in enumerate(logical_runs, start=1):
        for unit in units:
            calls.append(
                {
                    "run_ordinal": run_ordinal,
                    "logical_run_id": logical_run_id,
                    "unit_ordinal": unit.unit_ordinal,
                    "call_id": call_prefix + secrets.token_hex(32),
                    "case_ids": list(unit.case_ids),
                    "request_fingerprints": [
                        row.request_fingerprint for row in unit.rows
                    ],
                    "request_sha256": unit.request_sha256,
                    "request_bytes": len(_canonical_json(unit.request) + b"\n"),
                }
            )
    return {
        "version": PLAN_VERSION,
        "launcher_version": VERSION,
        "challenge_id": challenge["challenge_id"],
        "challenge_manifest_sha256": _sha256(challenge_raw),
        "scope": PUBLIC_SCOPE,
        "provider": claims.provider,
        "model": GMAIL_TEMPORAL_VERIFIER_MODEL,
        "reasoning_effort": GMAIL_TEMPORAL_VERIFIER_REASONING_EFFORT,
        "run_count": RUN_COUNT,
        "case_count": len(cases),
        "candidate_case_count": sum(bool(case.preparation.requests) for case in cases),
        "zero_work_case_count": sum(not case.preparation.requests for case in cases),
        "request_count_per_run": len(_request_rows(cases)),
        "call_count_per_run": len(units),
        "max_items_per_call": external.MAX_VERIFIER_BATCH_SIZE,
        "max_request_bytes": external.MAX_VERIFIER_REQUEST_BYTES,
        "cases": [
            {
                "case_id": case.case_id,
                "disposition": case.preparation.disposition,
                "admission_basis": case.preparation.admission_basis,
                "runner_policy_fingerprint": (
                    case.preparation.runner_policy_fingerprint
                ),
                "admission_policy_fingerprint": (
                    case.preparation.admission_policy_fingerprint
                ),
                "verifier_policy_fingerprint": (
                    case.preparation.verifier_policy_fingerprint
                ),
                "source_sha256": case.preparation.source_sha256,
                "analysis_fingerprint": case.preparation.analysis_fingerprint,
                "batch_plan_fingerprint": case.preparation.batch_plan_fingerprint,
                "target_fingerprint": case.preparation.target_fingerprint,
                "request_fingerprints": [
                    request.request_fingerprint for request in case.preparation.requests
                ],
            }
            for case in cases
        ],
        "calls": calls,
        "source_module_sha256": _source_hashes(),
        "gold_sha256_committed_but_not_opened": challenge["gold_sha256"],
        "gold_accessed": False,
        "public_synthetic": True,
        "contains_private_gmail": False,
        "release_eligible": False,
        "restricted_execution": claims.restricted_execution,
        "ephemeral_execution": claims.ephemeral_execution,
        "local_model_used": claims.local_model_used,
        "test_invoker_used": claims.test_invoker_used,
        "created_at": _now(),
    }


def _validate_response(
    unit: _CallUnit, response: Mapping[str, Any]
) -> dict[str, tuple[Mapping[str, Any], ...]]:
    if (
        set(response) != {"version", "pages"}
        or response.get("version") != external.VERIFIER_RESPONSE_VERSION
    ):
        raise PublicChallengeError("external verifier response schema is invalid")
    pages = response.get("pages")
    if not isinstance(pages, list) or len(pages) != len(unit.rows):
        raise PublicChallengeError("external verifier response coverage is invalid")
    by_case: dict[str, list[Mapping[str, Any]]] = {
        case_id: [] for case_id in unit.case_ids
    }
    for row, page in zip(unit.rows, pages, strict=True):
        if (
            not isinstance(page, Mapping)
            or set(page) != {"request_fingerprint", "verdicts"}
            or page.get("request_fingerprint") != row.request_fingerprint
        ):
            raise PublicChallengeError("external verifier page authority is invalid")
        verdicts = page.get("verdicts")
        if not isinstance(verdicts, list):
            raise PublicChallengeError("external verifier verdicts are invalid")
        actual: list[str] = []
        for verdict in verdicts:
            if (
                not isinstance(verdict, Mapping)
                or set(verdict) != {"candidate_id", "verdict"}
                or not isinstance(verdict.get("candidate_id"), str)
                or verdict.get("verdict")
                not in {"supported", "uncertain", "unsupported"}
            ):
                raise PublicChallengeError("external verifier verdict is invalid")
            actual.append(str(verdict["candidate_id"]))
        if tuple(actual) != row.candidate_ids:
            raise PublicChallengeError(
                "external verifier candidate coverage is invalid"
            )
        by_case[row.case_id].append(
            {
                "request_fingerprint": row.request_fingerprint,
                "batch_fingerprint": row.batch_fingerprint,
                "frontier_fingerprint": row.frontier_fingerprint,
                "page_plan_fingerprint": row.page_plan_fingerprint,
                "page_fingerprint": row.page_fingerprint,
                "verdicts": [dict(item) for item in verdicts],
            }
        )
    return {key: tuple(value) for key, value in by_case.items()}


def _call_path(output_root: Path, run_ordinal: int, unit_ordinal: int) -> Path:
    return output_root / "calls" / f"run-{run_ordinal}" / f"unit-{unit_ordinal:03d}"


def _execute_call(
    *,
    output_root: Path,
    key: bytes,
    plan: Mapping[str, Any],
    unit: _CallUnit,
    run_ordinal: int,
    invoke: ModelInvoker,
    timeout_seconds: int,
    claims: _ExecutionClaims,
) -> _CompletedCall:
    entries = [
        item
        for item in plan["calls"]
        if item["run_ordinal"] == run_ordinal
        and item["unit_ordinal"] == unit.unit_ordinal
    ]
    if len(entries) != 1:
        raise PublicChallengeError("call plan authority is ambiguous")
    entry = entries[0]
    call_id = str(entry["call_id"])
    logical_run_id = str(entry["logical_run_id"])
    if (
        _CALL_ID_RE.fullmatch(call_id) is None
        or _LOGICAL_RUN_ID_RE.fullmatch(logical_run_id) is None
        or entry["request_sha256"] != unit.request_sha256
    ):
        raise PublicChallengeError("call plan authority is invalid")
    call_root = _call_path(output_root, run_ordinal, unit.unit_ordinal)
    _private_directory(call_root, create=True)
    request_raw = _canonical_json(unit.request) + b"\n"
    _write_private_new(call_root / "request.json", request_raw)
    started_at = _now()
    start = _signed(
        {
            "version": CALL_START_VERSION,
            "challenge_id": plan["challenge_id"],
            "run_ordinal": run_ordinal,
            "logical_run_id": logical_run_id,
            "unit_ordinal": unit.unit_ordinal,
            "call_id": call_id,
            "provider": claims.provider,
            "model": GMAIL_TEMPORAL_VERIFIER_MODEL,
            "reasoning_effort": GMAIL_TEMPORAL_VERIFIER_REASONING_EFFORT,
            "request_sha256": unit.request_sha256,
            "started_at": started_at,
            "external_call_started": claims.external_call_started,
            "restricted_execution": claims.restricted_execution,
            "ephemeral_execution": claims.ephemeral_execution,
            "local_model_used": claims.local_model_used,
            "test_invoker_used": claims.test_invoker_used,
            "public_synthetic": True,
            "release_eligible": False,
        },
        key=key,
        domain=CALL_START_DOMAIN,
        signature_field="start_hmac_sha256",
    )
    start_raw = _canonical_json(start) + b"\n"
    _write_private_new(call_root / "started.json", start_raw)
    response: Mapping[str, Any] | None = None
    error_type: str | None = None
    case_pages: dict[str, tuple[Mapping[str, Any], ...]] | None = None
    try:
        response = invoke(
            unit.request,
            external._verifier_response_schema(len(unit.rows)),  # noqa: SLF001
            GMAIL_TEMPORAL_VERIFIER_MODEL,
            GMAIL_TEMPORAL_VERIFIER_REASONING_EFFORT,
            timeout_seconds,
        )
        case_pages = _validate_response(unit, response)
    except Exception as exc:  # noqa: BLE001 - details are hashed, never reflected.
        error_type = type(exc).__name__
        diagnostic_sha256 = _sha256(
            f"{type(exc).__module__}.{type(exc).__name__}:{exc}".encode("utf-8")
        )
        completed_at = _now()
        receipt = _signed(
            {
                "version": CALL_RECEIPT_VERSION,
                "challenge_id": plan["challenge_id"],
                "run_ordinal": run_ordinal,
                "logical_run_id": logical_run_id,
                "unit_ordinal": unit.unit_ordinal,
                "call_id": call_id,
                "provider": claims.provider,
                "model": GMAIL_TEMPORAL_VERIFIER_MODEL,
                "reasoning_effort": GMAIL_TEMPORAL_VERIFIER_REASONING_EFFORT,
                "started_at": started_at,
                "completed_at": completed_at,
                "request_sha256": unit.request_sha256,
                "response_sha256": None,
                "status": "failed",
                "error_type": error_type,
                "diagnostic_sha256": diagnostic_sha256,
                "external_call_started": claims.external_call_started,
                "restricted_execution": claims.restricted_execution,
                "ephemeral_execution": claims.ephemeral_execution,
                "local_model_used": claims.local_model_used,
                "test_invoker_used": claims.test_invoker_used,
                "public_synthetic": True,
                "release_eligible": False,
            },
            key=key,
            domain=CALL_RECEIPT_DOMAIN,
            signature_field="receipt_hmac_sha256",
        )
        _write_private_new(call_root / "receipt.json", _canonical_json(receipt) + b"\n")
        raise PublicChallengeError("restricted external verifier call failed") from exc
    assert response is not None and case_pages is not None
    response_raw = _canonical_json(response) + b"\n"
    _write_private_new(call_root / "response.json", response_raw)
    completed_at = _now()
    receipt = _signed(
        {
            "version": CALL_RECEIPT_VERSION,
            "challenge_id": plan["challenge_id"],
            "run_ordinal": run_ordinal,
            "logical_run_id": logical_run_id,
            "unit_ordinal": unit.unit_ordinal,
            "call_id": call_id,
            "provider": claims.provider,
            "model": GMAIL_TEMPORAL_VERIFIER_MODEL,
            "reasoning_effort": GMAIL_TEMPORAL_VERIFIER_REASONING_EFFORT,
            "started_at": started_at,
            "completed_at": completed_at,
            "request_sha256": unit.request_sha256,
            "response_sha256": _sha256(response_raw),
            "status": "success",
            "error_type": None,
            "diagnostic_sha256": None,
            "external_call_started": claims.external_call_started,
            "restricted_execution": claims.restricted_execution,
            "ephemeral_execution": claims.ephemeral_execution,
            "local_model_used": claims.local_model_used,
            "test_invoker_used": claims.test_invoker_used,
            "public_synthetic": True,
            "release_eligible": False,
        },
        key=key,
        domain=CALL_RECEIPT_DOMAIN,
        signature_field="receipt_hmac_sha256",
    )
    receipt_raw = _canonical_json(receipt) + b"\n"
    _write_private_new(call_root / "receipt.json", receipt_raw)
    return _CompletedCall(
        run_ordinal=run_ordinal,
        unit_ordinal=unit.unit_ordinal,
        call_id=call_id,
        logical_run_id=logical_run_id,
        started_at=started_at,
        completed_at=completed_at,
        request_sha256=unit.request_sha256,
        response_sha256=_sha256(response_raw),
        start_sha256=_sha256(start_raw),
        receipt_sha256=_sha256(receipt_raw),
        response=response,
        case_pages=case_pages,
    )


def _component_value(case: _Case, call: _CompletedCall) -> dict[str, Any]:
    preparation = case.preparation
    pages = call.case_pages.get(case.case_id)
    if pages is None or len(pages) != len(preparation.requests):
        raise PublicChallengeError("case prediction coverage is incomplete")
    return {
        "version": GMAIL_TEMPORAL_COMPONENT_VERSION,
        "run_ordinal": call.run_ordinal,
        "invocation_id": call.call_id,
        "provider": GMAIL_TEMPORAL_EXTERNAL_PROVIDER,
        "model": GMAIL_TEMPORAL_VERIFIER_MODEL,
        "reasoning_effort": GMAIL_TEMPORAL_VERIFIER_REASONING_EFFORT,
        "started_at": call.started_at,
        "completed_at": call.completed_at,
        "runner_policy_fingerprint": gmail_temporal_runner_policy_fingerprint(),
        "admission_policy_fingerprint": (gmail_temporal_admission_policy_fingerprint()),
        "verifier_policy_fingerprint": gmail_temporal_verifier_policy_fingerprint(),
        "source_sha256": preparation.source_sha256,
        "analysis_fingerprint": preparation.analysis_fingerprint,
        "batch_plan_fingerprint": preparation.batch_plan_fingerprint,
        "target_fingerprint": preparation.target_fingerprint,
        "pages": [dict(item) for item in pages],
        "complete": True,
        "routable": False,
    }


def _component_artifacts(
    output_root: Path,
    cases: Sequence[_Case],
    calls: Sequence[_CompletedCall],
) -> dict[str, tuple[Path, ...]]:
    by_run_case: dict[tuple[int, str], _CompletedCall] = {}
    for call in calls:
        for case_id in call.case_pages:
            key = (call.run_ordinal, case_id)
            if key in by_run_case:
                raise PublicChallengeError("case spans multiple calls in one run")
            by_run_case[key] = call
    output: dict[str, tuple[Path, ...]] = {}
    for case in cases:
        if not case.preparation.requests:
            output[case.case_id] = ()
            continue
        paths: list[Path] = []
        for run_ordinal in range(1, RUN_COUNT + 1):
            call = by_run_case.get((run_ordinal, case.case_id))
            if call is None:
                raise PublicChallengeError("case prediction run is missing")
            value = _component_value(case, call)
            path = output_root / "components" / case.case_id / f"run-{run_ordinal}.json"
            _write_private_new(path, _canonical_json(value) + b"\n")
            paths.append(path)
        output[case.case_id] = tuple(paths)
    return output


def _prediction_seal(
    *,
    key: bytes,
    challenge: Mapping[str, Any],
    challenge_raw: bytes,
    plan_raw: bytes,
    cases: Sequence[_Case],
    calls: Sequence[_CompletedCall],
    components: Mapping[str, tuple[Path, ...]],
    claims: _ExecutionClaims,
) -> dict[str, Any]:
    return _signed(
        {
            "version": PREDICTION_SEAL_VERSION,
            "launcher_version": VERSION,
            "challenge_id": challenge["challenge_id"],
            "challenge_manifest_sha256": _sha256(challenge_raw),
            "plan_sha256": _sha256(plan_raw),
            "gold_sha256_committed_but_not_opened": challenge["gold_sha256"],
            "gold_accessed": False,
            "provider": claims.provider,
            "model": GMAIL_TEMPORAL_VERIFIER_MODEL,
            "reasoning_effort": GMAIL_TEMPORAL_VERIFIER_REASONING_EFFORT,
            "run_count": RUN_COUNT,
            "invocation_count": len(calls),
            "external_call_count": (len(calls) if claims.external_call_started else 0),
            "call_ids": [call.call_id for call in calls],
            "call_evidence": [
                {
                    "run_ordinal": call.run_ordinal,
                    "unit_ordinal": call.unit_ordinal,
                    "logical_run_id": call.logical_run_id,
                    "call_id": call.call_id,
                    "request_sha256": call.request_sha256,
                    "start_sha256": call.start_sha256,
                    "response_sha256": call.response_sha256,
                    "receipt_sha256": call.receipt_sha256,
                }
                for call in calls
            ],
            "request_set_sha256": _sha256(
                _canonical_json(sorted(call.request_sha256 for call in calls))
            ),
            "response_set_sha256": _sha256(
                _canonical_json(sorted(call.response_sha256 for call in calls))
            ),
            "receipt_set_sha256": _sha256(
                _canonical_json(sorted(call.receipt_sha256 for call in calls))
            ),
            "cases": [
                {
                    "case_id": case.case_id,
                    "disposition": case.preparation.disposition,
                    "component_sha256": [
                        _sha256(_private_file(path))
                        for path in components[case.case_id]
                    ],
                }
                for case in cases
            ],
            "public_synthetic": True,
            "contains_private_gmail": False,
            "release_eligible": False,
            "restricted_execution": claims.restricted_execution,
            "ephemeral_execution": claims.ephemeral_execution,
            "local_model_used": claims.local_model_used,
            "test_invoker_used": claims.test_invoker_used,
            "sealed_at": _now(),
        },
        key=key,
        domain=PREDICTION_SEAL_DOMAIN,
        signature_field="seal_hmac_sha256",
    )


def run_public_challenge(
    challenge_path: Path,
    hmac_key_path: Path,
    output_root: Path,
    *,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    codex_binary: str | None = None,
    invoke: ModelInvoker | None = None,
    test_only_allow_injected_invoker: bool = False,
) -> dict[str, Any]:
    """Run and persist one fresh public challenge without opening semantic gold."""

    if not 30 <= timeout_seconds <= MAX_TIMEOUT_SECONDS:
        raise PublicChallengeError("timeout is outside the safe bound")
    if (invoke is not None) != test_only_allow_injected_invoker:
        raise PublicChallengeError("injected invoker requires explicit test-only mode")
    claims = _execution_claims(test_invoker_used=invoke is not None)
    key = _key(hmac_key_path)
    challenge, challenge_raw = _load_challenge(challenge_path, key=key)
    cases = _prepare_cases(challenge)
    rows = _request_rows(cases)
    units = bounded_public_call_units(rows)
    if rows and not units:
        raise PublicChallengeError("candidate-bearing challenge has no call units")
    _fresh_private_directory(output_root)
    plan = _signed(
        _plan_value(
            challenge=challenge,
            challenge_raw=challenge_raw,
            cases=cases,
            units=units,
            claims=claims,
        ),
        key=key,
        domain=PLAN_DOMAIN,
        signature_field="plan_hmac_sha256",
    )
    plan_raw = _canonical_json(plan) + b"\n"
    _write_private_new(output_root / "plan.json", plan_raw)
    active_invoke = invoke or external.RestrictedCodexInvoker(codex_binary)
    completed: list[_CompletedCall] = []
    for run_ordinal in range(1, RUN_COUNT + 1):
        for unit in units:
            completed.append(
                _execute_call(
                    output_root=output_root,
                    key=key,
                    plan=plan,
                    unit=unit,
                    run_ordinal=run_ordinal,
                    invoke=active_invoke,
                    timeout_seconds=timeout_seconds,
                    claims=claims,
                )
            )

    components = _component_artifacts(output_root, cases, completed)
    # Validate every component against freshly reconstructed production authority
    # before the seal or any persistence mutation becomes possible.
    import pkm_brain.gmail_temporal_runner as production_runner

    paths = BrainPaths.from_value(str(challenge["brain_home"]))
    for case in cases:
        if not components[case.case_id]:
            continue
        authority = production_runner._build_authority(  # noqa: SLF001
            paths,
            document_id=case.document_id,
            gmail_message_id=case.gmail_message_id,
        )
        production_runner._load_components(  # noqa: SLF001
            components[case.case_id], authority=authority
        )
    seal = _prediction_seal(
        key=key,
        challenge=challenge,
        challenge_raw=challenge_raw,
        plan_raw=plan_raw,
        cases=cases,
        calls=completed,
        components=components,
        claims=claims,
    )
    seal_raw = _canonical_json(seal) + b"\n"
    _write_private_new(output_root / "prediction-seal.json", seal_raw)

    results: list[dict[str, Any]] = []
    for case in cases:
        try:
            result = run_gmail_temporal_review(
                paths,
                document_id=case.document_id,
                gmail_message_id=case.gmail_message_id,
                component_artifacts=components[case.case_id],
            )
        except GmailTemporalRunnerError as exc:
            raise PublicChallengeError(
                "production finalization failed after seal"
            ) from exc
        projection: Mapping[str, Any] | None = None
        if result.run_id is not None:
            with connection(paths.sqlite_path) as conn:
                row = conn.execute(
                    "SELECT projection_json FROM gmail_temporal_review_runs WHERE id = ?",
                    (result.run_id,),
                ).fetchone()
            if row is None:
                raise PublicChallengeError("persisted projection is unavailable")
            projection = json.loads(str(row["projection_json"]))
        results.append(
            {
                "case_id": case.case_id,
                "runner_result": asdict(result),
                "projection": projection,
            }
        )
    result_value = _signed(
        {
            "version": RESULT_VERSION,
            "launcher_version": VERSION,
            "challenge_id": challenge["challenge_id"],
            "challenge_manifest_sha256": _sha256(challenge_raw),
            "plan_sha256": _sha256(plan_raw),
            "prediction_seal_sha256": _sha256(seal_raw),
            "gold_sha256_committed_but_not_opened": challenge["gold_sha256"],
            "gold_accessed": False,
            "provider": claims.provider,
            "model": GMAIL_TEMPORAL_VERIFIER_MODEL,
            "reasoning_effort": GMAIL_TEMPORAL_VERIFIER_REASONING_EFFORT,
            "invocation_count": len(completed),
            "external_call_count": (
                len(completed) if claims.external_call_started else 0
            ),
            "results": results,
            "public_synthetic": True,
            "contains_private_gmail": False,
            "release_eligible": False,
            "restricted_execution": claims.restricted_execution,
            "ephemeral_execution": claims.ephemeral_execution,
            "local_model_used": claims.local_model_used,
            "test_invoker_used": claims.test_invoker_used,
            "complete": True,
            "completed_at": _now(),
        },
        key=key,
        domain=RESULT_DOMAIN,
        signature_field="result_hmac_sha256",
    )
    result_raw = _canonical_json(result_value) + b"\n"
    _write_private_new(output_root / "results.json", result_raw)
    return {
        "version": VERSION,
        "status": "complete",
        "challenge_id": challenge["challenge_id"],
        "cases": len(cases),
        "candidate_cases": sum(bool(case.preparation.requests) for case in cases),
        "zero_work_cases": sum(not case.preparation.requests for case in cases),
        "requests_per_run": len(rows),
        "calls_per_run": len(units),
        "invocations": len(completed),
        "external_calls": len(completed) if claims.external_call_started else 0,
        "artifact_count": sum(
            int(item["runner_result"]["artifact_count"]) for item in results
        ),
        "gold_accessed": False,
        "public_synthetic": True,
        "release_eligible": False,
        "restricted_execution": claims.restricted_execution,
        "test_invoker_used": claims.test_invoker_used,
        "local_model_used": claims.local_model_used,
        "private_content_printed": False,
    }


def _load_signed_artifact(
    path: Path,
    *,
    key: bytes,
    domain: bytes,
    signature_field: str,
    label: str,
) -> tuple[dict[str, Any], bytes]:
    raw = _private_file(path)
    value = _strict_json(raw, label=label)
    if not _verify_signed(
        value,
        key=key,
        domain=domain,
        signature_field=signature_field,
    ):
        raise PublicChallengeError(f"{label} authentication failed")
    return value, raw


def _plan_case_rows(cases: Sequence[_Case]) -> list[dict[str, Any]]:
    return [
        {
            "case_id": case.case_id,
            "disposition": case.preparation.disposition,
            "admission_basis": case.preparation.admission_basis,
            "runner_policy_fingerprint": case.preparation.runner_policy_fingerprint,
            "admission_policy_fingerprint": (
                case.preparation.admission_policy_fingerprint
            ),
            "verifier_policy_fingerprint": (
                case.preparation.verifier_policy_fingerprint
            ),
            "source_sha256": case.preparation.source_sha256,
            "analysis_fingerprint": case.preparation.analysis_fingerprint,
            "batch_plan_fingerprint": case.preparation.batch_plan_fingerprint,
            "target_fingerprint": case.preparation.target_fingerprint,
            "request_fingerprints": [
                request.request_fingerprint for request in case.preparation.requests
            ],
        }
        for case in cases
    ]


def _claims_from_plan(plan: Mapping[str, Any]) -> _ExecutionClaims:
    test_value = plan.get("test_invoker_used")
    if not isinstance(test_value, bool):
        raise PublicChallengeError("public challenge execution mode is invalid")
    claims = _execution_claims(test_invoker_used=test_value)
    if (
        plan.get("provider") != claims.provider
        or plan.get("restricted_execution") is not claims.restricted_execution
        or plan.get("ephemeral_execution") is not claims.ephemeral_execution
        or plan.get("local_model_used") is not claims.local_model_used
    ):
        raise PublicChallengeError("public challenge execution claims are invalid")
    return claims


def _validate_plan_authority(
    plan: Mapping[str, Any],
    *,
    challenge: Mapping[str, Any],
    challenge_raw: bytes,
    cases: Sequence[_Case],
    units: Sequence[_CallUnit],
) -> tuple[_ExecutionClaims, dict[tuple[int, int], Mapping[str, Any]]]:
    expected_keys = {
        "version",
        "launcher_version",
        "challenge_id",
        "challenge_manifest_sha256",
        "scope",
        "provider",
        "model",
        "reasoning_effort",
        "run_count",
        "case_count",
        "candidate_case_count",
        "zero_work_case_count",
        "request_count_per_run",
        "call_count_per_run",
        "max_items_per_call",
        "max_request_bytes",
        "cases",
        "calls",
        "source_module_sha256",
        "gold_sha256_committed_but_not_opened",
        "gold_accessed",
        "public_synthetic",
        "contains_private_gmail",
        "release_eligible",
        "restricted_execution",
        "ephemeral_execution",
        "local_model_used",
        "test_invoker_used",
        "created_at",
        "plan_hmac_sha256",
    }
    if set(plan) != expected_keys:
        raise PublicChallengeError("public challenge plan schema is invalid")
    claims = _claims_from_plan(plan)
    rows = _request_rows(cases)
    stable = {
        "version": PLAN_VERSION,
        "launcher_version": VERSION,
        "challenge_id": challenge["challenge_id"],
        "challenge_manifest_sha256": _sha256(challenge_raw),
        "scope": PUBLIC_SCOPE,
        "provider": claims.provider,
        "model": GMAIL_TEMPORAL_VERIFIER_MODEL,
        "reasoning_effort": GMAIL_TEMPORAL_VERIFIER_REASONING_EFFORT,
        "run_count": RUN_COUNT,
        "case_count": len(cases),
        "candidate_case_count": sum(bool(case.preparation.requests) for case in cases),
        "zero_work_case_count": sum(not case.preparation.requests for case in cases),
        "request_count_per_run": len(rows),
        "call_count_per_run": len(units),
        "max_items_per_call": external.MAX_VERIFIER_BATCH_SIZE,
        "max_request_bytes": external.MAX_VERIFIER_REQUEST_BYTES,
        "cases": _plan_case_rows(cases),
        "source_module_sha256": _source_hashes(),
        "gold_sha256_committed_but_not_opened": challenge["gold_sha256"],
        "gold_accessed": False,
        "public_synthetic": True,
        "contains_private_gmail": False,
        "release_eligible": False,
        "restricted_execution": claims.restricted_execution,
        "ephemeral_execution": claims.ephemeral_execution,
        "local_model_used": claims.local_model_used,
        "test_invoker_used": claims.test_invoker_used,
    }
    if any(plan.get(key) != value for key, value in stable.items()):
        raise PublicChallengeError("public challenge plan authority is stale")
    if _aware_timestamp(plan.get("created_at")) is None:
        raise PublicChallengeError("public challenge plan chronology is invalid")
    raw_calls = plan.get("calls")
    if not isinstance(raw_calls, list) or len(raw_calls) != RUN_COUNT * len(units):
        raise PublicChallengeError("public challenge call plan coverage is invalid")
    expected_call_keys = {
        "run_ordinal",
        "logical_run_id",
        "unit_ordinal",
        "call_id",
        "case_ids",
        "request_fingerprints",
        "request_sha256",
        "request_bytes",
    }
    by_key: dict[tuple[int, int], Mapping[str, Any]] = {}
    logical_by_run: dict[int, str] = {}
    call_ids: set[str] = set()
    for raw_call in raw_calls:
        if not isinstance(raw_call, Mapping) or set(raw_call) != expected_call_keys:
            raise PublicChallengeError("public challenge call plan schema is invalid")
        run_ordinal = raw_call.get("run_ordinal")
        unit_ordinal = raw_call.get("unit_ordinal")
        if (
            not isinstance(run_ordinal, int)
            or isinstance(run_ordinal, bool)
            or run_ordinal not in range(1, RUN_COUNT + 1)
            or not isinstance(unit_ordinal, int)
            or isinstance(unit_ordinal, bool)
            or unit_ordinal not in range(1, len(units) + 1)
        ):
            raise PublicChallengeError("public challenge call plan ordinal is invalid")
        key = (run_ordinal, unit_ordinal)
        if key in by_key:
            raise PublicChallengeError("public challenge call plan is duplicated")
        unit = units[unit_ordinal - 1]
        call_id = raw_call.get("call_id")
        logical_id = raw_call.get("logical_run_id")
        expected_test_marker = "_test_" in str(call_id)
        if (
            not isinstance(call_id, str)
            or _CALL_ID_RE.fullmatch(call_id) is None
            or call_id in call_ids
            or expected_test_marker is not claims.test_invoker_used
            or not isinstance(logical_id, str)
            or _LOGICAL_RUN_ID_RE.fullmatch(logical_id) is None
            or ("_test_" in logical_id) is not claims.test_invoker_used
            or raw_call.get("case_ids") != list(unit.case_ids)
            or raw_call.get("request_fingerprints")
            != [row.request_fingerprint for row in unit.rows]
            or raw_call.get("request_sha256") != unit.request_sha256
            or raw_call.get("request_bytes")
            != len(_canonical_json(unit.request) + b"\n")
        ):
            raise PublicChallengeError(
                "public challenge call plan authority is invalid"
            )
        previous = logical_by_run.setdefault(run_ordinal, logical_id)
        if previous != logical_id:
            raise PublicChallengeError("logical run spans inconsistent identities")
        call_ids.add(call_id)
        by_key[key] = raw_call
    if (units and len(set(logical_by_run.values())) != RUN_COUNT) or set(by_key) != {
        (run, unit.unit_ordinal) for run in range(1, RUN_COUNT + 1) for unit in units
    }:
        raise PublicChallengeError("public challenge logical runs are incomplete")
    return claims, by_key


def _validate_call_evidence(
    *,
    output_root: Path,
    key: bytes,
    challenge: Mapping[str, Any],
    plan_calls: Mapping[tuple[int, int], Mapping[str, Any]],
    units: Sequence[_CallUnit],
    claims: _ExecutionClaims,
) -> tuple[_CompletedCall, ...]:
    calls_root = output_root / "calls"
    if not units:
        if calls_root.exists() or calls_root.is_symlink():
            raise PublicChallengeError("zero-work challenge fabricated call evidence")
        return ()
    _private_directory(calls_root)
    expected_run_names = {f"run-{value}" for value in range(1, RUN_COUNT + 1)}
    if {path.name for path in calls_root.iterdir()} != expected_run_names:
        raise PublicChallengeError("public challenge call directory is invalid")
    completed: list[_CompletedCall] = []
    start_keys = {
        "version",
        "challenge_id",
        "run_ordinal",
        "logical_run_id",
        "unit_ordinal",
        "call_id",
        "provider",
        "model",
        "reasoning_effort",
        "request_sha256",
        "started_at",
        "external_call_started",
        "restricted_execution",
        "ephemeral_execution",
        "local_model_used",
        "test_invoker_used",
        "public_synthetic",
        "release_eligible",
        "start_hmac_sha256",
    }
    receipt_keys = {
        "version",
        "challenge_id",
        "run_ordinal",
        "logical_run_id",
        "unit_ordinal",
        "call_id",
        "provider",
        "model",
        "reasoning_effort",
        "started_at",
        "completed_at",
        "request_sha256",
        "response_sha256",
        "status",
        "error_type",
        "diagnostic_sha256",
        "external_call_started",
        "restricted_execution",
        "ephemeral_execution",
        "local_model_used",
        "test_invoker_used",
        "public_synthetic",
        "release_eligible",
        "receipt_hmac_sha256",
    }
    for run_ordinal in range(1, RUN_COUNT + 1):
        run_root = calls_root / f"run-{run_ordinal}"
        _private_directory(run_root)
        expected_unit_names = {f"unit-{unit.unit_ordinal:03d}" for unit in units}
        if {path.name for path in run_root.iterdir()} != expected_unit_names:
            raise PublicChallengeError("public challenge call unit coverage is invalid")
        for unit in units:
            entry = plan_calls[(run_ordinal, unit.unit_ordinal)]
            call_root = _call_path(output_root, run_ordinal, unit.unit_ordinal)
            _private_directory(call_root)
            if {path.name for path in call_root.iterdir()} != {
                "request.json",
                "started.json",
                "response.json",
                "receipt.json",
            }:
                raise PublicChallengeError(
                    "public challenge call evidence is incomplete"
                )
            request_raw = _private_file(call_root / "request.json")
            request = _strict_json(request_raw, label="public verifier request")
            if request != unit.request or _sha256(request_raw) != unit.request_sha256:
                raise PublicChallengeError("public verifier request authority is stale")
            start, start_raw = _load_signed_artifact(
                call_root / "started.json",
                key=key,
                domain=CALL_START_DOMAIN,
                signature_field="start_hmac_sha256",
                label="public verifier call start",
            )
            expected_common = {
                "challenge_id": challenge["challenge_id"],
                "run_ordinal": run_ordinal,
                "logical_run_id": entry["logical_run_id"],
                "unit_ordinal": unit.unit_ordinal,
                "call_id": entry["call_id"],
                "provider": claims.provider,
                "model": GMAIL_TEMPORAL_VERIFIER_MODEL,
                "reasoning_effort": GMAIL_TEMPORAL_VERIFIER_REASONING_EFFORT,
                "request_sha256": unit.request_sha256,
                "external_call_started": claims.external_call_started,
                "restricted_execution": claims.restricted_execution,
                "ephemeral_execution": claims.ephemeral_execution,
                "local_model_used": claims.local_model_used,
                "test_invoker_used": claims.test_invoker_used,
                "public_synthetic": True,
                "release_eligible": False,
            }
            if (
                set(start) != start_keys
                or start.get("version") != CALL_START_VERSION
                or any(
                    start.get(name) != value for name, value in expected_common.items()
                )
                or _aware_timestamp(start.get("started_at")) is None
            ):
                raise PublicChallengeError("public verifier call start is invalid")
            response_raw = _private_file(call_root / "response.json")
            response = _strict_json(response_raw, label="public verifier response")
            case_pages = _validate_response(unit, response)
            receipt, receipt_raw = _load_signed_artifact(
                call_root / "receipt.json",
                key=key,
                domain=CALL_RECEIPT_DOMAIN,
                signature_field="receipt_hmac_sha256",
                label="public verifier call receipt",
            )
            started_at = _aware_timestamp(start["started_at"])
            completed_at = _aware_timestamp(receipt.get("completed_at"))
            if (
                set(receipt) != receipt_keys
                or receipt.get("version") != CALL_RECEIPT_VERSION
                or any(
                    receipt.get(name) != value
                    for name, value in expected_common.items()
                )
                or receipt.get("started_at") != start["started_at"]
                or completed_at is None
                or started_at is None
                or completed_at < started_at
                or receipt.get("response_sha256") != _sha256(response_raw)
                or receipt.get("status") != "success"
                or receipt.get("error_type") is not None
                or receipt.get("diagnostic_sha256") is not None
            ):
                raise PublicChallengeError("public verifier call receipt is invalid")
            completed.append(
                _CompletedCall(
                    run_ordinal=run_ordinal,
                    unit_ordinal=unit.unit_ordinal,
                    call_id=str(entry["call_id"]),
                    logical_run_id=str(entry["logical_run_id"]),
                    started_at=str(start["started_at"]),
                    completed_at=str(receipt["completed_at"]),
                    request_sha256=unit.request_sha256,
                    response_sha256=_sha256(response_raw),
                    start_sha256=_sha256(start_raw),
                    receipt_sha256=_sha256(receipt_raw),
                    response=response,
                    case_pages=case_pages,
                )
            )
    return tuple(completed)


def _validate_component_evidence(
    *,
    output_root: Path,
    challenge: Mapping[str, Any],
    cases: Sequence[_Case],
    calls: Sequence[_CompletedCall],
) -> tuple[dict[str, tuple[Path, ...]], dict[str, Any]]:
    import pkm_brain.gmail_temporal_runner as production_runner

    by_run_case: dict[tuple[int, str], _CompletedCall] = {}
    for call in calls:
        for case_id in call.case_pages:
            key = (call.run_ordinal, case_id)
            if key in by_run_case:
                raise PublicChallengeError("case spans multiple calls in one run")
            by_run_case[key] = call
    components_root = output_root / "components"
    candidate_cases = [case for case in cases if case.preparation.requests]
    paths = BrainPaths.from_value(str(challenge["brain_home"]))
    authorities = {
        case.case_id: production_runner._build_authority(  # noqa: SLF001
            paths,
            document_id=case.document_id,
            gmail_message_id=case.gmail_message_id,
        )
        for case in cases
    }
    if not candidate_cases:
        if components_root.exists() or components_root.is_symlink():
            raise PublicChallengeError("zero-work challenge fabricated components")
        return ({case.case_id: () for case in cases}, authorities)
    _private_directory(components_root)
    if {path.name for path in components_root.iterdir()} != {
        case.case_id for case in candidate_cases
    }:
        raise PublicChallengeError("public component case coverage is invalid")
    output: dict[str, tuple[Path, ...]] = {}
    for case in cases:
        authority = authorities[case.case_id]
        if not case.preparation.requests:
            output[case.case_id] = ()
            continue
        case_root = components_root / case.case_id
        _private_directory(case_root)
        expected_names = {f"run-{value}.json" for value in range(1, RUN_COUNT + 1)}
        if {path.name for path in case_root.iterdir()} != expected_names:
            raise PublicChallengeError("public component run coverage is invalid")
        case_paths: list[Path] = []
        for run_ordinal in range(1, RUN_COUNT + 1):
            call = by_run_case.get((run_ordinal, case.case_id))
            if call is None:
                raise PublicChallengeError("public component call authority is missing")
            path = case_root / f"run-{run_ordinal}.json"
            raw = _private_file(path)
            value = _strict_json(raw, label="public verifier component")
            if value != _component_value(case, call):
                raise PublicChallengeError("public verifier component is stale")
            case_paths.append(path)
        production_runner._load_components(  # noqa: SLF001
            tuple(case_paths), authority=authority
        )
        output[case.case_id] = tuple(case_paths)
    return output, authorities


def _validate_seal_authority(
    seal: Mapping[str, Any],
    *,
    key: bytes,
    challenge: Mapping[str, Any],
    challenge_raw: bytes,
    plan_raw: bytes,
    cases: Sequence[_Case],
    calls: Sequence[_CompletedCall],
    components: Mapping[str, tuple[Path, ...]],
    claims: _ExecutionClaims,
) -> None:
    expected = _prediction_seal(
        key=key,
        challenge=challenge,
        challenge_raw=challenge_raw,
        plan_raw=plan_raw,
        cases=cases,
        calls=calls,
        components=components,
        claims=claims,
    )
    comparable = dict(seal)
    expected_comparable = dict(expected)
    for value in (comparable, expected_comparable):
        value.pop("seal_hmac_sha256", None)
        value.pop("sealed_at", None)
    if (
        comparable != expected_comparable
        or _aware_timestamp(seal.get("sealed_at")) is None
    ):
        raise PublicChallengeError("prediction seal authority is invalid")


def _validate_persisted_results(
    result: Mapping[str, Any],
    *,
    challenge: Mapping[str, Any],
    challenge_raw: bytes,
    plan_raw: bytes,
    seal_raw: bytes,
    cases: Sequence[_Case],
    components: Mapping[str, tuple[Path, ...]],
    calls: Sequence[_CompletedCall],
    claims: _ExecutionClaims,
) -> dict[str, Mapping[str, Any]]:
    result_keys = {
        "version",
        "launcher_version",
        "challenge_id",
        "challenge_manifest_sha256",
        "plan_sha256",
        "prediction_seal_sha256",
        "gold_sha256_committed_but_not_opened",
        "gold_accessed",
        "provider",
        "model",
        "reasoning_effort",
        "invocation_count",
        "external_call_count",
        "results",
        "public_synthetic",
        "contains_private_gmail",
        "release_eligible",
        "restricted_execution",
        "ephemeral_execution",
        "local_model_used",
        "test_invoker_used",
        "complete",
        "completed_at",
        "result_hmac_sha256",
    }
    stable = {
        "version": RESULT_VERSION,
        "launcher_version": VERSION,
        "challenge_id": challenge["challenge_id"],
        "challenge_manifest_sha256": _sha256(challenge_raw),
        "plan_sha256": _sha256(plan_raw),
        "prediction_seal_sha256": _sha256(seal_raw),
        "gold_sha256_committed_but_not_opened": challenge["gold_sha256"],
        "gold_accessed": False,
        "provider": claims.provider,
        "model": GMAIL_TEMPORAL_VERIFIER_MODEL,
        "reasoning_effort": GMAIL_TEMPORAL_VERIFIER_REASONING_EFFORT,
        "invocation_count": len(calls),
        "external_call_count": len(calls) if claims.external_call_started else 0,
        "public_synthetic": True,
        "contains_private_gmail": False,
        "release_eligible": False,
        "restricted_execution": claims.restricted_execution,
        "ephemeral_execution": claims.ephemeral_execution,
        "local_model_used": claims.local_model_used,
        "test_invoker_used": claims.test_invoker_used,
        "complete": True,
    }
    if (
        set(result) != result_keys
        or any(result.get(name) != value for name, value in stable.items())
        or _aware_timestamp(result.get("completed_at")) is None
    ):
        raise PublicChallengeError("public challenge result authority is invalid")
    raw_rows = result.get("results")
    if not isinstance(raw_rows, list) or len(raw_rows) != len(cases):
        raise PublicChallengeError("prediction result case coverage is invalid")
    by_case: dict[str, Mapping[str, Any]] = {}
    paths = BrainPaths.from_value(str(challenge["brain_home"]))
    with connection(paths.sqlite_path) as conn:
        for case, raw_row in zip(cases, raw_rows, strict=True):
            if (
                not isinstance(raw_row, Mapping)
                or set(raw_row) != {"case_id", "runner_result", "projection"}
                or raw_row.get("case_id") != case.case_id
                or not isinstance(raw_row.get("runner_result"), Mapping)
            ):
                raise PublicChallengeError("prediction result case schema is invalid")
            runner = raw_row["runner_result"]
            expected_runner_keys = {
                "version",
                "disposition",
                "message_scope_key",
                "admission_basis",
                "expression_count",
                "batch_count",
                "candidate_count",
                "page_count",
                "component_count",
                "artifact_count",
                "cluster_review_count",
                "group_count",
                "persisted",
                "head_cleared",
                "run_id",
                "head_generation",
                "execution_id",
                "replayed",
                "head_changed",
                "independent_invocations_verified",
                "private_content_printed",
                "routable",
            }
            projection = raw_row.get("projection")
            artifacts = (
                projection.get("artifacts", [])
                if isinstance(projection, Mapping)
                else []
            )
            reviews = (
                projection.get("cluster_reviews", [])
                if isinstance(projection, Mapping)
                else []
            )
            groups = (
                projection.get("groups", []) if isinstance(projection, Mapping) else []
            )
            preparation = case.preparation
            expected_runner = {
                "version": GMAIL_TEMPORAL_RUNNER_VERSION,
                "disposition": preparation.disposition,
                "message_scope_key": preparation.message_scope_key,
                "admission_basis": preparation.admission_basis,
                "expression_count": preparation.expression_count,
                "batch_count": preparation.batch_count,
                "candidate_count": preparation.candidate_count,
                "page_count": preparation.page_count,
                "component_count": RUN_COUNT if components[case.case_id] else 0,
                "artifact_count": len(artifacts),
                "cluster_review_count": len(reviews),
                "group_count": len(groups),
                "persisted": True,
                "independent_invocations_verified": False,
                "private_content_printed": False,
                "routable": False,
            }
            if (
                set(runner) != expected_runner_keys
                or any(
                    runner.get(name) != value for name, value in expected_runner.items()
                )
                or (
                    runner.get("head_generation") is not None
                    and (
                        not isinstance(runner.get("head_generation"), int)
                        or isinstance(runner.get("head_generation"), bool)
                    )
                )
                or not isinstance(runner.get("execution_id"), str)
            ):
                raise PublicChallengeError("production runner result is invalid")
            execution = conn.execute(
                "SELECT * FROM gmail_temporal_review_executions WHERE id = ?",
                (runner["execution_id"],),
            ).fetchone()
            if execution is None or (
                execution["message_scope_key"] != preparation.message_scope_key
                or execution["pipeline_scope"] != GMAIL_TEMPORAL_PIPELINE_SCOPE
                or execution["document_id"] != case.document_id
                or execution["source_sha256"] != preparation.source_sha256
                or execution["provider"] != GMAIL_TEMPORAL_EXTERNAL_PROVIDER
                or execution["model"] != GMAIL_TEMPORAL_VERIFIER_MODEL
                or execution["reasoning_effort"]
                != GMAIL_TEMPORAL_VERIFIER_REASONING_EFFORT
                or execution["review_run_id"] != runner.get("run_id")
                or execution["component_count"] != expected_runner["component_count"]
            ):
                raise PublicChallengeError("production execution receipt is stale")
            component_rows = conn.execute(
                """
                SELECT run_ordinal, invocation_id, started_at, completed_at,
                       artifact_sha256, payload_json
                FROM gmail_temporal_review_components
                WHERE execution_id = ? ORDER BY run_ordinal
                """,
                (runner["execution_id"],),
            ).fetchall()
            expected_component_rows = []
            for ordinal, path in enumerate(components[case.case_id], start=1):
                raw = _private_file(path)
                value = _strict_json(raw, label="persisted verifier component")
                expected_component_rows.append(
                    (
                        ordinal,
                        value["invocation_id"],
                        value["started_at"],
                        value["completed_at"],
                        _sha256(raw),
                        raw.decode("utf-8"),
                    )
                )
            if [tuple(row) for row in component_rows] != expected_component_rows:
                raise PublicChallengeError("persisted component evidence is stale")
            head = conn.execute(
                """
                SELECT run_id, generation FROM gmail_temporal_review_heads
                WHERE message_scope_key = ? AND pipeline_scope = ?
                """,
                (preparation.message_scope_key, GMAIL_TEMPORAL_PIPELINE_SCOPE),
            ).fetchone()
            if runner.get("head_generation") is None:
                if head is not None:
                    raise PublicChallengeError("production temporal head is stale")
            elif (
                head is None
                or head["run_id"] != runner.get("run_id")
                or head["generation"] != runner.get("head_generation")
            ):
                raise PublicChallengeError("production temporal head is stale")
            run_id = runner.get("run_id")
            if run_id is None:
                if projection is not None or components[case.case_id]:
                    raise PublicChallengeError(
                        "zero-work result fabricated projection evidence"
                    )
            else:
                if not isinstance(projection, Mapping):
                    raise PublicChallengeError("persisted projection is invalid")
                stored = conn.execute(
                    "SELECT projection_json FROM gmail_temporal_review_runs WHERE id = ?",
                    (run_id,),
                ).fetchone()
                if (
                    stored is None
                    or json.loads(str(stored["projection_json"])) != projection
                ):
                    raise PublicChallengeError("persisted projection is stale")
            by_case[case.case_id] = raw_row
    return by_case


def _artifact_hypotheses(artifact: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    values = artifact.get("hypotheses")
    if not isinstance(values, list):
        return []
    return [item for item in values if isinstance(item, Mapping)]


def _normalized_subject(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split()).casefold()
    return normalized or None


_SUBJECT_IDENTITY_STOPWORDS = {
    "appointment",
    "cancelled",
    "completed",
    "confirm",
    "confirmed",
    "design",
    "event",
    "interview",
    "meeting",
    "moved",
    "planning",
    "project",
    "rescheduled",
    "review",
    "scheduled",
    "session",
}


def _subject_identity_tokens(value: Any) -> set[str]:
    normalized = _normalized_subject(value)
    if normalized is None:
        return set()
    return {
        token
        for token in re.findall(r"[a-z0-9]+", normalized)
        if token not in _SUBJECT_IDENTITY_STOPWORDS
    }


def _subject_matches(expected: str, actual: str) -> bool:
    if _normalized_subject(expected) == _normalized_subject(actual):
        return True
    expected_tokens = _subject_identity_tokens(expected)
    actual_tokens = _subject_identity_tokens(actual)
    return bool(expected_tokens and expected_tokens <= actual_tokens)


def _authority_subject_surfaces(authority: Any) -> dict[str, str]:
    text = authority.source.text
    output: dict[str, str] = {}
    for mention in authority.analysis.mentions:
        if mention.start < 0 or mention.end <= mention.start or mention.end > len(text):
            raise PublicChallengeError("production subject authority is invalid")
        output[mention.mention_id] = text[mention.start : mention.end]
    return output


def _hypothesis_matches_member(
    hypothesis: Mapping[str, Any],
    member: Mapping[str, Any],
    *,
    subject_surfaces: Mapping[str, str],
) -> bool:
    expected_subject = _normalized_subject(member.get("subject"))
    mention_ids = hypothesis.get("subject_mention_ids")
    if expected_subject is None or not isinstance(mention_ids, list) or not mention_ids:
        return False
    actual_subjects = {
        surface
        for mention_id in mention_ids
        if isinstance(mention_id, str)
        and isinstance((surface := subject_surfaces.get(mention_id)), str)
    }
    return (
        any(_subject_matches(expected_subject, actual) for actual in actual_subjects)
        and hypothesis.get("relation") == member.get("relation")
        and hypothesis.get("lifecycle") == member.get("lifecycle")
    )


def _exact_artifact_match(
    artifact: Mapping[str, Any],
    member: Mapping[str, Any],
    *,
    subject_surfaces: Mapping[str, str],
) -> bool:
    expected_verdict = member.get("expected_verdict", "supported")
    expected_status = "uncertain" if expected_verdict == "uncertain" else "supported"
    if artifact.get("evidence_status") != expected_status:
        return False
    hypotheses = _artifact_hypotheses(artifact)
    if not hypotheses or not all(
        _hypothesis_matches_member(
            hypothesis,
            member,
            subject_surfaces=subject_surfaces,
        )
        for hypothesis in hypotheses
    ):
        return False
    actual_values = {item.get("normalized_value") for item in hypotheses}
    if "values" in member:
        expected_values = member.get("values")
        return (
            isinstance(expected_values, list)
            and set(expected_values) == actual_values
            and len(actual_values) == len(expected_values)
        )
    return actual_values == {member.get("value")}


def _reschedule_artifact(
    projection: Mapping[str, Any],
    member: Mapping[str, Any],
    *,
    subject_surfaces: Mapping[str, str],
) -> tuple[str, str] | None:
    artifacts = {
        str(item.get("artifact_id")): item
        for item in projection.get("artifacts", [])
        if isinstance(item, Mapping)
    }
    role = member.get("lifecycle")
    for group in projection.get("groups", []):
        if (
            not isinstance(group, Mapping)
            or group.get("kind") != "reschedule"
            or group.get("coverage") != "complete"
        ):
            continue
        for group_member in group.get("members", []):
            if (
                not isinstance(group_member, Mapping)
                or group_member.get("role") != role
            ):
                continue
            artifact_ids = group_member.get("artifact_ids")
            if not isinstance(artifact_ids, list) or len(artifact_ids) != 1:
                continue
            artifact_id = str(artifact_ids[0])
            artifact = artifacts.get(artifact_id)
            if artifact is None:
                continue
            if _exact_artifact_match(
                artifact,
                member,
                subject_surfaces=subject_surfaces,
            ):
                group_id = group.get("group_id")
                if isinstance(group_id, str) and group_id:
                    return artifact_id, group_id
    return None


def _validate_gold(gold: Mapping[str, Any]) -> None:
    if (
        set(gold) != {"version", "created_before_predictions", "cases"}
        or gold.get("version") != GOLD_VERSION
        or gold.get("created_before_predictions") is not True
        or not isinstance(gold.get("cases"), list)
        or not gold["cases"]
    ):
        raise PublicChallengeError("public semantic gold schema is invalid")
    seen_cases: set[str] = set()
    positive_cases = 0
    negative_cases = 0
    for row in gold["cases"]:
        if (
            not isinstance(row, Mapping)
            or not {"case_id", "members"} <= set(row)
            or not set(row)
            <= {"case_id", "members", "forbidden", "complete_group_required"}
            or not isinstance(row.get("case_id"), str)
            or _CASE_ID_RE.fullmatch(row["case_id"]) is None
            or row["case_id"] in seen_cases
            or not isinstance(row.get("members"), list)
            or (
                "complete_group_required" in row
                and not isinstance(row["complete_group_required"], bool)
            )
        ):
            raise PublicChallengeError("public semantic gold case schema is invalid")
        seen_cases.add(row["case_id"])
        members = row["members"]
        positive_cases += int(bool(members))
        negative_cases += int(not members)
        forbidden = row.get("forbidden", [])
        if (
            not isinstance(forbidden, list)
            or len(forbidden) != len(set(forbidden))
            or any(
                not isinstance(value, str)
                or _NORMALIZED_TEMPORAL_VALUE_RE.fullmatch(value) is None
                for value in forbidden
            )
        ):
            raise PublicChallengeError(
                "public semantic gold forbidden values are invalid"
            )
        seen_members: set[bytes] = set()
        expected_values: set[str] = set()
        for member in members:
            if not isinstance(member, Mapping):
                raise PublicChallengeError("public semantic gold member is invalid")
            keys = set(member)
            has_value = "value" in member
            has_values = "values" in member
            if (
                not {"subject", "relation", "lifecycle"} <= keys
                or not keys
                <= {
                    "subject",
                    "relation",
                    "lifecycle",
                    "value",
                    "values",
                    "expected_verdict",
                }
                or has_value == has_values
                or _normalized_subject(member.get("subject")) is None
                or member.get("relation") not in {"occurrence", "deadline"}
                or member.get("lifecycle")
                not in {
                    "none",
                    "unknown",
                    "scheduled",
                    "cancelled",
                    "completed",
                    "rescheduled_old",
                    "rescheduled_replacement",
                }
                or member.get("expected_verdict", "supported")
                not in {"supported", "uncertain"}
            ):
                raise PublicChallengeError(
                    "public semantic gold member schema is invalid"
                )
            values = member.get("values") if has_values else [member.get("value")]
            if (
                not isinstance(values, list)
                or not values
                or len(values) != len(set(values))
                or any(
                    not isinstance(value, str)
                    or _NORMALIZED_TEMPORAL_VALUE_RE.fullmatch(value) is None
                    for value in values
                )
                or (has_values and member.get("expected_verdict") != "uncertain")
            ):
                raise PublicChallengeError(
                    "public semantic gold value schema is invalid"
                )
            expected_values.update(values)
            identity = _canonical_json(dict(member))
            if identity in seen_members:
                raise PublicChallengeError("public semantic gold member is duplicated")
            seen_members.add(identity)
        if expected_values & set(forbidden):
            raise PublicChallengeError("public semantic gold contradicts itself")
    if positive_cases == 0 or negative_cases == 0:
        raise PublicChallengeError("public semantic gold denominators are vacuous")


def score_public_challenge(
    challenge_path: Path,
    gold_path: Path,
    hmac_key_path: Path,
    output_root: Path,
) -> dict[str, Any]:
    """Open gold only after a complete authenticated prediction result exists."""

    key = _key(hmac_key_path)
    _private_directory(output_root)
    challenge, challenge_raw = _load_challenge(challenge_path, key=key)
    plan, plan_raw = _load_signed_artifact(
        output_root / "plan.json",
        key=key,
        domain=PLAN_DOMAIN,
        signature_field="plan_hmac_sha256",
        label="public challenge plan",
    )
    seal, seal_raw = _load_signed_artifact(
        output_root / "prediction-seal.json",
        key=key,
        domain=PREDICTION_SEAL_DOMAIN,
        signature_field="seal_hmac_sha256",
        label="prediction seal",
    )
    result, result_raw = _load_signed_artifact(
        output_root / "results.json",
        key=key,
        domain=RESULT_DOMAIN,
        signature_field="result_hmac_sha256",
        label="public challenge result",
    )
    cases = _prepare_cases(challenge)
    units = bounded_public_call_units(_request_rows(cases))
    claims, plan_calls = _validate_plan_authority(
        plan,
        challenge=challenge,
        challenge_raw=challenge_raw,
        cases=cases,
        units=units,
    )
    calls = _validate_call_evidence(
        output_root=output_root,
        key=key,
        challenge=challenge,
        plan_calls=plan_calls,
        units=units,
        claims=claims,
    )
    components, authorities = _validate_component_evidence(
        output_root=output_root,
        challenge=challenge,
        cases=cases,
        calls=calls,
    )
    _validate_seal_authority(
        seal,
        key=key,
        challenge=challenge,
        challenge_raw=challenge_raw,
        plan_raw=plan_raw,
        cases=cases,
        calls=calls,
        components=components,
        claims=claims,
    )
    result_rows = _validate_persisted_results(
        result,
        challenge=challenge,
        challenge_raw=challenge_raw,
        plan_raw=plan_raw,
        seal_raw=seal_raw,
        cases=cases,
        components=components,
        calls=calls,
        claims=claims,
    )
    plan_at = _aware_timestamp(plan.get("created_at"))
    seal_at = _aware_timestamp(seal.get("sealed_at"))
    result_at = _aware_timestamp(result.get("completed_at"))
    if (
        plan_at is None
        or seal_at is None
        or result_at is None
        or seal_at < plan_at
        or result_at < seal_at
        or any(
            (_aware_timestamp(call.started_at) or seal_at) < plan_at
            or (_aware_timestamp(call.completed_at) or plan_at) > seal_at
            for call in calls
        )
    ):
        raise PublicChallengeError("prediction chronology or authority is invalid")
    challenge_sha = _sha256(challenge_raw)
    gold_raw = _private_file(gold_path)
    if _sha256(gold_raw) != challenge["gold_sha256"]:
        raise PublicChallengeError("gold commitment does not match challenge freeze")
    gold = _strict_json(gold_raw, label="public semantic gold")
    _validate_gold(gold)
    selected_ids = [str(item["case_id"]) for item in challenge["cases"]]
    gold_rows = {
        str(item.get("case_id")): item
        for item in gold.get("cases", [])
        if isinstance(item, Mapping)
    }
    if any(case_id not in gold_rows for case_id in selected_ids):
        raise PublicChallengeError("semantic gold does not cover every selected case")
    if not any(gold_rows[case_id]["members"] for case_id in selected_ids) or not any(
        not gold_rows[case_id]["members"] for case_id in selected_ids
    ):
        raise PublicChallengeError("selected semantic gold denominators are vacuous")
    if set(result_rows) != set(selected_ids):
        raise PublicChallengeError("prediction result case coverage is invalid")

    gold_members = 0
    supported_gold_members = 0
    matched_members = 0
    confirmed_members = 0
    total_artifacts = 0
    supported_artifacts = 0
    matched_artifact_ids: set[tuple[str, str]] = set()
    cluster_reviews = 0
    negative_cases = 0
    selected_negative_cases = 0
    complete_group_cases = 0
    complete_group_cases_recovered = 0
    forbidden_hypotheses = 0
    per_case: list[dict[str, Any]] = []
    for case_id in selected_ids:
        gold_row = gold_rows[case_id]
        members = gold_row.get("members")
        if not isinstance(members, list):
            raise PublicChallengeError("semantic gold member schema is invalid")
        prediction = result_rows[case_id]
        projection = prediction.get("projection")
        subject_surfaces = _authority_subject_surfaces(authorities[case_id])
        artifacts: list[Mapping[str, Any]] = []
        reviews: list[Mapping[str, Any]] = []
        if projection is not None:
            if not isinstance(projection, Mapping):
                raise PublicChallengeError("prediction projection is invalid")
            artifacts = [
                item
                for item in projection.get("artifacts", [])
                if isinstance(item, Mapping)
            ]
            reviews = [
                item
                for item in projection.get("cluster_reviews", [])
                if isinstance(item, Mapping)
            ]
        total_artifacts += len(artifacts)
        supported_artifacts += sum(
            item.get("evidence_status") == "supported" for item in artifacts
        )
        cluster_reviews += len(reviews)
        if not members:
            negative_cases += 1
            selected_negative_cases += int(bool(artifacts or reviews))
        forbidden_values = set(gold_row.get("forbidden", []))
        case_forbidden = sum(
            hypothesis.get("normalized_value") in forbidden_values
            for artifact in artifacts
            for hypothesis in _artifact_hypotheses(artifact)
        )
        forbidden_hypotheses += case_forbidden
        available = {
            str(item.get("artifact_id")): item
            for item in artifacts
            if isinstance(item.get("artifact_id"), str)
        }
        case_matches = 0
        case_confirmed = 0
        complete_required = gold_row.get("complete_group_required") is True
        if complete_required:
            complete_group_cases += 1
        reschedule_matches = 0
        reschedule_group_ids: set[str] = set()
        for member in members:
            if not isinstance(member, Mapping):
                raise PublicChallengeError("semantic gold member is invalid")
            gold_members += 1
            expected_verdict = member.get("expected_verdict", "supported")
            supported_gold_members += int(expected_verdict != "uncertain")
            matched_id: str | None = None
            if (
                member.get("lifecycle")
                in {
                    "rescheduled_old",
                    "rescheduled_replacement",
                }
                and projection is not None
            ):
                reschedule_match = _reschedule_artifact(
                    projection,
                    member,
                    subject_surfaces=subject_surfaces,
                )
                if reschedule_match is not None:
                    matched_id, group_id = reschedule_match
                    reschedule_group_ids.add(group_id)
                    reschedule_matches += 1
            else:
                for artifact_id, artifact in available.items():
                    if (case_id, artifact_id) in matched_artifact_ids:
                        continue
                    if _exact_artifact_match(
                        artifact,
                        member,
                        subject_surfaces=subject_surfaces,
                    ):
                        matched_id = artifact_id
                        break
            if matched_id is None or (case_id, matched_id) in matched_artifact_ids:
                continue
            matched_artifact_ids.add((case_id, matched_id))
            matched_members += 1
            case_matches += 1
            artifact = available[matched_id]
            if (
                expected_verdict != "uncertain"
                and artifact.get("evidence_status") == "supported"
            ):
                confirmed_members += 1
                case_confirmed += 1
        if (
            complete_required
            and reschedule_matches == len(members)
            and len(reschedule_group_ids) == 1
        ):
            complete_group_cases_recovered += 1
        per_case.append(
            {
                "case_id": case_id,
                "gold_members": len(members),
                "matched_members": case_matches,
                "confirmed_members": case_confirmed,
                "artifacts": len(artifacts),
                "cluster_reviews": len(reviews),
                "negative_selected": bool(not members and (artifacts or reviews)),
                "forbidden_hypotheses": case_forbidden,
            }
        )
    matched_supported_artifacts = sum(
        result_rows[case_id]["projection"] is not None
        and any(
            isinstance(item, Mapping)
            and item.get("artifact_id") == artifact_id
            and item.get("evidence_status") == "supported"
            for item in result_rows[case_id]["projection"].get("artifacts", [])
        )
        for case_id, artifact_id in matched_artifact_ids
    )
    effective_recall = matched_members / gold_members if gold_members else 1.0
    confirmed_recall = (
        confirmed_members / supported_gold_members if supported_gold_members else 1.0
    )
    supported_precision = (
        matched_supported_artifacts / supported_artifacts
        if supported_artifacts
        else (1.0 if supported_gold_members == 0 else 0.0)
    )
    review_outputs = total_artifacts + cluster_reviews
    review_precision = (
        matched_members / review_outputs
        if review_outputs
        else (1.0 if gold_members == 0 else 0.0)
    )
    complete_group_recall = (
        complete_group_cases_recovered / complete_group_cases
        if complete_group_cases
        else 1.0
    )
    gate = {
        "all_members_recovered": matched_members == gold_members,
        "all_supported_members_confirmed": (
            confirmed_members == supported_gold_members
        ),
        "perfect_supported_precision": supported_precision == 1.0,
        "perfect_review_precision": review_precision == 1.0,
        "complete_structural_groups": (
            complete_group_cases_recovered == complete_group_cases
        ),
        "no_selected_hard_negatives": selected_negative_cases == 0,
        "no_forbidden_hypotheses": forbidden_hypotheses == 0,
        "restricted_external_execution": (
            claims.restricted_execution
            and claims.external_call_started
            and not claims.test_invoker_used
        ),
    }
    score = _signed(
        {
            "version": SCORE_VERSION,
            "launcher_version": VERSION,
            "challenge_id": challenge["challenge_id"],
            "challenge_manifest_sha256": challenge_sha,
            "gold_sha256": _sha256(gold_raw),
            "prediction_seal_sha256": _sha256(seal_raw),
            "result_sha256": _sha256(result_raw),
            "gold_opened_after_prediction_seal": True,
            "gold_members": gold_members,
            "supported_gold_members": supported_gold_members,
            "matched_members": matched_members,
            "confirmed_members": confirmed_members,
            "artifacts": total_artifacts,
            "supported_artifacts": supported_artifacts,
            "matched_artifacts": len(matched_artifact_ids),
            "cluster_reviews": cluster_reviews,
            "negative_cases": negative_cases,
            "selected_negative_cases": selected_negative_cases,
            "forbidden_hypotheses": forbidden_hypotheses,
            "complete_group_cases": complete_group_cases,
            "complete_group_cases_recovered": complete_group_cases_recovered,
            "effective_member_recall": effective_recall,
            "confirmed_member_recall": confirmed_recall,
            "supported_artifact_precision": supported_precision,
            "review_output_precision": review_precision,
            "complete_group_recall": complete_group_recall,
            "cases": per_case,
            "gates": gate,
            "smoke_gate_passed": all(gate.values()),
            "public_synthetic": True,
            "release_eligible": False,
            "scored_at": _now(),
        },
        key=key,
        domain=SCORE_DOMAIN,
        signature_field="score_hmac_sha256",
    )
    _write_private_new(output_root / "score.json", _canonical_json(score) + b"\n")
    return {
        "version": SCORE_VERSION,
        "status": "complete",
        "gold_members": gold_members,
        "matched_members": matched_members,
        "supported_gold_members": supported_gold_members,
        "confirmed_members": confirmed_members,
        "artifacts": total_artifacts,
        "supported_artifacts": supported_artifacts,
        "cluster_reviews": cluster_reviews,
        "negative_cases": negative_cases,
        "selected_negative_cases": selected_negative_cases,
        "forbidden_hypotheses": forbidden_hypotheses,
        "effective_member_recall": effective_recall,
        "confirmed_member_recall": confirmed_recall,
        "supported_artifact_precision": supported_precision,
        "review_output_precision": review_precision,
        "complete_group_recall": complete_group_recall,
        "smoke_gate_passed": all(gate.values()),
        "gold_opened_after_prediction_seal": True,
        "public_synthetic": True,
        "release_eligible": False,
        "test_invoker_used": claims.test_invoker_used,
        "private_content_printed": False,
    }


def _safe_failure(phase: str) -> dict[str, Any]:
    return {
        "version": VERSION,
        "phase": phase,
        "status": "failed",
        "error": "public_temporal_challenge_failed",
        "public_synthetic": True,
        "release_eligible": False,
        "private_content_printed": False,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="phase", required=True)
    run = subparsers.add_parser("run")
    run.add_argument("--challenge", type=Path, required=True)
    run.add_argument("--hmac-key", type=Path, required=True)
    run.add_argument("--output-root", type=Path, required=True)
    run.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS)
    run.add_argument("--codex-binary")
    score = subparsers.add_parser("score")
    score.add_argument("--challenge", type=Path, required=True)
    score.add_argument("--gold", type=Path, required=True)
    score.add_argument("--hmac-key", type=Path, required=True)
    score.add_argument("--output-root", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.phase == "run":
            result = run_public_challenge(
                args.challenge,
                args.hmac_key,
                args.output_root,
                timeout_seconds=args.timeout,
                codex_binary=args.codex_binary,
            )
        else:
            result = score_public_challenge(
                args.challenge,
                args.gold,
                args.hmac_key,
                args.output_root,
            )
    except (PublicChallengeError, OSError, ValueError):
        print(json.dumps(_safe_failure(str(args.phase)), sort_keys=True))
        raise SystemExit(2) from None
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))


if __name__ == "__main__":
    main()
