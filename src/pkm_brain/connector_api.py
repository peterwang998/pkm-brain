from __future__ import annotations

from typing import Any

from .connector_auth import (
    begin_connector_authorization,
    configure_connector_auth,
    disconnect_connector_auth,
)
from .connectors import (
    get_connector,
    run_connector_capture,
    set_connector_enabled,
    update_connector_settings,
)
from .paths import BrainPaths


def dispatch_connector_post(
    paths: BrainPaths,
    parts: list[str],
    payload: dict[str, Any],
) -> dict[str, Any]:
    if len(parts) == 3 and parts[2] in {"enable", "disable"}:
        return set_connector_enabled(paths, parts[1], enabled=parts[2] == "enable")
    if len(parts) == 3 and parts[2] == "run":
        return run_connector_capture(
            paths,
            connector_ids=[parts[1]],
            respect_enabled=False,
            respect_cadence=False,
        ).as_dict()
    if len(parts) == 4 and parts[2:] == ["auth", "start"]:
        result = begin_connector_authorization(paths, parts[1])
        result["connector"] = get_connector(paths, parts[1])
        return result
    if len(parts) == 4 and parts[2:] == ["auth", "disconnect"]:
        disconnect_connector_auth(paths, parts[1])
        return get_connector(paths, parts[1])
    raise ValueError("unknown connector command")


def dispatch_connector_put(
    paths: BrainPaths,
    parts: list[str],
    payload: dict[str, Any],
) -> dict[str, Any]:
    if len(parts) == 3 and parts[2] == "settings":
        settings = payload.get("settings") if isinstance(payload.get("settings"), dict) else payload
        return update_connector_settings(paths, parts[1], settings)
    if len(parts) == 4 and parts[2:] == ["auth", "config"]:
        configure_connector_auth(
            paths,
            parts[1],
            client_id=str(payload.get("client_id") or ""),
            client_secret=(
                str(payload["client_secret"])
                if payload.get("client_secret") is not None
                else None
            ),
        )
        return get_connector(paths, parts[1])
    raise ValueError("unknown connector update")
