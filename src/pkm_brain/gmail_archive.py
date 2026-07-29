from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import select
import shlex
import sqlite3
import stat
import subprocess
import sys
import time
import zlib
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from email import policy
from email.message import Message
from email.parser import BytesParser
from email.utils import getaddresses
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


GMAIL_ARCHIVE_KEYCHAIN_SERVICE = "com.pkm-brain.gmail-archive"
GMAIL_ARCHIVE_KEY_BYTES = 32
GMAIL_ARCHIVE_NONCE_BYTES = 12
GMAIL_ARCHIVE_MAX_RAW_BYTES = 128 * 1024 * 1024
GMAIL_ARCHIVE_MAX_SEARCH_TEXT_CHARS = 2_000_000
GMAIL_ARCHIVE_MAX_QUERY_CHARS = 2_000
GMAIL_ARCHIVE_MAX_RESULTS = 50
GMAIL_ARCHIVE_MAX_THREAD_MESSAGES = 500
GMAIL_ARCHIVE_MAX_THREAD_BODY_CHARS = 4_000_000
GMAIL_ARCHIVE_MAX_STATE_BYTES = 1024 * 1024
GMAIL_ARCHIVE_MAX_PENDING_IDS = 10_000
GMAIL_ARCHIVE_DIRECTORY_MODE = 0o700
GMAIL_ARCHIVE_FILE_MODE = 0o600

_SCHEMA_VERSION = 2
_LEGACY_SCHEMA_VERSION = 1
_VERIFIER_PLAINTEXT = b"pkm-brain gmail archive key verifier v1"
_VERIFIER_AAD = b"pkm-brain/gmail-archive/verifier/v1"
_CODE = re.compile(r"^[a-zA-Z0-9_.-]{1,128}$")
_IDENTITY_FINGERPRINT = re.compile(r"^[0-9a-f]{64}$")
_WORD = re.compile(r"[^\W_]+", re.UNICODE)
_SPAM_TRASH = {"SPAM", "TRASH"}


class GmailArchiveError(RuntimeError):
    """The encrypted Gmail archive could not safely complete an operation."""


class GmailArchiveSecurityError(GmailArchiveError):
    """The archive path or permissions are unsafe."""


class GmailArchiveLockedError(GmailArchiveError):
    """The archive key is missing, wrong, or unavailable."""


class GmailArchiveIntegrityError(GmailArchiveError):
    """Authenticated archive content is corrupt."""


PromptRunner = Callable[[Sequence[str], str], int]
_KEYCHAIN_PASSWORD_PROMPTS = (
    b"password data for new item:",
    b"retype password for new item:",
)


def _script_pty_argv(command: Sequence[str], platform: str) -> list[str]:
    if platform.startswith("linux"):
        # util-linux script accepts the child command only through -c. -e
        # preserves the child's exit status, matching BSD script semantics.
        return [
            "/usr/bin/script",
            "-q",
            "-e",
            "-c",
            shlex.join(command),
            "/dev/null",
        ]
    return ["/usr/bin/script", "-q", "/dev/null", *command]


def _run_security_password_prompt(
    argv: Sequence[str],
    password: str,
    *,
    timeout_seconds: float = 10.0,
) -> int:
    """Run macOS security on a PTY and answer both hidden password prompts."""

    command = [str(value) for value in argv]
    if (
        not command
        or not password
        or "\n" in password
        or "\r" in password
        or timeout_seconds <= 0
    ):
        raise GmailArchiveLockedError("Unable to create Gmail archive key")
    process: subprocess.Popen[bytes] | None = None
    deadline = time.monotonic() + timeout_seconds
    buffer = b""

    try:
        # macOS script owns the controlling PTY, avoiding forkpty inside the
        # daemon's multithreaded process. /dev/null prevents transcript storage.
        process = subprocess.Popen(
            _script_pty_argv(command, sys.platform),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
        )
        assert process.stdin is not None and process.stdout is not None

        def read_chunk(wait_seconds: float) -> bytes:
            readable, _, _ = select.select(
                [process.stdout.fileno()], [], [], wait_seconds
            )
            if not readable:
                return b""
            return os.read(process.stdout.fileno(), 4096)

        def write_password() -> None:
            process.stdin.write(password.encode("utf-8") + b"\n")
            process.stdin.flush()

        for prompt in _KEYCHAIN_PASSWORD_PROMPTS:
            while True:
                match = buffer.lower().find(prompt)
                if match >= 0:
                    buffer = buffer[match + len(prompt) :]
                    break
                if process.poll() is not None:
                    raise GmailArchiveLockedError(
                        "Unable to complete Gmail archive Keychain prompt"
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise GmailArchiveLockedError(
                        "Gmail archive Keychain prompt timed out"
                    )
                chunk = read_chunk(remaining)
                if chunk:
                    buffer = (buffer + chunk)[-8192:]
            write_password()

        while process.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GmailArchiveLockedError(
                    "Gmail archive Keychain prompt timed out"
                )
            # Drain output without returning or logging the prompt transcript.
            read_chunk(remaining)
        assert process.returncode is not None
        return int(process.returncode)
    except GmailArchiveLockedError:
        raise
    except (OSError, ValueError) as exc:
        raise GmailArchiveLockedError("Unable to create Gmail archive key") from exc
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1)
        if process is not None:
            if process.stdin is not None:
                process.stdin.close()
            if process.stdout is not None:
                process.stdout.close()


class GmailArchiveKeyProvider(Protocol):
    def load_key(self) -> bytes | None: ...

    def load_or_create_key(self) -> bytes: ...

    def delete_key(self) -> None: ...


class StaticGmailArchiveKeyProvider:
    """Injectable key provider for tests and isolated local tools."""

    def __init__(self, key: bytes | None = None) -> None:
        self.key = key

    def load_key(self) -> bytes | None:
        return self.key

    def load_or_create_key(self) -> bytes:
        if self.key is None:
            self.key = secrets.token_bytes(GMAIL_ARCHIVE_KEY_BYTES)
        return self.key

    def delete_key(self) -> None:
        self.key = None


Runner = Callable[..., subprocess.CompletedProcess[str]]


class MacOSKeychainGmailArchiveKeyProvider:
    """Store one Brain-home-scoped archive key in macOS Keychain."""

    def __init__(
        self,
        home: str | Path,
        *,
        runner: Runner = subprocess.run,
        prompt_runner: PromptRunner = _run_security_password_prompt,
        security_path: str | Path = "/usr/bin/security",
        platform: str | None = None,
    ) -> None:
        resolved = str(Path(home).expanduser().resolve())
        self.account = hashlib.sha256(resolved.encode()).hexdigest()[:16]
        self.runner = runner
        self.prompt_runner = prompt_runner
        self.security_path = Path(security_path)
        self.platform = platform or sys.platform

    def load_key(self) -> bytes | None:
        self._require_available()
        result = self.runner(
            [
                str(self.security_path),
                "find-generic-password",
                "-a",
                self.account,
                "-s",
                GMAIL_ARCHIVE_KEYCHAIN_SERVICE,
                "-w",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).casefold()
            if "could not be found" in detail or "item not found" in detail:
                return None
            raise GmailArchiveLockedError("Unable to read Gmail archive key")
        return _decode_key(result.stdout.strip())

    def load_or_create_key(self) -> bytes:
        existing = self.load_key()
        if existing is not None:
            return existing
        key = secrets.token_bytes(GMAIL_ARCHIVE_KEY_BYTES)
        encoded = base64.b64encode(key).decode("ascii")
        # macOS security only reads this form correctly from a terminal. The
        # secret travels through the PTY prompts and is never an argv value.
        return_code = self.prompt_runner(
            [
                str(self.security_path),
                "add-generic-password",
                "-a",
                self.account,
                "-s",
                GMAIL_ARCHIVE_KEYCHAIN_SERVICE,
                "-U",
                "-w",
            ],
            encoded,
        )
        if return_code == 0:
            stored = self.load_key()
            if stored is not None:
                return stored
        raced = self.load_key()
        if raced is not None:
            return raced
        raise GmailArchiveLockedError("Unable to create Gmail archive key")

    def delete_key(self) -> None:
        self._require_available()
        result = self.runner(
            [
                str(self.security_path),
                "delete-generic-password",
                "-a",
                self.account,
                "-s",
                GMAIL_ARCHIVE_KEYCHAIN_SERVICE,
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        detail = (result.stderr or result.stdout).casefold()
        if result.returncode != 0 and not (
            "could not be found" in detail or "item not found" in detail
        ):
            raise GmailArchiveLockedError("Unable to delete Gmail archive key")

    def _require_available(self) -> None:
        if self.platform != "darwin" or not self.security_path.is_file():
            raise GmailArchiveLockedError("macOS Keychain is unavailable")


@dataclass(frozen=True)
class ArchiveMessage:
    message_id: str
    thread_id: str
    raw_rfc822: bytes
    internal_date: str | None = None
    label_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ArchiveState:
    account_key: str
    phase: str
    query: str
    window_start: str
    window_end: str
    updated_at: str
    page_token: str | None = None
    history_id: str | None = None
    baseline_history_id: str | None = None
    pending_message_ids: tuple[str, ...] = ()
    continuation_history_id: str | None = None
    estimate: int | None = None
    processed: int = 0
    coverage_complete: bool = False
    reset_required: bool = False
    last_success_at: str | None = None
    error: str | None = None
    identity_fingerprint: str | None = None


@dataclass(frozen=True)
class ArchiveApplyResult:
    inserted: int
    updated: int
    deleted: int
    state: ArchiveState


@dataclass(frozen=True)
class ArchiveAttachment:
    filename: str | None
    content_type: str
    size: int | None


@dataclass(frozen=True)
class ArchiveSearchResult:
    message_id: str
    thread_id: str
    internal_date: str | None
    subject: str | None
    from_addresses: tuple[str, ...]
    to_addresses: tuple[str, ...]
    cc_addresses: tuple[str, ...]
    snippet: str
    attachment_filenames: tuple[str, ...]


@dataclass(frozen=True)
class ArchiveOpenedMessage:
    message_id: str
    thread_id: str
    internal_date: str | None
    date_header: str | None
    subject: str | None
    from_addresses: tuple[str, ...]
    to_addresses: tuple[str, ...]
    cc_addresses: tuple[str, ...]
    label_ids: tuple[str, ...]
    list_id: str | None
    list_unsubscribe: str | None
    precedence: str | None
    auto_submitted: str | None
    body_text: str
    attachments: tuple[ArchiveAttachment, ...]
    account_key: str = ""
    body_truncated: bool = False


@dataclass(frozen=True)
class ArchiveThreadResult:
    thread_id: str
    total_messages: int
    messages: tuple[ArchiveOpenedMessage, ...]
    truncated: bool
    account_key: str = ""
    omitted_message_count: int = 0
    body_truncated_message_count: int = 0


@dataclass(frozen=True)
class ArchiveThreadSnapshot:
    thread_id: str
    source_revision: str
    total_message_count: int
    visible_message_count: int
    deleted_message_count: int
    hidden_message_count: int
    created_at: str | None
    updated_at: str | None
    archive_updated_at: str
    raw_size: int
    account_key: str = ""


@dataclass(frozen=True)
class ArchiveStatus:
    key_state: str
    message_count: int
    active_message_count: int
    deleted_count: int
    thread_count: int
    hidden_count: int
    state: ArchiveState | None


@dataclass(frozen=True)
class _ParsedMessage:
    subject: str | None
    date_header: str | None
    from_addresses: tuple[str, ...]
    to_addresses: tuple[str, ...]
    cc_addresses: tuple[str, ...]
    list_id: str | None
    list_unsubscribe: str | None
    precedence: str | None
    auto_submitted: str | None
    body_text: str
    body_truncated: bool
    attachments: tuple[ArchiveAttachment, ...]


@dataclass(frozen=True)
class _PreparedMessage:
    source: ArchiveMessage
    internal_date: str | None
    hidden: bool
    raw_nonce: bytes
    raw_ciphertext: bytes
    text_nonce: bytes
    text_ciphertext: bytes
    raw_digest: str
    metadata_digest: str


class GmailArchiveStore:
    """A small application-encrypted local Gmail copy.

    SQLite holds only opaque identifiers, timestamps, flags, sizes, encrypted raw
    RFC bytes, encrypted parsed text, and one synchronization state per account.
    """

    def __init__(
        self,
        db_path: str | Path,
        key_provider: GmailArchiveKeyProvider,
    ) -> None:
        self.db_path = Path(db_path).expanduser().absolute()
        self.key_provider = key_provider

    @classmethod
    def for_paths(
        cls,
        paths: Any,
        key_provider: GmailArchiveKeyProvider | None = None,
    ) -> GmailArchiveStore:
        home = Path(paths.home).expanduser().absolute()
        configured = getattr(paths, "gmail_archive_sqlite_path", None)
        db_path = Path(configured) if configured else (
            home / "cache" / "gmail-archive" / "gmail-archive.sqlite"
        )
        return cls(
            db_path,
            key_provider or MacOSKeychainGmailArchiveKeyProvider(home),
        )

    def initialize(self) -> None:
        _prepare_archive_file(self.db_path)
        with self._connect(write=True, verify_schema=False) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS archive_meta (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    schema_version INTEGER NOT NULL,
                    verifier_nonce BLOB,
                    verifier_ciphertext BLOB,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages (
                    account_key TEXT NOT NULL,
                    message_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    internal_date TEXT,
                    hidden INTEGER NOT NULL CHECK (hidden IN (0, 1)),
                    deleted INTEGER NOT NULL CHECK (deleted IN (0, 1)),
                    raw_nonce BLOB NOT NULL,
                    raw_ciphertext BLOB NOT NULL,
                    text_nonce BLOB NOT NULL,
                    text_ciphertext BLOB NOT NULL,
                    raw_size INTEGER NOT NULL,
                    raw_digest TEXT,
                    metadata_digest TEXT,
                    stored_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (account_key, message_id)
                );
                CREATE INDEX IF NOT EXISTS messages_thread
                    ON messages(account_key, thread_id, internal_date);
                CREATE TABLE IF NOT EXISTS sync_state (
                    account_key TEXT PRIMARY KEY,
                    state_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                """
            )
            conn.execute(
                """
                INSERT OR IGNORE INTO archive_meta(
                    id, schema_version, verifier_nonce, verifier_ciphertext, created_at
                ) VALUES(1, ?, NULL, NULL, ?)
                """,
                (_SCHEMA_VERSION, _now_iso()),
            )
            _add_revision_digest_columns(conn)
            _verify_schema(conn)
        self._try_migrate_archive_schema()
        _secure_archive_files(self.db_path)

    def provision_key(self) -> None:
        self.initialize()
        key: bytes
        with self._connect(write=True) as conn:
            row = conn.execute(
                "SELECT verifier_nonce, verifier_ciphertext FROM archive_meta WHERE id=1"
            ).fetchone()
            assert row is not None
            if row[0] is not None or row[1] is not None:
                key = self._load_key()
                _verify_key(key, bytes(row[0]), bytes(row[1]))
            else:
                key = _validated_key(self.key_provider.load_or_create_key())
                nonce = secrets.token_bytes(GMAIL_ARCHIVE_NONCE_BYTES)
                ciphertext = AESGCM(key).encrypt(
                    nonce, _VERIFIER_PLAINTEXT, _VERIFIER_AAD
                )
                conn.execute(
                    """
                    UPDATE archive_meta
                    SET verifier_nonce=?, verifier_ciphertext=?
                    WHERE id=1 AND verifier_nonce IS NULL AND verifier_ciphertext IS NULL
                    """,
                    (nonce, ciphertext),
                )
        self._migrate_archive_schema(key)

    def get_state(self, account_key: str) -> ArchiveState | None:
        account_key = _identifier(account_key, "account key")
        self._require_initialized()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT state_json FROM sync_state WHERE account_key=?",
                (account_key,),
            ).fetchone()
        return _state_from_json(str(row[0])) if row is not None else None

    def list_thread_snapshots(
        self,
        account_key: str,
    ) -> tuple[ArchiveThreadSnapshot, ...]:
        """Return a metadata-only, immutable manifest for every stored thread."""

        account_key = _identifier(account_key, "account key")
        self._require_initialized()
        self._ensure_revision_digests()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT thread_id, message_id, internal_date, hidden, deleted,
                       raw_size, updated_at, raw_digest, metadata_digest
                FROM messages
                WHERE account_key=?
                ORDER BY thread_id, message_id
                """,
                (account_key,),
            ).fetchall()
        return _thread_snapshots(account_key, rows)

    def get_thread_snapshot(
        self,
        account_key: str,
        thread_id: str,
    ) -> ArchiveThreadSnapshot | None:
        """Return one thread manifest entry without decrypting message content."""

        account_key = _identifier(account_key, "account key")
        thread_id = _identifier(thread_id, "thread id")
        self._require_initialized()
        self._ensure_revision_digests()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT thread_id, message_id, internal_date, hidden, deleted,
                       raw_size, updated_at, raw_digest, metadata_digest
                FROM messages
                WHERE account_key=? AND thread_id=?
                ORDER BY message_id
                """,
                (account_key, thread_id),
            ).fetchall()
        snapshots = _thread_snapshots(account_key, rows)
        return snapshots[0] if snapshots else None

    def apply_batch(
        self,
        account_key: str,
        *,
        messages: Sequence[ArchiveMessage] = (),
        deleted_message_ids: Sequence[str] = (),
        state: ArchiveState,
    ) -> ArchiveApplyResult:
        account_key = _identifier(account_key, "account key")
        if state.account_key != account_key:
            raise ValueError("Archive state account does not match batch account")
        state_json = _state_json(state)
        key = self._require_key()
        prepared = tuple(self._prepare_message(account_key, item, key) for item in messages)
        deleted_ids = tuple(
            dict.fromkeys(_identifier(value, "message id") for value in deleted_message_ids)
        )
        now = _canonical_timestamp(state.updated_at)
        inserted = updated = deleted = 0
        with self._connect(write=True) as conn:
            self._migrate_archive_schema_in_connection(conn, key)
            conn.execute("BEGIN IMMEDIATE")
            try:
                for item in prepared:
                    source = item.source
                    exists = conn.execute(
                        "SELECT 1 FROM messages WHERE account_key=? AND message_id=?",
                        (account_key, source.message_id),
                    ).fetchone()
                    stored_at = now
                    if exists is not None:
                        stored_at = str(
                            conn.execute(
                                """
                                SELECT stored_at FROM messages
                                WHERE account_key=? AND message_id=?
                                """,
                                (account_key, source.message_id),
                            ).fetchone()[0]
                        )
                    conn.execute(
                        """
                        INSERT INTO messages(
                            account_key, message_id, thread_id, internal_date,
                            hidden, deleted, raw_nonce, raw_ciphertext,
                            text_nonce, text_ciphertext, raw_size, stored_at, updated_at,
                            raw_digest, metadata_digest
                        ) VALUES(?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(account_key, message_id) DO UPDATE SET
                            thread_id=excluded.thread_id,
                            internal_date=excluded.internal_date,
                            hidden=excluded.hidden,
                            deleted=0,
                            raw_nonce=excluded.raw_nonce,
                            raw_ciphertext=excluded.raw_ciphertext,
                            text_nonce=excluded.text_nonce,
                            text_ciphertext=excluded.text_ciphertext,
                            raw_size=excluded.raw_size,
                            raw_digest=excluded.raw_digest,
                            metadata_digest=excluded.metadata_digest,
                            updated_at=excluded.updated_at
                        """,
                        (
                            account_key,
                            source.message_id,
                            source.thread_id,
                            item.internal_date,
                            int(item.hidden),
                            item.raw_nonce,
                            item.raw_ciphertext,
                            item.text_nonce,
                            item.text_ciphertext,
                            len(source.raw_rfc822),
                            stored_at,
                            now,
                            item.raw_digest,
                            item.metadata_digest,
                        ),
                    )
                    inserted += int(exists is None)
                    updated += int(exists is not None)
                for message_id in deleted_ids:
                    cursor = conn.execute(
                        """
                        UPDATE messages SET deleted=1, updated_at=?
                        WHERE account_key=? AND message_id=? AND deleted=0
                        """,
                        (now, account_key, message_id),
                    )
                    deleted += cursor.rowcount
                conn.execute(
                    """
                    INSERT INTO sync_state(account_key, state_json, updated_at)
                    VALUES(?, ?, ?)
                    ON CONFLICT(account_key) DO UPDATE SET
                        state_json=excluded.state_json,
                        updated_at=excluded.updated_at
                    """,
                    (account_key, state_json, now),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return ArchiveApplyResult(inserted, updated, deleted, state)

    def search(
        self,
        account_key: str,
        query: str,
        *,
        limit: int = 20,
        include_spam_trash: bool = False,
        after: str | datetime | None = None,
        before: str | datetime | None = None,
        from_address: str | None = None,
        to_address: str | None = None,
    ) -> tuple[ArchiveSearchResult, ...]:
        account_key = _identifier(account_key, "account key")
        query = str(query).strip()
        if len(query) > GMAIL_ARCHIVE_MAX_QUERY_CHARS:
            raise ValueError("Mail search query is too long")
        if not 1 <= limit <= GMAIL_ARCHIVE_MAX_RESULTS:
            raise ValueError(f"Mail search limit must be 1-{GMAIL_ARCHIVE_MAX_RESULTS}")
        after_value = _filter_timestamp(after)
        before_value = _filter_timestamp(before)
        if after_value and before_value and after_value >= before_value:
            raise ValueError("Mail search after must be earlier than before")
        wanted_from = _normalized_address_filter(from_address)
        wanted_to = _normalized_address_filter(to_address)
        terms = tuple(value.casefold() for value in _WORD.findall(query))
        key = self._require_key()
        clauses = ["account_key=?", "deleted=0"]
        parameters: list[Any] = [account_key]
        if not include_spam_trash:
            clauses.append("hidden=0")
        if after_value:
            clauses.append("internal_date>=?")
            parameters.append(after_value)
        if before_value:
            clauses.append("internal_date<?")
            parameters.append(before_value)
        sql = (
            "SELECT message_id, thread_id, internal_date, text_nonce, text_ciphertext "
            "FROM messages WHERE "
            + " AND ".join(clauses)
            + " ORDER BY internal_date DESC, message_id DESC"
        )
        output: list[ArchiveSearchResult] = []
        with self._connect() as conn:
            rows = conn.execute(sql, parameters)
            for row in rows:
                parsed = _decrypt_search_payload(
                    key,
                    account_key,
                    str(row[0]),
                    bytes(row[3]),
                    bytes(row[4]),
                )
                haystack = "\n".join(
                    value
                    for value in (
                        parsed["subject"] or "",
                        *parsed["from_addresses"],
                        *parsed["to_addresses"],
                        *parsed["cc_addresses"],
                        parsed["body_text"],
                        *(value or "" for value in parsed["attachment_filenames"]),
                    )
                    if value
                ).casefold()
                if terms and not all(term in haystack for term in terms):
                    continue
                from_values = tuple(parsed["from_addresses"])
                to_values = tuple(parsed["to_addresses"])
                cc_values = tuple(parsed["cc_addresses"])
                if wanted_from and wanted_from not in {v.casefold() for v in from_values}:
                    continue
                if wanted_to and wanted_to not in {
                    v.casefold() for v in (*to_values, *cc_values)
                }:
                    continue
                output.append(
                    ArchiveSearchResult(
                        message_id=str(row[0]),
                        thread_id=str(row[1]),
                        internal_date=str(row[2]) if row[2] is not None else None,
                        subject=parsed["subject"],
                        from_addresses=from_values,
                        to_addresses=to_values,
                        cc_addresses=cc_values,
                        snippet=_snippet(str(parsed["body_text"])),
                        attachment_filenames=tuple(
                            value
                            for value in parsed["attachment_filenames"]
                            if value is not None
                        ),
                    )
                )
                if len(output) == limit:
                    break
        return tuple(output)

    def open_thread(
        self,
        account_key: str,
        thread_id: str,
        *,
        max_messages: int = 100,
        max_body_chars: int = 1_000_000,
        include_spam_trash: bool = False,
    ) -> ArchiveThreadResult:
        account_key = _identifier(account_key, "account key")
        thread_id = _identifier(thread_id, "thread id")
        if not 1 <= max_messages <= GMAIL_ARCHIVE_MAX_THREAD_MESSAGES:
            raise ValueError("Mail thread message limit is outside its bound")
        if not 1 <= max_body_chars <= GMAIL_ARCHIVE_MAX_THREAD_BODY_CHARS:
            raise ValueError("Mail thread body limit is outside its bound")
        key = self._require_key()
        hidden = "" if include_spam_trash else " AND hidden=0"
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT message_id, internal_date, raw_nonce, raw_ciphertext, raw_size,
                       text_nonce, text_ciphertext
                FROM messages
                WHERE account_key=? AND thread_id=? AND deleted=0
                """
                + hidden
                + " ORDER BY internal_date, message_id",
                (account_key, thread_id),
            ).fetchall()
        total = len(rows)
        selected_rows = rows[-max_messages:]
        opened: list[ArchiveOpenedMessage] = []
        remaining = max_body_chars
        # Spend the body budget from newest to oldest, then restore chronological
        # order for callers.  The former oldest-first loop could consume the full
        # budget before the messages that explain the thread's current state.
        for row in reversed(selected_rows):
            raw = _decrypt_raw(
                key,
                account_key,
                str(row[0]),
                bytes(row[2]),
                bytes(row[3]),
                int(row[4]),
            )
            parsed = _parse_message(raw, body_limit=remaining)
            encrypted_metadata = _decrypt_search_payload(
                key,
                account_key,
                str(row[0]),
                bytes(row[5]),
                bytes(row[6]),
            )
            remaining = max(0, remaining - len(parsed.body_text))
            opened.append(
                ArchiveOpenedMessage(
                    message_id=str(row[0]),
                    thread_id=thread_id,
                    internal_date=str(row[1]) if row[1] is not None else None,
                    date_header=parsed.date_header,
                    subject=parsed.subject,
                    from_addresses=parsed.from_addresses,
                    to_addresses=parsed.to_addresses,
                    cc_addresses=parsed.cc_addresses,
                    label_ids=tuple(encrypted_metadata["label_ids"]),
                    list_id=parsed.list_id,
                    list_unsubscribe=parsed.list_unsubscribe,
                    precedence=parsed.precedence,
                    auto_submitted=parsed.auto_submitted,
                    body_text=parsed.body_text,
                    attachments=parsed.attachments,
                    account_key=account_key,
                    body_truncated=parsed.body_truncated,
                )
            )
        opened.reverse()
        omitted_message_count = max(0, total - len(opened))
        body_truncated_message_count = sum(
            int(message.body_truncated) for message in opened
        )
        return ArchiveThreadResult(
            thread_id=thread_id,
            total_messages=total,
            messages=tuple(opened),
            truncated=bool(omitted_message_count or body_truncated_message_count),
            account_key=account_key,
            omitted_message_count=omitted_message_count,
            body_truncated_message_count=body_truncated_message_count,
        )

    def status(self, account_key: str) -> ArchiveStatus:
        account_key = _identifier(account_key, "account key")
        if not self.db_path.exists():
            return ArchiveStatus("uninitialized", 0, 0, 0, 0, 0, None)
        _assert_private_archive(self.db_path)
        with self._connect() as conn:
            verifier = conn.execute(
                "SELECT verifier_nonce, verifier_ciphertext FROM archive_meta WHERE id=1"
            ).fetchone()
            counts = conn.execute(
                """
                SELECT COUNT(*),
                       SUM(CASE WHEN deleted=0 THEN 1 ELSE 0 END),
                       SUM(CASE WHEN deleted=1 THEN 1 ELSE 0 END),
                       COUNT(DISTINCT CASE WHEN deleted=0 THEN thread_id END),
                       SUM(CASE WHEN deleted=0 AND hidden=1 THEN 1 ELSE 0 END)
                FROM messages WHERE account_key=?
                """,
                (account_key,),
            ).fetchone()
            state_row = conn.execute(
                "SELECT state_json FROM sync_state WHERE account_key=?",
                (account_key,),
            ).fetchone()
        key_state = self._key_state(verifier)
        values = [int(value or 0) for value in counts]
        state = _state_from_json(str(state_row[0])) if state_row else None
        return ArchiveStatus(key_state, *values, state)

    def _prepare_message(
        self,
        account_key: str,
        source: ArchiveMessage,
        key: bytes,
    ) -> _PreparedMessage:
        message_id = _identifier(source.message_id, "message id")
        _identifier(source.thread_id, "thread id")
        raw = bytes(source.raw_rfc822)
        if not raw or len(raw) > GMAIL_ARCHIVE_MAX_RAW_BYTES:
            raise ValueError("Raw Gmail message is outside its byte bound")
        labels = _labels(source.label_ids)
        parsed = _parse_message(raw, body_limit=GMAIL_ARCHIVE_MAX_SEARCH_TEXT_CHARS)
        payload = {
            "subject": parsed.subject,
            "from_addresses": list(parsed.from_addresses),
            "to_addresses": list(parsed.to_addresses),
            "cc_addresses": list(parsed.cc_addresses),
            "body_text": parsed.body_text,
            "attachment_filenames": [item.filename for item in parsed.attachments],
            "label_ids": list(labels),
        }
        raw_nonce = secrets.token_bytes(GMAIL_ARCHIVE_NONCE_BYTES)
        text_nonce = secrets.token_bytes(GMAIL_ARCHIVE_NONCE_BYTES)
        raw_ciphertext = AESGCM(key).encrypt(
            raw_nonce,
            zlib.compress(raw, level=6),
            _aad(account_key, message_id, "raw"),
        )
        text_ciphertext = AESGCM(key).encrypt(
            text_nonce,
            _json_bytes(payload),
            _aad(account_key, message_id, "text"),
        )
        return _PreparedMessage(
            source=source,
            internal_date=_internal_date(source.internal_date),
            hidden=bool(_SPAM_TRASH & {value.upper() for value in labels}),
            raw_nonce=raw_nonce,
            raw_ciphertext=raw_ciphertext,
            text_nonce=text_nonce,
            text_ciphertext=text_ciphertext,
            raw_digest=hashlib.sha256(raw).hexdigest(),
            metadata_digest=_message_metadata_digest(
                thread_id=source.thread_id,
                internal_date=_internal_date(source.internal_date),
                label_ids=labels,
            ),
        )

    def _ensure_revision_digests(self) -> None:
        with self._connect() as conn:
            version = _archive_schema_version(conn)
            columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(messages)")
            }
            missing = (
                conn.execute(
                    """
                    SELECT 1 FROM messages
                    WHERE raw_digest IS NULL OR metadata_digest IS NULL
                    LIMIT 1
                    """
                ).fetchone()
                if {"raw_digest", "metadata_digest"} <= columns
                else True
            )
        if version == _SCHEMA_VERSION and missing is None:
            return
        self._migrate_archive_schema(self._require_key())

    def _try_migrate_archive_schema(self) -> None:
        try:
            key = self._load_key()
        except GmailArchiveLockedError:
            return
        with self._connect() as conn:
            verifier = conn.execute(
                "SELECT verifier_nonce, verifier_ciphertext FROM archive_meta WHERE id=1"
            ).fetchone()
        if verifier is None or verifier[0] is None or verifier[1] is None:
            return
        _verify_key(key, bytes(verifier[0]), bytes(verifier[1]))
        self._migrate_archive_schema(key)

    def _migrate_archive_schema(self, key: bytes) -> None:
        with self._connect(write=True) as conn:
            self._migrate_archive_schema_in_connection(conn, key)

    def _migrate_archive_schema_in_connection(
        self,
        conn: sqlite3.Connection,
        key: bytes,
    ) -> None:
        _add_revision_digest_columns(conn)
        rows = conn.execute(
            """
            SELECT account_key, message_id, thread_id, internal_date,
                   raw_nonce, raw_ciphertext, text_nonce, text_ciphertext, raw_size
            FROM messages
            WHERE raw_digest IS NULL OR metadata_digest IS NULL
            ORDER BY account_key, message_id
            """
        ).fetchall()
        for row in rows:
            account_key = str(row["account_key"])
            message_id = str(row["message_id"])
            raw = _decrypt_raw(
                key,
                account_key,
                message_id,
                bytes(row["raw_nonce"]),
                bytes(row["raw_ciphertext"]),
                int(row["raw_size"]),
            )
            search_payload = _decrypt_search_payload(
                key,
                account_key,
                message_id,
                bytes(row["text_nonce"]),
                bytes(row["text_ciphertext"]),
            )
            conn.execute(
                """
                UPDATE messages
                SET raw_digest=?, metadata_digest=?
                WHERE account_key=? AND message_id=?
                """,
                (
                    hashlib.sha256(raw).hexdigest(),
                    _message_metadata_digest(
                        thread_id=str(row["thread_id"]),
                        internal_date=(
                            str(row["internal_date"])
                            if row["internal_date"] is not None
                            else None
                        ),
                        label_ids=tuple(search_payload["label_ids"]),
                    ),
                    account_key,
                    message_id,
                ),
            )
        conn.execute(
            "UPDATE archive_meta SET schema_version=? WHERE id=1",
            (_SCHEMA_VERSION,),
        )

    def _require_initialized(self) -> None:
        if not self.db_path.exists():
            raise GmailArchiveError("Gmail archive is not initialized")
        _assert_private_archive(self.db_path)
        with self._connect():
            pass

    def _require_key(self) -> bytes:
        self._require_initialized()
        key = self._load_key()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT verifier_nonce, verifier_ciphertext FROM archive_meta WHERE id=1"
            ).fetchone()
        if row is None or row[0] is None or row[1] is None:
            raise GmailArchiveLockedError("Gmail archive key is not provisioned")
        _verify_key(key, bytes(row[0]), bytes(row[1]))
        return key

    def _load_key(self) -> bytes:
        key = self.key_provider.load_key()
        if key is None:
            raise GmailArchiveLockedError("Gmail archive key is missing")
        return _validated_key(key)

    def _key_state(self, verifier: sqlite3.Row | None) -> str:
        if verifier is None or verifier[0] is None or verifier[1] is None:
            return "uninitialized"
        try:
            key = self.key_provider.load_key()
        except GmailArchiveLockedError:
            return "unavailable"
        if key is None:
            return "key_missing"
        try:
            _verify_key(_validated_key(key), bytes(verifier[0]), bytes(verifier[1]))
        except (GmailArchiveLockedError, GmailArchiveIntegrityError):
            return "wrong_key"
        return "available"

    @contextmanager
    def _connect(
        self,
        *,
        write: bool = False,
        verify_schema: bool = True,
    ) -> Iterator[sqlite3.Connection]:
        _assert_private_archive(self.db_path)
        conn = sqlite3.connect(self.db_path, timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=5000")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA temp_store=MEMORY")
            if write:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=FULL")
                conn.execute("PRAGMA secure_delete=ON")
            else:
                conn.execute("PRAGMA query_only=ON")
            if verify_schema:
                _verify_schema(conn)
            yield conn
        finally:
            conn.close()
            if write:
                _secure_archive_files(self.db_path)


def _verify_schema(conn: sqlite3.Connection) -> None:
    tables = {
        str(row[0])
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    if tables != {"archive_meta", "messages", "sync_state"}:
        raise GmailArchiveError("Gmail archive schema is unsupported")
    version = _archive_schema_version(conn)
    if version not in {_LEGACY_SCHEMA_VERSION, _SCHEMA_VERSION}:
        raise GmailArchiveError("Gmail archive schema version is unsupported")
    if version == _SCHEMA_VERSION:
        columns = {
            str(row[1]) for row in conn.execute("PRAGMA table_info(messages)")
        }
        if not {"raw_digest", "metadata_digest"} <= columns:
            raise GmailArchiveError("Gmail archive schema is missing revision digests")


def _archive_schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT schema_version FROM archive_meta WHERE id=1"
    ).fetchone()
    return int(row[0]) if row is not None else 0


def _add_revision_digest_columns(conn: sqlite3.Connection) -> None:
    columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(messages)")}
    if "raw_digest" not in columns:
        conn.execute("ALTER TABLE messages ADD COLUMN raw_digest TEXT")
    if "metadata_digest" not in columns:
        conn.execute("ALTER TABLE messages ADD COLUMN metadata_digest TEXT")


def _prepare_archive_file(path: Path) -> None:
    _reject_symlink_components(path.parent)
    if not path.parent.exists():
        path.parent.mkdir(parents=True, mode=GMAIL_ARCHIVE_DIRECTORY_MODE)
    _assert_private_directory(path.parent)
    if path.is_symlink():
        raise GmailArchiveSecurityError("Gmail archive database must not be a symlink")
    if not path.exists():
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags, GMAIL_ARCHIVE_FILE_MODE)
        os.close(descriptor)
    _assert_private_file(path)


def _assert_private_archive(path: Path) -> None:
    _reject_symlink_components(path.parent)
    _assert_private_directory(path.parent)
    _assert_private_file(path)
    for suffix in ("-wal", "-shm"):
        companion = Path(f"{path}{suffix}")
        if companion.exists() or companion.is_symlink():
            _assert_private_file(companion)


def _assert_private_directory(path: Path) -> None:
    try:
        value = os.stat(path, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise GmailArchiveSecurityError("Gmail archive directory is missing") from exc
    if not stat.S_ISDIR(value.st_mode) or stat.S_IMODE(value.st_mode) & 0o077:
        raise GmailArchiveSecurityError("Gmail archive directory must be owner-only")


def _assert_private_file(path: Path) -> None:
    try:
        value = os.stat(path, follow_symlinks=False)
    except FileNotFoundError as exc:
        raise GmailArchiveSecurityError("Gmail archive file is missing") from exc
    if not stat.S_ISREG(value.st_mode) or stat.S_IMODE(value.st_mode) & 0o077:
        raise GmailArchiveSecurityError("Gmail archive file must be owner-only")


def _reject_symlink_components(path: Path) -> None:
    for component in (path, *path.parents):
        if component.is_symlink():
            raise GmailArchiveSecurityError("Gmail archive path traverses a symlink")


def _secure_archive_files(path: Path) -> None:
    os.chmod(path.parent, GMAIL_ARCHIVE_DIRECTORY_MODE)
    for candidate in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
        if candidate.exists() or candidate.is_symlink():
            _assert_private_file(candidate)
            os.chmod(candidate, GMAIL_ARCHIVE_FILE_MODE)


def _decode_key(value: str) -> bytes:
    try:
        key = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise GmailArchiveLockedError("Gmail archive Keychain value is invalid") from exc
    return _validated_key(key)


def _validated_key(value: bytes) -> bytes:
    key = bytes(value)
    if len(key) != GMAIL_ARCHIVE_KEY_BYTES:
        raise GmailArchiveLockedError("Gmail archive key has the wrong length")
    return key


def _verify_key(key: bytes, nonce: bytes, ciphertext: bytes) -> None:
    try:
        value = AESGCM(key).decrypt(nonce, ciphertext, _VERIFIER_AAD)
    except (InvalidTag, ValueError) as exc:
        raise GmailArchiveIntegrityError("Gmail archive key verification failed") from exc
    if value != _VERIFIER_PLAINTEXT:
        raise GmailArchiveIntegrityError("Gmail archive key verification failed")


def _aad(account_key: str, message_id: str, kind: str) -> bytes:
    return f"pkm-brain/gmail-archive/v1/{kind}/{account_key}/{message_id}".encode()


def _identifier(value: str, label: str) -> str:
    normalized = str(value).strip()
    if not normalized or len(normalized) > 512 or "\x00" in normalized:
        raise ValueError(f"Invalid Gmail archive {label}")
    return normalized


def _labels(values: Sequence[str]) -> tuple[str, ...]:
    if len(values) > 1_000:
        raise ValueError("Too many Gmail labels")
    return tuple(_identifier(value, "label") for value in values)


def _state_json(state: ArchiveState) -> str:
    _identifier(state.account_key, "account key")
    if not _CODE.fullmatch(state.phase):
        raise ValueError("Invalid Gmail archive phase")
    if len(state.query) > 16_000:
        raise ValueError("Gmail archive query is too long")
    if len(state.pending_message_ids) > GMAIL_ARCHIVE_MAX_PENDING_IDS:
        raise ValueError("Too many pending Gmail message IDs")
    for value in state.pending_message_ids:
        _identifier(value, "pending message id")
    if state.estimate is not None and state.estimate < 0:
        raise ValueError("Gmail archive estimate must not be negative")
    if state.processed < 0:
        raise ValueError("Gmail archive progress must not be negative")
    _canonical_timestamp(state.updated_at)
    if state.last_success_at:
        _canonical_timestamp(state.last_success_at)
    if state.error is not None and len(state.error) > 2_000:
        raise ValueError("Gmail archive state error is too long")
    if (
        state.identity_fingerprint is not None
        and not _IDENTITY_FINGERPRINT.fullmatch(state.identity_fingerprint)
    ):
        raise ValueError("Gmail archive identity fingerprint is invalid")
    value = json.dumps(asdict(state), ensure_ascii=True, separators=(",", ":"))
    if len(value.encode()) > GMAIL_ARCHIVE_MAX_STATE_BYTES:
        raise ValueError("Gmail archive state is too large")
    return value


def _state_from_json(value: str) -> ArchiveState:
    try:
        raw = json.loads(value)
        raw["pending_message_ids"] = tuple(raw.get("pending_message_ids", ()))
        state = ArchiveState(**raw)
        _state_json(state)
        return state
    except (TypeError, ValueError, KeyError, json.JSONDecodeError) as exc:
        raise GmailArchiveIntegrityError("Gmail archive sync state is invalid") from exc


def _internal_date(value: str | None) -> str | None:
    if value is None or not str(value).strip():
        return None
    normalized = str(value).strip()
    if normalized.isdigit():
        parsed = datetime.fromtimestamp(int(normalized) / 1000, tz=timezone.utc)
    else:
        parsed = _parse_timestamp(normalized)
    return parsed.isoformat(timespec="milliseconds")


def _canonical_timestamp(value: str | datetime) -> str:
    return _parse_timestamp(value).isoformat(timespec="milliseconds")


def _filter_timestamp(value: str | datetime | None) -> str | None:
    return None if value is None else _canonical_timestamp(value)


def _parse_timestamp(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        normalized = str(value).strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ValueError("Invalid Gmail archive timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _normalized_address_filter(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().casefold()
    if not normalized or len(normalized) > 512:
        raise ValueError("Invalid mail address filter")
    return normalized


def _parse_message(raw: bytes, *, body_limit: int) -> _ParsedMessage:
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw)
    except Exception as exc:
        raise ValueError("Raw Gmail message could not be parsed") from exc
    subject = _header(message, "Subject")
    date_header = _header(message, "Date")
    from_addresses = _addresses(message, "From")
    to_addresses = _addresses(message, "To")
    cc_addresses = _addresses(message, "Cc")
    list_id = _header(message, "List-Id")
    list_unsubscribe = _header(message, "List-Unsubscribe")
    precedence = _header(message, "Precedence")
    auto_submitted = _header(message, "Auto-Submitted")
    attachments: list[ArchiveAttachment] = []
    plain: list[str] = []
    html: list[str] = []

    def visit(part: Message) -> None:
        if len(attachments) >= 1_000:
            return
        disposition = (part.get_content_disposition() or "").casefold()
        filename = part.get_filename()
        content_type = part.get_content_type().casefold()
        if disposition == "attachment" or filename or content_type == "message/rfc822":
            payload = part.get_payload(decode=True)
            attachments.append(
                ArchiveAttachment(
                    filename=_bounded(filename, 8_000),
                    content_type=_bounded(content_type, 255)
                    or "application/octet-stream",
                    size=len(payload) if isinstance(payload, bytes) else None,
                )
            )
            # An attached multipart or message/rfc822 may itself contain text
            # parts. It is evidence bytes, not part of the parent message body.
            return
        if part.is_multipart():
            payload = part.get_payload()
            if isinstance(payload, list):
                for child in payload:
                    if isinstance(child, Message):
                        visit(child)
            return
        if content_type not in {"text/plain", "text/html"}:
            return
        try:
            content = part.get_content()
        except Exception:
            payload = part.get_payload(decode=True) or b""
            charset = part.get_content_charset() or "utf-8"
            content = payload.decode(charset, errors="replace")
        value = str(content)
        (plain if content_type == "text/plain" else html).append(value)

    visit(message)
    body = "\n".join(plain)
    if not body.strip() and html:
        body = _html_text("\n".join(html))
    body = body.replace("\x00", "")
    body_truncated = len(body) > body_limit
    body = body[:body_limit]
    return _ParsedMessage(
        subject=subject,
        date_header=date_header,
        from_addresses=from_addresses,
        to_addresses=to_addresses,
        cc_addresses=cc_addresses,
        list_id=list_id,
        list_unsubscribe=list_unsubscribe,
        precedence=precedence,
        auto_submitted=auto_submitted,
        body_text=body,
        body_truncated=body_truncated,
        attachments=tuple(attachments),
    )


def _header(message: Message, name: str) -> str | None:
    value = message.get(name)
    return _bounded(str(value), 8_000) if value is not None else None


def _addresses(message: Message, name: str) -> tuple[str, ...]:
    values = message.get_all(name, [])
    output: list[str] = []
    for _display, address in getaddresses([str(value) for value in values]):
        normalized = address.strip().casefold()
        if normalized and normalized not in output:
            output.append(normalized[:512])
        if len(output) == 1_000:
            break
    return tuple(output)


def _bounded(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    return str(value).replace("\x00", "")[:limit]


class _TextHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.values: list[str] = []

    def handle_data(self, data: str) -> None:
        self.values.append(data)


def _html_text(value: str) -> str:
    parser = _TextHTMLParser()
    try:
        parser.feed(value)
    except Exception:
        return value
    return " ".join(part.strip() for part in parser.values if part.strip())


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()


def _message_metadata_digest(
    *,
    thread_id: str,
    internal_date: str | None,
    label_ids: Sequence[str],
) -> str:
    digest = hashlib.sha256()
    digest.update(b"pkm-brain/gmail-archive/message-metadata/v1\0")
    digest.update(
        _json_bytes(
            {
                "thread_id": str(thread_id),
                "internal_date": internal_date,
                "label_ids": sorted(set(_labels(label_ids))),
            }
        )
    )
    return digest.hexdigest()


def _decrypt_search_payload(
    key: bytes,
    account_key: str,
    message_id: str,
    nonce: bytes,
    ciphertext: bytes,
) -> dict[str, Any]:
    try:
        plaintext = AESGCM(key).decrypt(
            nonce, ciphertext, _aad(account_key, message_id, "text")
        )
        value = json.loads(plaintext)
        required = {
            "subject",
            "from_addresses",
            "to_addresses",
            "cc_addresses",
            "body_text",
            "attachment_filenames",
        }
        if not isinstance(value, dict) or not required <= value.keys():
            raise ValueError
        for field in ("from_addresses", "to_addresses", "cc_addresses", "attachment_filenames"):
            if not isinstance(value[field], list):
                raise ValueError
        label_ids = value.get("label_ids", [])
        if not isinstance(label_ids, list) or not all(
            isinstance(item, str) for item in label_ids
        ):
            raise ValueError
        value["label_ids"] = list(_labels(label_ids))
        return value
    except (InvalidTag, ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GmailArchiveIntegrityError("Encrypted Gmail search text is corrupt") from exc


def _thread_snapshots(
    account_key: str,
    rows: Sequence[sqlite3.Row],
) -> tuple[ArchiveThreadSnapshot, ...]:
    grouped: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        grouped.setdefault(str(row["thread_id"]), []).append(row)

    output: list[ArchiveThreadSnapshot] = []
    for thread_id, thread_rows in grouped.items():
        source_dates = [
            str(row["internal_date"])
            for row in thread_rows
            if row["internal_date"] is not None
        ]
        digest = hashlib.sha256()
        digest.update(b"pkm-brain/gmail-archive/thread-revision/v2\0")
        for row in thread_rows:
            digest.update(
                _json_bytes(
                    [
                        str(row["message_id"]),
                        (
                            str(row["internal_date"])
                            if row["internal_date"] is not None
                            else None
                        ),
                        bool(row["deleted"]),
                        bool(row["hidden"]),
                        int(row["raw_size"]),
                        str(row["raw_digest"]),
                        str(row["metadata_digest"]),
                    ]
                )
            )
            digest.update(b"\n")
        output.append(
            ArchiveThreadSnapshot(
                thread_id=thread_id,
                source_revision=digest.hexdigest(),
                total_message_count=len(thread_rows),
                visible_message_count=sum(
                    int(not bool(row["deleted"]) and not bool(row["hidden"]))
                    for row in thread_rows
                ),
                deleted_message_count=sum(
                    int(bool(row["deleted"])) for row in thread_rows
                ),
                hidden_message_count=sum(
                    int(not bool(row["deleted"]) and bool(row["hidden"]))
                    for row in thread_rows
                ),
                created_at=min(source_dates) if source_dates else None,
                updated_at=max(source_dates) if source_dates else None,
                archive_updated_at=max(
                    str(row["updated_at"]) for row in thread_rows
                ),
                raw_size=sum(int(row["raw_size"]) for row in thread_rows),
                account_key=account_key,
            )
        )
    return tuple(output)


def _decrypt_raw(
    key: bytes,
    account_key: str,
    message_id: str,
    nonce: bytes,
    ciphertext: bytes,
    expected_size: int,
) -> bytes:
    try:
        compressed = AESGCM(key).decrypt(
            nonce, ciphertext, _aad(account_key, message_id, "raw")
        )
        decompressor = zlib.decompressobj()
        raw = decompressor.decompress(compressed, GMAIL_ARCHIVE_MAX_RAW_BYTES + 1)
        raw += decompressor.flush()
    except (InvalidTag, ValueError, zlib.error) as exc:
        raise GmailArchiveIntegrityError("Encrypted Gmail message is corrupt") from exc
    if (
        len(raw) != expected_size
        or len(raw) > GMAIL_ARCHIVE_MAX_RAW_BYTES
        or not decompressor.eof
        or decompressor.unused_data
    ):
        raise GmailArchiveIntegrityError("Encrypted Gmail message has an invalid size")
    return raw


def _snippet(body: str) -> str:
    return " ".join(body.split())[:600]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def gmail_archive_identity_fingerprint(email: str, provider_subject: str | None) -> str:
    """Bind archive state to one policy-approved immutable Gmail identity."""

    normalized_email = str(email).strip().casefold()
    normalized_subject = str(provider_subject or "").strip()
    if not normalized_email or not normalized_subject:
        raise ValueError("Gmail archive identity requires email and provider subject")
    payload = f"{normalized_email}\0{normalized_subject}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()
