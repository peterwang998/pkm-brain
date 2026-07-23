#!/usr/bin/env python3
"""Execute one sealed Gmail fact-parity packet through production Brain.

This adapter intentionally owns no benchmark-stage semantics.  It renders the
same admitted message body for both arms, invokes the selected production API
inside a disposable Brain home, and returns only production candidates plus
run-scoped persistence evidence.  The parent runner derives stage membership.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any


REQUEST_VERSION = "gmail_fact_parity_adapter_request_v1"
RESPONSE_VERSION = "gmail_fact_parity_adapter_response_v1"
PACKET_VERSION = "gmail_fact_parity_packet_v2"
PRODUCTION_API = "pkm_brain.extraction.extract_recent_documents"
CONDITIONAL_ADMISSION_VERSION = "gmail_fact_parity_conditional_admission_v1"
PRIVATE_FILE_MODE = 0o600
PRIVATE_DIRECTORY_MODE = 0o700
DEFAULT_TIMEOUT_SECONDS = 900

_REQUEST_KEYS = {"version", "run_id", "arm", "packet", "runtime_config"}
_RUNTIME_KEYS = {
    "production_api",
    "shadow",
    "isolated_disposable_home",
    "packet_policy",
    "model",
    "reasoning_effort",
    "prompt_version",
}
_PACKET_KEYS = {
    "version",
    "packet_id",
    "thread_id",
    "revision_id",
    "projection_version",
    "classifier_version",
    "messages",
}
_MESSAGE_KEYS = {"message_id", "internal_date", "text"}
_RESPONSE_KEYS = {
    "version",
    "packet_id",
    "thread_id",
    "production_api",
    "prompt_version",
    "members",
    "invocations",
}
_OPAQUE_PATTERNS = {
    "packet_id": re.compile(r"^gfp_p_[0-9a-f]{32}$"),
    "thread_id": re.compile(r"^gfp_t_[0-9a-f]{32}$"),
    "revision_id": re.compile(r"^gfp_r_[0-9a-f]{32}$"),
    "message_id": re.compile(r"^gfp_m_[0-9a-f]{32}$"),
}
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_INVOCATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_HEADING_RE = re.compile(
    r"^## Message (?P<ordinal>[1-9][0-9]*) — (?P<date>.+?) — "
    r"(?P<message_id>gfp_m_[0-9a-f]{32})$"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ROLE_NAMES = (
    "EXTRACTOR",
    "RESOLVER",
    "GARDENER",
    "SYNTHESIZER",
    "CRITIC",
    "AUDITOR",
)


class GmailFactParityAdapterError(ValueError):
    """Raised when production provenance cannot be established safely."""


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
        raise GmailFactParityAdapterError("value is not canonical JSON") from exc


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read_private_file(path: Path) -> bytes:
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise GmailFactParityAdapterError("request is not a regular file")
        if stat.S_IMODE(metadata.st_mode) != PRIVATE_FILE_MODE:
            raise GmailFactParityAdapterError("request must have mode 0600")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            return handle.read()
    except GmailFactParityAdapterError:
        raise
    except OSError as exc:
        raise GmailFactParityAdapterError("request could not be read safely") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _write_private_new(path: Path, payload: bytes) -> None:
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise GmailFactParityAdapterError("response parent is unsafe")
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
        raise GmailFactParityAdapterError("response write failed") from exc


def _aware_timestamp(value: Any, *, label: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 80:
        raise GmailFactParityAdapterError(f"{label} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise GmailFactParityAdapterError(f"{label} is invalid") from exc
    if parsed.tzinfo is None:
        raise GmailFactParityAdapterError(f"{label} lacks a timezone")
    return value


def _exact_mapping(value: Any, keys: set[str], *, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise GmailFactParityAdapterError(f"{label} schema is invalid")
    return value


def validate_request(value: Any) -> dict[str, Any]:
    request = _exact_mapping(value, _REQUEST_KEYS, label="request")
    if request.get("version") != REQUEST_VERSION:
        raise GmailFactParityAdapterError("request version is invalid")
    run_id = request.get("run_id")
    if not isinstance(run_id, str) or _RUN_ID_RE.fullmatch(run_id) is None:
        raise GmailFactParityAdapterError("run ID is invalid")
    arm = request.get("arm")
    if arm not in {"original", "v2"}:
        raise GmailFactParityAdapterError("adapter arm is invalid")

    runtime = _exact_mapping(
        request.get("runtime_config"), _RUNTIME_KEYS, label="runtime configuration"
    )
    if (
        runtime.get("production_api") != PRODUCTION_API
        or runtime.get("shadow") is not False
        or runtime.get("isolated_disposable_home") is not True
        or runtime.get("packet_policy") != "identical_sealed_packet"
    ):
        raise GmailFactParityAdapterError("runtime configuration is invalid")
    for key in ("model", "reasoning_effort", "prompt_version"):
        if not isinstance(runtime.get(key), str) or not str(runtime[key]).strip():
            raise GmailFactParityAdapterError(f"runtime {key} is invalid")

    packet = _exact_mapping(request.get("packet"), _PACKET_KEYS, label="packet")
    if packet.get("version") != PACKET_VERSION:
        raise GmailFactParityAdapterError("packet version is invalid")
    for key in ("packet_id", "thread_id", "revision_id"):
        pattern = _OPAQUE_PATTERNS[key]
        item = packet.get(key)
        if not isinstance(item, str) or pattern.fullmatch(item) is None:
            raise GmailFactParityAdapterError(f"packet {key} is invalid")
    for key in ("projection_version", "classifier_version"):
        item = packet.get(key)
        if isinstance(item, bool) or not isinstance(item, int) or item < 1:
            raise GmailFactParityAdapterError(f"packet {key} is invalid")
    messages = packet.get("messages")
    if not isinstance(messages, list) or not messages:
        raise GmailFactParityAdapterError("packet messages are invalid")
    seen: set[str] = set()
    for raw_message in messages:
        message = _exact_mapping(raw_message, _MESSAGE_KEYS, label="message")
        message_id = message.get("message_id")
        if (
            not isinstance(message_id, str)
            or _OPAQUE_PATTERNS["message_id"].fullmatch(message_id) is None
            or message_id in seen
        ):
            raise GmailFactParityAdapterError("message identity is invalid")
        seen.add(message_id)
        internal_date = _aware_timestamp(
            message.get("internal_date"), label="message internal_date"
        )
        text = message.get("text")
        if not isinstance(text, str) or not text or "\x00" in text:
            raise GmailFactParityAdapterError("message text is invalid")
        first_line = text.split("\n", 1)[0]
        heading = _HEADING_RE.fullmatch(first_line)
        if (
            heading is None
            or heading.group("message_id") != message_id
            or heading.group("date") != internal_date
        ):
            raise GmailFactParityAdapterError("message heading is invalid")
    return {
        "version": REQUEST_VERSION,
        "run_id": run_id,
        "arm": arm,
        "packet": dict(packet),
        "runtime_config": dict(runtime),
    }


def render_packet_body(
    packet: Mapping[str, Any],
) -> tuple[str, list[dict[str, Any]]]:
    """Render the byte-identical admitted body shared by both production arms."""

    heading = f"# Email thread: {packet['thread_id']}"
    body_parts = [heading]
    ranges: list[dict[str, Any]] = []
    cursor = len(heading)
    for ordinal, message in enumerate(packet["messages"], start=1):
        _first_line, separator, remainder = str(message["text"]).partition("\n")
        rendered = (
            f"## Message {ordinal} — {message['internal_date']} — "
            f"{message['message_id']}"
        )
        if separator:
            rendered += separator + remainder
        start = cursor + 2
        end = start + len(rendered)
        ranges.append(
            {
                "message_id": message["message_id"],
                "internal_date": message["internal_date"],
                "start_offset": start,
                "end_offset": end,
            }
        )
        body_parts.append(rendered)
        cursor = end
    return "\n\n".join(body_parts), ranges


def _write_private_text(path: Path, text: str, *, executable: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(PRIVATE_DIRECTORY_MODE)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o700 if executable else PRIVATE_FILE_MODE)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
    path.chmod(0o700 if executable else PRIVATE_FILE_MODE)


def _json_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def write_original_source(home: Path, body: str, packet: Mapping[str, Any]) -> Path:
    # The original renderer has no trusted frontmatter.  Use the same opaque
    # thread title as V2's frontmatter so document title is not an avoidable
    # model-input confound between the two arms.
    source = home / "inbox" / "documents" / "fact-parity" / f"{packet['thread_id']}.md"
    _write_private_text(source, body)
    return source


def write_v2_source(
    home: Path,
    body: str,
    ranges: Sequence[Mapping[str, Any]],
    packet: Mapping[str, Any],
) -> Path:
    from pkm_brain.gmail_projection import (
        GMAIL_KNOWLEDGE_CLASSIFIER_VERSION,
        GMAIL_KNOWLEDGE_PROJECTION_VERSION,
        GMAIL_MESSAGE_POLICY_VERSION,
        gmail_projection_session_id,
    )

    if (
        packet.get("projection_version") != GMAIL_KNOWLEDGE_PROJECTION_VERSION
        or packet.get("classifier_version") != GMAIL_KNOWLEDGE_CLASSIFIER_VERSION
    ):
        raise GmailFactParityAdapterError(
            "packet renderer version does not match V2 production"
        )

    account_key = "gmail.fact-parity"
    source_revision = sha256_bytes(canonical_json(packet))
    session_id = gmail_projection_session_id(
        account_key=account_key,
        thread_id=str(packet["thread_id"]),
        source_revision=source_revision,
        projection_version=GMAIL_KNOWLEDGE_PROJECTION_VERSION,
    )
    message_ids = [str(item["message_id"]) for item in ranges]
    policies = [
        {
            "message_id": message_id,
            "delivery_kind": "human",
            "advertising_bases": [],
            "fact_admission_basis": "durable_human_candidate",
            "provider_important": False,
            "provider_starred": False,
            "human_signal_basis": "provider_category_personal",
            "operator_message_after": False,
        }
        for message_id in message_ids
    ]
    first_date = str(ranges[0]["internal_date"])
    last_date = str(ranges[-1]["internal_date"])
    frontmatter = [
        "---",
        f"title: {_json_string(str(packet['thread_id']))}",
        "source_type: gmail_thread",
        "source_trust: untrusted_external",
        f"created_at: {_json_string(first_date)}",
        f"source_updated_at: {_json_string(last_date)}",
        f"captured_at: {_json_string(last_date)}",
        f"archive_updated_at: {_json_string(last_date)}",
        f"gmail_account_key: {_json_string(account_key)}",
        f"gmail_thread_id: {_json_string(str(packet['thread_id']))}",
        f"gmail_source_revision: {_json_string(source_revision)}",
        f"gmail_projection_version: {GMAIL_KNOWLEDGE_PROJECTION_VERSION}",
        f"gmail_classifier_version: {GMAIL_KNOWLEDGE_CLASSIFIER_VERSION}",
        "gmail_provider_labels_available: true",
        "classification: human",
        "delivery_kind: human",
        "fact_importance: durable_candidate",
        "actionability: informational",
        "importance_confidence: 1.0",
        "gmail_human_signal_basis: provider_category_personal",
        "fact_admission_basis: durable_human_candidate",
        "fact_eligible: true",
        "gmail_message_timestamps_version: 1",
        f"gmail_message_ids: {canonical_json(message_ids).decode('utf-8')}",
        f"retained_message_count: {len(message_ids)}",
        f"gmail_message_timestamps: {canonical_json(list(ranges)).decode('utf-8')}",
        f"gmail_message_policy_version: {GMAIL_MESSAGE_POLICY_VERSION}",
        f"gmail_message_policies: {canonical_json(policies).decode('utf-8')}",
        "gmail_fact_admitted_message_ids: "
        f"{canonical_json(message_ids).decode('utf-8')}",
        "deleted: false",
        f"parity_admission_convention: {CONDITIONAL_ADMISSION_VERSION}",
        "---",
    ]
    source = home / "inbox" / "documents" / "gmail" / f"{session_id}.md"
    _write_private_text(source, "\n".join(frontmatter) + "\n\n" + body + "\n")
    return source


def _assert_package_authority(production_root: Path) -> None:
    import pkm_brain

    package_file = Path(str(pkm_brain.__file__ or "")).resolve()
    expected = (production_root / "src" / "pkm_brain").resolve()
    if package_file.parent != expected:
        raise GmailFactParityAdapterError("interpreter package authority is invalid")


def _prompt_version(arm: str) -> str:
    if arm == "original":
        from pkm_brain.extraction import EXTRACTION_PROMPT_VERSION

        return str(EXTRACTION_PROMPT_VERSION)
    try:
        from pkm_brain.extraction_contract import EXTRACTION_PROMPT_VERSION
    except ImportError:
        from pkm_brain.extraction import EXTRACTION_PROMPT_VERSION
    return str(EXTRACTION_PROMPT_VERSION)


def _real_codex_binary() -> Path:
    configured = (
        os.environ.get("PKM_BRAIN_FACT_PARITY_CODEX_BIN")
        or os.environ.get("PKM_BRAIN_CODEX_BIN")
        or shutil.which("codex")
    )
    if not configured:
        raise GmailFactParityAdapterError("external Codex is unavailable")
    path = Path(configured).expanduser().resolve()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise GmailFactParityAdapterError("external Codex is unavailable")
    return path


_CODEX_SHIM = r"""#!__PYTHON__
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat()


def value_after(arguments, option):
    try:
        return arguments[arguments.index(option) + 1]
    except (ValueError, IndexError):
        return None


def reasoning_effort(arguments):
    values = []
    for index, value in enumerate(arguments):
        if value in {"-c", "--config"} and index + 1 < len(arguments):
            values.append(arguments[index + 1])
        elif value.startswith("--config="):
            values.append(value.split("=", 1)[1])
    for value in values:
        match = re.fullmatch(
            r"model_reasoning_effort\s*=\s*[\"']?([A-Za-z0-9_-]+)[\"']?",
            value,
        )
        if match:
            return match.group(1).lower()
    return None


def session_id(output):
    resolved = None
    for raw_line in output.decode("utf-8", errors="replace").splitlines():
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        candidates = [event.get("thread_id"), event.get("session_id")]
        payload = event.get("payload")
        if isinstance(payload, dict):
            candidates.append(payload.get("session_id"))
        for candidate in candidates:
            if isinstance(candidate, str) and candidate:
                resolved = candidate
                break
    return resolved


def safe_read(path):
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError("response is not a regular file")
        with os.fdopen(descriptor, "rb") as handle:
            descriptor = None
            return handle.read()
    finally:
        if descriptor is not None:
            os.close(descriptor)


def append_ledger(row):
    path = Path(os.environ["PKM_BRAIN_FACT_PARITY_LEDGER"])
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        with os.fdopen(descriptor, "ab", closefd=True) as handle:
            descriptor = None
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            handle.write(
                json.dumps(
                    row,
                    ensure_ascii=False,
                    allow_nan=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
                + b"\n"
            )
            handle.flush()
            os.fsync(handle.fileno())
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        path.chmod(0o600)
    finally:
        if descriptor is not None:
            os.close(descriptor)


def main():
    real = os.environ["PKM_BRAIN_FACT_PARITY_REAL_CODEX_BIN"]
    arguments = sys.argv[1:]
    request = sys.stdin.buffer.read()
    started_at = now()
    try:
        completed = subprocess.run(
            [real, *arguments], input=request, capture_output=True, check=False
        )
    except Exception:
        if "exec" in arguments:
            append_ledger(
                {
                    "attestation_failure": "codex_process_error",
                    "request_sha256": hashlib.sha256(request).hexdigest(),
                }
            )
        return 89
    os.write(1, completed.stdout)
    os.write(2, completed.stderr)
    if "exec" not in arguments:
        return completed.returncode

    def poison(reason):
        # Deliberately not the accepted ledger schema.  Production can swallow
        # provider failures (for example, a critic can fail closed), so leaving
        # no row would let a partial ledger masquerade as complete provenance.
        # A poison row makes the adapter fail closed if execution continues.
        append_ledger(
            {
                "attestation_failure": reason,
                "request_sha256": hashlib.sha256(request).hexdigest(),
                "process_output_sha256": hashlib.sha256(
                    completed.stdout + completed.stderr
                ).hexdigest(),
            }
        )

    if completed.returncode != 0:
        poison("codex_nonzero_exit")
        return completed.returncode
    output_path = value_after(arguments, "--output-last-message")
    model = value_after(arguments, "--model")
    effort = reasoning_effort(arguments)
    invocation_id = session_id(completed.stdout)
    expected_model = os.environ["PKM_BRAIN_FACT_PARITY_EXPECTED_MODEL"]
    expected_effort = os.environ["PKM_BRAIN_FACT_PARITY_EXPECTED_EFFORT"]
    if (
        not output_path
        or not model
        or not effort
        or model != expected_model
        or effort != expected_effort
        or not invocation_id
        or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", invocation_id)
        is None
    ):
        poison("codex_invocation_identity_invalid")
        return 86
    try:
        response = safe_read(Path(output_path))
    except (OSError, RuntimeError):
        poison("codex_response_unavailable")
        return 87
    if not response:
        poison("codex_response_empty")
        return 88
    append_ledger(
        {
            "invocation_id": invocation_id,
            "request_sha256": hashlib.sha256(request).hexdigest(),
            "response_sha256": hashlib.sha256(response).hexdigest(),
            "provider": "external-codex",
            "model": model,
            "reasoning_effort": effort,
            "started_at": started_at,
            "completed_at": now(),
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
"""


def write_codex_shim(home: Path) -> tuple[Path, Path, Path]:
    real = _real_codex_binary()
    ledger = home / "logs" / "fact-parity-codex-ledger.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.parent.chmod(PRIVATE_DIRECTORY_MODE)
    shim = home / "bin" / "codex-fact-parity"
    source = _CODEX_SHIM.replace("__PYTHON__", str(Path(sys.executable).resolve()))
    _write_private_text(shim, source, executable=True)
    return shim, ledger, real


def configure_production_environment(
    *,
    home: Path,
    production_root: Path,
    model: str,
    reasoning_effort: str,
    timeout_seconds: int,
) -> Path:
    shim, ledger, real = write_codex_shim(home)
    for key in list(os.environ):
        if key.startswith(
            (
                "PKM_BRAIN_LLM_",
                "PKM_BRAIN_CODEX_",
                "PKM_BRAIN_OPENAI_",
                "PKM_BRAIN_ANTHROPIC_",
                "PKM_BRAIN_OLLAMA_",
            )
        ):
            os.environ.pop(key, None)
    os.environ.update(
        {
            "BRAIN_HOME": str(home),
            "PKM_BRAIN_LLM_PROVIDER": "codex",
            "PKM_BRAIN_CODEX_BIN": str(shim),
            "PKM_BRAIN_CODEX_CWD": str(production_root),
            "PKM_BRAIN_CODEX_MODEL": model,
            "PKM_BRAIN_CODEX_MODEL_FALLBACKS": "",
            "PKM_BRAIN_CODEX_REASONING_EFFORT": reasoning_effort,
            "PKM_BRAIN_CODEX_TIMEOUT_SECONDS": str(timeout_seconds),
            "PKM_BRAIN_FACT_PARITY_REAL_CODEX_BIN": str(real),
            "PKM_BRAIN_FACT_PARITY_LEDGER": str(ledger),
            "PKM_BRAIN_FACT_PARITY_EXPECTED_MODEL": model,
            "PKM_BRAIN_FACT_PARITY_EXPECTED_EFFORT": reasoning_effort,
        }
    )
    for role in _ROLE_NAMES:
        os.environ[f"PKM_BRAIN_LLM_{role}_PROVIDER"] = "codex"
        os.environ[f"PKM_BRAIN_LLM_{role}_MODEL"] = model
        os.environ[f"PKM_BRAIN_LLM_{role}_MODEL_FALLBACKS"] = ""
        os.environ[f"PKM_BRAIN_LLM_{role}_REASONING_EFFORT"] = reasoning_effort
    return ledger


def _packet_run_id(run_id: str, packet_id: str) -> str:
    digest = sha256_bytes(canonical_json({"run_id": run_id, "packet_id": packet_id}))
    return f"gfp_{digest[:24]}"


def _active_document_id(paths: Any, source: Path) -> str:
    from pkm_brain.db import connection

    with connection(paths.sqlite_path) as conn:
        rows = conn.execute(
            """
            SELECT id
            FROM documents
            WHERE source_path = ? AND status = 'active'
            ORDER BY ingested_at, id
            """,
            (str(source.resolve()),),
        ).fetchall()
    if len(rows) != 1:
        raise GmailFactParityAdapterError("source did not resolve to one document")
    return str(rows[0]["id"])


def _document_chunks(paths: Any, document_id: str) -> dict[str, dict[str, Any]]:
    from pkm_brain.db import connection

    with connection(paths.sqlite_path) as conn:
        rows = conn.execute(
            """
            SELECT id, start_offset, end_offset, text
            FROM chunks
            WHERE document_id = ?
            ORDER BY chunk_index, id
            """,
            (document_id,),
        ).fetchall()
    output = {
        str(row["id"]): {
            "chunk_id": str(row["id"]),
            "start_offset": row["start_offset"],
            "end_offset": row["end_offset"],
            "text": str(row["text"]),
        }
        for row in rows
    }
    if not output:
        raise GmailFactParityAdapterError("production ingestion emitted no chunks")
    return output


def evidence_message_ids(
    candidate: Mapping[str, Any],
    *,
    chunks: Mapping[str, Mapping[str, Any]],
    body: str,
    message_ranges: Sequence[Mapping[str, Any]],
    packet_message_ids: Sequence[str],
) -> list[str]:
    spans = candidate.get("source_spans")
    if not isinstance(spans, list) or not spans:
        raise GmailFactParityAdapterError("candidate has no deterministic evidence")
    resolved: set[str] = set()
    for span in spans:
        if not isinstance(span, Mapping):
            raise GmailFactParityAdapterError("candidate evidence span is invalid")
        chunk = chunks.get(str(span.get("chunk_id") or ""))
        if chunk is None:
            raise GmailFactParityAdapterError("candidate evidence chunk is unknown")
        start = span.get("start")
        end = span.get("end")
        chunk_start = chunk.get("start_offset")
        chunk_end = chunk.get("end_offset")
        chunk_text = str(chunk.get("text") or "")
        if (
            isinstance(start, bool)
            or not isinstance(start, int)
            or isinstance(end, bool)
            or not isinstance(end, int)
            or isinstance(chunk_start, bool)
            or not isinstance(chunk_start, int)
            or isinstance(chunk_end, bool)
            or not isinstance(chunk_end, int)
            or start < 0
            or end <= start
            or end > len(chunk_text)
            or chunk_start < 0
            or chunk_end <= chunk_start
            or chunk_end > len(body)
        ):
            raise GmailFactParityAdapterError("candidate evidence span is invalid")
        source_slice = body[chunk_start:chunk_end]
        relative_origin = source_slice.find(chunk_text)
        if (
            relative_origin < 0
            or source_slice.find(chunk_text, relative_origin + 1) >= 0
        ):
            raise GmailFactParityAdapterError("chunk offset provenance is ambiguous")
        absolute_start = chunk_start + relative_origin + start
        absolute_end = chunk_start + relative_origin + end
        if body[absolute_start:absolute_end] != chunk_text[start:end]:
            raise GmailFactParityAdapterError("chunk offset provenance is invalid")
        matches = [
            item
            for item in message_ranges
            if int(item["start_offset"]) <= absolute_start
            and absolute_end <= int(item["end_offset"])
        ]
        if len(matches) != 1:
            raise GmailFactParityAdapterError("candidate evidence message is ambiguous")
        resolved.add(str(matches[0]["message_id"]))
    ordered = [item for item in packet_message_ids if item in resolved]
    if not ordered or len(ordered) != len(resolved):
        raise GmailFactParityAdapterError("candidate evidence message is invalid")

    metadata = candidate.get("metadata")
    if isinstance(metadata, Mapping):
        claimed: list[str] = []
        if metadata.get("gmail_message_id") is not None:
            claimed.append(str(metadata["gmail_message_id"]))
        if metadata.get("gmail_message_ids") is not None:
            values = metadata["gmail_message_ids"]
            if not isinstance(values, list) or any(
                not isinstance(item, str) for item in values
            ):
                raise GmailFactParityAdapterError("candidate Gmail metadata is invalid")
            claimed.extend(values)
        if any(item not in resolved for item in claimed):
            raise GmailFactParityAdapterError(
                "candidate Gmail metadata is inconsistent"
            )
    return ordered


def _run_persistence_rows(paths: Any, run_id: str) -> tuple[list[Any], list[Any]]:
    from pkm_brain.cos_actions import row_to_action
    from pkm_brain.db import connection
    from pkm_brain.wiki_facts import row_to_fact

    with connection(paths.sqlite_path) as conn:
        action_rows = conn.execute(
            """
            SELECT *
            FROM cos_actions
            WHERE run_id = ?
            ORDER BY created_at, id
            """,
            (run_id,),
        ).fetchall()
        actions = [row_to_action(row) for row in action_rows]
        target_ids = sorted(
            {
                str(fact_id)
                for action in actions
                for fact_id in action.get("target_fact_ids") or []
                if str(fact_id)
            }
        )
        facts = []
        for fact_id in target_ids:
            row = conn.execute(
                "SELECT * FROM facts WHERE id = ?", (fact_id,)
            ).fetchone()
            if row is not None:
                facts.append(row_to_fact(row))
    fact_ids = {str(item["id"]) for item in facts}
    for action in actions:
        if action.get("status") in {"applied", "auto_applied"} and any(
            str(item) not in fact_ids for item in action.get("target_fact_ids") or []
        ):
            raise GmailFactParityAdapterError("applied action persistence is missing")
    return actions, facts


def _read_invocations(
    ledger: Path, *, model: str, reasoning_effort: str
) -> list[dict[str, Any]]:
    try:
        raw_lines = _read_private_file(ledger).splitlines()
    except GmailFactParityAdapterError as exc:
        raise GmailFactParityAdapterError(
            "Codex invocation ledger is unavailable"
        ) from exc
    if not raw_lines:
        raise GmailFactParityAdapterError("Codex invocation ledger is empty")
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    previous_started: datetime | None = None
    expected_keys = {
        "invocation_id",
        "request_sha256",
        "response_sha256",
        "provider",
        "model",
        "reasoning_effort",
        "started_at",
        "completed_at",
    }
    for raw_line in raw_lines:
        try:
            item = json.loads(raw_line.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise GmailFactParityAdapterError(
                "Codex invocation ledger is invalid"
            ) from exc
        if not isinstance(item, Mapping) or set(item) != expected_keys:
            raise GmailFactParityAdapterError("Codex invocation schema is invalid")
        invocation_id = item.get("invocation_id")
        if (
            not isinstance(invocation_id, str)
            or _INVOCATION_ID_RE.fullmatch(invocation_id) is None
            or invocation_id in seen
            or item.get("provider") != "external-codex"
            or item.get("model") != model
            or item.get("reasoning_effort") != reasoning_effort
            or _SHA256_RE.fullmatch(str(item.get("request_sha256") or "")) is None
            or _SHA256_RE.fullmatch(str(item.get("response_sha256") or "")) is None
        ):
            raise GmailFactParityAdapterError("Codex invocation provenance is invalid")
        seen.add(invocation_id)
        started = _parse_ledger_time(item.get("started_at"))
        completed = _parse_ledger_time(item.get("completed_at"))
        if completed < started or (
            previous_started is not None and started < previous_started
        ):
            raise GmailFactParityAdapterError("Codex invocation chronology is invalid")
        previous_started = started
        rows.append(dict(item))
    count = len(rows)
    return [
        {**item, "window_index": index, "window_count": count}
        for index, item in enumerate(rows)
    ]


def _parse_ledger_time(value: Any) -> datetime:
    _aware_timestamp(value, label="Codex invocation timestamp")
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def execute_request(
    request: Mapping[str, Any], *, production_root: Path
) -> dict[str, Any]:
    request = validate_request(request)
    production_root = production_root.resolve()
    _assert_package_authority(production_root)
    prompt_version = _prompt_version(str(request["arm"]))
    runtime = request["runtime_config"]
    if prompt_version != runtime["prompt_version"]:
        raise GmailFactParityAdapterError("production prompt version is invalid")

    packet = request["packet"]
    body, message_ranges = render_packet_body(packet)
    timeout_seconds = int(
        os.environ.get("PKM_BRAIN_FACT_PARITY_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS)
    )
    if timeout_seconds < 1:
        raise GmailFactParityAdapterError("adapter timeout is invalid")

    previous_environment = dict(os.environ)
    previous_umask = os.umask(0o077)
    try:
        with tempfile.TemporaryDirectory(
            prefix="gmail-fact-parity-adapter-"
        ) as raw_home:
            home = Path(raw_home)
            home.chmod(PRIVATE_DIRECTORY_MODE)
            ledger = configure_production_environment(
                home=home,
                production_root=production_root,
                model=str(runtime["model"]),
                reasoning_effort=str(runtime["reasoning_effort"]),
                timeout_seconds=timeout_seconds,
            )
            from pkm_brain.extraction import extract_recent_documents
            from pkm_brain.paths import BrainPaths
            from pkm_brain.service import BrainService

            paths = BrainPaths.from_value(home)
            service = BrainService(paths)
            service.init_workspace()
            source = (
                write_original_source(home, body, packet)
                if request["arm"] == "original"
                else write_v2_source(home, body, message_ranges, packet)
            )
            ingestion = service.ingest(source=source)
            if ingestion.errors or ingestion.changed != 1:
                raise GmailFactParityAdapterError("production ingestion failed")
            document_id = _active_document_id(paths, source)
            scoped_run_id = _packet_run_id(
                str(request["run_id"]), str(packet["packet_id"])
            )
            result = extract_recent_documents(
                paths,
                limit=1,
                document_ids=[document_id],
                changed_only=False,
                shadow=False,
                provider="codex",
                run_id=scoped_run_id,
                max_workers=1,
                critic_max_workers=1,
            )
            if result.get("status") != "ok":
                raise GmailFactParityAdapterError("production extraction failed")
            documents = result.get("documents")
            if (
                not isinstance(documents, list)
                or len(documents) != 1
                or documents[0].get("document_id") != document_id
            ):
                raise GmailFactParityAdapterError("production extraction scope drifted")
            candidates = result.get("candidates")
            if not isinstance(candidates, list) or any(
                not isinstance(item, Mapping) for item in candidates
            ):
                raise GmailFactParityAdapterError("production candidates are invalid")
            actions, facts = _run_persistence_rows(paths, scoped_run_id)
            result_action_ids = {
                str(item.get("id") or "")
                for item in result.get("actions") or []
                if isinstance(item, Mapping)
            }
            if result_action_ids != {str(item["id"]) for item in actions}:
                raise GmailFactParityAdapterError("production action ledger drifted")
            chunks = _document_chunks(paths, document_id)
            packet_ids = [str(item["message_id"]) for item in packet["messages"]]
            members = [
                {
                    "candidate": dict(candidate),
                    "evidence_message_ids": evidence_message_ids(
                        candidate,
                        chunks=chunks,
                        body=body,
                        message_ranges=message_ranges,
                        packet_message_ids=packet_ids,
                    ),
                    "actions": actions,
                    "persisted_facts": facts,
                }
                for candidate in candidates
            ]
            invocations = _read_invocations(
                ledger,
                model=str(runtime["model"]),
                reasoning_effort=str(runtime["reasoning_effort"]),
            )
            response = {
                "version": RESPONSE_VERSION,
                "packet_id": packet["packet_id"],
                "thread_id": packet["thread_id"],
                "production_api": PRODUCTION_API,
                "prompt_version": prompt_version,
                "members": members,
                "invocations": invocations,
            }
            if set(response) != _RESPONSE_KEYS:
                raise GmailFactParityAdapterError("response schema is invalid")
            canonical_json(response)
            return response
    finally:
        os.umask(previous_umask)
        os.environ.clear()
        os.environ.update(previous_environment)


def run_adapter(request_path: Path, response_path: Path) -> None:
    if (
        request_path.absolute() == response_path.absolute()
        or response_path.exists()
        or response_path.is_symlink()
    ):
        raise GmailFactParityAdapterError("fresh distinct response path is required")
    try:
        value = json.loads(_read_private_file(request_path).decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise GmailFactParityAdapterError("request JSON is invalid") from exc
    response = execute_request(value, production_root=Path.cwd())
    _write_private_new(response_path, canonical_json(response) + b"\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--response", type=Path, required=True)
    args = parser.parse_args()
    try:
        run_adapter(args.request, args.response)
    except Exception:
        # The parent runner hashes diagnostics.  Never replay packet/model content,
        # temporary paths, or traceback locals across the private process boundary.
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
