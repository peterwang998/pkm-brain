from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Any, Protocol

import yaml

from .llm_usage import (
    anthropic_response_usage,
    codex_jsonl_usage,
    configure_provider_usage,
    ollama_response_usage,
    openai_response_usage,
    record_provider_usage,
)
from .util import now_iso

if TYPE_CHECKING:
    from .paths import BrainPaths

DEFAULT_LLM_PROVIDER = "codex"
OPENAI_DEFAULT_MODEL = "gpt-5.5"
OPENAI_DEFAULT_FALLBACK_MODELS = ("gpt-5.4", "gpt-5.4-mini", "gpt-5")
ANTHROPIC_DEFAULT_MODEL = "claude-sonnet-4-5"
ANTHROPIC_DEFAULT_FALLBACK_MODELS: tuple[str, ...] = ()
CODEX_DEFAULT_MODEL = "gpt-5.6-sol"
CODEX_DEFAULT_FALLBACK_MODELS = ("gpt-5.6-luna",)
LLM_ROLE_ORDER = (
    "extractor",
    "resolver",
    "gardener",
    "synthesizer",
    "critic",
    "auditor",
)
LLM_ROLES = set(LLM_ROLE_ORDER)
COS_PROPOSER_ROLES = ("extractor", "resolver", "gardener", "synthesizer")
COS_REVIEWER_ROLES = ("critic", "auditor")
CRITIC_AUDITOR_MODEL_OVERLAP_WARNING = (
    "critic and auditor use the same provider/model; audit independence is reduced"
)
COS_LLM_CONFIG_FILENAME = "cos_llm.yaml"
VALID_PROVIDERS = {"openai", "anthropic", "ollama", "codex"}
_COS_ROLE_PROVIDER_LOCK = Lock()


class LLMConfigurationError(RuntimeError):
    pass


class LLMProviderError(RuntimeError):
    pass


class LLMProvider(Protocol):
    name: str
    model: str

    def complete(self, prompt: str) -> str: ...


@dataclass(frozen=True)
class ProviderStatus:
    provider: str
    model: str | None
    configured: bool
    missing: list[str]
    base_url: str | None = None
    fallback_models: list[str] | None = None
    reasoning_effort: str | None = None
    cost_source: str | None = None


@dataclass(frozen=True)
class CosRoleSelection:
    role: str
    provider: str | None
    provider_source: str | None
    model: str | None = None
    model_source: str | None = None
    model_fallbacks: list[str] | None = None
    fallback_source: str | None = None
    reasoning_effort: str | None = None
    reasoning_effort_source: str | None = None
    base_url: str | None = None
    base_url_source: str | None = None


class OpenAIProvider:
    name = "openai"

    def __init__(self) -> None:
        self.api_key = os.environ.get("OPENAI_API_KEY")
        self.models = model_candidates(
            "PKM_BRAIN_OPENAI_MODEL",
            OPENAI_DEFAULT_MODEL,
            "PKM_BRAIN_OPENAI_MODEL_FALLBACKS",
            OPENAI_DEFAULT_FALLBACK_MODELS,
        )
        self.model = self.models[0]
        self.base_url = os.environ.get(
            "PKM_BRAIN_OPENAI_BASE_URL", "https://api.openai.com/v1"
        )
        if not self.api_key:
            raise LLMConfigurationError(
                "OPENAI_API_KEY is required for OpenAI provider"
            )

    def complete(self, prompt: str) -> str:
        def run_once(model: str) -> str:
            started_at = now_iso()
            started_clock = time.perf_counter()
            data: dict[str, Any] | None = None
            status = "failed"
            error_type: str | None = None
            payload = {
                "model": model,
                "messages": [
                    {
                        "role": "system",
                        "content": "Return only valid JSON. Do not wrap it in Markdown.",
                    },
                    {"role": "user", "content": prompt},
                ],
            }
            try:
                data = post_json(
                    f"{self.base_url.rstrip('/')}/chat/completions",
                    payload,
                    {"Authorization": f"Bearer {self.api_key}"},
                )
                content = str(data["choices"][0]["message"]["content"])
                self.model = model
                status = "success"
                return content
            except (KeyError, IndexError, TypeError) as exc:
                error_type = type(exc).__name__
                raise LLMProviderError(
                    f"Unexpected OpenAI response shape: {data}"
                ) from exc
            except Exception as exc:
                error_type = type(exc).__name__
                raise
            finally:
                record_provider_usage(
                    self,
                    model=model,
                    usage=openai_response_usage(data),
                    status=status,
                    started_at=started_at,
                    duration_ms=int((time.perf_counter() - started_clock) * 1000),
                    error_type=error_type,
                )

        return complete_with_model_fallbacks("OpenAI", self.models, run_once)


class AnthropicProvider:
    name = "anthropic"

    def __init__(self) -> None:
        self.api_key = os.environ.get("ANTHROPIC_API_KEY")
        self.models = model_candidates(
            "PKM_BRAIN_ANTHROPIC_MODEL",
            ANTHROPIC_DEFAULT_MODEL,
            "PKM_BRAIN_ANTHROPIC_MODEL_FALLBACKS",
            ANTHROPIC_DEFAULT_FALLBACK_MODELS,
        )
        self.model = self.models[0]
        self.base_url = os.environ.get(
            "PKM_BRAIN_ANTHROPIC_BASE_URL", "https://api.anthropic.com"
        )
        if not self.api_key:
            raise LLMConfigurationError(
                "ANTHROPIC_API_KEY is required for Anthropic provider"
            )

    def complete(self, prompt: str) -> str:
        def run_once(model: str) -> str:
            started_at = now_iso()
            started_clock = time.perf_counter()
            data: dict[str, Any] | None = None
            status = "failed"
            error_type: str | None = None
            payload = {
                "model": model,
                "max_tokens": 4096,
                "system": "Return only valid JSON. Do not wrap it in Markdown.",
                "messages": [{"role": "user", "content": prompt}],
            }
            try:
                data = post_json(
                    f"{self.base_url.rstrip('/')}/v1/messages",
                    payload,
                    {
                        "x-api-key": self.api_key,
                        "anthropic-version": "2023-06-01",
                    },
                )
                content = "".join(
                    part.get("text", "")
                    for part in data["content"]
                    if part.get("type") == "text"
                )
                self.model = model
                status = "success"
                return content
            except (KeyError, TypeError) as exc:
                error_type = type(exc).__name__
                raise LLMProviderError(
                    f"Unexpected Anthropic response shape: {data}"
                ) from exc
            except Exception as exc:
                error_type = type(exc).__name__
                raise
            finally:
                record_provider_usage(
                    self,
                    model=model,
                    usage=anthropic_response_usage(data),
                    status=status,
                    started_at=started_at,
                    duration_ms=int((time.perf_counter() - started_clock) * 1000),
                    error_type=error_type,
                )

        return complete_with_model_fallbacks("Anthropic", self.models, run_once)


class OllamaProvider:
    name = "ollama"

    def __init__(self) -> None:
        self.model = os.environ.get("PKM_BRAIN_OLLAMA_MODEL")
        self.models = model_candidates(
            "PKM_BRAIN_OLLAMA_MODEL", "", "PKM_BRAIN_OLLAMA_MODEL_FALLBACKS", ()
        )
        self.base_url = os.environ.get(
            "PKM_BRAIN_OLLAMA_BASE_URL", "http://localhost:11434"
        )
        if not self.models:
            raise LLMConfigurationError(
                "PKM_BRAIN_OLLAMA_MODEL is required for Ollama provider"
            )
        self.model = self.models[0]

    def complete(self, prompt: str) -> str:
        def run_once(model: str) -> str:
            started_at = now_iso()
            started_clock = time.perf_counter()
            data: dict[str, Any] | None = None
            status = "failed"
            error_type: str | None = None
            payload = {
                "model": model,
                "stream": False,
                "messages": [
                    {
                        "role": "system",
                        "content": "Return only valid JSON. Do not wrap it in Markdown.",
                    },
                    {"role": "user", "content": prompt},
                ],
            }
            try:
                data = post_json(f"{self.base_url.rstrip('/')}/api/chat", payload, {})
                content = str(data["message"]["content"])
                self.model = model
                status = "success"
                return content
            except (KeyError, TypeError) as exc:
                error_type = type(exc).__name__
                raise LLMProviderError(
                    f"Unexpected Ollama response shape: {data}"
                ) from exc
            except Exception as exc:
                error_type = type(exc).__name__
                raise
            finally:
                record_provider_usage(
                    self,
                    model=model,
                    usage=ollama_response_usage(data),
                    status=status,
                    started_at=started_at,
                    duration_ms=int((time.perf_counter() - started_clock) * 1000),
                    error_type=error_type,
                )

        return complete_with_model_fallbacks("Ollama", self.models, run_once)


class CodexProvider:
    name = "codex"

    def __init__(self) -> None:
        self.models = model_candidates(
            "PKM_BRAIN_CODEX_MODEL",
            CODEX_DEFAULT_MODEL,
            "PKM_BRAIN_CODEX_MODEL_FALLBACKS",
            CODEX_DEFAULT_FALLBACK_MODELS,
        )
        self.model = self.models[0]
        self.binary = os.environ.get("PKM_BRAIN_CODEX_BIN") or shutil.which("codex")
        self.cwd = Path(os.environ.get("PKM_BRAIN_CODEX_CWD", Path.cwd())).expanduser()
        self.timeout = int(os.environ.get("PKM_BRAIN_CODEX_TIMEOUT_SECONDS", "900"))
        self.reasoning_effort = normalized_reasoning_effort(
            os.environ.get("PKM_BRAIN_CODEX_REASONING_EFFORT")
        )
        if not self.binary:
            raise LLMConfigurationError(
                "codex executable was not found; install/login to Codex CLI first"
            )
        missing = codex_missing_configuration(self.binary)
        if missing:
            raise LLMConfigurationError(", ".join(missing))

    def complete(self, prompt: str) -> str:
        return complete_with_model_fallbacks(
            "Codex", self.models, lambda model: self._complete_once(model, prompt)
        )

    def _complete_once(self, model: str, prompt: str) -> str:
        started_at = now_iso()
        started_clock = time.perf_counter()
        usage: dict[str, int] | None = None
        metadata: dict[str, Any] = {}
        status = "failed"
        error_type: str | None = None
        with tempfile.TemporaryDirectory(prefix="pkm-brain-codex-") as temp_dir:
            output_path = Path(temp_dir) / "last-message.txt"
            command = [
                self.binary,
                "--ask-for-approval",
                "never",
                *codex_reasoning_effort_args(self.reasoning_effort),
                "exec",
                "--json",
                "--sandbox",
                "read-only",
                "--cd",
                str(self.cwd),
                "--skip-git-repo-check",
                "--model",
                model,
                "--output-last-message",
                str(output_path),
                "-",
            ]
            try:
                completed = subprocess.run(
                    command,
                    input=codex_prompt(prompt),
                    text=True,
                    capture_output=True,
                    timeout=self.timeout,
                    check=False,
                )
            except subprocess.TimeoutExpired as exc:
                error_type = type(exc).__name__
                record_provider_usage(
                    self,
                    model=model,
                    usage=None,
                    status=status,
                    started_at=started_at,
                    duration_ms=int((time.perf_counter() - started_clock) * 1000),
                    error_type=error_type,
                )
                raise LLMProviderError(
                    f"Codex timed out after {self.timeout} seconds"
                ) from exc
            usage, metadata = codex_jsonl_usage(completed.stdout)
            try:
                if completed.returncode != 0:
                    stderr = completed.stderr.strip()
                    stdout = completed.stdout.strip()
                    detail = stderr or stdout or f"exit code {completed.returncode}"
                    raise LLMProviderError(f"Codex provider failed: {detail}")
                if output_path.exists():
                    output = output_path.read_text(encoding="utf-8").strip()
                else:
                    output = completed.stdout.strip()
                if not output:
                    raise LLMProviderError("Codex provider returned an empty response")
                self.model = model
                status = "success"
                return output
            except Exception as exc:
                error_type = type(exc).__name__
                raise
            finally:
                record_provider_usage(
                    self,
                    model=model,
                    usage=usage,
                    status=status,
                    started_at=started_at,
                    duration_ms=int((time.perf_counter() - started_clock) * 1000),
                    error_type=error_type,
                    session_id=metadata.get("session_id"),
                    rate_limits=metadata.get("rate_limits"),
                )


def get_provider(
    provider: str | None = None, *, role: str | None = None
) -> LLMProvider:
    selected = selected_provider(provider, role=role)
    with role_model_environment(selected, role):
        if selected == "openai":
            return OpenAIProvider()
        if selected == "anthropic":
            return AnthropicProvider()
        if selected == "ollama":
            return OllamaProvider()
        if selected == "codex":
            return CodexProvider()
    raise LLMConfigurationError(
        "LLM provider must be one of: openai, anthropic, ollama, codex"
    )


def provider_status(
    provider: str | None = None, *, role: str | None = None
) -> dict[str, Any]:
    selected = selected_provider(provider, role=role)
    with role_model_environment(selected, role):
        return _provider_status_for_selected(selected)


def _provider_status_for_selected(selected: str) -> dict[str, Any]:
    if selected not in VALID_PROVIDERS:
        return ProviderStatus(
            provider=selected or "unset",
            model=None,
            configured=False,
            missing=["valid provider"],
        ).__dict__
    if selected == "openai":
        missing = [key for key in ["OPENAI_API_KEY"] if not os.environ.get(key)]
        models = model_candidates(
            "PKM_BRAIN_OPENAI_MODEL",
            OPENAI_DEFAULT_MODEL,
            "PKM_BRAIN_OPENAI_MODEL_FALLBACKS",
            OPENAI_DEFAULT_FALLBACK_MODELS,
        )
        return ProviderStatus(
            provider="openai",
            model=models[0],
            configured=not missing,
            missing=missing,
            base_url=os.environ.get(
                "PKM_BRAIN_OPENAI_BASE_URL", "https://api.openai.com/v1"
            ),
            fallback_models=models[1:],
            cost_source="OpenAI API billing; not covered by ChatGPT subscription usage",
        ).__dict__
    if selected == "anthropic":
        missing = [key for key in ["ANTHROPIC_API_KEY"] if not os.environ.get(key)]
        models = model_candidates(
            "PKM_BRAIN_ANTHROPIC_MODEL",
            ANTHROPIC_DEFAULT_MODEL,
            "PKM_BRAIN_ANTHROPIC_MODEL_FALLBACKS",
            ANTHROPIC_DEFAULT_FALLBACK_MODELS,
        )
        return ProviderStatus(
            provider="anthropic",
            model=models[0],
            configured=not missing,
            missing=missing,
            base_url=os.environ.get(
                "PKM_BRAIN_ANTHROPIC_BASE_URL", "https://api.anthropic.com"
            ),
            fallback_models=models[1:],
            cost_source="Anthropic API billing",
        ).__dict__
    if selected == "codex":
        binary = os.environ.get("PKM_BRAIN_CODEX_BIN") or shutil.which("codex")
        missing = codex_missing_configuration(binary)
        models = model_candidates(
            "PKM_BRAIN_CODEX_MODEL",
            CODEX_DEFAULT_MODEL,
            "PKM_BRAIN_CODEX_MODEL_FALLBACKS",
            CODEX_DEFAULT_FALLBACK_MODELS,
        )
        return ProviderStatus(
            provider="codex",
            model=models[0],
            configured=not missing,
            missing=missing,
            base_url=None,
            fallback_models=models[1:],
            reasoning_effort=normalized_reasoning_effort(
                os.environ.get("PKM_BRAIN_CODEX_REASONING_EFFORT")
            ),
            cost_source="Codex CLI account; uses ChatGPT plan usage when Codex CLI is signed in with ChatGPT",
        ).__dict__
    models = model_candidates(
        "PKM_BRAIN_OLLAMA_MODEL", "", "PKM_BRAIN_OLLAMA_MODEL_FALLBACKS", ()
    )
    missing = [] if models else ["PKM_BRAIN_OLLAMA_MODEL"]
    return ProviderStatus(
        provider="ollama",
        model=models[0] if models else None,
        configured=not missing,
        missing=missing,
        base_url=os.environ.get("PKM_BRAIN_OLLAMA_BASE_URL", "http://localhost:11434"),
        fallback_models=models[1:],
        cost_source="Local Ollama runtime",
    ).__dict__


def selected_provider(provider: str | None = None, *, role: str | None = None) -> str:
    role_provider = None
    if role:
        role_provider = os.environ.get(role_env(role, "PROVIDER"))
    return (
        (
            provider
            or role_provider
            or os.environ.get("PKM_BRAIN_LLM_PROVIDER")
            or DEFAULT_LLM_PROVIDER
        )
        .strip()
        .lower()
    )


def cos_llm_config_path(paths: "BrainPaths") -> Path:
    return paths.config_local / COS_LLM_CONFIG_FILENAME


def load_cos_llm_config(paths: "BrainPaths") -> dict[str, Any]:
    config_path = cos_llm_config_path(paths)
    if not config_path.exists():
        return {}
    try:
        parsed = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise LLMConfigurationError(f"{config_path} is not valid YAML: {exc}") from exc
    if not isinstance(parsed, dict):
        raise LLMConfigurationError(f"{config_path} must contain a YAML mapping")
    return parsed


def cos_rebuild_allows_critic_auditor_model_overlap(paths: "BrainPaths") -> bool:
    return cos_rebuild_boolean_flag(paths, "allow_critic_auditor_model_overlap")


def cos_rebuild_allows_critic_proposer_model_overlap(paths: "BrainPaths") -> bool:
    return cos_rebuild_boolean_flag(paths, "allow_critic_proposer_model_overlap")


def cos_rebuild_boolean_flag(paths: "BrainPaths", key: str) -> bool:
    config = load_cos_llm_config(paths)
    rebuild_config = config.get("rebuild")
    if rebuild_config is None:
        return False
    if not isinstance(rebuild_config, dict):
        raise LLMConfigurationError(
            "cos_llm.yaml rebuild configuration must be a YAML mapping"
        )
    value = rebuild_config.get(key)
    if value is None:
        return False
    if not isinstance(value, bool):
        raise LLMConfigurationError(
            f"cos_llm.yaml rebuild.{key} must be true or false"
        )
    return value


def resolve_cos_role_selection(
    paths: "BrainPaths",
    role: str,
    *,
    provider: str | None = None,
) -> CosRoleSelection:
    normalized_role = normalize_llm_role(role)
    config = load_cos_llm_config(paths)
    default_config = normalize_cos_llm_role_config(config.get("default"))
    role_config = cos_role_config_from_mapping(config, normalized_role)
    provider_choice = first_configured_value(
        (provider, "argument"),
        (
            os.environ.get(role_env(normalized_role, "PROVIDER")),
            f"env:{role_env(normalized_role, 'PROVIDER')}",
        ),
        (role_config.get("provider"), f"config:roles.{normalized_role}.provider"),
        (os.environ.get("PKM_BRAIN_LLM_PROVIDER"), "env:PKM_BRAIN_LLM_PROVIDER"),
        (default_config.get("provider"), "config:default.provider"),
    )
    model_choice = first_configured_value(
        (
            os.environ.get(role_env(normalized_role, "MODEL")),
            f"env:{role_env(normalized_role, 'MODEL')}",
        ),
        (role_config.get("model"), f"config:roles.{normalized_role}.model"),
        (default_config.get("model"), "config:default.model"),
    )
    fallback_choice = first_model_fallbacks_value(
        (
            os.environ.get(role_env(normalized_role, "MODEL_FALLBACKS")),
            f"env:{role_env(normalized_role, 'MODEL_FALLBACKS')}",
        ),
        (
            role_config.get("model_fallbacks"),
            f"config:roles.{normalized_role}.model_fallbacks",
        ),
        (default_config.get("model_fallbacks"), "config:default.model_fallbacks"),
    )
    effort_choice = first_configured_value(
        (
            os.environ.get(role_env(normalized_role, "REASONING_EFFORT")),
            f"env:{role_env(normalized_role, 'REASONING_EFFORT')}",
        ),
        (
            role_config.get("reasoning_effort"),
            f"config:roles.{normalized_role}.reasoning_effort",
        ),
        (default_config.get("reasoning_effort"), "config:default.reasoning_effort"),
    )
    base_url_choice = first_configured_value(
        (
            os.environ.get(role_env(normalized_role, "BASE_URL")),
            f"env:{role_env(normalized_role, 'BASE_URL')}",
        ),
        (role_config.get("base_url"), f"config:roles.{normalized_role}.base_url"),
        (default_config.get("base_url"), "config:default.base_url"),
    )
    provider_name = (
        normalize_provider_name(provider_choice[0])
        if provider_choice[0] is not None
        else None
    )
    return CosRoleSelection(
        role=normalized_role,
        provider=provider_name,
        provider_source=provider_choice[1],
        model=str(model_choice[0]).strip() if model_choice[0] is not None else None,
        model_source=model_choice[1],
        model_fallbacks=fallback_choice[0],
        fallback_source=fallback_choice[1],
        reasoning_effort=normalized_reasoning_effort(effort_choice[0]),
        reasoning_effort_source=effort_choice[1],
        base_url=str(base_url_choice[0]).strip()
        if base_url_choice[0] is not None
        else None,
        base_url_source=base_url_choice[1],
    )


def cos_role_config_from_mapping(config: dict[str, Any], role: str) -> dict[str, Any]:
    roles = config.get("roles")
    role_config: dict[str, Any] = {}
    if isinstance(roles, dict):
        role_config.update(normalize_cos_llm_role_config(roles.get(role)))
    role_config.update(normalize_cos_llm_role_config(config.get(role)))
    return role_config


def normalize_cos_llm_role_config(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    output: dict[str, Any] = {}
    for key in ("provider", "model", "model_fallbacks", "base_url", "reasoning_effort"):
        if key in value and value[key] is not None:
            output[key] = value[key]
    if "fallback_models" in value and "model_fallbacks" not in output:
        output["model_fallbacks"] = value["fallback_models"]
    return output


def first_configured_value(
    *candidates: tuple[Any, str],
) -> tuple[Any | None, str | None]:
    for value, source in candidates:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value, source
    return None, None


def first_model_fallbacks_value(
    *candidates: tuple[Any, str],
) -> tuple[list[str] | None, str | None]:
    for value, source in candidates:
        parsed = parse_model_fallbacks_value(value)
        if parsed is not None:
            return parsed, source
    return None, None


def parse_model_fallbacks_value(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        parsed = split_model_list(value)
        return parsed if parsed else None
    if isinstance(value, list):
        parsed = [str(item).strip() for item in value if str(item).strip()]
        return parsed if parsed else None
    return None


def normalize_provider_name(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    return normalized or None


def normalized_reasoning_effort(value: Any) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if not normalized:
        return None
    allowed = {"minimal", "low", "medium", "high", "xhigh"}
    if normalized not in allowed:
        raise LLMConfigurationError(
            f"reasoning_effort must be one of: {', '.join(sorted(allowed))}"
        )
    return normalized


def codex_reasoning_effort_args(reasoning_effort: str | None) -> list[str]:
    if not reasoning_effort:
        return []
    return ["-c", f'model_reasoning_effort="{reasoning_effort}"']


def normalize_llm_role(role: str) -> str:
    normalized = role.strip().lower()
    if normalized not in LLM_ROLES:
        raise LLMConfigurationError(f"unknown LLM role: {role}")
    return normalized


@contextmanager
def cos_role_provider_environment(selection: CosRoleSelection) -> Any:
    if selection.provider is None:
        yield
        return
    env_map = provider_model_env_names(selection.provider)
    overrides: dict[str, str] = {}
    if selection.model is not None and env_map.get("model"):
        overrides[env_map["model"]] = selection.model
    if selection.model_fallbacks is not None and env_map.get("fallbacks"):
        overrides[env_map["fallbacks"]] = ",".join(selection.model_fallbacks)
    if selection.reasoning_effort is not None and env_map.get("reasoning_effort"):
        overrides[env_map["reasoning_effort"]] = selection.reasoning_effort
    if selection.base_url is not None and env_map.get("base_url"):
        overrides[env_map["base_url"]] = selection.base_url
    previous = {key: os.environ.get(key) for key in overrides}
    try:
        for key, value in overrides.items():
            os.environ[key] = value
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def cos_role_provider_configured(
    paths: "BrainPaths",
    role: str,
    *,
    llm_provider: LLMProvider | None = None,
    provider: str | None = None,
) -> bool:
    if llm_provider is not None:
        return True
    return (
        resolve_cos_role_selection(paths, role, provider=provider).provider is not None
    )


def get_cos_role_provider(
    paths: "BrainPaths",
    role: str,
    *,
    provider: str | None = None,
    llm_provider: LLMProvider | None = None,
    usage_cycle_id: str | None = None,
    usage_run_id: str | None = None,
    usage_stage: str | None = None,
) -> LLMProvider | None:
    if llm_provider is not None:
        return configure_provider_usage(
            llm_provider,
            paths,
            normalize_llm_role(role),
            cycle_id=usage_cycle_id,
            run_id=usage_run_id,
            stage=usage_stage,
        )
    with _COS_ROLE_PROVIDER_LOCK:
        selection = resolve_cos_role_selection(paths, role, provider=provider)
        if selection.provider is None:
            return None
        with cos_role_provider_environment(selection):
            active_provider = get_provider(selection.provider, role=role)
    return configure_provider_usage(
        active_provider,
        paths,
        selection.role,
        cycle_id=usage_cycle_id,
        run_id=usage_run_id,
        stage=usage_stage,
    )


def get_cos_action_provider(
    paths: "BrainPaths",
    role: str,
    action: dict[str, Any],
    *,
    provider: str | None = None,
    llm_provider: LLMProvider | None = None,
    stage: str | None = None,
) -> LLMProvider | None:
    run_id = str(action.get("run_id") or "").strip() or None
    return get_cos_role_provider(
        paths,
        role,
        provider=provider,
        llm_provider=llm_provider,
        usage_cycle_id=run_id,
        usage_run_id=run_id,
        usage_stage=stage,
    )


def cos_provider_status(paths: "BrainPaths") -> dict[str, Any]:
    role_rows = [cos_role_provider_status(paths, role) for role in LLM_ROLE_ORDER]
    warnings = cos_provider_separation_warnings(role_rows)
    return {
        "config_path": str(cos_llm_config_path(paths)),
        "config_exists": cos_llm_config_path(paths).exists(),
        "roles": role_rows,
        "warnings": warnings,
        "errors": [],
    }


def cos_role_provider_status(paths: "BrainPaths", role: str) -> dict[str, Any]:
    selection = resolve_cos_role_selection(paths, role)
    if selection.provider is None:
        return {
            "role": selection.role,
            "role_configured": False,
            "configured": False,
            "provider": None,
            "provider_source": None,
            "model": None,
            "base_url": None,
            "fallback_models": [],
            "reasoning_effort": None,
            "missing": [],
            "cost_source": None,
            "warnings": [],
        }
    with cos_role_provider_environment(selection):
        status = _provider_status_for_selected(selection.provider)
    return {
        "role": selection.role,
        "role_configured": True,
        "configured": bool(status.get("configured")),
        "provider": status.get("provider"),
        "provider_source": selection.provider_source,
        "model": status.get("model"),
        "model_source": selection.model_source,
        "base_url": status.get("base_url"),
        "base_url_source": selection.base_url_source,
        "fallback_models": status.get("fallback_models") or [],
        "fallback_source": selection.fallback_source,
        "reasoning_effort": status.get("reasoning_effort"),
        "reasoning_effort_source": selection.reasoning_effort_source,
        "missing": status.get("missing") or [],
        "cost_source": status.get("cost_source"),
        "warnings": [],
    }


def cos_provider_separation_warnings(role_rows: list[dict[str, Any]]) -> list[str]:
    by_role = {str(row["role"]): row for row in role_rows}
    warnings: list[str] = []
    for reviewer in COS_REVIEWER_ROLES:
        reviewer_row = by_role.get(reviewer)
        if not row_has_provider_model(reviewer_row):
            continue
        for proposer in COS_PROPOSER_ROLES:
            proposer_row = by_role.get(proposer)
            if not row_has_provider_model(proposer_row):
                continue
            if same_provider_model(reviewer_row, proposer_row):
                warnings.append(
                    f"{reviewer} uses the same provider/model as {proposer}; "
                    "separation of duties is not independent"
                )
    critic = by_role.get("critic")
    auditor = by_role.get("auditor")
    if (
        row_has_provider_model(critic)
        and row_has_provider_model(auditor)
        and same_provider_model(critic, auditor)
    ):
        warnings.append(CRITIC_AUDITOR_MODEL_OVERLAP_WARNING)
    for warning in warnings:
        for row in role_rows:
            if str(row["role"]) in warning:
                row.setdefault("warnings", []).append(warning)
    return warnings


def row_has_provider_model(row: dict[str, Any] | None) -> bool:
    return bool(
        row and row.get("role_configured") and row.get("provider") and row.get("model")
    )


def same_provider_model(
    left: dict[str, Any] | None, right: dict[str, Any] | None
) -> bool:
    return bool(
        left
        and right
        and left.get("provider") == right.get("provider")
        and left.get("model") == right.get("model")
    )


def role_env(role: str, suffix: str) -> str:
    normalized = normalize_llm_role(role).upper()
    return f"PKM_BRAIN_LLM_{normalized}_{suffix}"


@contextmanager
def role_model_environment(provider: str, role: str | None) -> Any:
    if not role:
        yield
        return
    model = os.environ.get(role_env(role, "MODEL"))
    fallbacks = os.environ.get(role_env(role, "MODEL_FALLBACKS"))
    reasoning_effort = os.environ.get(role_env(role, "REASONING_EFFORT"))
    base_url = os.environ.get(role_env(role, "BASE_URL"))
    env_map = provider_model_env_names(provider)
    overrides: dict[str, str] = {}
    if model is not None and env_map.get("model"):
        overrides[env_map["model"]] = model
    if fallbacks is not None and env_map.get("fallbacks"):
        overrides[env_map["fallbacks"]] = fallbacks
    if reasoning_effort is not None and env_map.get("reasoning_effort"):
        normalized_effort = normalized_reasoning_effort(reasoning_effort)
        if normalized_effort:
            overrides[env_map["reasoning_effort"]] = normalized_effort
    if base_url is not None and env_map.get("base_url"):
        overrides[env_map["base_url"]] = base_url
    previous = {key: os.environ.get(key) for key in overrides}
    try:
        for key, value in overrides.items():
            os.environ[key] = value
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def provider_model_env_names(provider: str) -> dict[str, str]:
    if provider == "openai":
        return {
            "model": "PKM_BRAIN_OPENAI_MODEL",
            "fallbacks": "PKM_BRAIN_OPENAI_MODEL_FALLBACKS",
            "base_url": "PKM_BRAIN_OPENAI_BASE_URL",
        }
    if provider == "anthropic":
        return {
            "model": "PKM_BRAIN_ANTHROPIC_MODEL",
            "fallbacks": "PKM_BRAIN_ANTHROPIC_MODEL_FALLBACKS",
            "base_url": "PKM_BRAIN_ANTHROPIC_BASE_URL",
        }
    if provider == "ollama":
        return {
            "model": "PKM_BRAIN_OLLAMA_MODEL",
            "fallbacks": "PKM_BRAIN_OLLAMA_MODEL_FALLBACKS",
            "base_url": "PKM_BRAIN_OLLAMA_BASE_URL",
        }
    if provider == "codex":
        return {
            "model": "PKM_BRAIN_CODEX_MODEL",
            "fallbacks": "PKM_BRAIN_CODEX_MODEL_FALLBACKS",
            "reasoning_effort": "PKM_BRAIN_CODEX_REASONING_EFFORT",
        }
    return {}


def complete_json(
    prompt: str,
    *,
    schema: dict[str, Any] | None = None,
    provider: str | None = None,
    role: str | None = None,
    llm_provider: LLMProvider | None = None,
    paths: "BrainPaths | None" = None,
    max_attempts: int = 2,
    usage_cycle_id: str | None = None,
    usage_run_id: str | None = None,
    usage_stage: str | None = None,
) -> dict[str, Any]:
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    if llm_provider is not None:
        active_provider = llm_provider
    elif role is not None:
        if paths is None:
            from .paths import BrainPaths

            paths = BrainPaths.from_value(None)
        active_provider = get_cos_role_provider(
            paths,
            role,
            provider=provider,
            usage_cycle_id=usage_cycle_id,
            usage_run_id=usage_run_id,
            usage_stage=usage_stage,
        )
        if active_provider is None:
            raise LLMConfigurationError(
                f"No CoS LLM provider configured for role: {role}"
            )
    else:
        active_provider = get_provider(provider, role=role)
    current_prompt = json_prompt(prompt, schema=schema)
    last_response = ""
    last_error: Exception | None = None
    for attempt in range(max_attempts):
        last_response = active_provider.complete(current_prompt)
        try:
            parsed = parse_json_object(last_response)
            if schema is not None:
                validate_minimal_schema(parsed, schema)
            return parsed
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            last_error = exc
            if attempt == max_attempts - 1:
                break
            current_prompt = repair_json_prompt(
                prompt, last_response, exc, schema=schema
            )
    raise LLMProviderError(
        f"LLM did not return valid JSON after {max_attempts} attempts: {last_error}"
    ) from last_error


def model_candidates(
    model_env: str,
    default_model: str,
    fallback_env: str,
    default_fallbacks: tuple[str, ...],
) -> list[str]:
    primary = (os.environ.get(model_env) or default_model).strip()
    fallback_value = os.environ.get(fallback_env)
    fallback_models = (
        list(default_fallbacks)
        if fallback_value is None
        else split_model_list(fallback_value)
    )
    return dedupe_models([primary, *fallback_models])


def json_prompt(prompt: str, *, schema: dict[str, Any] | None = None) -> str:
    schema_text = (
        f"\nRequired JSON shape:\n{json.dumps(schema, sort_keys=True)}\n"
        if schema
        else ""
    )
    return (
        "Return exactly one valid JSON object. Do not wrap it in Markdown."
        f"{schema_text}\nPrompt:\n{prompt}"
    )


def repair_json_prompt(
    original_prompt: str,
    invalid_response: str,
    error: Exception,
    *,
    schema: dict[str, Any] | None = None,
) -> str:
    clipped = invalid_response[:4000]
    schema_text = (
        f"\nRequired JSON shape:\n{json.dumps(schema, sort_keys=True)}\n"
        if schema
        else ""
    )
    return (
        "Repair the previous response so it is exactly one valid JSON object. "
        "Return only the repaired JSON object. Preserve the required top-level keys; "
        "when the schema requires an array and there are no items, return an empty array "
        "rather than omitting the key.\n"
        f"Parse error: {error}\n"
        f"{schema_text}"
        # Extraction prompts put the source window after the instructions. A
        # head-only clip removed all source evidence, making a valid retry
        # impossible for large transcripts.
        f"Original prompt:\n{original_prompt}\n"
        f"Invalid response:\n{clipped}"
    )


def parse_json_object(response: str) -> dict[str, Any]:
    text = strip_json_markdown(response.strip())
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = json.loads(extract_json_object_text(text))
    if not isinstance(parsed, dict):
        raise ValueError("expected a JSON object")
    return parsed


def strip_json_markdown(text: str) -> str:
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return "\n".join(lines).strip()
    return text


def extract_json_object_text(text: str) -> str:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise json.JSONDecodeError("no JSON object found", text, 0)
    return text[start : end + 1]


def validate_minimal_schema(value: dict[str, Any], schema: dict[str, Any]) -> None:
    required = schema.get("required")
    if isinstance(required, list):
        missing = [str(key) for key in required if str(key) not in value]
        if missing:
            raise ValueError(f"JSON object missing required keys: {', '.join(missing)}")
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return
    for key, property_schema in properties.items():
        if key not in value or not isinstance(property_schema, dict):
            continue
        expected = property_schema.get("type")
        if expected is None or json_value_matches_type(value[key], expected):
            continue
        expected_text = (
            " or ".join(str(item) for item in expected)
            if isinstance(expected, list)
            else str(expected)
        )
        raise ValueError(f"JSON property {key!r} must have type {expected_text}")


def json_value_matches_type(value: Any, expected: Any) -> bool:
    expected_types = expected if isinstance(expected, list) else [expected]
    for expected_type in expected_types:
        if expected_type == "array" and isinstance(value, list):
            return True
        if expected_type == "object" and isinstance(value, dict):
            return True
        if expected_type == "string" and isinstance(value, str):
            return True
        if expected_type == "boolean" and isinstance(value, bool):
            return True
        if expected_type == "integer" and isinstance(value, int) and not isinstance(value, bool):
            return True
        if expected_type == "number" and isinstance(value, (int, float)) and not isinstance(value, bool):
            return True
        if expected_type == "null" and value is None:
            return True
    return False


def split_model_list(value: str) -> list[str]:
    return [
        item.strip() for item in value.replace("\n", ",").split(",") if item.strip()
    ]


def dedupe_models(models: list[str]) -> list[str]:
    seen = set()
    result = []
    for model in models:
        if not model or model in seen:
            continue
        seen.add(model)
        result.append(model)
    return result


def complete_with_model_fallbacks(
    provider: str, models: list[str], complete_once: Callable[[str], str]
) -> str:
    model_errors = []
    for index, model in enumerate(models):
        try:
            return complete_once(model)
        except LLMProviderError as exc:
            if not is_model_selection_error(str(exc)):
                raise
            model_errors.append(f"{model}: {exc}")
            if index < len(models) - 1:
                continue
            detail = "; ".join(model_errors)
            raise LLMProviderError(
                f"{provider} provider failed for all configured models: {detail}"
            ) from exc
    raise LLMProviderError(f"{provider} provider has no configured models")


def is_model_selection_error(message: str) -> bool:
    lower = message.lower()
    non_model_markers = (
        "api key",
        "unauthorized",
        "authentication",
        "quota",
        "rate limit",
        "billing",
        "credit",
    )
    if any(marker in lower for marker in non_model_markers):
        return False
    model_markers = (
        "model",
        "does not exist",
        "not found",
        "unsupported",
        "unknown",
        "invalid value",
        "invalid_request_error",
        "do not have access",
    )
    return "model" in lower and any(marker in lower for marker in model_markers)


def post_json(
    url: str, payload: dict[str, Any], headers: dict[str, str]
) -> dict[str, Any]:
    encoded = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=encoded,
        headers={
            "Content-Type": "application/json",
            **headers,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise LLMProviderError(f"{url} returned HTTP {exc.code}: {body}") from exc
    except urllib.error.URLError as exc:
        raise LLMProviderError(f"Could not reach {url}: {exc}") from exc


def codex_prompt(prompt: str) -> str:
    return (
        "You are running as the Codex-backed LLM provider for pkm-brain.\n"
        "Return only valid JSON. Do not edit files. Do not wrap the JSON in Markdown.\n"
        "Use the provided prompt as the complete task.\n\n"
        f"{prompt}"
    )


def codex_missing_configuration(binary: str | None) -> list[str]:
    if not binary:
        return ["codex"]
    try:
        status = subprocess.run(
            [binary, "login", "status"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return ["codex login"]
    output = f"{status.stdout}\n{status.stderr}"
    if status.returncode != 0 or "Logged in" not in output:
        return ["codex login"]
    return []
