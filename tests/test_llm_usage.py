from __future__ import annotations

import json
from pathlib import Path

from pkm_brain import llm
from pkm_brain.llm import CodexProvider
from pkm_brain.llm_usage import (
    codex_jsonl_usage,
    configure_provider_usage,
    llm_usage_summary,
)
from pkm_brain.paths import BrainPaths


def test_codex_jsonl_usage_reads_public_and_internal_event_shapes() -> None:
    output = "\n".join(
        [
            json.dumps({"type": "thread.started", "thread_id": "thread_test"}),
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {
                        "input_tokens": 120,
                        "cached_input_tokens": 20,
                        "output_tokens": 30,
                        "reasoning_output_tokens": 10,
                        "total_tokens": 150,
                    },
                }
            ),
            json.dumps(
                {
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "last_token_usage": {
                                "input_tokens": 125,
                                "cached_input_tokens": 25,
                                "output_tokens": 35,
                                "reasoning_output_tokens": 12,
                                "total_tokens": 160,
                            }
                        },
                        "rate_limits": {"primary": {"used_percent": 7.0}},
                    },
                }
            ),
        ]
    )

    usage, metadata = codex_jsonl_usage(output)

    assert usage == {
        "input_tokens": 125,
        "cached_input_tokens": 25,
        "output_tokens": 35,
        "reasoning_output_tokens": 12,
        "total_tokens": 160,
    }
    assert metadata["session_id"] == "thread_test"
    assert metadata["rate_limits"]["primary"]["used_percent"] == 7.0


def test_codex_provider_logs_usage_by_run_and_evaluator_alias(
    monkeypatch, tmp_path: Path
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    monkeypatch.setenv("PKM_BRAIN_CODEX_BIN", str(tmp_path / "codex"))
    monkeypatch.setenv("PKM_BRAIN_CODEX_MODEL", "gpt-5.6-luna")
    monkeypatch.setenv("PKM_BRAIN_CODEX_MODEL_FALLBACKS", "gpt-5.6-luna")
    monkeypatch.setenv("PKM_BRAIN_CODEX_REASONING_EFFORT", "medium")

    class Completed:
        def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_run(command: list[str], **kwargs: object) -> Completed:
        if command[1:3] == ["login", "status"]:
            return Completed(0, stdout="Logged in")
        assert "--json" in command
        output_path = Path(command[command.index("--output-last-message") + 1])
        output_path.write_text('{"decision":"agree"}', encoding="utf-8")
        return Completed(
            0,
            stdout="\n".join(
                [
                    json.dumps(
                        {"type": "thread.started", "thread_id": "thread_usage"}
                    ),
                    json.dumps(
                        {
                            "type": "turn.completed",
                            "usage": {
                                "input_tokens": 1000,
                                "cached_input_tokens": 400,
                                "output_tokens": 80,
                                "reasoning_output_tokens": 30,
                                "total_tokens": 1080,
                            },
                        }
                    ),
                ]
            ),
        )

    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    provider = configure_provider_usage(
        CodexProvider(),
        paths,
        "critic",
        cycle_id="automation_test",
        run_id="automation_test",
        stage="evaluation",
    )

    assert provider.complete("evaluate") == '{"decision":"agree"}'
    summary = llm_usage_summary(paths, cycle_id="automation_test", limit=1)

    assert summary["cycle_count"] == 1
    assert summary["available_cycle_count"] == 1
    assert summary["totals"]["total_tokens"] == 1080
    assert summary["roles"][0]["role"] == "evaluator"
    assert summary["roles"][0]["total_tokens"] == 1080
    cycle = summary["cycles"][0]
    assert cycle["total_tokens"] == 1080
    assert cycle["cached_input_tokens"] == 400
    assert cycle["uncached_input_tokens"] == 600
    assert cycle["request_count"] == 1
    assert cycle["roles"][0]["role"] == "evaluator"
    assert cycle["roles"][0]["source_roles"] == ["critic"]


def test_complete_json_passes_usage_context_to_role_provider(
    monkeypatch, tmp_path: Path
) -> None:
    paths = BrainPaths.from_value(tmp_path / "brain")
    captured: dict[str, object] = {}

    class Provider:
        def complete(self, prompt: str) -> str:
            return '{"decision":"route_existing"}'

    def fake_get_role_provider(
        supplied_paths: BrainPaths,
        role: str,
        **kwargs: object,
    ) -> Provider:
        captured.update({"paths": supplied_paths, "role": role, **kwargs})
        return Provider()

    monkeypatch.setattr(llm, "get_cos_role_provider", fake_get_role_provider)

    result = llm.complete_json(
        "Resolve this route.",
        role="resolver",
        paths=paths,
        usage_cycle_id="benchmark-run",
        usage_run_id="benchmark-run",
        usage_stage="route_resolution",
    )

    assert result == {"decision": "route_existing"}
    assert captured["paths"] == paths
    assert captured["role"] == "resolver"
    assert captured["usage_cycle_id"] == "benchmark-run"
    assert captured["usage_run_id"] == "benchmark-run"
    assert captured["usage_stage"] == "route_resolution"
