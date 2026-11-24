from __future__ import annotations

import json
import sys
from pathlib import Path

from chantier.adapters.executors.agents import (
    AiderOllamaExecutor,
    AntigravityExecutor,
    ClaudeCodeExecutor,
    CodexExecutor,
)
from chantier.adapters.executors.process import run_process
from chantier.application.ports import ExecutionRequest
from chantier.bootstrap import ADAPTERS, build
from chantier.domain.model import Phase, Ticket, TicketKind, Tier, Usage
from chantier.domain.routing import ExecutorSpec


def spec(adapter: str, model: str = "m", args: tuple[str, ...] = ()) -> ExecutorSpec:
    return ExecutorSpec(name=adapter, adapter=adapter, tier=Tier.FRONTIER, model=model, args=args)


def test_claude_stream_json_usage_and_text():
    ex, usage = ClaudeCodeExecutor(spec("claude_code")), Usage()
    assistant = {"type": "assistant", "message": {"content": [
        {"type": "text", "text": "Reading the spec"},
        {"type": "tool_use", "name": "Edit", "input": {"file_path": "src/a.py"}}]}}
    result = {"type": "result", "result": "Done.", "total_cost_usd": 0.42,
              "usage": {"input_tokens": 100, "cache_creation_input_tokens": 50,
                        "cache_read_input_tokens": 1000, "output_tokens": 20}}
    assert "tool Edit" in (ex.interpret(json.dumps(assistant), usage) or "")
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


def test_codex_failure_events_keep_their_reason():
    ex, usage = CodexExecutor(spec("codex")), Usage()
    error = {"type": "error", "message": "unexpected status 401 Unauthorized"}
    failed = {"type": "turn.failed", "error": {"message": "quota exceeded"}}
    assert ex.interpret(json.dumps(error), usage) == "codex error: unexpected status 401 Unauthorized"
    assert ex.interpret(json.dumps(failed), usage) == "codex error: quota exceeded"
    assert ex.interpret(json.dumps({"type": "thread.started", "thread_id": "t"}), usage) is None


def test_codex_command_builds_flags(tmp_path: Path):
    req = ExecutionRequest(
        ticket=Ticket(id="demo/s", project="demo", kind=TicketKind.IMPLEMENT, title="t", spec_path="s.md"),
        phase=Phase.IMPLEMENT, workdir=tmp_path, prompt="implement spec", log_path=tmp_path / "run.log",
    )
    ex = CodexExecutor(spec("codex", model="gpt-5-codex", args=("--full-auto",)))
    assert ex.command(req) == [
        "codex", "exec", "--json", "-C", str(tmp_path), "-m", "gpt-5-codex", "--full-auto", "implement spec",
    ]


def test_antigravity_command_builds_flags(tmp_path: Path):
    req = ExecutionRequest(
        ticket=Ticket(id="demo/s", project="demo", kind=TicketKind.IMPLEMENT, title="t", spec_path="s.md"),
        phase=Phase.IMPLEMENT,
        workdir=tmp_path,
        prompt="implement spec",
        log_path=tmp_path / "run.log",
    )
    ex = AntigravityExecutor(spec("antigravity", model="gemini-4-neon-medium", args=("--mode", "auto")))
    assert ex.command(req) == [
        "agy", "--output-format", "stream-json", "--model", "gemini-4-neon-medium",
        "--mode", "auto", "-p", "implement spec",
    ]
    ex_nomodel = AntigravityExecutor(spec("antigravity", model=""))
    assert ex_nomodel.command(req) == ["agy", "--output-format", "stream-json", "-p", "implement spec"]


def test_antigravity_stream_json_events_and_usage():
    ex, usage = AntigravityExecutor(spec("antigravity")), Usage()
    init = {"event": "init", "conversation_id": "c1", "init": {"model": "gemini-4-neon-medium"}}
    user_step = {"event": "step_update", "step_update": {"step_index": 0, "state": "DONE", "step_type": "user_input"}}
    delta = {"event": "step_update", "step_update": {
        "step_index": 1, "state": "ACTIVE", "step_type": "agent_response", "text_delta": "partial",
    }}
    tool_active = {"event": "step_update", "step_update": {
        "step_index": 2, "state": "ACTIVE", "step_type": "tool", "tool_name": "run_command",
        "tool_info": {"name": "run_command", "parameters": {"CommandLine": "uv run pytest -q"}},
    }}
    tool_done = {"event": "step_update", "step_update": {
        "step_index": 2, "state": "DONE", "step_type": "tool", "tool_name": "run_command",
        "tool_info": {"name": "run_command", "output": "passed"},
    }}
    result = {"event": "result", "result": {
        "conversation_id": "c1", "status": "SUCCESS", "response": "Done.\nVERDICT: PASS",
        "usage": {"input_tokens": 12493, "output_tokens": 43, "thinking_tokens": 10,
                  "cache_read_tokens": 9497, "total_tokens": 12536},
    }}
    assert ex.interpret(json.dumps(init), usage) is None
    assert ex.interpret(json.dumps(user_step), usage) is None
    assert ex.interpret(json.dumps(delta), usage) is None
    assert "tool run_command" in (ex.interpret(json.dumps(tool_active), usage) or "")
    assert ex.interpret(json.dumps(tool_done), usage) is None
    assert ex.interpret(json.dumps(result), usage) == "Done.\nVERDICT: PASS"
    assert (usage.input_tokens, usage.cache_read_tokens, usage.output_tokens) == (12493, 9497, 43)
    assert usage.reported_cost_usd is None


def test_antigravity_error_plain_line_and_bootstrap(tmp_path: Path):
    ex, usage = AntigravityExecutor(spec("antigravity")), Usage()
    assert ex.interpret("error: invalid model", usage) == "error: invalid model"
    assert ex.interpret(json.dumps({"event": "unknown_event"}), usage) is None
    err = {"event": "result", "result": {"status": "ERROR", "response": "", "error": "auth failed",
                                         "usage": {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0}}}
    assert ex.interpret(json.dumps(err), usage) == "auth failed"
    assert ADAPTERS["antigravity"] is AntigravityExecutor

    home = tmp_path / "h"
    home.mkdir()
    (home / "projects").mkdir()
    (home / "config.yaml").write_text(
        "executors:\n  agy:\n    adapter: antigravity\n    tier: frontier\n    model: gemini-4-neon-medium\n"
    )
    app = build(home)
    assert isinstance(app.scheduler.executors["agy"], AntigravityExecutor)


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
