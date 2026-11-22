from __future__ import annotations

import json
import sys
from pathlib import Path

from chantier.adapters.executors.agents import AiderOllamaExecutor, ClaudeCodeExecutor, CodexExecutor
from chantier.adapters.executors.process import run_process
from chantier.domain.model import Tier, Usage
from chantier.domain.routing import ExecutorSpec


def spec(adapter: str) -> ExecutorSpec:
    return ExecutorSpec(name=adapter, adapter=adapter, tier=Tier.FRONTIER, model="m")


def test_claude_stream_json_usage_and_text():
    ex, usage = ClaudeCodeExecutor(spec("claude_code")), Usage()
    assistant = {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Reading the spec"},
        {"type": "tool_use", "name": "Edit", "input": {"file_path": "src/a.py"}}]}}
    result = {"type": "result", "result": "Done.", "total_cost_usd": 0.42,
              "usage": {"input_tokens": 100, "cache_creation_input_tokens": 50,
                        "cache_read_input_tokens": 1000, "output_tokens": 20}}
    assert "tool Edit" in ex.interpret(json.dumps(assistant), usage)
    assert ex.interpret(json.dumps({"type": "system", "subtype": "init"}), usage) is None
    assert ex.interpret(json.dumps(result), usage) == "Done."
    assert (usage.input_tokens, usage.cache_read_tokens, usage.output_tokens) == (150, 1000, 20)
    assert usage.reported_cost_usd == 0.42


def test_codex_json_events():
    ex, usage = CodexExecutor(spec("codex")), Usage()
    ex.interpret(json.dumps({"type": "turn.completed",
                             "usage": {"input_tokens": 500, "cached_input_tokens": 200, "output_tokens": 40}}), usage)
    text = ex.interpret(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}}), usage)
    assert text == "ok"
    assert (usage.input_tokens, usage.cache_read_tokens, usage.output_tokens) == (300, 200, 40)


def test_aider_token_report_line():
    ex, usage = AiderOllamaExecutor(spec("aider_ollama")), Usage()
    assert ex.interpret("Tokens: 4.2k sent, 512 received.", usage) is None
    assert (usage.input_tokens, usage.output_tokens) == (4200, 512)


def test_watchdog_kills_a_silent_process(tmp_path: Path):
    result = run_process(
        [sys.executable, "-c", "import time; print('start', flush=True); time.sleep(30)"],
        tmp_path, tmp_path / "log.txt", env={}, timeout_s=60, idle_timeout_s=2,
        interpret=lambda line, usage: line, observe=lambda line: None,
    )
    assert result.killed_reason == "no output for 2 s"
    assert "killed" in (tmp_path / "log.txt").read_text()


def test_missing_executable_is_reported_not_raised(tmp_path: Path):
    result = run_process(["definitely-not-a-binary-xyz"], tmp_path, tmp_path / "log.txt", env={},
                         timeout_s=5, idle_timeout_s=5, interpret=lambda line, u: line, observe=lambda line: None)
    assert result.exit_code == 127


def test_ollama_host_comes_from_the_environment(monkeypatch):
    ex = AiderOllamaExecutor(spec("aider_ollama"))
    monkeypatch.delenv("OLLAMA_API_BASE", raising=False)
    assert ex.ollama_url == "http://localhost:11434"
    monkeypatch.setenv("OLLAMA_API_BASE", "http://gpu-box:11434/")
    assert ex.ollama_url == "http://gpu-box:11434"
    assert ex.environment()["OLLAMA_API_BASE"] == "http://gpu-box:11434"
