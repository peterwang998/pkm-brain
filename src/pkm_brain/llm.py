from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
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


def get_provider(provider: str | None = None) -> LLMProvider:
    selected = selected_provider(provider)
    if selected == "openai":
        return OpenAIProvider()
    if selected == "anthropic":
        return AnthropicProvider()
    if selected == "ollama":
        return OllamaProvider()
    if selected == "codex":
        return CodexProvider()
    raise LLMConfigurationError("LLM provider must be one of: openai, anthropic, ollama, codex")


def provider_status(provider: str | None = None) -> dict[str, Any]:
    selected = selected_provider(provider)
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


def selected_provider(provider: str | None = None) -> str:
    return (provider or os.environ.get("PKM_BRAIN_LLM_PROVIDER") or DEFAULT_LLM_PROVIDER).strip().lower()


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
