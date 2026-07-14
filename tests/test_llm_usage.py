from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from pkm_brain import llm
from pkm_brain.llm import CodexProvider
from pkm_brain.llm_usage import (
    capture_provider_usage,
    codex_jsonl_usage,
    configure_provider_usage,
    llm_usage_summary,
    record_provider_usage,
)
from pkm_brain.paths import BrainPaths


def _record_test_usage(
    provider: object,
    *,
    model: str,
    usage: dict[str, int] | None,
) -> None:
    record_provider_usage(
        provider,
        model=model,
        usage=usage,
        status="success",
        started_at="2026-07-14T08:00:00Z",
        duration_ms=25,
    )


def test_provider_usage_capture_isolates_parallel_same_provider_calls() -> None:
    provider = object()
    barrier = threading.Barrier(2)

    def capture_in_thread(model: str, total_tokens: int) -> tuple[str, int]:
        with capture_provider_usage(provider) as captured:
            barrier.wait(timeout=5)
            _record_test_usage(
                provider,
                model=model,
                usage={"input_tokens": total_tokens, "total_tokens": total_tokens},
            )
        record = captured.records[0]
        assert record.usage is not None
        return record.model, record.usage["total_tokens"]

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(capture_in_thread, "model-a", 101),
            executor.submit(capture_in_thread, "model-b", 202),
        ]

    assert {future.result() for future in futures} == {
        ("model-a", 101),
        ("model-b", 202),
    }


def test_provider_usage_capture_uses_nearest_matching_nested_capture() -> None:
    provider = object()
    other_provider = object()

    with capture_provider_usage(provider) as outer:
        _record_test_usage(provider, model="outer-before", usage={"input_tokens": 1})
        with capture_provider_usage(other_provider) as other:
            _record_test_usage(provider, model="outer-through-other", usage={"input_tokens": 2})
            with capture_provider_usage(provider) as inner:
                _record_test_usage(provider, model="inner", usage={"input_tokens": 3})
                _record_test_usage(
                    other_provider,
                    model="other",
                    usage={"input_tokens": 4},
                )
        _record_test_usage(provider, model="outer-after", usage={"input_tokens": 5})

    _record_test_usage(provider, model="after-exit", usage={"input_tokens": 6})

    assert [record.model for record in outer.records] == [
        "outer-before",
        "outer-through-other",
        "outer-after",
    ]
    assert [record.model for record in inner.records] == ["inner"]
    assert [record.model for record in other.records] == ["other"]


def test_provider_usage_capture_represents_missing_usage() -> None:
    provider = object()

    with capture_provider_usage(provider) as captured:
        result = record_provider_usage(
            provider,
            model="usage-unavailable",
            usage=None,
            status="error",
            started_at="2026-07-14T08:00:00Z",
            duration_ms=-10,
            error_type="ProviderUsageUnavailable",
            session_id="session-test",
            rate_limits={"primary": {"used_percent": 12}},
        )

    assert result is None
    assert len(captured.records) == 1
    record = captured.records[0]
    assert record.model == "usage-unavailable"
    assert record.usage is None
    assert record.status == "error"
    assert record.duration_ms == 0
    assert record.error_type == "ProviderUsageUnavailable"
    assert record.session_id == "session-test"
    assert record.rate_limits == {"primary": {"used_percent": 12}}


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
