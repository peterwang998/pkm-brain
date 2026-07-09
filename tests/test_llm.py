from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from pkm_brain.cli import app
from pkm_brain import llm
from pkm_brain.llm import CodexProvider, LLMConfigurationError, LLMProviderError, OpenAIProvider
from pkm_brain.paths import BrainPaths


runner = CliRunner()


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
    monkeypatch.setenv("PKM_BRAIN_CODEX_REASONING_EFFORT", "medium")
    calls = []

    class Completed:
        def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(command: list[str], **kwargs: object) -> Completed:
        if command[1:3] == ["login", "status"]:
            return Completed(0, stdout="Logged in")
        assert command[1:5] == [
            "--ask-for-approval",
            "never",
            "-c",
            'model_reasoning_effort="medium"',
        ]
        assert command[5] == "exec"
        assert "--ask-for-approval" not in command[4:]
        assert "--skip-git-repo-check" in command
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


def test_cos_role_provider_status_uses_default_and_role_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    clear_cos_llm_env(monkeypatch)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    paths = BrainPaths.from_value(tmp_path / "brain")
    paths.config_local.mkdir(parents=True)
    (paths.config_local / "cos_llm.yaml").write_text(
        """
default:
  provider: ollama
  model: llama3
  model_fallbacks:
    - llama3.1
roles:
  auditor:
    provider: openai
    model: gpt-audit
""".strip()
        + "\n",
        encoding="utf-8",
    )

    report = llm.cos_provider_status(paths)
    roles = {row["role"]: row for row in report["roles"]}

    assert roles["extractor"]["role_configured"] is True
    assert roles["extractor"]["configured"] is True
    assert roles["extractor"]["provider"] == "ollama"
    assert roles["extractor"]["provider_source"] == "config:default.provider"
    assert roles["extractor"]["model"] == "llama3"
    assert roles["extractor"]["fallback_models"] == ["llama3.1"]
    assert roles["auditor"]["provider"] == "openai"
    assert roles["auditor"]["model"] == "gpt-audit"
    assert "OPENAI_API_KEY" in roles["auditor"]["missing"]


def test_cos_role_provider_status_warns_on_reviewer_proposer_overlap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    clear_cos_llm_env(monkeypatch)
    paths = BrainPaths.from_value(tmp_path / "brain")
    paths.config_local.mkdir(parents=True)
    (paths.config_local / "cos_llm.yaml").write_text(
        """
default:
  provider: ollama
  model: shared-model
""".strip()
        + "\n",
        encoding="utf-8",
    )

    report = llm.cos_provider_status(paths)

    assert any("auditor uses the same provider/model as extractor" in warning for warning in report["warnings"])
    assert any("critic uses the same provider/model as resolver" in warning for warning in report["warnings"])


def test_complete_json_resolves_provider_from_cos_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    clear_cos_llm_env(monkeypatch)
    paths = BrainPaths.from_value(tmp_path / "brain")
    paths.config_local.mkdir(parents=True)
    (paths.config_local / "cos_llm.yaml").write_text(
        """
default:
  provider: codex
  model: llama3
roles:
  extractor:
    model: extractor-model
    reasoning_effort: medium
""".strip()
        + "\n",
        encoding="utf-8",
    )
    calls: list[tuple[str | None, str | None, str | None]] = []

    class FakeProvider:
        name = "fake"
        model = "fake-model"

        def complete(self, prompt: str) -> str:
            return json.dumps({"ok": True})

    def fake_get_provider(provider: str | None = None, *, role: str | None = None) -> FakeProvider:
        calls.append((provider, role, os.environ.get("PKM_BRAIN_CODEX_MODEL")))
        assert os.environ.get("PKM_BRAIN_CODEX_REASONING_EFFORT") == "medium"
        return FakeProvider()

    monkeypatch.setattr(llm, "get_provider", fake_get_provider)

    assert llm.complete_json("return ok", role="extractor", paths=paths) == {"ok": True}
    assert calls == [("codex", "extractor", "extractor-model")]


def test_complete_json_with_unconfigured_cos_role_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    clear_cos_llm_env(monkeypatch)
    paths = BrainPaths.from_value(tmp_path / "brain")

    with pytest.raises(LLMConfigurationError, match="No CoS LLM provider configured"):
        llm.complete_json("return ok", role="extractor", paths=paths)


def test_cos_providers_cli_reports_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    clear_cos_llm_env(monkeypatch)
    paths = BrainPaths.from_value(tmp_path / "brain")
    paths.config_local.mkdir(parents=True)
    (paths.config_local / "cos_llm.yaml").write_text(
        """
default:
  provider: ollama
  model: llama3
""".strip()
        + "\n",
        encoding="utf-8",
    )

    result = runner.invoke(app, ["cos", "providers", "--json", "--home", str(paths.home)])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    roles = {row["role"]: row for row in payload["roles"]}
    assert payload["config_exists"] is True
    assert roles["gardener"]["provider"] == "ollama"
    assert roles["gardener"]["model"] == "llama3"


def clear_cos_llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PKM_BRAIN_LLM_PROVIDER", raising=False)
    for role in llm.LLM_ROLE_ORDER:
        for suffix in ("PROVIDER", "MODEL", "MODEL_FALLBACKS", "BASE_URL", "REASONING_EFFORT"):
            monkeypatch.delenv(llm.role_env(role, suffix), raising=False)
