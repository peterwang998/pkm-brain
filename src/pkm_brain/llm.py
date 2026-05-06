from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
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


def get_provider(provider: str | None = None) -> LLMProvider:
    selected = (provider or os.environ.get("PKM_BRAIN_LLM_PROVIDER") or "").strip().lower()
    if selected == "openai":
        return OpenAIProvider()
    if selected == "anthropic":
        return AnthropicProvider()
    if selected == "ollama":
        return OllamaProvider()
    raise LLMConfigurationError("PKM_BRAIN_LLM_PROVIDER must be one of: openai, anthropic, ollama")


def provider_status(provider: str | None = None) -> dict[str, Any]:
    selected = (provider or os.environ.get("PKM_BRAIN_LLM_PROVIDER") or "").strip().lower()
    if selected not in {"openai", "anthropic", "ollama"}:
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

