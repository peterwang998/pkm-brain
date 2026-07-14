from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import secrets
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

import yaml

from .paths import BrainPaths
from .util import now_iso


AUTH_CONFIG_VERSION = 1
AUTH_CALLBACK_HOST = "127.0.0.1"
AUTH_CALLBACK_PORT = 53682
AUTH_FLOW_TTL = timedelta(minutes=10)
KEYCHAIN_SERVICE = "com.pkm-brain.connector-auth"


@dataclass(frozen=True)
class OAuthProvider:
    connector_id: str
    display_name: str
    authorization_endpoint: str
    token_endpoint: str
    setup_url: str
    scopes: tuple[str, ...]
    client_secret_required: bool
    phase: str = "identity_only"
    uses_pkce: bool = False
    uses_nonce: bool = False
    issuer: str | None = None

    def manifest(self) -> dict[str, Any]:
        return {
            "kind": "oauth2",
            "provider": self.connector_id,
            "phase": self.phase,
            "requested_scopes": list(self.scopes),
            "client_secret_required": self.client_secret_required,
            "redirect_uri": callback_uri(self.connector_id),
            "setup_url": self.setup_url,
        }


OAUTH_PROVIDERS: dict[str, OAuthProvider] = {
    "gmail": OAuthProvider(
        connector_id="gmail",
        display_name="Google",
        authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
        token_endpoint="https://oauth2.googleapis.com/token",
        setup_url="https://console.cloud.google.com/apis/credentials",
        scopes=(
            "openid",
            "email",
            "profile",
            "https://www.googleapis.com/auth/gmail.readonly",
        ),
        client_secret_required=False,
        phase="read_only",
        uses_pkce=True,
        issuer="https://accounts.google.com",
    ),
    "calendar": OAuthProvider(
        connector_id="calendar",
        display_name="Google Calendar",
        authorization_endpoint="https://accounts.google.com/o/oauth2/v2/auth",
        token_endpoint="https://oauth2.googleapis.com/token",
        setup_url="https://console.cloud.google.com/apis/credentials",
        scopes=(
            "openid",
            "email",
            "profile",
            "https://www.googleapis.com/auth/calendar.events.owned.readonly",
        ),
        client_secret_required=False,
        phase="read_only",
        uses_pkce=True,
        issuer="https://accounts.google.com",
    ),
    "slack": OAuthProvider(
        connector_id="slack",
        display_name="Slack",
        authorization_endpoint="https://slack.com/openid/connect/authorize",
        token_endpoint="https://slack.com/api/openid.connect.token",
        setup_url="https://api.slack.com/apps",
        scopes=("openid", "profile", "email"),
        client_secret_required=True,
        uses_nonce=True,
        issuer="https://slack.com",
    ),
}


class CredentialStore(Protocol):
    def load(self, connector_id: str) -> dict[str, Any] | None:
        ...

    def save(self, connector_id: str, payload: dict[str, Any]) -> None:
        ...

    def delete(self, connector_id: str) -> None:
        ...


class MemoryCredentialStore:
    def __init__(self) -> None:
        self.values: dict[str, dict[str, Any]] = {}

    def load(self, connector_id: str) -> dict[str, Any] | None:
        value = self.values.get(connector_id)
        return dict(value) if value is not None else None

    def save(self, connector_id: str, payload: dict[str, Any]) -> None:
        self.values[connector_id] = dict(payload)

    def delete(self, connector_id: str) -> None:
        self.values.pop(connector_id, None)


class UnavailableCredentialStore:
    def load(self, connector_id: str) -> dict[str, Any] | None:
        raise RuntimeError("macOS Keychain is unavailable")

    def save(self, connector_id: str, payload: dict[str, Any]) -> None:
        raise RuntimeError("macOS Keychain is unavailable")

    def delete(self, connector_id: str) -> None:
        raise RuntimeError("macOS Keychain is unavailable")


class MacOSKeychainCredentialStore:
    def __init__(self, paths: BrainPaths) -> None:
        home = str(paths.home.expanduser().resolve())
        self.account_prefix = hashlib.sha256(home.encode("utf-8")).hexdigest()[:16]

    def load(self, connector_id: str) -> dict[str, Any] | None:
        result = subprocess.run(
            [
                "/usr/bin/security",
                "find-generic-password",
                "-a",
                self._account(connector_id),
                "-s",
                KEYCHAIN_SERVICE,
                "-w",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            if "could not be found" in detail.casefold():
                return None
            raise RuntimeError("Unable to read connector credentials from Keychain")
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError("Connector credentials in Keychain are invalid") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("Connector credentials in Keychain are invalid")
        return payload

    def save(self, connector_id: str, payload: dict[str, Any]) -> None:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        result = subprocess.run(
            [
                "/usr/bin/security",
                "add-generic-password",
                "-a",
                self._account(connector_id),
                "-s",
                KEYCHAIN_SERVICE,
                "-w",
                encoded,
                "-U",
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError("Unable to save connector credentials in Keychain")

    def delete(self, connector_id: str) -> None:
        result = subprocess.run(
            [
                "/usr/bin/security",
                "delete-generic-password",
                "-a",
                self._account(connector_id),
                "-s",
                KEYCHAIN_SERVICE,
            ],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        detail = (result.stderr or result.stdout).strip()
        if result.returncode != 0 and "could not be found" not in detail.casefold():
            raise RuntimeError("Unable to delete connector credentials from Keychain")

    def _account(self, connector_id: str) -> str:
        return f"{self.account_prefix}:{connector_id}"


def credential_store_for(paths: BrainPaths) -> CredentialStore:
    if sys.platform == "darwin" and Path("/usr/bin/security").exists():
        return MacOSKeychainCredentialStore(paths)
    return UnavailableCredentialStore()


def auth_config_path(paths: BrainPaths) -> Path:
    return paths.config_local / "connector-auth.yaml"


_CONFIG_LOCK = threading.RLock()


def load_auth_config(paths: BrainPaths) -> dict[str, Any]:
    with _CONFIG_LOCK:
        path = auth_config_path(paths)
        if path.exists():
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if not isinstance(data, dict):
                data = {}
        else:
            data = {}
        data.setdefault("version", AUTH_CONFIG_VERSION)
        connectors = data.setdefault("connectors", {})
        for connector_id in OAUTH_PROVIDERS:
            connectors.setdefault(connector_id, default_auth_state())
        return data


def save_auth_config(paths: BrainPaths, config: dict[str, Any]) -> None:
    with _CONFIG_LOCK:
        path = auth_config_path(paths)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        temporary.write_text(
            yaml.safe_dump(config, sort_keys=True, allow_unicode=False),
            encoding="utf-8",
        )
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)


def default_auth_state() -> dict[str, Any]:
    return {
        "client_id": None,
        "status": "not_configured",
        "connected_at": None,
        "account_label": None,
        "provider_subject": None,
        "granted_scopes": [],
        "last_error": None,
        "updated_at": None,
    }


def auth_manifest(connector_id: str) -> dict[str, Any] | None:
    provider = OAUTH_PROVIDERS.get(connector_id)
    return provider.manifest() if provider is not None else None


def connector_auth_status(
    paths: BrainPaths,
    connector_id: str,
    *,
    store: CredentialStore | None = None,
) -> dict[str, Any] | None:
    provider = OAUTH_PROVIDERS.get(connector_id)
    if provider is None:
        return None
    config = load_auth_config(paths)
    state = dict(config["connectors"].get(connector_id) or default_auth_state())
    client_id = str(state.get("client_id") or "").strip()
    credentials: dict[str, Any] = {}
    credential_error: str | None = None
    if client_id:
        try:
            credentials = (store or credential_store_for(paths)).load(connector_id) or {}
        except RuntimeError as exc:
            credential_error = str(exc)

    has_secret = bool(credentials.get("client_secret"))
    has_token = any(
        credentials.get(key)
        for key in ("access_token", "refresh_token", "id_token")
    )
    requested = set(provider.scopes)
    granted = {str(item) for item in state.get("granted_scopes") or [] if item}
    if not client_id:
        status = "not_configured"
    elif credential_error:
        status = "unavailable"
    elif provider.client_secret_required and not has_secret:
        status = "not_configured"
    elif has_token and requested.issubset(granted):
        status = "connected"
    elif has_token:
        status = "reauthorization_required"
    elif _authorization_is_pending(state):
        status = "authorizing"
    elif state.get("status") == "error" and state.get("last_error"):
        status = "error"
    else:
        status = "ready"

    return {
        "kind": "oauth2",
        "provider": connector_id,
        "phase": provider.phase,
        "status": status,
        "client_id": client_id or None,
        "client_secret_configured": has_secret,
        "connected_at": state.get("connected_at"),
        "account_label": state.get("account_label"),
        "provider_subject": (
            state.get("provider_subject")
            or _provider_subject_from_credentials(credentials)
        ),
        "granted_scopes": sorted(granted),
        "requested_scopes": list(provider.scopes),
        "redirect_uri": callback_uri(connector_id),
        "setup_url": provider.setup_url,
        "can_authorize": bool(
            client_id
            and not credential_error
            and (has_secret or not provider.client_secret_required)
            and status != "authorizing"
        ),
        "can_disconnect": has_token,
        "last_error": credential_error or state.get("last_error"),
    }


def configure_connector_auth(
    paths: BrainPaths,
    connector_id: str,
    *,
    client_id: str,
    client_secret: str | None = None,
    store: CredentialStore | None = None,
) -> dict[str, Any]:
    provider = require_provider(connector_id)
    normalized_client_id = client_id.strip()
    if not normalized_client_id:
        raise ValueError("Client ID is required")
    credential_store = store or credential_store_for(paths)
    existing = credential_store.load(connector_id) or {}
    config = load_auth_config(paths)
    state = config["connectors"].setdefault(connector_id, default_auth_state())
    client_changed = str(state.get("client_id") or "") != normalized_client_id
    credentials = {} if client_changed else dict(existing)
    normalized_secret = (client_secret or "").strip()
    if normalized_secret:
        credentials["client_secret"] = normalized_secret
    if provider.client_secret_required and not credentials.get("client_secret"):
        raise ValueError("Client secret is required")
    credential_store.save(connector_id, credentials)
    state.update(
        {
            "client_id": normalized_client_id,
            "status": "ready",
            "connected_at": None if client_changed else state.get("connected_at"),
            "account_label": None if client_changed else state.get("account_label"),
            "provider_subject": (
                None if client_changed else state.get("provider_subject")
            ),
            "granted_scopes": [] if client_changed else state.get("granted_scopes", []),
            "last_error": None,
            "updated_at": now_iso(),
        }
    )
    save_auth_config(paths, config)
    return connector_auth_status(paths, connector_id, store=credential_store) or {}


def disconnect_connector_auth(
    paths: BrainPaths,
    connector_id: str,
    *,
    store: CredentialStore | None = None,
) -> dict[str, Any]:
    require_provider(connector_id)
    credential_store = store or credential_store_for(paths)
    credentials = credential_store.load(connector_id) or {}
    for key in (
        "access_token",
        "refresh_token",
        "id_token",
        "token_type",
        "scope",
        "expires_at",
    ):
        credentials.pop(key, None)
    if credentials:
        credential_store.save(connector_id, credentials)
    else:
        credential_store.delete(connector_id)
    config = load_auth_config(paths)
    state = config["connectors"].setdefault(connector_id, default_auth_state())
    state.update(
        {
            "status": "ready" if state.get("client_id") else "not_configured",
            "connected_at": None,
            "account_label": None,
            "provider_subject": None,
            "granted_scopes": [],
            "last_error": None,
            "updated_at": now_iso(),
        }
    )
    save_auth_config(paths, config)
    return connector_auth_status(paths, connector_id, store=credential_store) or {}


@dataclass
class PendingOAuthFlow:
    paths: BrainPaths
    provider: OAuthProvider
    store: CredentialStore
    state: str
    client_id: str
    client_secret: str | None
    redirect_uri: str
    created_at: datetime
    code_verifier: str | None = None
    nonce: str | None = None


TokenExchanger = Callable[[PendingOAuthFlow, str], dict[str, Any]]


class OAuthCallbackBroker:
    def __init__(
        self,
        *,
        host: str = AUTH_CALLBACK_HOST,
        port: int = AUTH_CALLBACK_PORT,
        token_exchanger: TokenExchanger | None = None,
    ) -> None:
        self.host = host
        self.requested_port = port
        self.token_exchanger = token_exchanger
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._pending: dict[str, PendingOAuthFlow] = {}
        self._lock = threading.RLock()

    @property
    def port(self) -> int:
        if self._server is None:
            return self.requested_port
        return int(self._server.server_address[1])

    def begin(
        self,
        paths: BrainPaths,
        provider: OAuthProvider,
        client_id: str,
        client_secret: str | None,
        store: CredentialStore,
    ) -> dict[str, Any]:
        self._ensure_server()
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64) if provider.uses_pkce else None
        nonce = secrets.token_urlsafe(32) if provider.uses_nonce else None
        redirect_uri = callback_uri(provider.connector_id, port=self.port)
        flow = PendingOAuthFlow(
            paths=paths,
            provider=provider,
            store=store,
            state=state,
            client_id=client_id,
            client_secret=client_secret,
            redirect_uri=redirect_uri,
            created_at=datetime.now(timezone.utc),
            code_verifier=verifier,
            nonce=nonce,
        )
        with self._lock:
            self._discard_expired_locked()
            self._pending[state] = flow
        return {
            "authorization_url": build_authorization_url(flow),
            "redirect_uri": redirect_uri,
            "expires_at": (flow.created_at + AUTH_FLOW_TTL).replace(microsecond=0).isoformat(),
        }

    def shutdown(self) -> None:
        with self._lock:
            server = self._server
            thread = self._thread
            self._server = None
            self._thread = None
            self._pending.clear()
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None:
            thread.join(timeout=2)

    def _ensure_server(self) -> None:
        with self._lock:
            if self._server is not None:
                return
            broker = self

            class CallbackHandler(BaseHTTPRequestHandler):
                def do_GET(self) -> None:
                    parsed = urlparse(self.path)
                    broker._serve_callback(self, parsed.path, parse_qs(parsed.query))

                def do_POST(self) -> None:
                    parsed = urlparse(self.path)
                    length = int(self.headers.get("Content-Length") or 0)
                    body = self.rfile.read(length).decode("utf-8") if length else ""
                    broker._serve_callback(self, parsed.path, parse_qs(body))

                def log_message(self, format: str, *args: Any) -> None:
                    return

            try:
                server = ThreadingHTTPServer((self.host, self.requested_port), CallbackHandler)
            except OSError as exc:
                raise ValueError(
                    f"OAuth callback port {self.requested_port} is unavailable; restart PKM Brain and try again"
                ) from exc
            server.daemon_threads = True
            thread = threading.Thread(
                target=server.serve_forever,
                name="pkm-brain-oauth-callback",
                daemon=True,
            )
            thread.start()
            self._server = server
            self._thread = thread

    def _serve_callback(
        self,
        handler: BaseHTTPRequestHandler,
        path: str,
        query: dict[str, list[str]],
    ) -> None:
        connector_id = path.removeprefix("/oauth/callback/").strip("/")
        state = _query_value(query, "state")
        with self._lock:
            self._discard_expired_locked()
            flow = self._pending.pop(state, None) if state else None
        if flow is None or flow.provider.connector_id != connector_id:
            self._write_callback_page(handler, False, "Authorization request is invalid or expired")
            return
        provider_error = _query_value(query, "error")
        if provider_error:
            detail = _query_value(query, "error_description") or provider_error
            record_authorization_error(flow.paths, connector_id, detail)
            self._write_callback_page(handler, False, detail)
            return
        code = _query_value(query, "code")
        if not code:
            detail = "Authorization provider did not return a code"
            record_authorization_error(flow.paths, connector_id, detail)
            self._write_callback_page(handler, False, detail)
            return
        try:
            token_response = (self.token_exchanger or exchange_authorization_code)(flow, code)
            complete_authorization(flow, token_response)
        except Exception as exc:
            detail = str(exc) or "Authorization failed"
            record_authorization_error(flow.paths, connector_id, detail)
            self._write_callback_page(handler, False, detail)
            return
        self._write_callback_page(handler, True, f"{flow.provider.display_name} connected")

    def _discard_expired_locked(self) -> None:
        cutoff = datetime.now(timezone.utc) - AUTH_FLOW_TTL
        expired = [state for state, flow in self._pending.items() if flow.created_at < cutoff]
        for state in expired:
            self._pending.pop(state, None)

    @staticmethod
    def _write_callback_page(
        handler: BaseHTTPRequestHandler,
        success: bool,
        message: str,
    ) -> None:
        title = "Authorization complete" if success else "Authorization failed"
        body = (
            "<!doctype html><html><head><meta charset=\"utf-8\"><title>"
            f"{html.escape(title)}</title></head><body><main><h1>{html.escape(title)}</h1>"
            f"<p>{html.escape(message)}</p><p>Return to PKM Brain.</p></main></body></html>"
        ).encode("utf-8")
        handler.send_response(HTTPStatus.OK.value if success else HTTPStatus.BAD_REQUEST.value)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Content-Security-Policy", "default-src 'none'")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)


_DEFAULT_BROKER = OAuthCallbackBroker()


def begin_connector_authorization(
    paths: BrainPaths,
    connector_id: str,
    *,
    store: CredentialStore | None = None,
    broker: OAuthCallbackBroker | None = None,
) -> dict[str, Any]:
    provider = require_provider(connector_id)
    credential_store = store or credential_store_for(paths)
    config = load_auth_config(paths)
    state = config["connectors"].setdefault(connector_id, default_auth_state())
    client_id = str(state.get("client_id") or "").strip()
    if not client_id:
        raise ValueError("Configure the OAuth client before connecting")
    credentials = credential_store.load(connector_id) or {}
    client_secret = str(credentials.get("client_secret") or "").strip() or None
    if provider.client_secret_required and not client_secret:
        raise ValueError("Configure the OAuth client secret before connecting")
    result = (broker or _DEFAULT_BROKER).begin(
        paths,
        provider,
        client_id,
        client_secret,
        credential_store,
    )
    state.update(
        {
            "status": "authorizing",
            "last_error": None,
            "updated_at": now_iso(),
        }
    )
    save_auth_config(paths, config)
    return result


def callback_uri(connector_id: str, *, port: int = AUTH_CALLBACK_PORT) -> str:
    return f"http://{AUTH_CALLBACK_HOST}:{port}/oauth/callback/{connector_id}"


def build_authorization_url(flow: PendingOAuthFlow) -> str:
    parameters = {
        "client_id": flow.client_id,
        "redirect_uri": flow.redirect_uri,
        "response_type": "code",
        "scope": " ".join(flow.provider.scopes),
        "state": flow.state,
    }
    if flow.provider.uses_pkce and flow.code_verifier:
        digest = hashlib.sha256(flow.code_verifier.encode("ascii")).digest()
        parameters["code_challenge"] = _base64url(digest)
        parameters["code_challenge_method"] = "S256"
        parameters["access_type"] = "offline"
        parameters["prompt"] = "consent"
    if flow.provider.uses_nonce and flow.nonce:
        parameters["nonce"] = flow.nonce
    return f"{flow.provider.authorization_endpoint}?{urlencode(parameters)}"


def exchange_authorization_code(flow: PendingOAuthFlow, code: str) -> dict[str, Any]:
    form = {
        "client_id": flow.client_id,
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": flow.redirect_uri,
    }
    if flow.client_secret:
        form["client_secret"] = flow.client_secret
    if flow.code_verifier:
        form["code_verifier"] = flow.code_verifier
    request = Request(
        flow.provider.token_endpoint,
        data=urlencode(form).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "PKM-Brain/connector-auth",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = _oauth_http_error(exc)
        raise ValueError(f"OAuth token exchange failed: {detail}") from exc
    except (URLError, TimeoutError) as exc:
        raise ValueError("OAuth token exchange could not reach the provider") from exc
    except json.JSONDecodeError as exc:
        raise ValueError("OAuth provider returned an invalid token response") from exc
    if not isinstance(payload, dict):
        raise ValueError("OAuth provider returned an invalid token response")
    if payload.get("ok") is False or payload.get("error"):
        detail = str(payload.get("error_description") or payload.get("error") or "provider rejected request")
        raise ValueError(f"OAuth token exchange failed: {detail}")
    return payload


def complete_authorization(flow: PendingOAuthFlow, token_response: dict[str, Any]) -> None:
    if not any(token_response.get(key) for key in ("access_token", "refresh_token", "id_token")):
        raise ValueError("OAuth provider returned no usable token")
    id_token = str(token_response.get("id_token") or "")
    claims = decode_id_token_claims(id_token)
    if not claims:
        raise ValueError("OAuth provider returned an invalid identity token")
    _validate_identity_claims(flow, claims)
    existing = flow.store.load(flow.provider.connector_id) or {}
    existing_refresh_token = str(existing.get("refresh_token") or "").strip()
    response_refresh_token = str(token_response.get("refresh_token") or "").strip()
    if existing_refresh_token and not response_refresh_token:
        existing_subject = _provider_subject_from_credentials(existing)
        response_subject = _provider_subject(claims)
        if (
            existing_subject is None
            or response_subject is None
            or existing_subject != response_subject
        ):
            raise ValueError(
                "OAuth provider did not return a refresh token and the existing "
                "credential identity could not be verified"
            )
    credentials = {"client_secret": flow.client_secret} if flow.client_secret else {}
    for key in ("access_token", "refresh_token", "id_token", "token_type", "scope"):
        value = token_response.get(key)
        if value:
            credentials[key] = value
        elif key == "refresh_token" and existing_refresh_token:
            credentials[key] = existing_refresh_token
    if token_response.get("expires_in") is not None:
        try:
            expires_at = datetime.now(timezone.utc) + timedelta(seconds=int(token_response["expires_in"]))
            credentials["expires_at"] = expires_at.replace(microsecond=0).isoformat()
        except (TypeError, ValueError):
            pass
    flow.store.save(flow.provider.connector_id, credentials)
    granted = _granted_scopes(token_response, flow.provider.scopes)
    config = load_auth_config(flow.paths)
    state = config["connectors"].setdefault(flow.provider.connector_id, default_auth_state())
    state.update(
        {
            "status": "connected",
            "connected_at": now_iso(),
            "account_label": _account_label(claims),
            "provider_subject": _provider_subject(claims),
            "granted_scopes": granted,
            "last_error": None,
            "updated_at": now_iso(),
        }
    )
    save_auth_config(flow.paths, config)


def record_authorization_error(paths: BrainPaths, connector_id: str, detail: str) -> None:
    config = load_auth_config(paths)
    state = config["connectors"].setdefault(connector_id, default_auth_state())
    state.update(
        {
            "status": "error",
            "last_error": detail[:500],
            "updated_at": now_iso(),
        }
    )
    save_auth_config(paths, config)


def decode_id_token_claims(id_token: str) -> dict[str, Any]:
    parts = id_token.split(".")
    if len(parts) != 3:
        return {}
    try:
        raw = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(raw).decode("utf-8"))
    except (ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def require_provider(connector_id: str) -> OAuthProvider:
    provider = OAUTH_PROVIDERS.get(connector_id)
    if provider is None:
        raise ValueError(f"connector does not support OAuth: {connector_id}")
    return provider


def _authorization_is_pending(state: dict[str, Any]) -> bool:
    if state.get("status") != "authorizing" or not state.get("updated_at"):
        return False
    try:
        updated = datetime.fromisoformat(str(state["updated_at"]))
    except ValueError:
        return False
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - updated < AUTH_FLOW_TTL


def _validate_identity_claims(flow: PendingOAuthFlow, claims: dict[str, Any]) -> None:
    if flow.nonce and claims.get("nonce") != flow.nonce:
        raise ValueError("OAuth identity nonce did not match")
    audience = claims.get("aud")
    if audience and audience != flow.client_id and flow.client_id not in (audience if isinstance(audience, list) else []):
        raise ValueError("OAuth identity audience did not match")
    issuer = str(claims.get("iss") or "")
    if flow.provider.issuer and issuer and issuer not in {flow.provider.issuer, "accounts.google.com"}:
        raise ValueError("OAuth identity issuer did not match")
    if claims.get("exp") is not None:
        try:
            expires_at = int(claims["exp"])
        except (TypeError, ValueError) as exc:
            raise ValueError("OAuth identity expiry is invalid") from exc
        if expires_at <= int(datetime.now(timezone.utc).timestamp()):
            raise ValueError("OAuth identity token is expired")


def _granted_scopes(response: dict[str, Any], fallback: tuple[str, ...]) -> list[str]:
    raw = response.get("scope")
    if isinstance(raw, str):
        values = raw.replace(",", " ").split()
        return sorted(set(values))
    if isinstance(raw, list):
        return sorted({str(item) for item in raw if item})
    return list(fallback)


def _account_label(claims: dict[str, Any]) -> str | None:
    for key in ("email", "name", "sub"):
        value = str(claims.get(key) or "").strip()
        if value:
            return value
    return None


def _provider_subject(claims: dict[str, Any]) -> str | None:
    value = str(claims.get("sub") or "").strip()
    return value[:255] or None


def _provider_subject_from_credentials(credentials: dict[str, Any]) -> str | None:
    id_token = str(credentials.get("id_token") or "")
    return _provider_subject(decode_id_token_claims(id_token)) if id_token else None


def _query_value(query: dict[str, list[str]], key: str) -> str:
    values = query.get(key) or []
    return str(values[0]) if values else ""


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _oauth_http_error(exc: HTTPError) -> str:
    try:
        payload = json.loads(exc.read().decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return f"HTTP {exc.code}"
    if isinstance(payload, dict):
        return str(payload.get("error_description") or payload.get("error") or f"HTTP {exc.code}")
    return f"HTTP {exc.code}"
