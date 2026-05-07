from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol


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


class OpenAIProvider:
    name = "openai"

    def __init__(self) -> None:
        self.api_key = os.environ.get("OPENAI_API_KEY")
        self.model = os.environ.get("PKM_BRAIN_OPENAI_MODEL", "gpt-5.5")
        self.base_url = os.environ.get("PKM_BRAIN_OPENAI_BASE_URL", "https://api.openai.com/v1")
        if not self.api_key:
            raise LLMConfigurationError("OPENAI_API_KEY is required for OpenAI provider")

    def complete(self, prompt: str) -> str:
        payload = {
            "model": self.model,
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
            return str(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMProviderError(f"Unexpected OpenAI response shape: {data}") from exc


class AnthropicProvider:
    name = "anthropic"

    def __init__(self) -> None:
        self.api_key = os.environ.get("ANTHROPIC_API_KEY")
        self.model = os.environ.get("PKM_BRAIN_ANTHROPIC_MODEL", "claude-sonnet-4-5")
        self.base_url = os.environ.get("PKM_BRAIN_ANTHROPIC_BASE_URL", "https://api.anthropic.com")
        if not self.api_key:
            raise LLMConfigurationError("ANTHROPIC_API_KEY is required for Anthropic provider")

    def complete(self, prompt: str) -> str:
        payload = {
            "model": self.model,
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
            return "".join(part.get("text", "") for part in data["content"] if part.get("type") == "text")
        except (KeyError, TypeError) as exc:
            raise LLMProviderError(f"Unexpected Anthropic response shape: {data}") from exc


class OllamaProvider:
    name = "ollama"

    def __init__(self) -> None:
        self.model = os.environ.get("PKM_BRAIN_OLLAMA_MODEL")
        self.base_url = os.environ.get("PKM_BRAIN_OLLAMA_BASE_URL", "http://localhost:11434")
        if not self.model:
            raise LLMConfigurationError("PKM_BRAIN_OLLAMA_MODEL is required for Ollama provider")

    def complete(self, prompt: str) -> str:
        payload = {
            "model": self.model,
            "stream": False,
            "messages": [
                {"role": "system", "content": "Return only valid JSON. Do not wrap it in Markdown."},
                {"role": "user", "content": prompt},
            ],
        }
        data = post_json(f"{self.base_url.rstrip('/')}/api/chat", payload, {})
        try:
            return str(data["message"]["content"])
        except (KeyError, TypeError) as exc:
            raise LLMProviderError(f"Unexpected Ollama response shape: {data}") from exc


class CodexProvider:
    name = "codex"

    def __init__(self) -> None:
        self.model = os.environ.get("PKM_BRAIN_CODEX_MODEL", "gpt-5.5")
        self.binary = os.environ.get("PKM_BRAIN_CODEX_BIN") or shutil.which("codex")
        self.cwd = Path(os.environ.get("PKM_BRAIN_CODEX_CWD", Path.cwd())).expanduser()
        self.timeout = int(os.environ.get("PKM_BRAIN_CODEX_TIMEOUT_SECONDS", "900"))
        if not self.binary:
            raise LLMConfigurationError("codex executable was not found; install/login to Codex CLI first")
        missing = codex_missing_configuration(self.binary)
        if missing:
            raise LLMConfigurationError(", ".join(missing))

    def complete(self, prompt: str) -> str:
        with tempfile.TemporaryDirectory(prefix="pkm-brain-codex-") as temp_dir:
            output_path = Path(temp_dir) / "last-message.txt"
            command = [
                self.binary,
                "exec",
                "--sandbox",
                "read-only",
                "--ask-for-approval",
                "never",
                "--cd",
                str(self.cwd),
                "--model",
                self.model,
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
            return output


def get_provider(provider: str | None = None) -> LLMProvider:
    selected = (provider or os.environ.get("PKM_BRAIN_LLM_PROVIDER") or "").strip().lower()
    if selected == "openai":
        return OpenAIProvider()
    if selected == "anthropic":
        return AnthropicProvider()
    if selected == "ollama":
        return OllamaProvider()
    if selected == "codex":
        return CodexProvider()
    raise LLMConfigurationError("PKM_BRAIN_LLM_PROVIDER must be one of: openai, anthropic, ollama, codex")


def provider_status(provider: str | None = None) -> dict[str, Any]:
    selected = (provider or os.environ.get("PKM_BRAIN_LLM_PROVIDER") or "").strip().lower()
    if selected not in {"openai", "anthropic", "ollama", "codex"}:
        return ProviderStatus(provider=selected or "unset", model=None, configured=False, missing=["PKM_BRAIN_LLM_PROVIDER"]).__dict__
    if selected == "openai":
        missing = [key for key in ["OPENAI_API_KEY"] if not os.environ.get(key)]
        return ProviderStatus(
            provider="openai",
            model=os.environ.get("PKM_BRAIN_OPENAI_MODEL", "gpt-5.5"),
            configured=not missing,
            missing=missing,
            base_url=os.environ.get("PKM_BRAIN_OPENAI_BASE_URL", "https://api.openai.com/v1"),
        ).__dict__
    if selected == "anthropic":
        missing = [key for key in ["ANTHROPIC_API_KEY"] if not os.environ.get(key)]
        return ProviderStatus(
            provider="anthropic",
            model=os.environ.get("PKM_BRAIN_ANTHROPIC_MODEL", "claude-sonnet-4-5"),
            configured=not missing,
            missing=missing,
            base_url=os.environ.get("PKM_BRAIN_ANTHROPIC_BASE_URL", "https://api.anthropic.com"),
        ).__dict__
    if selected == "codex":
        binary = os.environ.get("PKM_BRAIN_CODEX_BIN") or shutil.which("codex")
        missing = codex_missing_configuration(binary)
        return ProviderStatus(
            provider="codex",
            model=os.environ.get("PKM_BRAIN_CODEX_MODEL", "gpt-5.5"),
            configured=not missing,
            missing=missing,
            base_url=None,
        ).__dict__
    missing = [key for key in ["PKM_BRAIN_OLLAMA_MODEL"] if not os.environ.get(key)]
    return ProviderStatus(
        provider="ollama",
        model=os.environ.get("PKM_BRAIN_OLLAMA_MODEL"),
        configured=not missing,
        missing=missing,
        base_url=os.environ.get("PKM_BRAIN_OLLAMA_BASE_URL", "http://localhost:11434"),
    ).__dict__


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
