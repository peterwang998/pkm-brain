from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

DEFAULT_LLM_PROVIDER = "codex"
OPENAI_DEFAULT_MODEL = "gpt-5.5"
OPENAI_DEFAULT_FALLBACK_MODELS = ("gpt-5.4", "gpt-5.4-mini", "gpt-5")
ANTHROPIC_DEFAULT_MODEL = "claude-sonnet-4-5"
ANTHROPIC_DEFAULT_FALLBACK_MODELS: tuple[str, ...] = ()
CODEX_DEFAULT_MODEL = "gpt-5.5"
CODEX_DEFAULT_FALLBACK_MODELS = ("gpt-5.4", "gpt-5.3-codex", "gpt-5.2", "gpt-5")
LLM_ROLES = {"extractor", "gardener", "resolver", "critic", "synthesizer", "auditor"}


class LLMConfigurationError(RuntimeError):
    pass


class LLMProviderError(RuntimeError):
    pass


class LLMProvider(Protocol):
    name: str
    model: str

    def complete(self, prompt: str) -> str:
        ...


@dataclass(frozen=True)
class ProviderStatus:
    provider: str
    model: str | None
    configured: bool
    missing: list[str]
    base_url: str | None = None
    fallback_models: list[str] | None = None
    cost_source: str | None = None


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
        self.base_url = os.environ.get("PKM_BRAIN_OPENAI_BASE_URL", "https://api.openai.com/v1")
        if not self.api_key:
            raise LLMConfigurationError("OPENAI_API_KEY is required for OpenAI provider")

    def complete(self, prompt: str) -> str:
        def run_once(model: str) -> str:
            payload = {
                "model": model,
                "messages": [
                    {"role": "system", "content": "Return only valid JSON. Do not wrap it in Markdown."},
                    {"role": "user", "content": prompt},
                ],
            }
            data = post_json(
                f"{self.base_url.rstrip('/')}/chat/completions",
                payload,
                {"Authorization": f"Bearer {self.api_key}"},
            )
            try:
                content = str(data["choices"][0]["message"]["content"])
            except (KeyError, IndexError, TypeError) as exc:
                raise LLMProviderError(f"Unexpected OpenAI response shape: {data}") from exc
            self.model = model
            return content

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
        self.base_url = os.environ.get("PKM_BRAIN_ANTHROPIC_BASE_URL", "https://api.anthropic.com")
        if not self.api_key:
            raise LLMConfigurationError("ANTHROPIC_API_KEY is required for Anthropic provider")

    def complete(self, prompt: str) -> str:
        def run_once(model: str) -> str:
            payload = {
                "model": model,
                "max_tokens": 4096,
                "system": "Return only valid JSON. Do not wrap it in Markdown.",
                "messages": [{"role": "user", "content": prompt}],
            }
            data = post_json(
                f"{self.base_url.rstrip('/')}/v1/messages",
                payload,
                {
                    "x-api-key": self.api_key,
                    "anthropic-version": "2023-06-01",
                },
            )
            try:
                content = "".join(part.get("text", "") for part in data["content"] if part.get("type") == "text")
            except (KeyError, TypeError) as exc:
                raise LLMProviderError(f"Unexpected Anthropic response shape: {data}") from exc
            self.model = model
            return content

        return complete_with_model_fallbacks("Anthropic", self.models, run_once)


class OllamaProvider:
    name = "ollama"

    def __init__(self) -> None:
        self.model = os.environ.get("PKM_BRAIN_OLLAMA_MODEL")
        self.models = model_candidates("PKM_BRAIN_OLLAMA_MODEL", "", "PKM_BRAIN_OLLAMA_MODEL_FALLBACKS", ())
        self.base_url = os.environ.get("PKM_BRAIN_OLLAMA_BASE_URL", "http://localhost:11434")
        if not self.models:
            raise LLMConfigurationError("PKM_BRAIN_OLLAMA_MODEL is required for Ollama provider")
        self.model = self.models[0]

    def complete(self, prompt: str) -> str:
        def run_once(model: str) -> str:
            payload = {
                "model": model,
                "stream": False,
                "messages": [
                    {"role": "system", "content": "Return only valid JSON. Do not wrap it in Markdown."},
                    {"role": "user", "content": prompt},
                ],
            }
            data = post_json(f"{self.base_url.rstrip('/')}/api/chat", payload, {})
            try:
                content = str(data["message"]["content"])
            except (KeyError, TypeError) as exc:
                raise LLMProviderError(f"Unexpected Ollama response shape: {data}") from exc
            self.model = model
            return content

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
        if not self.binary:
            raise LLMConfigurationError("codex executable was not found; install/login to Codex CLI first")
        missing = codex_missing_configuration(self.binary)
        if missing:
            raise LLMConfigurationError(", ".join(missing))

    def complete(self, prompt: str) -> str:
        return complete_with_model_fallbacks("Codex", self.models, lambda model: self._complete_once(model, prompt))

    def _complete_once(self, model: str, prompt: str) -> str:
        with tempfile.TemporaryDirectory(prefix="pkm-brain-codex-") as temp_dir:
            output_path = Path(temp_dir) / "last-message.txt"
            command = [
                self.binary,
                "--ask-for-approval",
                "never",
                "exec",
                "--sandbox",
                "read-only",
                "--cd",
                str(self.cwd),
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
                raise LLMProviderError(f"Codex timed out after {self.timeout} seconds") from exc
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
            return output


def get_provider(provider: str | None = None, *, role: str | None = None) -> LLMProvider:
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
    raise LLMConfigurationError("LLM provider must be one of: openai, anthropic, ollama, codex")


def provider_status(provider: str | None = None, *, role: str | None = None) -> dict[str, Any]:
    selected = selected_provider(provider, role=role)
    with role_model_environment(selected, role):
        return _provider_status_for_selected(selected)


def _provider_status_for_selected(selected: str) -> dict[str, Any]:
    if selected not in {"openai", "anthropic", "ollama", "codex"}:
        return ProviderStatus(provider=selected or "unset", model=None, configured=False, missing=["valid provider"]).__dict__
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
            base_url=os.environ.get("PKM_BRAIN_OPENAI_BASE_URL", "https://api.openai.com/v1"),
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
            base_url=os.environ.get("PKM_BRAIN_ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
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
            cost_source="Codex CLI account; uses ChatGPT plan usage when Codex CLI is signed in with ChatGPT",
        ).__dict__
    models = model_candidates("PKM_BRAIN_OLLAMA_MODEL", "", "PKM_BRAIN_OLLAMA_MODEL_FALLBACKS", ())
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
        provider
        or role_provider
        or os.environ.get("PKM_BRAIN_LLM_PROVIDER")
        or DEFAULT_LLM_PROVIDER
    ).strip().lower()


def role_env(role: str, suffix: str) -> str:
    normalized = role.strip().upper()
    if normalized.lower() not in LLM_ROLES:
        raise LLMConfigurationError(f"unknown LLM role: {role}")
    return f"PKM_BRAIN_LLM_{normalized}_{suffix}"


@contextmanager
def role_model_environment(provider: str, role: str | None) -> Any:
    if not role:
        yield
        return
    model = os.environ.get(role_env(role, "MODEL"))
    fallbacks = os.environ.get(role_env(role, "MODEL_FALLBACKS"))
    env_map = provider_model_env_names(provider)
    overrides: dict[str, str] = {}
    if model is not None and env_map.get("model"):
        overrides[env_map["model"]] = model
    if fallbacks is not None and env_map.get("fallbacks"):
        overrides[env_map["fallbacks"]] = fallbacks
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
        return {"model": "PKM_BRAIN_OPENAI_MODEL", "fallbacks": "PKM_BRAIN_OPENAI_MODEL_FALLBACKS"}
    if provider == "anthropic":
        return {"model": "PKM_BRAIN_ANTHROPIC_MODEL", "fallbacks": "PKM_BRAIN_ANTHROPIC_MODEL_FALLBACKS"}
    if provider == "ollama":
        return {"model": "PKM_BRAIN_OLLAMA_MODEL", "fallbacks": "PKM_BRAIN_OLLAMA_MODEL_FALLBACKS"}
    if provider == "codex":
        return {"model": "PKM_BRAIN_CODEX_MODEL", "fallbacks": "PKM_BRAIN_CODEX_MODEL_FALLBACKS"}
    return {}


def complete_json(
    prompt: str,
    *,
    schema: dict[str, Any] | None = None,
    provider: str | None = None,
    role: str | None = None,
    llm_provider: LLMProvider | None = None,
    max_attempts: int = 2,
) -> dict[str, Any]:
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")
    active_provider = llm_provider or get_provider(provider, role=role)
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
            current_prompt = repair_json_prompt(prompt, last_response, exc, schema=schema)
    raise LLMProviderError(f"LLM did not return valid JSON after {max_attempts} attempts: {last_error}") from last_error


def model_candidates(
    model_env: str,
    default_model: str,
    fallback_env: str,
    default_fallbacks: tuple[str, ...],
) -> list[str]:
    primary = (os.environ.get(model_env) or default_model).strip()
    fallback_value = os.environ.get(fallback_env)
    fallback_models = list(default_fallbacks) if fallback_value is None else split_model_list(fallback_value)
    return dedupe_models([primary, *fallback_models])


def json_prompt(prompt: str, *, schema: dict[str, Any] | None = None) -> str:
    schema_text = f"\nRequired JSON shape:\n{json.dumps(schema, sort_keys=True)}\n" if schema else ""
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
    schema_text = f"\nRequired JSON shape:\n{json.dumps(schema, sort_keys=True)}\n" if schema else ""
    return (
        "Repair the previous response so it is exactly one valid JSON object. "
        "Return only the repaired JSON object.\n"
        f"Parse error: {error}\n"
        f"{schema_text}"
        f"Original prompt:\n{original_prompt[:4000]}\n"
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


def split_model_list(value: str) -> list[str]:
    return [item.strip() for item in value.replace("\n", ",").split(",") if item.strip()]


def dedupe_models(models: list[str]) -> list[str]:
    seen = set()
    result = []
    for model in models:
        if not model or model in seen:
            continue
        seen.add(model)
        result.append(model)
    return result


def complete_with_model_fallbacks(provider: str, models: list[str], complete_once: Callable[[str], str]) -> str:
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
            raise LLMProviderError(f"{provider} provider failed for all configured models: {detail}") from exc
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


def post_json(url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
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
