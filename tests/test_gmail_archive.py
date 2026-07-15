from __future__ import annotations

import base64
import os
import sqlite3
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest

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
