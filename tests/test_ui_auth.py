from __future__ import annotations

import http.client
import json
import stat
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from pkm_brain.paths import BrainPaths
from pkm_brain.ui_server import create_ui_server, ensure_ui_token, ui_token_path


@contextmanager
def running_ui(paths: BrainPaths, token: str | None = None) -> Iterator[tuple[str, int, str]]:
    token = token or ensure_ui_token(paths)
    server = create_ui_server(paths, "127.0.0.1", 0, token=token)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield str(host), int(port), token
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def request_status(host: str, port: int, token: str | None = None) -> tuple[int, dict[str, object]]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    conn = http.client.HTTPConnection(host, port, timeout=5)
    conn.request("GET", "/api/status", headers=headers)
    response = conn.getresponse()
    body = response.read().decode("utf-8")
    conn.close()
    return response.status, json.loads(body)


def test_ui_rejects_missing_token(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")

    with running_ui(paths) as (host, port, _token):
        status, body = request_status(host, port)

    assert status == 401
    assert "token" in str(body["error"])


def test_ui_rejects_wrong_token(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")

    with running_ui(paths) as (host, port, _token):
        status, body = request_status(host, port, token="wrong")

    assert status == 401
    assert "token" in str(body["error"])


def test_ui_accepts_valid_token_and_token_file_is_private(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    token = ensure_ui_token(paths)

    assert stat.S_IMODE(ui_token_path(paths).stat().st_mode) == 0o600
    with running_ui(paths, token=token) as (host, port, token):
        status, body = request_status(host, port, token=token)

    assert status == 200
    assert "doctor" in body
