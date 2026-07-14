#!/usr/bin/env python3
"""Benchmark a private Gmail corpus in an isolated PKM Brain home.

This is an experiment harness, not a production Gmail connector. It keeps Gmail
content under a caller-selected private root, never opens the live Brain DB, and
passes only explicitly selected representative days to fact extraction.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import dataclasses
import email.utils
import gzip
import html
import json
import math
import os
import re
import shutil
import sqlite3
import socket
import statistics
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable
from zoneinfo import ZoneInfo

from pkm_brain.cos_policy import (
    CURATION_STRICTNESS_PROFILES,
    active_policy_version,
    promote_policy_for_autonomy,
)
from pkm_brain.curation_settings import load_curation_settings
from pkm_brain.db import connection
from pkm_brain.evals import run_eval
from pkm_brain.extraction import extract_recent_documents, load_extraction_config
from pkm_brain.llm_usage import llm_usage_summary
from pkm_brain.paths import BrainPaths
from pkm_brain.policy_action_batch import decide_policy_actions
from pkm_brain.service import BrainService


DEFAULT_ROOT = Path(
    "~/Library/Application Support/PKM Brain Experiments/gmail-90d-20260713"
).expanduser()
DEFAULT_LIVE_HOME = Path("~/brain").expanduser()
KEYCHAIN_SERVICE = "google-workspace-mcp-codex"
GMAIL_API_ROOT = "https://gmail.googleapis.com/gmail/v1/users/me"
TOKEN_URL = "https://oauth2.googleapis.com/token"
LOCAL_TIMEZONE = ZoneInfo("America/Los_Angeles")
MAX_BODY_CHARS = 30_000
MIN_FACT_BODY_CHARS = 120
RETRYABLE_HTTP_CODES = {403, 408, 429, 500, 502, 503, 504}
PRIVATE_FILE_MODE = 0o600
PRIVATE_DIR_MODE = 0o700

REPLY_MARKERS = (
    re.compile(r"^\s*On .{1,500} wrote:\s*$", re.IGNORECASE),
    re.compile(r"^\s*-{2,}\s*Original Message\s*-{2,}\s*$", re.IGNORECASE),
    re.compile(r"^\s*Begin forwarded message:\s*$", re.IGNORECASE),
    re.compile(r"^\s*_{5,}\s*$"),
)
NO_REPLY_PATTERN = re.compile(
    r"(?:^|[<\s])(?:no[-_.]?reply|do[-_.]?not[-_.]?reply|notifications?|alerts?|mailer-daemon)@",
    re.IGNORECASE,
)
TRANSACTIONAL_SUBJECT_PATTERN = re.compile(
    r"\b(receipt|invoice|order|shipp(?:ed|ing)|delivery|verification|security alert|"
    r"password|statement|payment|reservation|confirmation|one[- ]time code|otp)\b",
    re.IGNORECASE,
)


@dataclasses.dataclass(frozen=True)
class NormalizedThread:
    thread_id: str
    path: Path
    classification: str
    fact_eligible: bool
    created_at: str
    updated_at: str
    updated_date: str
    message_count: int
    source_size_estimate: int
    normalized_bytes: int
    body_chars: int
    quoted_chars_removed: int
    attachment_count: int
    truncated_message_count: int

    def as_dict(self) -> dict[str, Any]:
        value = dataclasses.asdict(self)
        value["path"] = str(self.path)
        return value


class PlainTextHTMLParser(HTMLParser):
    BLOCK_TAGS = {
        "address",
        "article",
        "aside",
        "blockquote",
        "br",
        "div",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "p",
        "pre",
        "section",
        "table",
        "td",
        "th",
        "tr",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        lowered = tag.lower()
        if lowered in {"script", "style"}:
            self.ignored_depth += 1
        elif lowered in self.BLOCK_TAGS and self.parts:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered in {"script", "style"} and self.ignored_depth:
            self.ignored_depth -= 1
        elif lowered in self.BLOCK_TAGS and self.parts:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.ignored_depth:
            self.parts.append(data)

    def text(self) -> str:
        return normalize_whitespace("".join(self.parts))


class GmailClient:
    def __init__(
        self, *, timeout_seconds: int = 60, requests_per_second: float = 2.0
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self._access_token: str | None = None
        self._token_lock = threading.Lock()
        self._rate_lock = threading.Lock()
        self._request_interval = 1.0 / max(0.1, requests_per_second)
        self._next_request_at = 0.0
        self._install_ipv4_preference()

    @staticmethod
    def _install_ipv4_preference() -> None:
        """Avoid a known local IPv6 route that stalls Google API connections."""
        original = socket.getaddrinfo
        if getattr(original, "_pkm_gmail_ipv4_preferred", False):
            return

        def ipv4_first(*args: Any, **kwargs: Any) -> list[Any]:
            addresses = original(*args, **kwargs)
            ipv4 = [item for item in addresses if item[0] == socket.AF_INET]
            return ipv4 or addresses

        setattr(ipv4_first, "_pkm_gmail_ipv4_preferred", True)
        socket.getaddrinfo = ipv4_first

    def keychain_value(self, account: str) -> str:
        completed = subprocess.run(
            [
                "/usr/bin/security",
                "find-generic-password",
                "-s",
                KEYCHAIN_SERVICE,
                "-a",
                account,
                "-w",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        value = completed.stdout.strip()
        if not value:
            raise RuntimeError(f"Keychain item {account!r} is empty")
        return value

    def refresh_access_token(self) -> str:
        with self._token_lock:
            payload = urllib.parse.urlencode(
                {
                    "client_id": self.keychain_value("client_id"),
                    "client_secret": self.keychain_value("client_secret"),
                    "refresh_token": self.keychain_value("refresh_token"),
                    "grant_type": "refresh_token",
                }
            ).encode("ascii")
            request = urllib.request.Request(TOKEN_URL, data=payload, method="POST")
            with urllib.request.urlopen(
                request, timeout=self.timeout_seconds
            ) as response:
                body = json.load(response)
            token = str(body.get("access_token") or "").strip()
            if not token:
                raise RuntimeError("Google token response did not contain an access token")
            self._access_token = token
            return token

    def wait_for_request_slot(self) -> None:
        with self._rate_lock:
            now = time.monotonic()
            scheduled = max(now, self._next_request_at)
            self._next_request_at = scheduled + self._request_interval
        delay = scheduled - now
        if delay > 0:
            time.sleep(delay)

    def get_json(
        self,
        relative_path: str,
        *,
        params: dict[str, str | int] | None = None,
        attempts: int = 6,
    ) -> dict[str, Any]:
        query = urllib.parse.urlencode(params or {})
        url = f"{GMAIL_API_ROOT}/{relative_path.lstrip('/')}"
        if query:
            url = f"{url}?{query}"
        last_error: Exception | None = None
        for attempt in range(attempts):
            token = self._access_token or self.refresh_access_token()
            self.wait_for_request_slot()
            request = urllib.request.Request(
                url,
                headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            )
            try:
                with urllib.request.urlopen(
                    request, timeout=self.timeout_seconds
                ) as response:
                    payload = json.load(response)
                if not isinstance(payload, dict):
                    raise RuntimeError("Gmail API returned a non-object response")
                return payload
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code == 401 and attempt == 0:
                    self._access_token = None
                    continue
                if exc.code not in RETRYABLE_HTTP_CODES:
                    raise
            except (TimeoutError, urllib.error.URLError) as exc:
                last_error = exc
            if attempt + 1 < attempts:
                time.sleep(min(30.0, (2**attempt) + (attempt * 0.25)))
        raise RuntimeError(f"Gmail API request failed after {attempts} attempts") from last_error


def make_private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(PRIVATE_DIR_MODE)


def write_private_bytes(path: Path, payload: bytes) -> None:
    make_private_directory(path.parent)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_bytes(payload)
    temporary.chmod(PRIVATE_FILE_MODE)
    temporary.replace(path)
    path.chmod(PRIVATE_FILE_MODE)


def write_private_text(path: Path, payload: str) -> None:
    write_private_bytes(path, payload.encode("utf-8"))


def write_private_json(path: Path, payload: Any) -> None:
    write_private_text(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def normalized_header_map(payload: dict[str, Any]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = defaultdict(list)
    for item in payload.get("headers") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip().lower()
        value = sanitize_header(str(item.get("value") or ""))
        if name and value:
            result[name].append(value)
    return dict(result)


def first_header(headers: dict[str, list[str]], name: str) -> str:
    values = headers.get(name.lower()) or []
    return values[0] if values else ""


def sanitize_header(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def decode_body_data(value: str, charset: str = "utf-8") -> str:
    if not value:
        return ""
    padded = value + ("=" * (-len(value) % 4))
    raw = base64.urlsafe_b64decode(padded.encode("ascii"))
    try:
        return raw.decode(charset, errors="replace")
    except LookupError:
        return raw.decode("utf-8", errors="replace")


def charset_for_part(part: dict[str, Any]) -> str:
    headers = normalized_header_map(part)
    content_type = first_header(headers, "content-type")
    match = re.search(r"charset\s*=\s*[\"']?([^;\s\"']+)", content_type, re.I)
    return match.group(1) if match else "utf-8"


def is_attachment_part(part: dict[str, Any]) -> bool:
    filename = str(part.get("filename") or "").strip()
    headers = normalized_header_map(part)
    disposition = first_header(headers, "content-disposition").lower()
    return bool(filename or "attachment" in disposition)


def flatten_text_parts(part: dict[str, Any]) -> tuple[list[str], list[str], int]:
    """Return plain parts, HTML parts, and skipped attachment count."""
    if is_attachment_part(part):
        return [], [], 1
    mime_type = str(part.get("mimeType") or "").lower()
    children = [item for item in part.get("parts") or [] if isinstance(item, dict)]
    if children:
        plain: list[str] = []
        html_parts: list[str] = []
        attachments = 0
        for child in children:
            child_plain, child_html, child_attachments = flatten_text_parts(child)
            plain.extend(child_plain)
            html_parts.extend(child_html)
            attachments += child_attachments
        return plain, html_parts, attachments
    data = str((part.get("body") or {}).get("data") or "")
    if not data:
        attachment_id = str((part.get("body") or {}).get("attachmentId") or "")
        return [], [], int(bool(attachment_id))
    decoded = decode_body_data(data, charset_for_part(part))
    if mime_type == "text/plain":
        return [decoded], [], 0
    if mime_type == "text/html":
        return [], [decoded], 0
    return [], [], 0


def html_to_text(value: str) -> str:
    parser = PlainTextHTMLParser()
    parser.feed(value)
    parser.close()
    return parser.text()


def normalize_whitespace(value: str) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = "\n".join(line.rstrip() for line in value.splitlines())
    value = re.sub(r"[ \t]+", " ", value)
    value = re.sub(r"\n{3,}", "\n\n", value)
    return html.unescape(value).strip()


def strip_quoted_reply(value: str) -> tuple[str, int]:
    value = normalize_whitespace(value)
    lines = value.splitlines()
    cut_at = len(lines)
    for index, line in enumerate(lines):
        if any(pattern.match(line) for pattern in REPLY_MARKERS):
            cut_at = index
            break
        if (
            line.lower().startswith("from:")
            and index + 2 < len(lines)
            and lines[index + 1].lower().startswith(("sent:", "date:"))
            and lines[index + 2].lower().startswith("to:")
        ):
            cut_at = index
            break
    kept = lines[:cut_at]
    while kept and kept[-1].lstrip().startswith(">"):
        kept.pop()
    cleaned = normalize_whitespace("\n".join(kept))
    return cleaned, max(0, len(value) - len(cleaned))


def message_body(message: dict[str, Any]) -> tuple[str, int, int, bool]:
    plain, html_parts, attachment_count = flatten_text_parts(message.get("payload") or {})
    source = "\n\n".join(part for part in plain if part.strip())
    if not source:
        source = "\n\n".join(html_to_text(part) for part in html_parts if part.strip())
    cleaned, quoted_removed = strip_quoted_reply(source)
    truncated = len(cleaned) > MAX_BODY_CHARS
    if truncated:
        cleaned = cleaned[:MAX_BODY_CHARS].rstrip() + "\n\n[Message body truncated]"
    return cleaned, quoted_removed, attachment_count, truncated


def local_message_datetime(message: dict[str, Any]) -> datetime | None:
    raw = str(message.get("internalDate") or "").strip()
    if raw.isdigit():
        return datetime.fromtimestamp(int(raw) / 1000, tz=timezone.utc).astimezone(
            LOCAL_TIMEZONE
        )
    headers = normalized_header_map(message.get("payload") or {})
    parsed = email.utils.parsedate_to_datetime(first_header(headers, "date"))
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(LOCAL_TIMEZONE)


def thread_classification(messages: list[dict[str, Any]]) -> str:
    labels = {
        str(label)
        for message in messages
        for label in (message.get("labelIds") or [])
    }
    if "SENT" in labels:
        return "human"
    if labels.intersection({"CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_FORUMS"}):
        return "bulk"
    subjects: list[str] = []
    for message in messages:
        headers = normalized_header_map(message.get("payload") or {})
        sender = first_header(headers, "from")
        subjects.append(first_header(headers, "subject"))
        precedence = first_header(headers, "precedence").lower()
        auto_submitted = first_header(headers, "auto-submitted").lower()
        if first_header(headers, "list-unsubscribe") or first_header(headers, "list-id"):
            return "bulk"
        if precedence in {"bulk", "list", "junk"}:
            return "bulk"
        if NO_REPLY_PATTERN.search(sender):
            return "transactional"
        if auto_submitted and auto_submitted != "no":
            return "transactional"
    if "CATEGORY_UPDATES" in labels or any(
        TRANSACTIONAL_SUBJECT_PATTERN.search(subject) for subject in subjects
    ):
        return "transactional"
    return "human"


def frontmatter_string(value: str) -> str:
    return json.dumps(sanitize_header(value), ensure_ascii=True)


def display_subject(messages: list[dict[str, Any]]) -> str:
    for message in reversed(messages):
        headers = normalized_header_map(message.get("payload") or {})
        subject = first_header(headers, "subject")
        if subject:
            return subject
    return "Email thread"


def normalized_thread_document(
    thread: dict[str, Any],
    *,
    start_date: date,
    end_date_exclusive: date,
    corpus_root: Path,
) -> tuple[NormalizedThread, str] | None:
    thread_id = str(thread.get("id") or "").strip()
    messages_with_dates: list[tuple[datetime, dict[str, Any]]] = []
    for message in thread.get("messages") or []:
        if not isinstance(message, dict):
            continue
        message_date = local_message_datetime(message)
        if message_date and start_date <= message_date.date() < end_date_exclusive:
            messages_with_dates.append((message_date, message))
    messages_with_dates.sort(key=lambda item: item[0])
    if not thread_id or not messages_with_dates:
        return None
    messages = [item[1] for item in messages_with_dates]
    classification = thread_classification(messages)
    created_at = messages_with_dates[0][0].isoformat()
    updated_at = messages_with_dates[-1][0].isoformat()
    title = display_subject(messages)
    sections: list[str] = []
    total_body_chars = 0
    quoted_chars_removed = 0
    attachment_count = 0
    truncated_message_count = 0
    source_size_estimate = 0
    for index, (message_date, message) in enumerate(messages_with_dates, start=1):
        headers = normalized_header_map(message.get("payload") or {})
        body, removed, attachments, truncated = message_body(message)
        total_body_chars += len(body)
        quoted_chars_removed += removed
        attachment_count += attachments
        truncated_message_count += int(truncated)
        source_size_estimate += int(message.get("sizeEstimate") or 0)
        metadata = [
            f"## Message {index} - {message_date.isoformat()}",
            "",
            f"From: {first_header(headers, 'from') or '(unknown)'}",
            f"To: {first_header(headers, 'to') or '(unknown)'}",
        ]
        cc = first_header(headers, "cc")
        if cc:
            metadata.append(f"Cc: {cc}")
        metadata.extend(["", body or "[No text body captured]"])
        sections.append("\n".join(metadata))
    fact_eligible = classification == "human" and total_body_chars >= MIN_FACT_BODY_CHARS
    payload = (
        "---\n"
        f"title: {frontmatter_string(title)}\n"
        "source_type: gmail_thread\n"
        f"created_at: {frontmatter_string(created_at)}\n"
        f"source_updated_at: {frontmatter_string(updated_at)}\n"
        f"gmail_thread_id: {frontmatter_string(thread_id)}\n"
        f"classification: {classification}\n"
        f"fact_eligible: {'true' if fact_eligible else 'false'}\n"
        f"message_count: {len(messages)}\n"
        f"attachment_count: {attachment_count}\n"
        "---\n\n"
        f"# {title}\n\n"
        + "\n\n".join(sections)
        + "\n"
    )
    relative = Path(updated_at[:7].replace("-", "/")) / f"thread-{thread_id}.md"
    path = corpus_root / relative
    normalized = NormalizedThread(
        thread_id=thread_id,
        path=path.resolve(),
        classification=classification,
        fact_eligible=fact_eligible,
        created_at=created_at,
        updated_at=updated_at,
        updated_date=updated_at[:10],
        message_count=len(messages),
        source_size_estimate=source_size_estimate,
        normalized_bytes=len(payload.encode("utf-8")),
        body_chars=total_body_chars,
        quoted_chars_removed=quoted_chars_removed,
        attachment_count=attachment_count,
        truncated_message_count=truncated_message_count,
    )
    return normalized, payload


def thread_cache_path(cache_root: Path, thread_id: str) -> Path:
    return cache_root / "threads" / f"{thread_id}.json.gz"


def load_cached_thread(path: Path) -> dict[str, Any]:
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise ValueError(f"Invalid cached Gmail thread: {path}")
    return payload


def write_cached_thread(path: Path, payload: dict[str, Any]) -> None:
    compressed = gzip.compress(
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"),
        compresslevel=6,
    )
    write_private_bytes(path, compressed)


def list_thread_ids(
    client: GmailClient,
    *,
    query: str,
    cache_root: Path,
    refresh: bool,
) -> list[str]:
    query_path = cache_root / "query.json"
    ids_path = cache_root / "thread_ids.json"
    if not refresh and query_path.exists() and ids_path.exists():
        cached_query = read_json(query_path)
        if cached_query.get("query") == query:
            ids = read_json(ids_path)
            if isinstance(ids, list) and all(isinstance(item, str) for item in ids):
                print(f"Using {len(ids):,} cached Gmail thread IDs", flush=True)
                return ids
    ids: list[str] = []
    page_token = ""
    page = 0
    while True:
        params: dict[str, str | int] = {"q": query, "maxResults": 500}
        if page_token:
            params["pageToken"] = page_token
        payload = client.get_json("threads", params=params)
        page += 1
        ids.extend(
            str(item.get("id"))
            for item in payload.get("threads") or []
            if isinstance(item, dict) and item.get("id")
        )
        print(f"Listed {len(ids):,} Gmail threads across {page} page(s)", flush=True)
        page_token = str(payload.get("nextPageToken") or "")
        if not page_token:
            break
    ids = list(dict.fromkeys(ids))
    write_private_json(query_path, {"query": query, "listed_at": now_iso()})
    write_private_json(ids_path, ids)
    return ids


def fetch_threads(
    client: GmailClient,
    *,
    thread_ids: list[str],
    cache_root: Path,
    workers: int,
    refresh: bool,
) -> None:
    pending = [
        thread_id
        for thread_id in thread_ids
        if refresh or not thread_cache_path(cache_root, thread_id).exists()
    ]
    if not pending:
        print(f"Using {len(thread_ids):,} cached Gmail thread payloads", flush=True)
        return

    def fetch_one(thread_id: str) -> tuple[str, dict[str, Any]]:
        return thread_id, client.get_json(
            f"threads/{urllib.parse.quote(thread_id)}", params={"format": "full"}
        )

    completed_count = len(thread_ids) - len(pending)
    failures: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        future_map = {pool.submit(fetch_one, thread_id): thread_id for thread_id in pending}
        for future in concurrent.futures.as_completed(future_map):
            thread_id = future_map[future]
            try:
                fetched_id, payload = future.result()
                write_cached_thread(thread_cache_path(cache_root, fetched_id), payload)
            except Exception as exc:  # noqa: BLE001 - aggregate API failures safely
                failures.append(f"{thread_id}: {type(exc).__name__}")
            completed_count += 1
            if completed_count % 50 == 0 or completed_count == len(thread_ids):
                print(
                    f"Fetched/cached {completed_count:,}/{len(thread_ids):,} Gmail threads",
                    flush=True,
                )
    if failures:
        raise RuntimeError(
            f"Failed to fetch {len(failures)} Gmail thread(s); rerun to retry. "
            f"Failure types: {Counter(item.rsplit(': ', 1)[-1] for item in failures)}"
        )


def normalize_corpus(
    *,
    thread_ids: list[str],
    cache_root: Path,
    corpus_root: Path,
    start_date: date,
    end_date_exclusive: date,
) -> list[NormalizedThread]:
    make_private_directory(corpus_root)
    records: list[NormalizedThread] = []
    expected_paths: set[Path] = set()
    for index, thread_id in enumerate(thread_ids, start=1):
        result = normalized_thread_document(
            load_cached_thread(thread_cache_path(cache_root, thread_id)),
            start_date=start_date,
            end_date_exclusive=end_date_exclusive,
            corpus_root=corpus_root,
        )
        if result is None:
            continue
        record, payload = result
        write_private_text(record.path, payload)
        updated = datetime.fromisoformat(record.updated_at).timestamp()
        os.utime(record.path, (updated, updated))
        expected_paths.add(record.path.resolve())
        records.append(record)
        if index % 250 == 0:
            print(f"Normalized {index:,}/{len(thread_ids):,} cached threads", flush=True)
    for stale in corpus_root.rglob("*.md"):
        if stale.resolve() not in expected_paths:
            stale.unlink()
    records.sort(key=lambda item: (item.updated_at, item.thread_id))
    return records


def select_representative_days(
    records: list[NormalizedThread],
    *,
    requested: int,
    today: date,
) -> list[str]:
    counts = Counter(
        record.updated_date
        for record in records
        if record.fact_eligible
        and date.fromisoformat(record.updated_date) < today
        and date.fromisoformat(record.updated_date).weekday() < 5
    )
    ranked = sorted(counts.items(), key=lambda item: (item[1], item[0]))
    if not ranked:
        raise RuntimeError("No complete weekdays contain fact-eligible Gmail threads")
    count = min(max(1, requested), len(ranked))
    if count == 1:
        positions = [0.5]
    else:
        positions = [(index + 1) / (count + 1) for index in range(count)]
    selected: list[str] = []
    for position in positions:
        index = min(len(ranked) - 1, max(0, round(position * (len(ranked) - 1))))
        candidate = ranked[index][0]
        if candidate not in selected:
            selected.append(candidate)
    if len(selected) < count:
        for candidate, _ in sorted(ranked, key=lambda item: item[0], reverse=True):
            if candidate not in selected:
                selected.append(candidate)
            if len(selected) == count:
                break
    return sorted(selected)


def logical_and_allocated_bytes(path: Path) -> tuple[int, int, int]:
    if not path.exists():
        return 0, 0, 0
    files = [path] if path.is_file() else [item for item in path.rglob("*") if item.is_file()]
    logical = 0
    allocated = 0
    for item in files:
        stat = item.stat()
        logical += stat.st_size
        allocated += getattr(stat, "st_blocks", math.ceil(stat.st_size / 512)) * 512
    return logical, allocated, len(files)


def storage_snapshot(root: Path) -> dict[str, Any]:
    components = {
        "sqlite": root / "db",
        "vectors": root / "indexes",
        "raw": root / "raw",
        "logs": root / "logs",
        "wiki": root / "wiki",
        "memory": root / "memory",
        "config": root / "config",
        "evals": root / "evals",
        "reports": root / "reports",
    }
    result: dict[str, Any] = {}
    for name, path in components.items():
        logical, allocated, files = logical_and_allocated_bytes(path)
        result[name] = {
            "logical_bytes": logical,
            "allocated_bytes": allocated,
            "file_count": files,
        }
    logical, allocated, files = logical_and_allocated_bytes(root)
    result["total"] = {
        "logical_bytes": logical,
        "allocated_bytes": allocated,
        "file_count": files,
    }
    return result


def sqlite_counts(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    requested = (
        "documents",
        "chunks",
        "facts",
        "entities",
        "fact_entities",
        "cos_actions",
        "open_questions",
        "cos_stage_watermarks",
    )
    result: dict[str, Any] = {}
    with sqlite3.connect(path) as conn:
        existing = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        for table in requested:
            if table in existing:
                result[table] = int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        if "chunks" in existing:
            row = conn.execute(
                "SELECT COALESCE(SUM(token_count), 0), COALESCE(SUM(LENGTH(text)), 0) FROM chunks"
            ).fetchone()
            result["chunk_tokens"] = int(row[0])
            result["chunk_text_chars"] = int(row[1])
        if "documents" in existing:
            result["document_source_bytes"] = int(
                conn.execute(
                    "SELECT COALESCE(SUM(source_size), 0) FROM documents WHERE status = 'active'"
                ).fetchone()[0]
            )
        if "facts" in existing:
            result["facts_by_status"] = dict(
                conn.execute("SELECT status, COUNT(*) FROM facts GROUP BY status").fetchall()
            )
    return result


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def prepare_test_brain(brain_home: Path, live_home: Path) -> tuple[BrainPaths, dict[str, Any]]:
    paths = BrainPaths.from_value(brain_home)
    BrainService(paths).init_workspace()
    for name in ("config.yaml", "cos_llm.yaml"):
        source = live_home / "config" / "local" / name
        if not source.exists():
            raise FileNotFoundError(f"Required production-equivalent config is missing: {source}")
        destination = paths.config_local / name
        shutil.copy2(source, destination)
        destination.chmod(PRIVATE_FILE_MODE)
    labels_source = live_home / "evals" / "extraction_labels.jsonl"
    if labels_source.exists():
        labels_destination = paths.evals / "extraction_labels.jsonl"
        shutil.copy2(labels_source, labels_destination)
        labels_destination.chmod(PRIVATE_FILE_MODE)
    return paths, storage_snapshot(paths.home)


def ensure_benchmark_policy(paths: BrainPaths) -> int:
    settings = load_curation_settings(paths)
    with connection(paths.sqlite_path) as conn:
        current = active_policy_version(conn)
        current_ids = (
            {
                str(row["id"])
                for row in conn.execute(
                    "SELECT id FROM cos_policy WHERE active = 1 AND version = ?",
                    (current,),
                )
            }
            if current is not None
            else set()
        )
        if any("fact_upsert_clean_l2" in policy_id for policy_id in current_ids):
            return int(current)
        strictness = str(settings["strictness"])
        return promote_policy_for_autonomy(
            conn,
            reason="isolated Gmail benchmark production-policy seed",
            large_topology_fact_threshold=int(settings["topology_review_threshold"]),
            strictness=strictness,
            minimum_auto_confidence=float(
                CURATION_STRICTNESS_PROFILES[strictness]["minimum_auto_confidence"]
            ),
        )


def ensure_benchmark_extraction_eval(paths: BrainPaths) -> dict[str, Any]:
    result = run_eval(paths, suite="extraction")
    suite = (result.get("reports") or [{}])[0]
    metrics = suite.get("metrics") or {}
    if not result.get("passed"):
        raise RuntimeError("Isolated labeled extraction eval did not pass")
    if metrics.get("label_policy") != "labeled" or not metrics.get("label_case_count"):
        raise RuntimeError("Isolated extraction eval lacks labeled cases")
    return {
        "passed": True,
        "label_case_count": int(metrics["label_case_count"]),
        "report_path": str(result["report_path"]),
    }


def reset_stale_sample_decisions(
    paths: BrainPaths, *, run_ids: list[str], active_version: int
) -> dict[str, int]:
    if not run_ids:
        return {"actions_reset": 0, "questions_removed": 0}
    placeholders = ",".join("?" for _ in run_ids)
    with connection(paths.sqlite_path) as conn:
        rows = list(
            conn.execute(
                f"""
                SELECT id FROM cos_actions
                WHERE run_id IN ({placeholders})
                  AND (
                    COALESCE(policy_version, 0) < ?
                    OR policy_decision = 'eval_gate_failed'
                  )
                  AND status NOT IN ('applied', 'auto_applied', 'reverted')
                """,
                [*run_ids, active_version],
            )
        )
        action_ids = [str(row["id"]) for row in rows]
        if not action_ids:
            return {"actions_reset": 0, "questions_removed": 0}
        action_placeholders = ",".join("?" for _ in action_ids)
        question_count = int(
            conn.execute(
                f"SELECT COUNT(*) FROM open_questions WHERE action_id IN ({action_placeholders})",
                action_ids,
            ).fetchone()[0]
        )
        conn.execute(
            f"DELETE FROM open_questions WHERE action_id IN ({action_placeholders})",
            action_ids,
        )
        conn.execute(
            f"""
            UPDATE cos_actions
            SET status = 'proposed', critic_by = NULL, critic_decision = NULL,
                policy_id = NULL, policy_version = NULL, policy_decision = NULL,
                autonomy_level = NULL
            WHERE id IN ({action_placeholders})
            """,
            action_ids,
        )
    return {"actions_reset": len(action_ids), "questions_removed": question_count}


def ingest_corpus(paths: BrainPaths, corpus_root: Path) -> dict[str, Any]:
    service = BrainService(paths)
    started = time.perf_counter()
    result = service.ingest(corpus_root)
    duration_seconds = time.perf_counter() - started
    value = result.as_dict()
    value["duration_seconds"] = round(duration_seconds, 3)
    if value.get("errors"):
        raise RuntimeError(f"Brain ingestion failed with {len(value['errors'])} error(s)")
    vector_writes = value.get("vector_writes") or {}
    if vector_writes.get("status") != "ok":
        raise RuntimeError(f"Vector indexing did not complete: {vector_writes}")
    return value


def document_ids_by_source(paths: BrainPaths) -> dict[str, str]:
    with sqlite3.connect(paths.sqlite_path) as conn:
        return {
            str(Path(source_path).resolve()): str(document_id)
            for document_id, source_path in conn.execute(
                "SELECT id, source_path FROM documents WHERE status = 'active'"
            )
        }


def numeric_delta(after: dict[str, Any], before: dict[str, Any], key: str) -> int:
    return int(after.get(key) or 0) - int(before.get(key) or 0)


def compact_extraction_result(result: dict[str, Any]) -> dict[str, Any]:
    validation = result.get("validation") or {}
    return {
        "status": result.get("status"),
        "shadow": result.get("shadow"),
        "document_count": len(result.get("documents") or []),
        "candidate_count": len(result.get("candidates") or []),
        "action_count": len(result.get("actions") or []),
        "validation": {
            key: value
            for key, value in validation.items()
            if isinstance(value, (str, int, float, bool)) or value is None
        },
        "timing": result.get("timing") or {},
    }


def run_sample_extraction(
    *,
    paths: BrainPaths,
    records: list[NormalizedThread],
    selected_days: list[str],
    state_path: Path,
    force: bool,
) -> list[dict[str, Any]]:
    completed: dict[str, dict[str, Any]] = {}
    if state_path.exists() and not force:
        for item in read_json(state_path).get("days") or []:
            if isinstance(item, dict) and item.get("status") == "ok":
                completed[str(item.get("date"))] = item
    if force and state_path.exists():
        raise RuntimeError(
            "Refusing to force extraction against an existing Brain. Use a fresh experiment root."
        )
    source_to_id = document_ids_by_source(paths)
    results: list[dict[str, Any]] = []
    for selected_day in sorted(selected_days):
        if selected_day in completed:
            print(f"Using completed extraction metrics for {selected_day}", flush=True)
            results.append(completed[selected_day])
            continue
        day_records = [
            record
            for record in records
            if record.fact_eligible and record.updated_date == selected_day
        ]
        document_ids = [
            source_to_id[str(record.path.resolve())]
            for record in day_records
            if str(record.path.resolve()) in source_to_id
        ]
        if len(document_ids) != len(day_records):
            raise RuntimeError(
                f"Could not map all {selected_day} sample documents to isolated Brain IDs"
            )
        run_id = f"gmail_benchmark_{selected_day.replace('-', '')}"
        before = sqlite_counts(paths.sqlite_path)
        started = time.perf_counter()
        print(
            f"Extracting {len(document_ids):,} fact-eligible thread(s) for {selected_day}",
            flush=True,
        )
        extraction = extract_recent_documents(
            paths,
            limit=len(document_ids),
            shadow=False,
            changed_only=True,
            run_id=run_id,
            document_ids=document_ids,
        )
        duration_seconds = time.perf_counter() - started
        after = sqlite_counts(paths.sqlite_path)
        compact = compact_extraction_result(extraction)
        status = str(compact.get("status") or "unknown")
        if status != "ok" or compact["document_count"] != len(document_ids):
            raise RuntimeError(
                f"Extraction for {selected_day} was incomplete: "
                f"status={status}, documents={compact['document_count']}/{len(document_ids)}"
            )
        usage = llm_usage_summary(paths, cycle_id=run_id, limit=1)
        result = {
            "date": selected_day,
            "status": "ok",
            "run_id": run_id,
            "document_count": len(document_ids),
            "message_count": sum(record.message_count for record in day_records),
            "normalized_bytes": sum(record.normalized_bytes for record in day_records),
            "source_size_estimate": sum(
                record.source_size_estimate for record in day_records
            ),
            "duration_seconds": round(duration_seconds, 3),
            "extraction": compact,
            "database_delta": {
                key: numeric_delta(after, before, key)
                for key in (
                    "facts",
                    "entities",
                    "fact_entities",
                    "cos_actions",
                    "open_questions",
                    "cos_stage_watermarks",
                )
            },
            "usage": usage,
        }
        results.append(result)
        write_private_json(
            state_path,
            {"updated_at": now_iso(), "days": sorted(results, key=lambda item: item["date"])},
        )
    return sorted(results, key=lambda item: item["date"])


def sample_action_ids(paths: BrainPaths, run_id: str, *, undecided_only: bool) -> list[str]:
    query = "SELECT id FROM cos_actions WHERE run_id = ?"
    if undecided_only:
        query += " AND policy_id IS NULL"
    query += " ORDER BY created_at, id"
    with sqlite3.connect(paths.sqlite_path) as conn:
        return [str(row[0]) for row in conn.execute(query, (run_id,))]


def action_decision_counts(paths: BrainPaths, run_id: str) -> dict[str, Any]:
    with sqlite3.connect(paths.sqlite_path) as conn:
        statuses = dict(
            conn.execute(
                "SELECT status, COUNT(*) FROM cos_actions WHERE run_id = ? GROUP BY status",
                (run_id,),
            ).fetchall()
        )
        critics = dict(
            conn.execute(
                """
                SELECT COALESCE(critic_decision, 'not_required'), COUNT(*)
                FROM cos_actions WHERE run_id = ? GROUP BY 1
                """,
                (run_id,),
            ).fetchall()
        )
        policies = dict(
            conn.execute(
                """
                SELECT COALESCE(autonomy_level, 'undecided'), COUNT(*)
                FROM cos_actions WHERE run_id = ? GROUP BY 1
                """,
                (run_id,),
            ).fetchall()
        )
    return {"statuses": statuses, "critic_decisions": critics, "autonomy_levels": policies}


def evaluate_sample_actions(
    *,
    paths: BrainPaths,
    extraction_results: list[dict[str, Any]],
    state_path: Path,
    policy_version: int,
) -> list[dict[str, Any]]:
    critic_config = dict(load_extraction_config(paths).get("critic_review") or {})
    updated: list[dict[str, Any]] = []
    for original in sorted(extraction_results, key=lambda item: item["date"]):
        item = dict(original)
        run_id = str(item["run_id"])
        pending = sample_action_ids(paths, run_id, undecided_only=True)
        before = sqlite_counts(paths.sqlite_path)
        started = time.perf_counter()
        if pending:
            print(
                f"Evaluating {len(pending):,} extracted action(s) for {item['date']}",
                flush=True,
            )
            decide_policy_actions(paths, pending, critic_review=critic_config)
        duration_seconds = time.perf_counter() - started
        after = sqlite_counts(paths.sqlite_path)
        existing_evaluation = item.get("evaluation") or {}
        existing_evaluator_delta = dict(
            existing_evaluation.get("database_delta") or {}
        )
        if pending:
            evaluator_delta = {
                key: numeric_delta(after, before, key)
                for key in (
                    "facts",
                    "entities",
                    "fact_entities",
                    "cos_actions",
                    "open_questions",
                    "cos_stage_watermarks",
                )
            }
        elif int(existing_evaluation.get("policy_version") or 0) == policy_version:
            evaluator_delta = existing_evaluator_delta
        else:
            evaluator_delta = {}
        extraction_delta = dict(item.get("extractor_database_delta") or {})
        if not extraction_delta:
            combined_delta = dict(item.get("database_delta") or {})
            extraction_delta = {
                key: int(combined_delta.get(key) or 0)
                - int(existing_evaluator_delta.get(key) or 0)
                for key in set(combined_delta) | set(existing_evaluator_delta)
            }
        item["extractor_database_delta"] = extraction_delta
        item["database_delta"] = {
            key: int(extraction_delta.get(key) or 0) + int(evaluator_delta.get(key) or 0)
            for key in set(extraction_delta) | set(evaluator_delta)
        }
        if "extractor_usage" not in item:
            item["extractor_usage"] = item.get("usage") or {}
        item["usage"] = llm_usage_summary(paths, cycle_id=run_id, limit=1)
        item["evaluation"] = {
            "policy_version": policy_version,
            "newly_decided_action_count": len(pending),
            "duration_seconds": (
                round(duration_seconds, 3)
                if pending
                else float(existing_evaluation.get("duration_seconds") or 0.0)
            ),
            "database_delta": evaluator_delta,
            **action_decision_counts(paths, run_id),
        }
        updated.append(item)
        write_private_json(
            state_path,
            {"updated_at": now_iso(), "days": updated},
        )
    return updated


def mean(values: Iterable[float | int]) -> float:
    materialized = list(values)
    return statistics.fmean(materialized) if materialized else 0.0


def build_report(
    *,
    start_date: date,
    end_date_exclusive: date,
    query: str,
    records: list[NormalizedThread],
    selected_days: list[str],
    source_storage: dict[str, Any],
    cache_storage: dict[str, Any],
    baseline_storage: dict[str, Any],
    indexed_storage: dict[str, Any],
    before_counts: dict[str, Any],
    indexed_counts: dict[str, Any],
    ingest_result: dict[str, Any],
    extraction_results: list[dict[str, Any]],
    final_counts: dict[str, Any],
    final_storage: dict[str, Any],
) -> dict[str, Any]:
    classification_counts = Counter(record.classification for record in records)
    eligible = [record for record in records if record.fact_eligible]
    day_all = Counter(record.updated_date for record in records)
    day_eligible = Counter(record.updated_date for record in eligible)
    complete_dates = [
        (start_date + timedelta(days=offset)).isoformat()
        for offset in range((end_date_exclusive - start_date).days)
        if start_date + timedelta(days=offset) < datetime.now(LOCAL_TIMEZONE).date()
    ]
    sample_token_totals = [
        int(((item.get("usage") or {}).get("totals") or {}).get("total_tokens") or 0)
        for item in extraction_results
    ]
    sample_uncached_totals = [
        int(
            ((item.get("usage") or {}).get("totals") or {}).get(
                "uncached_input_tokens"
            )
            or 0
        )
        for item in extraction_results
    ]
    sample_documents = [int(item.get("document_count") or 0) for item in extraction_results]
    sample_facts = [
        int((item.get("database_delta") or {}).get("facts") or 0)
        for item in extraction_results
    ]
    sample_actions = [
        int(((item.get("extraction") or {}).get("action_count") or 0))
        for item in extraction_results
    ]
    avg_tokens_per_document = (
        sum(sample_token_totals) / sum(sample_documents) if sum(sample_documents) else 0
    )
    avg_facts_per_document = (
        sum(sample_facts) / sum(sample_documents) if sum(sample_documents) else 0
    )
    mean_daily_eligible = mean(day_eligible.get(day, 0) for day in complete_dates)
    mean_daily_all = mean(day_all.get(day, 0) for day in complete_dates)
    baseline_total = int(baseline_storage["total"]["allocated_bytes"])
    indexed_total = int(indexed_storage["total"]["allocated_bytes"])
    fact_total = int(final_storage["total"]["allocated_bytes"])
    return {
        "schema_version": 1,
        "generated_at": now_iso(),
        "window": {
            "start_date": start_date.isoformat(),
            "end_date_exclusive": end_date_exclusive.isoformat(),
            "calendar_days": (end_date_exclusive - start_date).days,
            "gmail_query": query,
        },
        "privacy_and_isolation": {
            "attachment_payloads_indexed": False,
            "attachment_fetch_endpoint_used": False,
            "api_cache_may_include_inline_mime_payloads": True,
            "quoted_reply_history_removed": True,
            "live_brain_database_opened": False,
            "live_brain_lineage_written": False,
            "production_connector_enabled": False,
            "sample_only_extraction": True,
            "selected_extraction_days": selected_days,
        },
        "corpus": {
            "thread_count": len(records),
            "message_count": sum(record.message_count for record in records),
            "classification_counts": dict(classification_counts),
            "fact_eligible_thread_count": len(eligible),
            "source_size_estimate_bytes": sum(
                record.source_size_estimate for record in records
            ),
            "normalized_bytes": sum(record.normalized_bytes for record in records),
            "quoted_chars_removed": sum(
                record.quoted_chars_removed for record in records
            ),
            "attachment_count_excluded": sum(
                record.attachment_count for record in records
            ),
            "truncated_message_count": sum(
                record.truncated_message_count for record in records
            ),
        },
        "storage": {
            "private_api_cache": cache_storage,
            "normalized_source": source_storage,
            "brain_baseline": baseline_storage,
            "brain_after_index": indexed_storage,
            "brain_after_sample_facts": final_storage,
            "index_growth_allocated_bytes": indexed_total - baseline_total,
            "sample_fact_growth_allocated_bytes": fact_total - indexed_total,
            "index_growth_per_thread_allocated_bytes": (
                (indexed_total - baseline_total) / len(records) if records else 0
            ),
            "linear_annual_index_growth_projection_bytes": (
                (indexed_total - baseline_total) * 365 / 90
            ),
        },
        "indexing": {
            "ingest_result": ingest_result,
            "before_counts": before_counts,
            "after_counts": indexed_counts,
        },
        "daily_volume": {
            "mean_all_threads_per_complete_day": mean_daily_all,
            "mean_fact_eligible_threads_per_complete_day": mean_daily_eligible,
            "selected_days": [
                {
                    "date": day,
                    "all_threads": day_all.get(day, 0),
                    "fact_eligible_threads": day_eligible.get(day, 0),
                }
                for day in selected_days
            ],
        },
        "fact_sample": {
            "days": extraction_results,
            "sample_day_count": len(extraction_results),
            "sample_document_count": sum(sample_documents),
            "mean_documents_per_sample_day": mean(sample_documents),
            "mean_total_tokens_per_sample_day": mean(sample_token_totals),
            "mean_uncached_input_tokens_per_sample_day": mean(sample_uncached_totals),
            "mean_facts_created_per_sample_day": mean(sample_facts),
            "mean_actions_per_sample_day": mean(sample_actions),
            "total_tokens_per_sample_document": avg_tokens_per_document,
            "facts_per_sample_document": avg_facts_per_document,
            "projected_mean_daily_tokens_for_fact_eligible_mail": (
                avg_tokens_per_document * mean_daily_eligible
            ),
            "projected_mean_daily_facts_for_fact_eligible_mail": (
                avg_facts_per_document * mean_daily_eligible
            ),
            "rough_all_mail_daily_token_counterfactual": (
                avg_tokens_per_document * mean_daily_all
            ),
            "projection_note": (
                "Token/fact projections scale observed per-document sample cost linearly. "
                "The all-mail counterfactual is intentionally rough because bulk and "
                "transactional messages differ from human correspondence."
            ),
        },
        "final_counts": final_counts,
    }


def run(args: argparse.Namespace) -> dict[str, Any]:
    root = args.root.expanduser().resolve()
    live_home = args.live_home.expanduser().resolve()
    make_private_directory(root)
    cache_root = root / "gmail-api-cache"
    corpus_root = root / "normalized-gmail"
    brain_home = root / "brain"
    artifacts = root / "artifacts"
    for path in (cache_root, corpus_root, artifacts):
        make_private_directory(path)

    today = datetime.now(LOCAL_TIMEZONE).date()
    end_date_exclusive = today + timedelta(days=1)
    start_date = end_date_exclusive - timedelta(days=args.days)
    api_after = start_date - timedelta(days=1)
    query = (
        f"after:{api_after.strftime('%Y/%m/%d')} "
        f"before:{end_date_exclusive.strftime('%Y/%m/%d')} -in:spam -in:trash"
    )
    client = GmailClient(
        timeout_seconds=args.timeout,
        requests_per_second=args.gmail_requests_per_second,
    )
    profile = client.get_json("profile")
    auth_summary = {
        "authorized": True,
        "history_id_present": bool(profile.get("historyId")),
        "profile_counts_present": all(
            key in profile for key in ("messagesTotal", "threadsTotal")
        ),
    }
    write_private_json(artifacts / "auth.json", auth_summary)
    print("Gmail OAuth verified through the Keychain-backed credential", flush=True)

    thread_ids = list_thread_ids(
        client,
        query=query,
        cache_root=cache_root,
        refresh=args.refresh,
    )
    fetch_threads(
        client,
        thread_ids=thread_ids,
        cache_root=cache_root,
        workers=args.fetch_workers,
        refresh=args.refresh,
    )
    records = normalize_corpus(
        thread_ids=thread_ids,
        cache_root=cache_root,
        corpus_root=corpus_root,
        start_date=start_date,
        end_date_exclusive=end_date_exclusive,
    )
    if not records:
        raise RuntimeError("The 90-day Gmail query produced no normalized threads")
    manifest_path = artifacts / "manifest.jsonl"
    write_private_text(
        manifest_path,
        "".join(json.dumps(record.as_dict(), sort_keys=True) + "\n" for record in records),
    )
    selected_days = select_representative_days(
        records, requested=args.sample_days, today=today
    )
    if len(selected_days) > 3:
        raise RuntimeError("Safety guard: extraction sample cannot exceed three days")
    write_private_json(
        artifacts / "sample.json",
        {
            "selected_days": selected_days,
            "eligible_thread_ids": [
                record.thread_id
                for record in records
                if record.fact_eligible and record.updated_date in selected_days
            ],
        },
    )
    print(
        f"Normalized {len(records):,} threads; selected extraction days: "
        f"{', '.join(selected_days)}",
        flush=True,
    )

    paths, current_baseline = prepare_test_brain(brain_home, live_home)
    benchmark_state_path = artifacts / "benchmark-state.json"
    state = read_json(benchmark_state_path) if benchmark_state_path.exists() else {}
    baseline_storage = state.get("baseline_storage") or current_baseline
    before_counts = state.get("before_counts") or sqlite_counts(paths.sqlite_path)
    if not state:
        write_private_json(
            benchmark_state_path,
            {
                "created_at": now_iso(),
                "baseline_storage": baseline_storage,
                "before_counts": before_counts,
            },
        )
    current_counts = sqlite_counts(paths.sqlite_path)
    index_matches_corpus = (
        int(current_counts.get("documents") or 0) == len(records)
        and int(current_counts.get("chunks") or 0) > 0
        and int(current_counts.get("document_source_bytes") or 0)
        == sum(record.normalized_bytes for record in records)
    )
    if state.get("ingest_result") and index_matches_corpus:
        current_ingest_result = state["ingest_result"]
        print("Using the existing verified isolated Brain index", flush=True)
    else:
        current_ingest_result = ingest_corpus(paths, corpus_root)
    indexed_storage = state.get("indexed_storage") or storage_snapshot(paths.home)
    indexed_counts = {**current_counts, **(state.get("indexed_counts") or {})}
    for table in (
        "cos_actions",
        "cos_stage_watermarks",
        "entities",
        "fact_entities",
        "facts",
        "open_questions",
    ):
        indexed_counts[table] = int(before_counts.get(table) or 0)
    indexed_counts["facts_by_status"] = dict(before_counts.get("facts_by_status") or {})
    ingest_result = state.get("ingest_result") or current_ingest_result
    if indexed_counts.get("documents") != len(records):
        raise RuntimeError(
            f"Isolated Brain indexed {indexed_counts.get('documents')} documents; "
            f"expected {len(records)}"
        )
    if indexed_counts.get("chunks") != ingest_result.get("embeddings_created") and not (
        ingest_result.get("documents_skipped") and ingest_result.get("embeddings_created") == 0
    ):
        raise RuntimeError("Chunk/vector counts do not establish complete indexing")
    write_private_json(
        benchmark_state_path,
        {
            "created_at": state.get("created_at") or now_iso(),
            "updated_at": now_iso(),
            "baseline_storage": baseline_storage,
            "before_counts": before_counts,
            "indexed_storage": indexed_storage,
            "indexed_counts": indexed_counts,
            "ingest_result": ingest_result,
        },
    )
    print(
        f"Indexed {indexed_counts.get('documents', 0):,} documents and "
            f"{indexed_counts.get('chunks', 0):,} chunks in the isolated Brain",
        flush=True,
    )

    policy_version = ensure_benchmark_policy(paths)
    print(f"Isolated Brain policy version {policy_version} is active", flush=True)
    eval_summary = ensure_benchmark_extraction_eval(paths)
    write_private_json(artifacts / "extraction-eval.json", eval_summary)
    print(
        f"Labeled extraction eval passed with {eval_summary['label_case_count']} cases",
        flush=True,
    )
    sample_run_ids = [
        f"gmail_benchmark_{selected_day.replace('-', '')}"
        for selected_day in selected_days
    ]
    reset_summary = reset_stale_sample_decisions(
        paths, run_ids=sample_run_ids, active_version=policy_version
    )
    if reset_summary["actions_reset"]:
        print(
            f"Reset {reset_summary['actions_reset']} bootstrap-policy sample decisions",
            flush=True,
        )
    extraction_results = run_sample_extraction(
        paths=paths,
        records=records,
        selected_days=selected_days,
        state_path=artifacts / "extraction-runs.json",
        force=args.force_extract,
    )
    extraction_results = evaluate_sample_actions(
        paths=paths,
        extraction_results=extraction_results,
        state_path=artifacts / "extraction-runs.json",
        policy_version=policy_version,
    )
    final_counts = sqlite_counts(paths.sqlite_path)
    final_storage = storage_snapshot(paths.home)
    source_logical, source_allocated, source_files = logical_and_allocated_bytes(corpus_root)
    cache_logical, cache_allocated, cache_files = logical_and_allocated_bytes(cache_root)
    report = build_report(
        start_date=start_date,
        end_date_exclusive=end_date_exclusive,
        query=query,
        records=records,
        selected_days=selected_days,
        source_storage={
            "logical_bytes": source_logical,
            "allocated_bytes": source_allocated,
            "file_count": source_files,
        },
        cache_storage={
            "logical_bytes": cache_logical,
            "allocated_bytes": cache_allocated,
            "file_count": cache_files,
        },
        baseline_storage=baseline_storage,
        indexed_storage=indexed_storage,
        before_counts=before_counts,
        indexed_counts=indexed_counts,
        ingest_result=ingest_result,
        extraction_results=extraction_results,
        final_counts=final_counts,
        final_storage=final_storage,
    )
    report["fact_sample"]["policy_gate"] = {
        "policy_version": policy_version,
        **eval_summary,
    }
    write_private_json(artifacts / "report.json", report)
    print(json.dumps(report_summary(report), indent=2, sort_keys=True), flush=True)
    return report


def report_summary(report: dict[str, Any]) -> dict[str, Any]:
    corpus = report["corpus"]
    storage = report["storage"]
    fact_sample = report["fact_sample"]
    return {
        "thread_count": corpus["thread_count"],
        "message_count": corpus["message_count"],
        "fact_eligible_thread_count": corpus["fact_eligible_thread_count"],
        "brain_index_growth_allocated_bytes": storage["index_growth_allocated_bytes"],
        "sample_days": report["privacy_and_isolation"]["selected_extraction_days"],
        "sample_document_count": fact_sample["sample_document_count"],
        "mean_total_tokens_per_sample_day": fact_sample[
            "mean_total_tokens_per_sample_day"
        ],
        "mean_facts_created_per_sample_day": fact_sample[
            "mean_facts_created_per_sample_day"
        ],
        "report_path": "artifacts/report.json",
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--live-home", type=Path, default=DEFAULT_LIVE_HOME)
    parser.add_argument("--days", type=int, default=90, choices=range(1, 366))
    parser.add_argument("--sample-days", type=int, default=3, choices=(1, 2, 3))
    parser.add_argument("--fetch-workers", type=int, default=4)
    parser.add_argument(
        "--gmail-requests-per-second",
        type=float,
        default=2.0,
        help="Global Gmail API request ceiling; 2/s fits the current standard user quota.",
    )
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Refresh cached Gmail list and thread responses.",
    )
    parser.add_argument(
        "--force-extract",
        action="store_true",
        help="Reserved for fresh roots; never duplicates an existing extraction.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    try:
        run(parse_args())
    except KeyboardInterrupt:
        print("Interrupted; cached Gmail responses can be resumed.", file=sys.stderr)
        raise SystemExit(130) from None
