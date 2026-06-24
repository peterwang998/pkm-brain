from __future__ import annotations

from pathlib import Path

import pytest

from pkm_brain import llm
from pkm_brain.llm import CodexProvider, LLMProviderError, OpenAIProvider


def test_openai_provider_falls_back_on_model_selection_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("PKM_BRAIN_OPENAI_MODEL", "missing-model")
    monkeypatch.setenv("PKM_BRAIN_OPENAI_MODEL_FALLBACKS", "gpt-5.4-mini,gpt-5")
    calls = []

    def fake_post_json(url: str, payload: dict, headers: dict) -> dict:
        calls.append(payload["model"])
        if payload["model"] == "missing-model":
            raise LLMProviderError("The model `missing-model` does not exist or you do not have access to it.")
        return {"choices": [{"message": {"content": '{"ok": true}'}}]}

    monkeypatch.setattr(llm, "post_json", fake_post_json)

    provider = OpenAIProvider()
    assert provider.complete("return JSON") == '{"ok": true}'
    assert calls == ["missing-model", "gpt-5.4-mini"]
    assert provider.model == "gpt-5.4-mini"


def test_openai_provider_does_not_hide_auth_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("PKM_BRAIN_OPENAI_MODEL", "missing-model")
    monkeypatch.setenv("PKM_BRAIN_OPENAI_MODEL_FALLBACKS", "gpt-5.4-mini")
    calls = []

    def fake_post_json(url: str, payload: dict, headers: dict) -> dict:
        calls.append(payload["model"])
        raise LLMProviderError("Unauthorized: invalid API key")

    monkeypatch.setattr(llm, "post_json", fake_post_json)

    with pytest.raises(LLMProviderError, match="invalid API key"):
        OpenAIProvider().complete("return JSON")
    assert calls == ["missing-model"]


def test_codex_provider_falls_back_on_model_selection_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PKM_BRAIN_CODEX_BIN", str(tmp_path / "codex"))
    monkeypatch.setenv("PKM_BRAIN_CODEX_MODEL", "missing-model")
    monkeypatch.setenv("PKM_BRAIN_CODEX_MODEL_FALLBACKS", "gpt-5.4-mini,gpt-5")
    calls = []

    class Completed:
        def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(command: list[str], **kwargs: object) -> Completed:
        if command[1:3] == ["login", "status"]:
            return Completed(0, stdout="Logged in")
        assert command[1:4] == ["--ask-for-approval", "never", "exec"]
        assert "--ask-for-approval" not in command[4:]
        model = command[command.index("--model") + 1]
        calls.append(model)
        if model == "missing-model":
            return Completed(1, stderr="invalid value 'missing-model' for '--model <MODEL>'")
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text('{"ok": true}', encoding="utf-8")
        return Completed(0)

    monkeypatch.setattr(llm.subprocess, "run", fake_run)

    provider = CodexProvider()
    assert provider.complete("return JSON") == '{"ok": true}'
    assert calls == ["missing-model", "gpt-5.4-mini"]
    assert provider.model == "gpt-5.4-mini"


def test_complete_json_repairs_malformed_response() -> None:
    class FakeProvider:
        name = "fake"
        model = "fake-model"

        def __init__(self) -> None:
            self.calls = 0

        def complete(self, prompt: str) -> str:
            self.calls += 1
            if self.calls == 1:
                return "not json"
            assert "Repair the previous response" in prompt
            return '{"ok": true}'

    provider = FakeProvider()

    assert llm.complete_json("return ok", llm_provider=provider) == {"ok": True}
    assert provider.calls == 2


def test_role_specific_provider_model_overrides_global(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")
    monkeypatch.setenv("PKM_BRAIN_LLM_PROVIDER", "openai")
    monkeypatch.setenv("PKM_BRAIN_OPENAI_MODEL", "global-model")
    monkeypatch.setenv("PKM_BRAIN_LLM_CRITIC_PROVIDER", "openai")
    monkeypatch.setenv("PKM_BRAIN_LLM_CRITIC_MODEL", "critic-model")
    monkeypatch.setenv("PKM_BRAIN_LLM_CRITIC_MODEL_FALLBACKS", "critic-fallback")

    provider = llm.get_provider(role="critic")

    assert isinstance(provider, OpenAIProvider)
    assert provider.model == "critic-model"
    assert provider.models == ["critic-model", "critic-fallback"]
    assert llm.provider_status(role="critic")["model"] == "critic-model"
