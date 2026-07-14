from __future__ import annotations

import http.client
import base64
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator
from urllib.parse import parse_qs, urlparse

import pytest
import yaml

from pkm_brain.capture import AgentLogCapture
from pkm_brain.connector_auth import (
    MemoryCredentialStore,
    OAUTH_PROVIDERS,
    OAuthCallbackBroker,
    PendingOAuthFlow,
    begin_connector_authorization,
    complete_authorization,
    configure_connector_auth,
    connector_auth_status,
    disconnect_connector_auth,
)
from pkm_brain.connectors import (
    BUILTIN_CONNECTORS,
    ConnectorContext,
    ConnectorManifest,
    PreflightReport,
    connector_config_path,
    list_connectors,
    run_connector_capture,
    runtime_settings,
    set_connector_enabled,
)
from pkm_brain.db import connection
from pkm_brain.paths import BrainPaths
from pkm_brain.service import BrainService
from pkm_brain.ui_server import create_ui_server
from test_capture import make_codex_fixture, make_hyprnote_fixture


def encoded_id_token(claims: dict[str, object]) -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode("ascii").rstrip("=")
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode("utf-8")).decode("ascii").rstrip("=")
    return f"{header}.{payload}.signature"


def test_connector_registry_exposes_builtin_manifests(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()

    result = list_connectors(paths)
    manifests = {item["manifest"]["id"]: item["manifest"] for item in result["connectors"]}

    assert {
        "codex",
        "claude",
        "opencode",
        "hyprnote",
        "files",
        "calendar",
        "gmail",
        "slack",
    }.issubset(manifests)
    assert manifests["codex"]["source_type"] == "agent_session_log"
    assert manifests["hyprnote"]["source_type"] == "hyprnote_meeting"
    assert manifests["hyprnote"]["default_enabled"] is False
    assert manifests["files"]["default_enabled"] is True
    assert manifests["gmail"]["lifecycle"] == "auth_only"
    assert manifests["gmail"]["capture_available"] is False
    assert manifests["gmail"]["default_cadence_s"] == 600
    assert "Today > Run Shadow" in manifests["gmail"]["activation_note"]
    assert "local operational mirror" in manifests["gmail"]["activation_note"]
    assert "knowledge ingestion remain" in manifests["gmail"]["activation_note"]
    assert manifests["gmail"]["auth"]["phase"] == "read_only"
    assert manifests["gmail"]["auth"]["requested_scopes"] == [
        "openid",
        "email",
        "profile",
        "https://www.googleapis.com/auth/gmail.readonly",
    ]
    assert manifests["calendar"]["lifecycle"] == "auth_only"
    assert manifests["calendar"]["capture_available"] is False
    assert "Today > Run Shadow" in manifests["calendar"]["activation_note"]
    assert manifests["calendar"]["auth"]["requested_scopes"] == [
        "openid",
        "email",
        "profile",
        "https://www.googleapis.com/auth/calendar.events.owned.readonly",
    ]
    assert manifests["slack"]["auth"]["client_secret_required"] is True


def test_auth_only_connectors_cannot_be_enabled_or_captured(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()

    with pytest.raises(ValueError, match="Gmail capture is not available"):
        set_connector_enabled(paths, "gmail", True)

    result = run_connector_capture(
        paths,
        connector_ids=["gmail"],
        respect_enabled=False,
        respect_cadence=False,
    ).as_dict()

    assert result["captured"] == 0
    assert result["connector_results"][0]["status"] == "skipped"
    assert result["connector_results"][0]["reason"] == "capture not implemented"


def test_google_oauth_identity_flow_uses_pkce_and_keeps_secrets_out_of_config(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    store = MemoryCredentialStore()
    broker = OAuthCallbackBroker(
        port=0,
        token_exchanger=lambda flow, code: {
            "access_token": "access-secret",
            "refresh_token": "refresh-secret",
            "scope": (
                "openid email profile "
                "https://www.googleapis.com/auth/userinfo.email "
                "https://www.googleapis.com/auth/userinfo.profile "
                "https://www.googleapis.com/auth/gmail.readonly"
            ),
            "expires_in": 3600,
            "id_token": encoded_id_token(
                {
                    "aud": flow.client_id,
                    "iss": "https://accounts.google.com",
                    "email": "person@example.com",
                    "sub": "google-subject-person-1",
                    "exp": int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp()),
                }
            ),
        },
    )
    configure_connector_auth(
        paths,
        "gmail",
        client_id="desktop-client-id",
        client_secret="client-secret",
        store=store,
    )
    try:
        started = begin_connector_authorization(paths, "gmail", store=store, broker=broker)
        parsed = urlparse(started["authorization_url"])
        query = parse_qs(parsed.query)

        assert parsed.netloc == "accounts.google.com"
        assert query["scope"] == [
            "openid email profile https://www.googleapis.com/auth/gmail.readonly"
        ]
        assert query["code_challenge_method"] == ["S256"]
        assert query["redirect_uri"] == [started["redirect_uri"]]

        callback = urlparse(started["redirect_uri"])
        connection = http.client.HTTPConnection(callback.hostname, callback.port, timeout=5)
        connection.request("GET", f"{callback.path}?state={query['state'][0]}&code=test-code")
        response = connection.getresponse()
        response.read()
        connection.close()

        assert response.status == 200
        status = connector_auth_status(paths, "gmail", store=store)
        assert status is not None
        assert status["status"] == "connected"
        assert status["account_label"] == "person@example.com"
        assert status["provider_subject"] == "google-subject-person-1"
        assert status["client_secret_configured"] is True
        assert status["granted_scopes"] == [
            "email",
            "https://www.googleapis.com/auth/gmail.readonly",
            "openid",
            "profile",
        ]

        config_text = (paths.config_local / "connector-auth.yaml").read_text(encoding="utf-8")
        assert "client-secret" not in config_text
        assert "access-secret" not in config_text
        assert "refresh-secret" not in config_text

        disconnected = disconnect_connector_auth(paths, "gmail", store=store)
        assert disconnected["status"] == "ready"
        assert store.load("gmail") == {"client_secret": "client-secret"}
    finally:
        broker.shutdown()


def test_slack_oauth_requires_client_secret(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    store = MemoryCredentialStore()

    with pytest.raises(ValueError, match="Client secret is required"):
        configure_connector_auth(
            paths,
            "slack",
            client_id="slack-client-id",
            store=store,
        )


def test_oauth_identity_flow_rejects_missing_identity_token(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    store = MemoryCredentialStore()
    broker = OAuthCallbackBroker(
        port=0,
        token_exchanger=lambda _flow, _code: {
            "access_token": "access-secret",
            "scope": (
                "openid email profile "
                "https://www.googleapis.com/auth/gmail.readonly"
            ),
        },
    )
    configure_connector_auth(
        paths,
        "gmail",
        client_id="desktop-client-id",
        store=store,
    )
    try:
        started = begin_connector_authorization(paths, "gmail", store=store, broker=broker)
        parsed = urlparse(started["authorization_url"])
        query = parse_qs(parsed.query)
        callback = urlparse(started["redirect_uri"])
        connection = http.client.HTTPConnection(callback.hostname, callback.port, timeout=5)
        connection.request("GET", f"{callback.path}?state={query['state'][0]}&code=test-code")
        response = connection.getresponse()
        response.read()
        connection.close()

        assert response.status == 400
        status = connector_auth_status(paths, "gmail", store=store)
        assert status is not None
        assert status["status"] == "error"
        assert status["last_error"] == "OAuth provider returned an invalid identity token"
        assert store.load("gmail") == {}
    finally:
        broker.shutdown()


def test_oauth_refresh_token_is_preserved_only_for_the_same_provider_subject(
    tmp_path: Path,
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    store = MemoryCredentialStore()
    configure_connector_auth(
        paths,
        "gmail",
        client_id="desktop-client-id",
        client_secret="client-secret",
        store=store,
    )
    expires_at = int((datetime.now(timezone.utc) + timedelta(hours=1)).timestamp())

    def identity_token(subject: str, email: str) -> str:
        return encoded_id_token(
            {
                "aud": "desktop-client-id",
                "iss": "https://accounts.google.com",
                "email": email,
                "sub": subject,
                "exp": expires_at,
            }
        )

    original_token = identity_token("google-subject-1", "person@example.com")
    store.save(
        "gmail",
        {
            "client_secret": "client-secret",
            "access_token": "old-access",
            "refresh_token": "old-refresh",
            "id_token": original_token,
        },
    )
    flow = PendingOAuthFlow(
        paths=paths,
        provider=OAUTH_PROVIDERS["gmail"],
        store=store,
        state="oauth-state",
        client_id="desktop-client-id",
        client_secret="client-secret",
        redirect_uri="http://127.0.0.1/oauth/callback/gmail",
        created_at=datetime.now(timezone.utc),
    )
    complete_authorization(
        flow,
        {
            "access_token": "same-account-access",
            "id_token": identity_token("google-subject-1", "person@example.com"),
        },
    )
    same_account_credentials = store.load("gmail")
    assert same_account_credentials is not None
    assert same_account_credentials["refresh_token"] == "old-refresh"

    with pytest.raises(ValueError, match="identity could not be verified"):
        complete_authorization(
            flow,
            {
                "access_token": "different-account-access",
                "id_token": identity_token(
                    "google-subject-2",
                    "other-person@example.com",
                ),
            },
        )

    assert store.load("gmail") == same_account_credentials
    status = connector_auth_status(paths, "gmail", store=store)
    assert status is not None
    assert status["provider_subject"] == "google-subject-1"
    assert status["account_label"] == "person@example.com"


def test_connector_capture_keeps_codex_paths_and_capture_source_keys(tmp_path: Path) -> None:
    state = make_codex_fixture(tmp_path)
    legacy_paths = BrainPaths.from_value(tmp_path / "legacy")
    new_paths = BrainPaths.from_value(tmp_path / "new")
    BrainService(legacy_paths).init_workspace()
    BrainService(new_paths).init_workspace()

    legacy = AgentLogCapture(
        legacy_paths,
        codex_state=state,
        claude_projects=tmp_path / "missing-claude",
        opencode_db=tmp_path / "missing-opencode.sqlite",
    ).capture(agent="codex")
    new = run_connector_capture(
        new_paths,
        connector_ids=["codex"],
        respect_enabled=False,
        respect_cadence=False,
        settings_overrides=runtime_settings(codex_state=state),
    )

    assert legacy.as_dict()["artifacts"] == [
        str(legacy_paths.inbox / "agent_logs" / "codex" / "codex-session-1.md")
    ]
    assert new.as_dict()["artifacts"] == [
        str(new_paths.inbox / "agent_logs" / "codex" / "codex-session-1.md")
    ]
    with connection(legacy_paths.sqlite_path) as conn:
        legacy_row = dict(conn.execute("SELECT id, captured_path FROM capture_sources").fetchone())
    with connection(new_paths.sqlite_path) as conn:
        new_row = dict(conn.execute("SELECT id, captured_path FROM capture_sources").fetchone())

    assert legacy_row["id"] == "codex:codex-session-1"
    assert new_row["id"] == legacy_row["id"]
    assert Path(legacy_row["captured_path"]).relative_to(legacy_paths.home) == Path(
        new_row["captured_path"]
    ).relative_to(new_paths.home)


def test_connector_capture_keeps_hyprnote_document_path(tmp_path: Path) -> None:
    root = make_hyprnote_fixture(tmp_path)
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()

    result = run_connector_capture(
        paths,
        connector_ids=["hyprnote"],
        respect_enabled=False,
        respect_cadence=False,
        settings_overrides=runtime_settings(hyprnote_root=root),
    ).as_dict()

    assert result["captured"] == 1
    assert result["artifacts"] == [
        str(paths.inbox / "documents" / "hyprnote" / "hyprnote-session-1.md")
    ]
    with connection(paths.sqlite_path) as conn:
        row = dict(conn.execute("SELECT id, source_kind, agent FROM capture_sources").fetchone())
    assert row == {
        "id": "hyprnote:hyprnote-session-1",
        "source_kind": "hyprnote_meeting",
        "agent": "hyprnote",
    }


def test_disabled_connector_is_respected_by_configured_tick(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    state = make_codex_fixture(tmp_path)

    set_connector_enabled(paths, "codex", False)
    result = run_connector_capture(
        paths,
        connector_ids=["codex"],
        respect_enabled=True,
        respect_cadence=False,
        settings_overrides=runtime_settings(codex_state=state),
    ).as_dict()

    assert result["captured"] == 0
    assert result["connector_results"][0]["status"] == "skipped"
    assert result["connector_results"][0]["reason"] == "disabled"
    assert not (paths.inbox / "agent_logs" / "codex").exists()


class BrokenConnector:
    manifest = ConnectorManifest(
        id="broken",
        display_name="Broken",
        description="Fails in tests.",
        source_type="agent_session_log",
        default_enabled=True,
        default_cadence_s=600,
    )

    def preflight(self, ctx: ConnectorContext) -> PreflightReport:
        return PreflightReport()

    def discover(self, ctx: ConnectorContext):
        raise RuntimeError("boom")

    def capture(self, ctx: ConnectorContext, candidates, *, dry_run: bool = False, export_outbox: bool = False):
        raise AssertionError("discover should fail first")


def test_connector_failure_is_isolated(tmp_path: Path, monkeypatch) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    state = make_codex_fixture(tmp_path)
    monkeypatch.setitem(BUILTIN_CONNECTORS, "broken", BrokenConnector)

    result = run_connector_capture(
        paths,
        connector_ids=["broken", "codex"],
        respect_enabled=False,
        respect_cadence=False,
        settings_overrides=runtime_settings(codex_state=state),
    ).as_dict()

    runs = {item["connector_id"]: item for item in result["connector_results"]}
    assert runs["broken"]["status"] == "failed"
    assert runs["codex"]["status"] == "ok"
    assert result["captured"] == 1
    assert result["errors"] == []
    assert any("broken: failed" in warning for warning in result["warnings"])

    config = yaml.safe_load(connector_config_path(paths).read_text(encoding="utf-8"))
    assert config["connectors"]["broken"]["health"]["status"] == "failing(1)"


@contextmanager
def running_ui(paths: BrainPaths, token: str = "test-token") -> Iterator[tuple[str, int, str]]:
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
    method: str,
    path: str,
    *,
    token: str,
    payload: dict[str, object] | None = None,
) -> tuple[int, dict[str, object]]:
    body = json.dumps(payload or {}).encode("utf-8") if payload is not None else None
    headers = {"Authorization": f"Bearer {token}"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    conn = http.client.HTTPConnection(host, port, timeout=5)
    conn.request(method, path, body=body, headers=headers)
    response = conn.getresponse()
    raw = response.read().decode("utf-8")
    conn.close()
    return response.status, json.loads(raw)


def test_connector_api_endpoints(tmp_path: Path, monkeypatch) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    store = MemoryCredentialStore()
    monkeypatch.setattr("pkm_brain.connector_auth.credential_store_for", lambda _paths: store)

    with running_ui(paths) as (host, port, token):
        status, body = request_json(host, port, "GET", "/api/connectors", token=token)
        assert status == 200
        assert body["count"] >= 5

        status, body = request_json(host, port, "POST", "/api/connectors/codex/disable", token=token)
        assert status == 200
        assert body["state"]["enabled"] is False

        status, body = request_json(
            host,
            port,
            "PUT",
            "/api/connectors/codex/settings",
            token=token,
            payload={"settings": {"state_db": str(tmp_path / "codex.sqlite")}},
        )
        assert status == 200
        assert body["state"]["settings"]["state_db"] == str(tmp_path / "codex.sqlite")

        status, body = request_json(host, port, "GET", "/api/connectors/codex", token=token)
        assert status == 200
        assert body["state"]["enabled"] is False

        status, body = request_json(host, port, "POST", "/api/connectors/codex/enable", token=token)
        assert status == 200
        assert body["state"]["enabled"] is True

        status, body = request_json(
            host,
            port,
            "PUT",
            "/api/connectors/gmail/auth/config",
            token=token,
            payload={"client_id": "desktop-client", "client_secret": "local-secret"},
        )
        assert status == 200
        assert body["state"]["auth"]["status"] == "ready"
        assert body["state"]["auth"]["client_secret_configured"] is True
        assert "local-secret" not in json.dumps(body)

        status, body = request_json(host, port, "POST", "/api/connectors/gmail/run", token=token)
        assert status == 200
        assert body["connector_results"][0]["reason"] == "capture not implemented"

        status, body = request_json(host, port, "POST", "/api/connectors/gmail/enable", token=token)
        assert status == 400
        assert body["error"] == "Gmail capture is not available"


def test_capture_sources_unique_index_remains_compatible(tmp_path: Path) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    BrainService(paths).init_workspace()
    state = make_codex_fixture(tmp_path)

    first = run_connector_capture(
        paths,
        connector_ids=["codex"],
        respect_enabled=False,
        respect_cadence=False,
        settings_overrides=runtime_settings(codex_state=state),
    ).as_dict()
    second = run_connector_capture(
        paths,
        connector_ids=["codex"],
        respect_enabled=False,
        respect_cadence=False,
        settings_overrides=runtime_settings(codex_state=state),
    ).as_dict()

    assert first["captured"] == 1
    assert second["skipped"] == 1
    with sqlite3.connect(paths.sqlite_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM capture_sources WHERE agent = 'codex'").fetchone()[0]
    assert count == 1
