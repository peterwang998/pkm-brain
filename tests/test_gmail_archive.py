from __future__ import annotations

import base64
import json
import os
import sqlite3
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from pkm_brain.gmail_archive import (
    ArchiveMessage,
    ArchiveState,
    GmailArchiveIntegrityError,
    GmailArchiveLockedError,
    GmailArchiveSecurityError,
    GmailArchiveStore,
    MacOSKeychainGmailArchiveKeyProvider,
    StaticGmailArchiveKeyProvider,
    _run_security_password_prompt,
)


KEY = b"k" * 32
NOW = "2026-07-14T18:00:00+00:00"


def _store(tmp_path: Path, key: bytes | None = KEY) -> GmailArchiveStore:
    directory = tmp_path / "archive"
    directory.mkdir(mode=0o700)
    return GmailArchiveStore(
        directory / "gmail.sqlite", StaticGmailArchiveKeyProvider(key)
    )


def _state(**changes: object) -> ArchiveState:
    value = ArchiveState(
        account_key="gmail.primary",
        phase="backfill",
        query="after:2026/04/15",
        window_start="2026-04-15",
        window_end="2026-07-14",
        updated_at=NOW,
        baseline_history_id="900",
        estimate=2,
    )
    return replace(value, **changes)


def _message(
    message_id: str,
    thread_id: str,
    *,
    subject: str,
    body: str,
    labels: tuple[str, ...] = (),
    attachment: bool = False,
) -> ArchiveMessage:
    if attachment:
        raw = (
            "From: Alice <alice@example.com>\r\n"
            "To: Peter <peter@example.com>\r\n"
            f"Subject: {subject}\r\n"
            "Date: Tue, 14 Jul 2026 10:00:00 -0700\r\n"
            "MIME-Version: 1.0\r\n"
            'Content-Type: multipart/mixed; boundary="b"\r\n\r\n'
            "--b\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
            f"{body}\r\n"
            "--b\r\nContent-Type: text/plain\r\n"
            'Content-Disposition: attachment; filename="plan.txt"\r\n\r\n'
            "attachment bytes\r\n--b--\r\n"
        ).encode()
    else:
        raw = (
            "From: Alice <alice@example.com>\r\n"
            "To: Peter <peter@example.com>\r\n"
            f"Subject: {subject}\r\n"
            "Date: Tue, 14 Jul 2026 10:00:00 -0700\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n\r\n"
            f"{body}\r\n"
        ).encode()
    return ArchiveMessage(
        message_id=message_id,
        thread_id=thread_id,
        raw_rfc822=raw,
        internal_date="1784052000000",
        label_ids=labels,
    )


def test_keychain_secret_is_saved_with_the_supported_security_form(
    tmp_path: Path,
) -> None:
    calls: list[tuple[list[str], dict[str, object]]] = []
    prompt_calls: list[tuple[list[str], str]] = []
    stored_password: str | None = None

    def runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((argv, kwargs))
        if argv[1] == "find-generic-password":
            if stored_password is not None:
                return subprocess.CompletedProcess(argv, 0, stored_password, "")
            return subprocess.CompletedProcess(argv, 44, "", "item not found")
        return subprocess.CompletedProcess(argv, 0, "", "")

    def prompt_runner(argv: Sequence[str], password: str) -> int:
        nonlocal stored_password
        prompt_calls.append((list(argv), password))
        stored_password = password
        return 0

    security = tmp_path / "security"
    security.write_text("")
    provider = MacOSKeychainGmailArchiveKeyProvider(
        tmp_path,
        runner=runner,
        prompt_runner=prompt_runner,
        security_path=security,
        platform="darwin",
    )
    key = provider.load_or_create_key()
    add_argv, prompted_password = prompt_calls[-1]
    encoded = base64.b64encode(key).decode()

    assert len(key) == 32
    assert add_argv[-2:] == ["-U", "-w"]
    assert encoded not in add_argv
    assert prompted_password == encoded
    assert all(call[0][1] != "add-generic-password" for call in calls)


def test_keychain_prompt_success_requires_valid_round_trip(tmp_path: Path) -> None:
    find_calls = 0

    def runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal find_calls
        del kwargs
        find_calls += 1
        if find_calls == 1:
            return subprocess.CompletedProcess(argv, 44, "", "item not found")
        return subprocess.CompletedProcess(argv, 0, "", "")

    security = tmp_path / "security"
    security.write_text("")
    provider = MacOSKeychainGmailArchiveKeyProvider(
        tmp_path,
        runner=runner,
        prompt_runner=lambda argv, password: 0,
        security_path=security,
        platform="darwin",
    )

    with pytest.raises(GmailArchiveLockedError, match="wrong length"):
        provider.load_or_create_key()

    assert find_calls == 2


def test_keychain_prompt_failure_does_not_claim_key_creation(tmp_path: Path) -> None:
    find_calls = 0

    def runner(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        nonlocal find_calls
        del kwargs
        find_calls += 1
        return subprocess.CompletedProcess(argv, 44, "", "item not found")

    def prompt_runner(argv: Sequence[str], password: str) -> int:
        assert password not in argv
        return 7

    security = tmp_path / "security"
    security.write_text("")
    provider = MacOSKeychainGmailArchiveKeyProvider(
        tmp_path,
        runner=runner,
        prompt_runner=prompt_runner,
        security_path=security,
        platform="darwin",
    )

    with pytest.raises(GmailArchiveLockedError, match="Unable to create"):
        provider.load_or_create_key()

    assert find_calls == 2


def test_security_prompt_runner_answers_both_pty_prompts() -> None:
    script = (
        "import getpass,sys; "
        "first=getpass.getpass('password data for new item:'); "
        "second=getpass.getpass('retype password for new item:'); "
        "sys.exit(0 if first and first == second else 9)"
    )

    result = _run_security_password_prompt(
        [sys.executable, "-c", script],
        "unit-test-secret",
        timeout_seconds=3,
    )

    assert result == 0


def test_security_prompt_runner_fails_if_second_prompt_never_arrives() -> None:
    script = (
        "import getpass,sys; "
        "getpass.getpass('password data for new item:'); "
        "sys.exit(7)"
    )

    with pytest.raises(GmailArchiveLockedError, match="Keychain prompt"):
        _run_security_password_prompt(
            [sys.executable, "-c", script],
            "unit-test-secret",
            timeout_seconds=3,
        )


def test_archive_encrypts_searches_and_opens_bounded_mail(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.initialize()
    store.provision_key()
    state = _state(processed=2)
    result = store.apply_batch(
        "gmail.primary",
        messages=(
            _message(
                "m1",
                "t1",
                subject="Project Aurora",
                body="secret-body-needle needs a reply",
                attachment=True,
            ),
            _message(
                "m2",
                "t2",
                subject="Marketing blast",
                body="hidden-promotion-needle",
                labels=("SPAM",),
            ),
        ),
        state=state,
    )

    assert (result.inserted, result.updated, result.deleted) == (2, 0, 0)
    assert store.get_state("gmail.primary") == state
    hits = store.search("gmail.primary", "Aurora reply", from_address="alice@example.com")
    assert [item.message_id for item in hits] == ["m1"]
    assert hits[0].attachment_filenames == ("plan.txt",)
    assert store.search("gmail.primary", "hidden-promotion-needle") == ()
    assert store.search(
        "gmail.primary", "hidden-promotion-needle", include_spam_trash=True
    )[0].message_id == "m2"

    thread = store.open_thread("gmail.primary", "t1", max_body_chars=12)
    assert thread.total_messages == 1
    assert thread.truncated
    assert thread.messages[0].body_text == "secret-body-"
    assert thread.messages[0].attachments[0].filename == "plan.txt"
    assert thread.messages[0].attachments[0].size == len(b"attachment bytes")

    status = store.status("gmail.primary")
    assert status.key_state == "available"
    assert (
        status.message_count,
        status.active_message_count,
        status.deleted_count,
        status.thread_count,
        status.hidden_count,
    ) == (2, 2, 0, 2, 1)
    assert status.state == state

    with sqlite3.connect(store.db_path) as conn:
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
    assert tables == {"archive_meta", "messages", "sync_state"}
    for candidate in (
        store.db_path,
        Path(f"{store.db_path}-wal"),
        Path(f"{store.db_path}-shm"),
    ):
        if candidate.exists():
            contents = candidate.read_bytes()
            assert b"Project Aurora" not in contents
            assert b"secret-body-needle" not in contents
            assert _message(
                "m1", "t1", subject="Project Aurora", body="secret-body-needle"
            ).raw_rfc822 not in contents


def test_updates_delete_in_place_and_retain_ciphertext(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.initialize()
    store.provision_key()
    store.apply_batch(
        "gmail.primary",
        messages=(_message("m1", "t1", subject="One", body="retained"),),
        state=_state(processed=1),
    )
    with sqlite3.connect(store.db_path) as conn:
        original = conn.execute(
            "SELECT raw_ciphertext FROM messages WHERE message_id='m1'"
        ).fetchone()[0]

    result = store.apply_batch(
        "gmail.primary",
        deleted_message_ids=("m1", "never-seen"),
        state=_state(phase="live", processed=1, coverage_complete=True),
    )
    assert result.deleted == 1
    assert store.search("gmail.primary", "retained") == ()
    assert store.open_thread("gmail.primary", "t1").total_messages == 0
    with sqlite3.connect(store.db_path) as conn:
        deleted, ciphertext = conn.execute(
            "SELECT deleted, raw_ciphertext FROM messages WHERE message_id='m1'"
        ).fetchone()
    assert deleted == 1
    assert ciphertext == original

    updated = _message("m1", "t1", subject="Two", body="restored")
    applied = store.apply_batch(
        "gmail.primary", messages=(updated,), state=_state(phase="live", processed=2)
    )
    assert (applied.inserted, applied.updated) == (0, 1)
    assert store.search("gmail.primary", "restored")[0].message_id == "m1"


def test_thread_snapshot_revision_tracks_updates_and_deletion(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.initialize()
    store.provision_key()
    store.apply_batch(
        "gmail.primary",
        messages=(_message("m1", "t1", subject="One", body="first"),),
        state=_state(processed=1),
    )

    initial = store.get_thread_snapshot("gmail.primary", "t1")
    assert initial is not None
    assert initial.total_message_count == 1
    assert initial.account_key == "gmail.primary"
    assert initial.visible_message_count == 1
    assert initial.deleted_message_count == 0
    assert initial.hidden_message_count == 0
    assert initial.created_at == initial.updated_at
    assert initial.archive_updated_at == "2026-07-14T18:00:00.000+00:00"
    assert initial.raw_size == len(
        _message("m1", "t1", subject="One", body="first").raw_rfc822
    )
    assert store.list_thread_snapshots("gmail.primary") == (initial,)

    store.apply_batch(
        "gmail.primary",
        messages=(_message("m1", "t1", subject="Two", body="second body"),),
        state=_state(updated_at="2026-07-14T18:05:00+00:00", processed=2),
    )
    updated = store.get_thread_snapshot("gmail.primary", "t1")
    assert updated is not None
    assert updated.source_revision != initial.source_revision
    assert updated.archive_updated_at == "2026-07-14T18:05:00.000+00:00"

    store.apply_batch(
        "gmail.primary",
        deleted_message_ids=("m1",),
        state=_state(updated_at="2026-07-14T18:10:00+00:00", processed=2),
    )
    deleted = store.get_thread_snapshot("gmail.primary", "t1")
    assert deleted is not None
    assert deleted.source_revision != updated.source_revision
    assert deleted.total_message_count == 1
    assert deleted.visible_message_count == 0
    assert deleted.deleted_message_count == 1
    assert deleted.hidden_message_count == 0
    assert deleted.archive_updated_at == "2026-07-14T18:10:00.000+00:00"
    assert store.get_thread_snapshot("gmail.primary", "missing") is None


def test_thread_revision_tracks_same_size_same_clock_content_and_labels(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.initialize()
    store.provision_key()
    first_message = _message(
        "m1", "t1", subject="Alpha", body="first-value", labels=("INBOX",)
    )
    second_message = _message(
        "m1", "t1", subject="Bravo", body="other-value", labels=("INBOX",)
    )
    assert len(first_message.raw_rfc822) == len(second_message.raw_rfc822)
    store.apply_batch(
        "gmail.primary", messages=(first_message,), state=_state(processed=1)
    )
    first = store.get_thread_snapshot("gmail.primary", "t1")
    assert first is not None

    store.apply_batch(
        "gmail.primary", messages=(second_message,), state=_state(processed=2)
    )
    content_changed = store.get_thread_snapshot("gmail.primary", "t1")
    assert content_changed is not None
    assert content_changed.archive_updated_at == first.archive_updated_at
    assert content_changed.raw_size == first.raw_size
    assert content_changed.source_revision != first.source_revision

    store.apply_batch(
        "gmail.primary",
        messages=(replace(second_message, label_ids=("INBOX", "STARRED")),),
        state=_state(processed=3),
    )
    labels_changed = store.get_thread_snapshot("gmail.primary", "t1")
    assert labels_changed is not None
    assert labels_changed.source_revision != content_changed.source_revision


def test_thread_revision_is_deterministic_across_archive_rebuilds(
    tmp_path: Path,
) -> None:
    revisions: list[str] = []
    message = _message(
        "m1",
        "t1",
        subject="Deterministic projection",
        body="Stable source content",
        labels=("INBOX", "CATEGORY_UPDATES"),
    )
    for index, updated_at in enumerate(
        ("2026-07-14T18:00:00+00:00", "2026-07-15T09:30:00+00:00")
    ):
        root = tmp_path / f"rebuild-{index}"
        root.mkdir()
        store = _store(root)
        store.initialize()
        store.provision_key()
        store.apply_batch(
            "gmail.primary",
            messages=(
                replace(message, label_ids=tuple(reversed(message.label_ids)))
                if index
                else message,
            ),
            state=_state(updated_at=updated_at, processed=1),
        )
        snapshot = store.get_thread_snapshot("gmail.primary", "t1")
        assert snapshot is not None
        revisions.append(snapshot.source_revision)

    assert revisions[0] == revisions[1]


def test_legacy_archive_revision_digests_are_backfilled_once(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.initialize()
    store.provision_key()
    store.apply_batch(
        "gmail.primary",
        messages=(
            _message(
                "m1",
                "t1",
                subject="Legacy digest migration",
                body="Stable encrypted source",
                labels=("INBOX",),
            ),
        ),
        state=_state(processed=1),
    )
    before = store.get_thread_snapshot("gmail.primary", "t1")
    assert before is not None
    with sqlite3.connect(store.db_path) as conn:
        conn.execute("UPDATE archive_meta SET schema_version=1 WHERE id=1")
        conn.execute(
            "UPDATE messages SET raw_digest=NULL, metadata_digest=NULL WHERE message_id='m1'"
        )

    migrated = store.get_thread_snapshot("gmail.primary", "t1")

    assert migrated == before
    with sqlite3.connect(store.db_path) as conn:
        version = conn.execute(
            "SELECT schema_version FROM archive_meta WHERE id=1"
        ).fetchone()[0]
        digests = conn.execute(
            "SELECT raw_digest, metadata_digest FROM messages WHERE message_id='m1'"
        ).fetchone()
    assert version == 2
    assert all(len(str(value)) == 64 for value in digests)


def test_open_thread_retains_newest_messages_and_reports_truncation_counts(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.initialize()
    store.provision_key()
    messages = tuple(
        replace(
            _message(
                f"m{index}",
                "t1",
                subject=f"Message {index}",
                body=f"body-{index}-" * 5,
            ),
            internal_date=f"178405200{index}000",
        )
        for index in range(1, 4)
    )
    store.apply_batch(
        "gmail.primary",
        messages=messages,
        state=_state(processed=3),
    )

    thread = store.open_thread(
        "gmail.primary",
        "t1",
        max_messages=2,
        max_body_chars=45,
    )

    assert thread.account_key == "gmail.primary"
    assert thread.total_messages == 3
    assert thread.omitted_message_count == 1
    assert thread.body_truncated_message_count == 1
    assert thread.truncated is True
    assert [message.message_id for message in thread.messages] == ["m2", "m3"]
    assert thread.messages[1].body_text == "body-3-" * 5 + "\r\n"
    assert thread.messages[1].body_truncated is False
    assert thread.messages[0].body_truncated is True
    assert all(message.account_key == "gmail.primary" for message in thread.messages)


def test_thread_snapshots_include_hidden_only_and_tombstoned_threads(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.initialize()
    store.provision_key()
    store.apply_batch(
        "gmail.primary",
        messages=(
            _message(
                "hidden-message",
                "hidden-thread",
                subject="Hidden",
                body="spam",
                labels=("SPAM",),
            ),
            _message(
                "deleted-message",
                "deleted-thread",
                subject="Deleted",
                body="gone",
            ),
        ),
        state=_state(processed=2),
    )
    store.apply_batch(
        "gmail.primary",
        deleted_message_ids=("deleted-message",),
        state=_state(updated_at="2026-07-14T18:05:00+00:00", processed=2),
    )

    snapshots = {
        item.thread_id: item for item in store.list_thread_snapshots("gmail.primary")
    }
    assert set(snapshots) == {"deleted-thread", "hidden-thread"}
    assert (
        snapshots["hidden-thread"].total_message_count,
        snapshots["hidden-thread"].visible_message_count,
        snapshots["hidden-thread"].deleted_message_count,
        snapshots["hidden-thread"].hidden_message_count,
    ) == (1, 0, 0, 1)
    assert (
        snapshots["deleted-thread"].total_message_count,
        snapshots["deleted-thread"].visible_message_count,
        snapshots["deleted-thread"].deleted_message_count,
        snapshots["deleted-thread"].hidden_message_count,
    ) == (1, 0, 1, 0)


def test_labels_are_encrypted_and_old_payloads_default_to_no_labels(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.initialize()
    store.provision_key()
    secret_label = "TOP-SECRET-LABEL-9917"
    store.apply_batch(
        "gmail.primary",
        messages=(
            _message(
                "m1",
                "t1",
                subject="Labels",
                body="body",
                labels=(secret_label, "INBOX"),
            ),
        ),
        state=_state(processed=1),
    )
    assert store.open_thread("gmail.primary", "t1").messages[0].label_ids == (
        secret_label,
        "INBOX",
    )

    for candidate in (
        store.db_path,
        Path(f"{store.db_path}-wal"),
        Path(f"{store.db_path}-shm"),
    ):
        if candidate.exists():
            assert secret_label.encode() not in candidate.read_bytes()

    # Recreate the encrypted text payload shape written by schema-v1 archives
    # before labels were included. Opening it must remain backward compatible.
    with sqlite3.connect(store.db_path) as conn:
        nonce, ciphertext = conn.execute(
            "SELECT text_nonce, text_ciphertext FROM messages WHERE message_id='m1'"
        ).fetchone()
        aad = b"pkm-brain/gmail-archive/v1/text/gmail.primary/m1"
        payload = json.loads(AESGCM(KEY).decrypt(nonce, ciphertext, aad))
        payload.pop("label_ids")
        replacement_nonce = b"n" * 12
        replacement_ciphertext = AESGCM(KEY).encrypt(
            replacement_nonce,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(),
            aad,
        )
        conn.execute(
            """
            UPDATE messages SET text_nonce=?, text_ciphertext=?
            WHERE message_id='m1'
            """,
            (replacement_nonce, replacement_ciphertext),
        )

    assert store.open_thread("gmail.primary", "t1").messages[0].label_ids == ()


def test_attached_rfc822_text_is_not_exposed_as_parent_body(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.initialize()
    store.provision_key()
    raw = (
        "From: Alice <alice@example.com>\r\n"
        "To: Peter <peter@example.com>\r\n"
        "Subject: Outer message\r\n"
        "Date: Tue, 14 Jul 2026 10:00:00 -0700\r\n"
        "List-Id: Example List <example.list.example.com>\r\n"
        "List-Unsubscribe: <mailto:leave@example.com>\r\n"
        "Precedence: bulk\r\n"
        "Auto-Submitted: auto-generated\r\n"
        "MIME-Version: 1.0\r\n"
        'Content-Type: multipart/mixed; boundary="outer"\r\n\r\n'
        "--outer\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n\r\n"
        "visible parent text\r\n"
        "--outer\r\n"
        "Content-Type: message/rfc822\r\n"
        'Content-Disposition: attachment; filename="forwarded.eml"\r\n\r\n'
        "From: Nested <nested@example.com>\r\n"
        "To: Alice <alice@example.com>\r\n"
        "Subject: Nested secret\r\n"
        "Content-Type: text/plain; charset=utf-8\r\n\r\n"
        "nested-secret-body-must-not-leak\r\n"
        "--outer--\r\n"
    ).encode()
    store.apply_batch(
        "gmail.primary",
        messages=(
            ArchiveMessage(
                message_id="m1",
                thread_id="t1",
                raw_rfc822=raw,
                internal_date="1784052000000",
            ),
        ),
        state=_state(processed=1),
    )

    opened = store.open_thread("gmail.primary", "t1").messages[0]
    assert "visible parent text" in opened.body_text
    assert "nested-secret-body-must-not-leak" not in opened.body_text
    assert opened.attachments[0].filename == "forwarded.eml"
    assert opened.attachments[0].content_type == "message/rfc822"
    assert opened.list_id == "Example List <example.list.example.com>"
    assert opened.list_unsubscribe == "<mailto:leave@example.com>"
    assert opened.precedence == "bulk"
    assert opened.auto_submitted == "auto-generated"
    assert store.search("gmail.primary", "nested-secret-body-must-not-leak") == ()


def test_missing_or_wrong_key_never_reprovisions_existing_archive(tmp_path: Path) -> None:
    provider = StaticGmailArchiveKeyProvider(KEY)
    directory = tmp_path / "archive"
    directory.mkdir(mode=0o700)
    store = GmailArchiveStore(directory / "gmail.sqlite", provider)
    store.initialize()
    store.provision_key()
    store.apply_batch(
        "gmail.primary",
        messages=(_message("m1", "t1", subject="One", body="body"),),
        state=_state(),
    )

    provider.delete_key()
    assert store.status("gmail.primary").key_state == "key_missing"
    with pytest.raises(GmailArchiveLockedError):
        store.search("gmail.primary", "body")
    with pytest.raises(GmailArchiveLockedError):
        store.provision_key()
    assert provider.key is None

    provider.key = b"w" * 32
    assert store.status("gmail.primary").key_state == "wrong_key"
    with pytest.raises(GmailArchiveIntegrityError):
        store.provision_key()
    assert provider.key == b"w" * 32


def test_authenticated_ciphertext_tampering_fails_closed(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.initialize()
    store.provision_key()
    store.apply_batch(
        "gmail.primary",
        messages=(_message("m1", "t1", subject="One", body="body"),),
        state=_state(),
    )
    with sqlite3.connect(store.db_path) as conn:
        value = bytearray(
            conn.execute(
                "SELECT text_ciphertext FROM messages WHERE message_id='m1'"
            ).fetchone()[0]
        )
        value[-1] ^= 1
        conn.execute(
            "UPDATE messages SET text_ciphertext=? WHERE message_id='m1'", (bytes(value),)
        )
    with pytest.raises(GmailArchiveIntegrityError):
        store.search("gmail.primary", "body")

    with sqlite3.connect(store.db_path) as conn:
        value = bytearray(
            conn.execute(
                "SELECT raw_ciphertext FROM messages WHERE message_id='m1'"
            ).fetchone()[0]
        )
        value[-1] ^= 1
        conn.execute(
            "UPDATE messages SET raw_ciphertext=? WHERE message_id='m1'", (bytes(value),)
        )
    with pytest.raises(GmailArchiveIntegrityError):
        store.open_thread("gmail.primary", "t1")


def test_status_does_not_create_archive_and_paths_fail_closed(tmp_path: Path) -> None:
    directory = tmp_path / "archive"
    directory.mkdir(mode=0o700)
    store = GmailArchiveStore(
        directory / "gmail.sqlite", StaticGmailArchiveKeyProvider(KEY)
    )
    assert store.status("gmail.primary").key_state == "uninitialized"
    assert not store.db_path.exists()

    insecure = tmp_path / "insecure"
    insecure.mkdir(mode=0o755)
    with pytest.raises(GmailArchiveSecurityError):
        GmailArchiveStore(
            insecure / "gmail.sqlite", StaticGmailArchiveKeyProvider(KEY)
        ).initialize()

    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    link = tmp_path / "linked"
    os.symlink(target, link)
    with pytest.raises(GmailArchiveSecurityError):
        GmailArchiveStore(
            link / "gmail.sqlite", StaticGmailArchiveKeyProvider(KEY)
        ).initialize()


def test_invalid_batch_does_not_advance_state(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.initialize()
    store.provision_key()
    initial = _state(processed=0)
    store.apply_batch("gmail.primary", state=initial)

    with pytest.raises(ValueError):
        store.apply_batch(
            "gmail.primary",
            messages=(ArchiveMessage("m1", "t1", b""),),
            state=_state(processed=1),
        )
    assert store.get_state("gmail.primary") == initial

    with pytest.raises(ValueError):
        store.apply_batch(
            "gmail.primary",
            state=_state(error="x" * 2_001),
        )
    assert store.get_state("gmail.primary") == initial
