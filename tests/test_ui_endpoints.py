from __future__ import annotations

import http.client
import json
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from pkm_brain.paths import BrainPaths
from pkm_brain.service import BrainService, memory_export_path
from pkm_brain.ui_server import create_ui_server, ensure_ui_token


@contextmanager
def running_ui(paths: BrainPaths) -> Iterator[tuple[str, int, str]]:
    token = ensure_ui_token(paths)
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


def request_json(
    host: str,
    port: int,
    token: str,
    method: str,
    path: str,
    body: dict[str, object] | None = None,
) -> tuple[int, dict[str, object]]:
    encoded = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Authorization": f"Bearer {token}"}
    if encoded:
        headers["Content-Type"] = "application/json"
    conn = http.client.HTTPConnection(host, port, timeout=5)
    conn.request(method, path, body=encoded, headers=headers)
    response = conn.getresponse()
    response_body = response.read().decode("utf-8")
    conn.close()
    return response.status, json.loads(response_body)


def test_status_endpoint_returns_service_layer_json(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")

    with running_ui(paths) as (host, port, token):
        status, body = request_json(host, port, token, "GET", "/api/status")

    assert status == 200
    assert body["doctor"]["home"] == str(paths.home)
    assert "index" in body
    assert "memory" in body


def test_memory_endpoint_lists_status_filtered_memories(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    svc = BrainService(paths, prefer_model_embeddings=False)
    memory_id = svc.propose_memory("FactMemory", "global", "Use the local UI for review.", ["manual:test"], 0.9)

    with running_ui(paths) as (host, port, token):
        status, body = request_json(host, port, token, "GET", "/api/memory?status=proposed")

    assert status == 200
    assert body["count"] == 1
    assert body["memories"][0]["id"] == memory_id


def test_memory_approve_endpoint_writes_same_export_as_cli_path(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    svc = BrainService(paths, prefer_model_embeddings=False)
    memory_id = svc.propose_memory("FactMemory", "global", "Approve from the UI.", ["manual:test"], 0.95)

    with running_ui(paths) as (host, port, token):
        status, body = request_json(host, port, token, "POST", f"/api/memory/{memory_id}/approve")

    assert status == 200
    assert body["status"] == "active"
    assert memory_export_path(paths, svc.get_memory(memory_id)).exists()
