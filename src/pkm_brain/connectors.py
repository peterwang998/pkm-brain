from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

import yaml

from .capture import (
    AgentLogCapture,
    AgentSessionCapture,
    CaptureResult,
    ClaudeAdapter,
    CodexAdapter,
    HyprnoteAdapter,
    OpenCodeAdapter,
)
from .connector_auth import auth_manifest, connector_auth_status
from .paths import BrainPaths
from .util import now_iso


CONNECTOR_CONFIG_VERSION = 1
AGENT_CONNECTOR_IDS = ("codex", "claude", "opencode", "hyprnote")
DEFAULT_AGENT_CONNECTOR_IDS = ("codex", "claude", "opencode")


@dataclass(frozen=True)
class SettingField:
    key: str
    label: str
    kind: str
    default: Any = None
    help: str = ""
    choices: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "kind": self.kind,
            "default": self.default,
            "help": self.help,
            "choices": list(self.choices),
        }


@dataclass(frozen=True)
class ConnectorManifest:
    id: str
    display_name: str
    description: str
    source_type: str
    default_enabled: bool
    default_cadence_s: int
    settings_schema: list[SettingField] = field(default_factory=list)
    permissions_note: str = ""
    lifecycle: str = "active"
    capture_available: bool = True
    auth: dict[str, Any] | None = None
    activation_note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "description": self.description,
            "source_type": self.source_type,
            "default_enabled": self.default_enabled,
            "default_cadence_s": self.default_cadence_s,
            "settings_schema": [field.as_dict() for field in self.settings_schema],
            "permissions_note": self.permissions_note,
            "lifecycle": self.lifecycle,
            "capture_available": self.capture_available,
            "auth": dict(self.auth) if self.auth is not None else None,
            "activation_note": self.activation_note,
        }


@dataclass(frozen=True)
class PreflightReport:
    ok: bool = True
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "warnings": list(self.warnings),
            "errors": list(self.errors),
        }


@dataclass(frozen=True)
class ConnectorContext:
    paths: BrainPaths
    settings: dict[str, Any]

    def path_setting(self, key: str, default: str | Path) -> Path:
        value = self.settings.get(key, default)
        return Path(str(value)).expanduser()


class Connector(Protocol):
    manifest: ConnectorManifest

    def preflight(self, ctx: ConnectorContext) -> PreflightReport:
        ...

    def discover(self, ctx: ConnectorContext) -> list[AgentSessionCapture]:
        ...

    def capture(
        self,
        ctx: ConnectorContext,
        candidates: list[AgentSessionCapture],
        *,
        dry_run: bool = False,
        export_outbox: bool = False,
    ) -> CaptureResult:
        ...


@dataclass(frozen=True)
class ConnectorRun:
    connector_id: str
    status: str
    started_at: str
    finished_at: str
    preflight: dict[str, Any]
    discovered: int = 0
    captured: int = 0
    skipped: int = 0
    exported: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    outbox_artifacts: list[str] = field(default_factory=list)
    reason: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "connector_id": self.connector_id,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "preflight": self.preflight,
            "discovered": self.discovered,
            "captured": self.captured,
            "skipped": self.skipped,
            "exported": self.exported,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "artifacts": list(self.artifacts),
            "outbox_artifacts": list(self.outbox_artifacts),
            "reason": self.reason,
        }


@dataclass
class ConnectorBatchResult:
    discovered: int = 0
    captured: int = 0
    skipped: int = 0
    exported: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    artifacts: list[str] = field(default_factory=list)
    outbox_artifacts: list[str] = field(default_factory=list)
    connector_results: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "discovered": self.discovered,
            "captured": self.captured,
            "skipped": self.skipped,
            "exported": self.exported,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "artifacts": list(self.artifacts),
            "outbox_artifacts": list(self.outbox_artifacts),
            "connector_results": list(self.connector_results),
        }

    def add_run(self, run: ConnectorRun) -> None:
        self.discovered += run.discovered
        self.captured += run.captured
        self.skipped += run.skipped
        self.exported += run.exported
        self.warnings.extend(run.warnings)
        self.artifacts.extend(run.artifacts)
        self.outbox_artifacts.extend(run.outbox_artifacts)
        if run.status == "failed":
            self.warnings.append(f"{run.connector_id}: failed; see connector_results")
        self.connector_results.append(run.as_dict())


class AgentSessionConnector:
    def __init__(
        self,
        manifest: ConnectorManifest,
        adapter_factory: Callable[[ConnectorContext], Any],
        *,
        preflight_path_key: str,
    ) -> None:
        self.manifest = manifest
        self._adapter_factory = adapter_factory
        self._preflight_path_key = preflight_path_key

    def preflight(self, ctx: ConnectorContext) -> PreflightReport:
        path = ctx.path_setting(self._preflight_path_key, "")
        if not path.exists():
            return PreflightReport(
                ok=True,
                warnings=[f"{self._preflight_path_key} not found: {path}"],
            )
        return PreflightReport()

    def discover(self, ctx: ConnectorContext) -> list[AgentSessionCapture]:
        return self._adapter_factory(ctx).capture_sessions()

    def capture(
        self,
        ctx: ConnectorContext,
        candidates: list[AgentSessionCapture],
        *,
        dry_run: bool = False,
        export_outbox: bool = False,
    ) -> CaptureResult:
        return AgentLogCapture(ctx.paths).capture_sessions(
            candidates,
            dry_run=dry_run,
            export_outbox=export_outbox,
        )


class FilesConnector:
    manifest = ConnectorManifest(
        id="files",
        display_name="Files",
        description="Manual inbox drop folder for files placed under inbox/documents/.",
        source_type="file_drop",
        default_enabled=True,
        default_cadence_s=600,
        permissions_note="Reads files already placed in this brain's inbox/documents folder.",
        lifecycle="passive",
        capture_available=False,
        activation_note="Files are ingested after they are placed in the inbox; there is no external source to poll.",
    )

    def preflight(self, ctx: ConnectorContext) -> PreflightReport:
        return PreflightReport(
            ok=True,
            warnings=[] if (ctx.paths.inbox / "documents").exists() else ["inbox/documents does not exist yet"],
        )

    def discover(self, ctx: ConnectorContext) -> list[AgentSessionCapture]:
        return []

    def capture(
        self,
        ctx: ConnectorContext,
        candidates: list[AgentSessionCapture],
        *,
        dry_run: bool = False,
        export_outbox: bool = False,
    ) -> CaptureResult:
        return CaptureResult(discovered=0, skipped=0)


def codex_connector() -> Connector:
    return AgentSessionConnector(
        ConnectorManifest(
            id="codex",
            display_name="Codex",
            description="Capture local Codex agent session logs.",
            source_type="agent_session_log",
            default_enabled=True,
            default_cadence_s=600,
            settings_schema=[
                SettingField(
                    "state_db",
                    "State database",
                    "path",
                    "~/.codex/state_5.sqlite",
                    "Codex thread state SQLite database.",
                )
            ],
            permissions_note="Reads ~/.codex/state_5.sqlite and rollout JSONL paths referenced by it.",
        ),
        lambda ctx: CodexAdapter(ctx.path_setting("state_db", "~/.codex/state_5.sqlite")),
        preflight_path_key="state_db",
    )


def claude_connector() -> Connector:
    return AgentSessionConnector(
        ConnectorManifest(
            id="claude",
            display_name="Claude",
            description="Capture local Claude Code project session logs.",
            source_type="agent_session_log",
            default_enabled=True,
            default_cadence_s=600,
            settings_schema=[
                SettingField(
                    "projects_dir",
                    "Projects directory",
                    "path",
                    "~/.claude/projects",
                    "Claude Code projects directory.",
                )
            ],
            permissions_note="Reads JSONL files under ~/.claude/projects.",
        ),
        lambda ctx: ClaudeAdapter(ctx.path_setting("projects_dir", "~/.claude/projects")),
        preflight_path_key="projects_dir",
    )


def opencode_connector() -> Connector:
    return AgentSessionConnector(
        ConnectorManifest(
            id="opencode",
            display_name="OpenCode",
            description="Capture local OpenCode session logs.",
            source_type="agent_session_log",
            default_enabled=True,
            default_cadence_s=600,
            settings_schema=[
                SettingField(
                    "db_path",
                    "Database",
                    "path",
                    "~/.local/share/opencode/opencode.db",
                    "OpenCode SQLite database.",
                )
            ],
            permissions_note="Reads ~/.local/share/opencode/opencode.db.",
        ),
        lambda ctx: OpenCodeAdapter(ctx.path_setting("db_path", "~/.local/share/opencode/opencode.db")),
        preflight_path_key="db_path",
    )


def hyprnote_connector() -> Connector:
    return AgentSessionConnector(
        ConnectorManifest(
            id="hyprnote",
            display_name="Hyprnote",
            description="Capture local Hyprnote meeting sessions.",
            source_type="hyprnote_meeting",
            default_enabled=False,
            default_cadence_s=600,
            settings_schema=[
                SettingField(
                    "root",
                    "Hyprnote root",
                    "path",
                    "~/Library/Application Support/hyprnote",
                    "Hyprnote application support directory.",
                )
            ],
            permissions_note="Reads meeting session folders under Hyprnote application support.",
            activation_note="Opt-in because it scans private meeting data outside the Brain home.",
        ),
        lambda ctx: HyprnoteAdapter(ctx.path_setting("root", "~/Library/Application Support/hyprnote")),
        preflight_path_key="root",
    )


class AuthOnlyConnector:
    def __init__(self, manifest: ConnectorManifest) -> None:
        self.manifest = manifest

    def preflight(self, ctx: ConnectorContext) -> PreflightReport:
        return PreflightReport(
            ok=True,
            warnings=["Authentication is available; capture is not implemented."],
        )

    def discover(self, ctx: ConnectorContext) -> list[AgentSessionCapture]:
        return []

    def capture(
        self,
        ctx: ConnectorContext,
        candidates: list[AgentSessionCapture],
        *,
        dry_run: bool = False,
        export_outbox: bool = False,
    ) -> CaptureResult:
        return CaptureResult(
            warnings=["Authentication is available; capture is not implemented."],
        )


def gmail_connector() -> Connector:
    return AuthOnlyConnector(
        ConnectorManifest(
            id="gmail",
            display_name="Gmail",
            description=(
                "Authorize a separate read-only Gmail grant for the manual "
                "Chief of Staff Shadow trial."
            ),
            source_type="gmail_message",
            default_enabled=False,
            default_cadence_s=900,
            permissions_note=(
                "Read-only Gmail access; sending, mutation, deletion, labels, and attachment "
                "download are unavailable."
            ),
            lifecycle="auth_only",
            capture_available=False,
            auth=auth_manifest("gmail"),
            activation_note=(
                "Use Today > Run Shadow for one bounded operational pass. Connector "
                "capture, automatic scheduling, and Gmail knowledge ingestion remain "
                "unavailable."
            ),
        )
    )


def calendar_connector() -> Connector:
    return AuthOnlyConnector(
        ConnectorManifest(
            id="calendar",
            display_name="Google Calendar",
            description=(
                "Authorize a separate read-only Google Calendar grant for the manual "
                "Chief of Staff Shadow trial."
            ),
            source_type="google_calendar",
            default_enabled=False,
            default_cadence_s=900,
            permissions_note=(
                "Reads events from the owned primary calendar only; event mutation, RSVP, "
                "calendar discovery, and shared-calendar access are unavailable."
            ),
            lifecycle="auth_only",
            capture_available=False,
            auth=auth_manifest("calendar"),
            activation_note=(
                "Use Today > Run Shadow to read the owned primary calendar into the "
                "local operational ledger. Connector capture and automatic scheduling "
                "remain unavailable."
            ),
        )
    )


def slack_connector() -> Connector:
    return AuthOnlyConnector(
        ConnectorManifest(
            id="slack",
            display_name="Slack",
            description="Authorize a Slack account for a future conversation connector.",
            source_type="slack_message",
            default_enabled=False,
            default_cadence_s=900,
            permissions_note="Identity access only; no workspace messages or channel history are requested.",
            lifecycle="auth_only",
            capture_available=False,
            auth=auth_manifest("slack"),
            activation_note="Capture remains unavailable until Slack preprocessing and privacy policy are approved.",
        )
    )


BUILTIN_CONNECTORS: dict[str, Callable[[], Connector]] = {
    "codex": codex_connector,
    "claude": claude_connector,
    "opencode": opencode_connector,
    "hyprnote": hyprnote_connector,
    "files": FilesConnector,
    "calendar": calendar_connector,
    "gmail": gmail_connector,
    "slack": slack_connector,
}


def connector_config_path(paths: BrainPaths) -> Path:
    return paths.config_local / "connectors.yaml"


def connector_registry() -> dict[str, Connector]:
    return {connector_id: factory() for connector_id, factory in BUILTIN_CONNECTORS.items()}


def load_connector_config(paths: BrainPaths) -> dict[str, Any]:
    path = connector_config_path(paths)
    if path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            data = {}
    else:
        data = {}
    data.setdefault("version", CONNECTOR_CONFIG_VERSION)
    connectors = data.setdefault("connectors", {})
    registry = connector_registry()
    for connector_id, connector in registry.items():
        connectors.setdefault(connector_id, default_connector_state(connector.manifest))
        state = connectors[connector_id]
        state.setdefault("enabled", connector.manifest.default_enabled)
        state.setdefault("cadence_s", connector.manifest.default_cadence_s)
        state.setdefault("settings", default_settings(connector.manifest))
        state.setdefault("health", default_health())
    return data


def save_connector_config(paths: BrainPaths, config: dict[str, Any]) -> None:
    path = connector_config_path(paths)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(config, sort_keys=True, allow_unicode=False),
        encoding="utf-8",
    )


def default_connector_state(manifest: ConnectorManifest) -> dict[str, Any]:
    return {
        "enabled": manifest.default_enabled,
        "cadence_s": manifest.default_cadence_s,
        "settings": default_settings(manifest),
        "health": default_health(),
    }


def default_settings(manifest: ConnectorManifest) -> dict[str, Any]:
    return {
        field.key: field.default
        for field in manifest.settings_schema
        if field.default is not None
    }


def default_health() -> dict[str, Any]:
    return {
        "status": "unknown",
        "consecutive_failures": 0,
        "last_run_at": None,
        "last_error": None,
        "last_result": None,
    }


def list_connectors(paths: BrainPaths) -> dict[str, Any]:
    config = load_connector_config(paths)
    registry = connector_registry()
    items = [
        connector_payload(paths, connector, config["connectors"][connector_id])
        for connector_id, connector in sorted(registry.items())
    ]
    return {"connectors": items, "count": len(items)}


def get_connector(paths: BrainPaths, connector_id: str) -> dict[str, Any]:
    config = load_connector_config(paths)
    registry = connector_registry()
    connector = require_connector(registry, connector_id)
    return connector_payload(paths, connector, config["connectors"][connector_id])


def set_connector_enabled(paths: BrainPaths, connector_id: str, enabled: bool) -> dict[str, Any]:
    config = load_connector_config(paths)
    registry = connector_registry()
    connector = require_connector(registry, connector_id)
    if enabled and not connector.manifest.capture_available:
        raise ValueError(f"{connector.manifest.display_name} capture is not available")
    config["connectors"][connector_id]["enabled"] = enabled
    save_connector_config(paths, config)
    return connector_payload(paths, connector, config["connectors"][connector_id])


def update_connector_settings(paths: BrainPaths, connector_id: str, settings: dict[str, Any]) -> dict[str, Any]:
    config = load_connector_config(paths)
    registry = connector_registry()
    connector = require_connector(registry, connector_id)
    current = dict(config["connectors"][connector_id].get("settings") or {})
    current.update(validate_settings(connector.manifest, settings))
    config["connectors"][connector_id]["settings"] = current
    save_connector_config(paths, config)
    return connector_payload(paths, connector, config["connectors"][connector_id])


def connector_payload(
    paths: BrainPaths,
    connector: Connector,
    state: dict[str, Any],
) -> dict[str, Any]:
    return {
        "manifest": connector.manifest.as_dict(),
        "state": {
            "enabled": bool(state.get("enabled", connector.manifest.default_enabled)),
            "cadence_s": int(state.get("cadence_s", connector.manifest.default_cadence_s)),
            "settings": dict(state.get("settings") or {}),
            "auth": connector_auth_status(paths, connector.manifest.id),
        },
        "health": dict(state.get("health") or default_health()),
    }


def require_connector(registry: dict[str, Connector], connector_id: str) -> Connector:
    connector = registry.get(connector_id)
    if connector is None:
        raise ValueError(f"unknown connector: {connector_id}")
    return connector


def validate_settings(manifest: ConnectorManifest, settings: dict[str, Any]) -> dict[str, Any]:
    fields = {field.key: field for field in manifest.settings_schema}
    unknown = sorted(set(settings) - set(fields))
    if unknown:
        raise ValueError(f"unknown connector setting(s): {', '.join(unknown)}")
    output: dict[str, Any] = {}
    for key, value in settings.items():
        field = fields[key]
        if field.kind == "bool":
            output[key] = bool(value)
        elif field.kind in {"string", "path", "secret"}:
            output[key] = str(value)
        elif field.kind == "choice":
            if str(value) not in field.choices:
                raise ValueError(f"{key} must be one of: {', '.join(field.choices)}")
            output[key] = str(value)
        else:
            raise ValueError(f"unsupported setting kind for {key}: {field.kind}")
    return output


def connector_ids_for_agent(agent: str, *, include_hyprnote: bool = False) -> list[str]:
    if agent == "all":
        selected = list(DEFAULT_AGENT_CONNECTOR_IDS)
        if include_hyprnote:
            selected.append("hyprnote")
        return selected
    if agent not in AGENT_CONNECTOR_IDS:
        raise ValueError(f"unknown agent connector: {agent}")
    return [agent]


def runtime_settings(
    *,
    codex_state: Path | None = None,
    claude_projects: Path | None = None,
    opencode_db: Path | None = None,
    hyprnote_root: Path | None = None,
) -> dict[str, dict[str, Any]]:
    output: dict[str, dict[str, Any]] = {}
    if codex_state is not None:
        output.setdefault("codex", {})["state_db"] = str(codex_state)
    if claude_projects is not None:
        output.setdefault("claude", {})["projects_dir"] = str(claude_projects)
    if opencode_db is not None:
        output.setdefault("opencode", {})["db_path"] = str(opencode_db)
    if hyprnote_root is not None:
        output.setdefault("hyprnote", {})["root"] = str(hyprnote_root)
    return output


def run_connector_capture(
    paths: BrainPaths,
    *,
    connector_ids: list[str] | None = None,
    respect_enabled: bool = True,
    respect_cadence: bool = True,
    dry_run: bool = False,
    export_outbox: bool = False,
    settings_overrides: dict[str, dict[str, Any]] | None = None,
) -> ConnectorBatchResult:
    config = load_connector_config(paths)
    registry = connector_registry()
    selected_ids = connector_ids or sorted(registry)
    selected_ids = [connector_id for connector_id in selected_ids if connector_id in registry]
    batch = ConnectorBatchResult()
    for connector_id in selected_ids:
        connector = registry[connector_id]
        state = config["connectors"][connector_id]
        if not connector.manifest.capture_available:
            batch.add_run(skipped_run(connector_id, "capture not implemented"))
            continue
        if respect_enabled and not bool(state.get("enabled", connector.manifest.default_enabled)):
            run = skipped_run(connector_id, "disabled")
            batch.add_run(run)
            continue
        if respect_cadence and not connector_due(state):
            run = skipped_run(connector_id, "cadence not due")
            batch.add_run(run)
            continue
        settings = dict(state.get("settings") or {})
        settings.update((settings_overrides or {}).get(connector_id, {}))
        run = run_one_connector(
            connector,
            ConnectorContext(paths=paths, settings=settings),
            dry_run=dry_run,
            export_outbox=export_outbox,
        )
        update_health(state, run)
        batch.add_run(run)
    save_connector_config(paths, config)
    return batch


def run_one_connector(
    connector: Connector,
    ctx: ConnectorContext,
    *,
    dry_run: bool,
    export_outbox: bool,
) -> ConnectorRun:
    started_at = now_iso()
    preflight = connector.preflight(ctx)
    if not preflight.ok:
        return ConnectorRun(
            connector_id=connector.manifest.id,
            status="failed",
            started_at=started_at,
            finished_at=now_iso(),
            preflight=preflight.as_dict(),
            errors=list(preflight.errors),
            warnings=list(preflight.warnings),
        )
    try:
        candidates = connector.discover(ctx)
        result = connector.capture(ctx, candidates, dry_run=dry_run, export_outbox=export_outbox)
    except Exception as exc:
        return ConnectorRun(
            connector_id=connector.manifest.id,
            status="failed",
            started_at=started_at,
            finished_at=now_iso(),
            preflight=preflight.as_dict(),
            errors=[str(exc)],
            warnings=list(preflight.warnings),
        )
    status = "failed" if result.errors else "ok"
    return ConnectorRun(
        connector_id=connector.manifest.id,
        status=status,
        started_at=started_at,
        finished_at=now_iso(),
        preflight=preflight.as_dict(),
        discovered=result.discovered,
        captured=result.captured,
        skipped=result.skipped,
        exported=result.exported,
        errors=list(result.errors),
        warnings=list(preflight.warnings) + list(result.warnings),
        artifacts=list(result.artifacts),
        outbox_artifacts=list(result.outbox_artifacts),
    )


def skipped_run(connector_id: str, reason: str) -> ConnectorRun:
    now = now_iso()
    return ConnectorRun(
        connector_id=connector_id,
        status="skipped",
        started_at=now,
        finished_at=now,
        preflight=PreflightReport().as_dict(),
        reason=reason,
    )


def update_health(state: dict[str, Any], run: ConnectorRun) -> None:
    health = state.setdefault("health", default_health())
    health["last_run_at"] = run.finished_at
    health["last_result"] = {
        "status": run.status,
        "discovered": run.discovered,
        "captured": run.captured,
        "skipped": run.skipped,
        "exported": run.exported,
    }
    if run.status == "failed":
        health["consecutive_failures"] = int(health.get("consecutive_failures") or 0) + 1
        health["status"] = f"failing({health['consecutive_failures']})"
        health["last_error"] = "; ".join(run.errors[:3]) or "connector failed"
    else:
        health["consecutive_failures"] = 0
        health["last_error"] = None
        health["status"] = "warning" if run.warnings else "ok"


def connector_due(state: dict[str, Any]) -> bool:
    health = state.get("health") or {}
    last_run_at = health.get("last_run_at")
    if not last_run_at:
        return True
    try:
        last = datetime.fromisoformat(str(last_run_at))
    except ValueError:
        return True
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    cadence = int(state.get("cadence_s") or 600)
    return datetime.now(timezone.utc) - last >= timedelta(seconds=cadence)
