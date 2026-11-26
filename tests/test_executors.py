from __future__ import annotations

import json
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from collections.abc import Sequence
from pathlib import Path

import pytest

from chantier.adapters.executors.agents import (
    AntigravityExecutor,
    ClaudeCodeExecutor,
    CodexExecutor,
)
from chantier.adapters.executors.ollama import OllamaExecutor, OllamaModelProbe
from chantier.adapters.executors.process import run_process
from chantier.application.ports import ExecutionRequest
from chantier.application.supervisor import LoopDetector
from chantier.bootstrap import ADAPTERS, build
from chantier.domain.model import Phase, Ticket, TicketKind, Tier, Usage
from chantier.domain.routing import ExecutorSpec
from chantier.specs import normalized_spec_hash


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
    ex = OllamaExecutor(spec("ollama"))
    monkeypatch.delenv("OLLAMA_API_BASE", raising=False)
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    assert ex.ollama_url == "http://localhost:11434"
    monkeypatch.setenv("OLLAMA_HOST", "gpu-host:11434/")
    assert ex.ollama_url == "http://gpu-host:11434"
    monkeypatch.setenv("OLLAMA_API_BASE", "http://gpu-box:11434/")
    assert ex.ollama_url == "http://gpu-box:11434"


class _FakeOllamaServer:
    def __init__(
        self,
        chat_turns: list[list[dict]],
        loaded_models: Sequence[str | dict] | None = None,
        tags: list[str] | None = None,
    ) -> None:
        self.chat_turns = list(chat_turns)
        self.loaded_models = list(loaded_models or [])
        self.tags = list(tags or [])
        self.generate_calls: list[dict] = []
        self.chat_requests: list[dict] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args) -> None:  # noqa: A002
                pass

            def do_GET(self) -> None:
                if self.path == "/api/ps":
                    models = [
                        {"name": m} if isinstance(m, str) else m
                        for m in outer.loaded_models
                    ]
                    body = json.dumps({"models": models}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(body)
                elif self.path == "/api/tags":
                    body = json.dumps({"models": [{"name": m} for m in outer.tags]}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(body)
                else:
                    self.send_response(404)
                    self.end_headers()

            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
                if self.path == "/api/generate":
                    outer.generate_calls.append(payload)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"done":true}')
                    return
                if self.path == "/api/chat":
                    outer.chat_requests.append(payload)
                    chunks = outer.chat_turns.pop(0)
                    if payload.get("stream", True) is False:
                        self.send_response(200)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(json.dumps(chunks[-1]).encode())
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "application/x-ndjson")
                    self.end_headers()
                    for chunk in chunks:
                        self.wfile.write((json.dumps(chunk) + "\n").encode())
                        self.wfile.flush()
                    return
                self.send_response(404)
                self.end_headers()

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self._server.server_port}"
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> _FakeOllamaServer:
        self._thread.start()
        return self

    def __exit__(self, *args) -> None:
        self._server.shutdown()
        self._server.server_close()


def _ollama_request(
    workdir: Path,
    approved_spec_sha: str | None = None,
    checks: tuple[str, ...] = (),
) -> ExecutionRequest:
    return ExecutionRequest(
        ticket=Ticket(
            id="demo/s", project="demo", kind=TicketKind.IMPLEMENT, title="t", spec_path="s.md",
            approved_spec_sha=approved_spec_sha,
        ),
        phase=Phase.IMPLEMENT,
        workdir=workdir,
        prompt="Implement s.md",
        log_path=workdir.parent / "run.log",
        checks=checks,
    )


def test_ollama_executor_tool_loop_warmup_and_usage(tmp_path: Path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    turns = [
        [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"id": "c1", "function": {"name": "write_file",
                                                  "arguments": {"path": "sub/hello.txt", "content": "hello world\n"}}},
                        {"id": "c2", "function": {"name": "edit_file",
                                                  "arguments": {"path": "sub/hello.txt",
                                                                "old_text": "world", "new_text": "chantier"}}},
                        {"id": "c3", "function": {"name": "read_file", "arguments": {"path": "sub/hello.txt"}}},
                        {"id": "c4", "function": {"name": "list_files", "arguments": {"path": "."}}},
                        {"id": "c5", "function": {"name": "run_command", "arguments": {
                            "command": f"{sys.executable} -c \"open('ok.txt','w').write('1')\""
                        }}},
                    ],
                },
                "done": False,
            },
            {"message": {"role": "assistant", "content": ""}, "done": True, "done_reason": "stop",
             "prompt_eval_count": 500, "prompt_eval_cached_count": 100, "eval_count": 60},
        ],
        [
            {"message": {"role": "assistant", "content": "Implemented and verified."}, "done": True,
             "done_reason": "stop", "prompt_eval_count": 800, "prompt_eval_cached_count": 450, "eval_count": 20},
        ],
    ]
    with _FakeOllamaServer(turns, loaded_models=["other:7b"]) as server:
        monkeypatch.setenv("OLLAMA_API_BASE", server.url)
        ex = OllamaExecutor(ExecutorSpec(
            name="ollama", adapter="ollama", tier=Tier.LOCAL, model="qwen3-coder:30b",
            args=("--num-ctx", "16384"),
        ))
        observed: list[str] = []
        result = ex.execute(_ollama_request(work), lambda line: observed.append(line) and None)

    assert result.exit_code == 0
    assert result.killed_reason is None
    assert "Implemented and verified." in result.summary
    assert (work / "sub/hello.txt").read_text() == "hello chantier\n"
    assert (work / "ok.txt").read_text() == "1"
    assert (result.usage.input_tokens, result.usage.cache_read_tokens, result.usage.output_tokens) == (750, 550, 80)
    assert any(c["model"] == "other:7b" and c["keep_alive"] == 0 for c in server.generate_calls)
    assert any(c["model"] == "qwen3-coder:30b" and c["keep_alive"] == -1 for c in server.generate_calls)
    assert server.chat_requests[0]["options"]["num_ctx"] == 16384
    tool_msgs = [m for m in server.chat_requests[1]["messages"] if m.get("role") == "tool"]
    assert "1: hello chantier" in tool_msgs[2]["content"]
    assert "sub/hello.txt" in tool_msgs[3]["content"]
    assert any(line.startswith("tool write_file ") for line in observed)
    assert "tool write_file" in (tmp_path / "run.log").read_text()


def test_ollama_executor_confines_paths_and_blocks_git(tmp_path: Path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    (work / ".git").mkdir()
    (work / ".git" / "config").write_text("secret\n")
    (tmp_path / "outside.txt").write_text("outside\n")
    turns = [
        [
            {"message": {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "function": {"name": "read_file", "arguments": {"path": "../outside.txt"}}},
                {"id": "c2", "function": {
                    "name": "write_file", "arguments": {"path": "../escape.txt", "content": "x"},
                }},
                {"id": "c3", "function": {"name": "read_file", "arguments": {"path": ".git/config"}}},
                {"id": "c4", "function": {"name": "run_command", "arguments": {"command": "cd ../mirror && ls"}}},
            ]}, "done": True, "done_reason": "stop", "prompt_eval_count": 100, "eval_count": 20},
        ],
        [
            {"message": {"role": "assistant", "content": "Stopped."}, "done": True, "done_reason": "stop",
             "prompt_eval_count": 150, "eval_count": 5},
        ],
    ]
    with _FakeOllamaServer(turns) as server:
        monkeypatch.setenv("OLLAMA_API_BASE", server.url)
        ex = OllamaExecutor(ExecutorSpec(name="ollama", adapter="ollama", tier=Tier.LOCAL, model="qwen3-coder:30b"))
        result = ex.execute(_ollama_request(work), lambda line: None)

    assert result.exit_code == 0
    assert not (tmp_path / "escape.txt").exists()
    tool_msgs = [m for m in server.chat_requests[1]["messages"] if m.get("role") == "tool"]
    assert "outside the workspace" in tool_msgs[0]["content"].lower()
    assert "outside the workspace" in tool_msgs[1]["content"].lower()
    assert ".git" in tool_msgs[2]["content"]
    assert "stay inside the workspace" in tool_msgs[3]["content"].lower()


def test_ollama_executor_watchdog_kills_on_loop_detector(tmp_path: Path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    essay = "\n".join(f"Verification bullet {i}: all checks look great so far" for i in range(25))
    loop_turn = [
        {"message": {"role": "assistant", "content": essay, "tool_calls": [
            {"id": "c1", "function": {"name": "list_files", "arguments": {"path": "."}}},
        ]}, "done": True, "done_reason": "stop", "prompt_eval_count": 50, "eval_count": 10},
    ]
    with _FakeOllamaServer([loop_turn for _ in range(10)]) as server:
        monkeypatch.setenv("OLLAMA_API_BASE", server.url)
        ex = OllamaExecutor(ExecutorSpec(name="ollama", adapter="ollama", tier=Tier.LOCAL, model="qwen3-coder:30b"))
        detector = LoopDetector(window=20, threshold=3)
        result = ex.execute(_ollama_request(work), detector)

    assert result.killed_reason is not None
    assert "loop detected" in result.killed_reason
    assert len(server.chat_requests) == 3


def test_ollama_executor_connection_error_returns_127_and_bootstrap(tmp_path: Path, monkeypatch):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    monkeypatch.setenv("OLLAMA_API_BASE", f"http://127.0.0.1:{port}")
    work = tmp_path / "work"
    work.mkdir()
    ex = OllamaExecutor(ExecutorSpec(name="ollama", adapter="ollama", tier=Tier.LOCAL, model="qwen3-coder:30b"))
    result = ex.execute(_ollama_request(work), lambda line: None)
    assert result.exit_code == 127
    assert ADAPTERS["ollama"] is OllamaExecutor

    home = tmp_path / "h"
    home.mkdir()
    (home / "projects").mkdir()
    (home / "config.yaml").write_text(
        "executors:\n  local:\n    adapter: ollama\n    tier: local\n    model: qwen3-coder:30b\n"
    )
    app = build(home)
    assert isinstance(app.scheduler.executors["local"], OllamaExecutor)


def test_ollama_executor_handles_malformed_tool_arguments(tmp_path: Path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    (work / "a.txt").write_text("line1\n")
    turns = [
        [
            {"message": {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "function": {"name": "read_file", "arguments": {"path": "a.txt", "start_line": "> 300"}}},
                {"id": "c2", "function": {"name": "list_files", "arguments": {"path": ".", "max_depth": None}}},
                {"id": "c3", "function": {
                    "name": "run_command", "arguments": {"command": "echo ok", "timeout_s": "bad"},
                }},
            ]}, "done": True, "done_reason": "stop", "prompt_eval_count": 100, "eval_count": 20},
        ],
        [
            {"message": {"role": "assistant", "content": "Recovered."}, "done": True, "done_reason": "stop",
             "prompt_eval_count": 150, "eval_count": 5},
        ],
    ]
    with _FakeOllamaServer(turns) as server:
        monkeypatch.setenv("OLLAMA_API_BASE", server.url)
        ex = OllamaExecutor(ExecutorSpec(name="ollama", adapter="ollama", tier=Tier.LOCAL, model="qwen3-coder:30b"))
        result = ex.execute(_ollama_request(work), lambda line: None)

    assert result.exit_code == 0
    tool_msgs = [m for m in server.chat_requests[1]["messages"] if m.get("role") == "tool"]
    assert "error:" in tool_msgs[0]["content"].lower()
    assert "a.txt" in tool_msgs[1]["content"]
    assert "error:" in tool_msgs[2]["content"].lower()


def test_ollama_executor_finish_tool_ends_run_cleanly(tmp_path: Path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    turns = [
        [
            {"message": {"role": "assistant", "content": "Done coding.", "tool_calls": [
                {"id": "c1", "function": {"name": "write_file", "arguments": {"path": "done.txt", "content": "ok\n"}}},
                {"id": "c2", "function": {"name": "finish", "arguments": {"summary": "All checks passed."}}},
            ]}, "done": True, "done_reason": "stop", "prompt_eval_count": 120, "eval_count": 30},
        ],
    ]
    with _FakeOllamaServer(turns) as server:
        monkeypatch.setenv("OLLAMA_API_BASE", server.url)
        ex = OllamaExecutor(ExecutorSpec(name="ollama", adapter="ollama", tier=Tier.LOCAL, model="qwen3-coder:30b"))
        result = ex.execute(_ollama_request(work), lambda line: None)

    assert result.exit_code == 0
    assert result.killed_reason is None
    assert (work / "done.txt").read_text() == "ok\n"
    assert "All checks passed." in result.summary
    assert len(server.chat_requests) == 1


def test_ollama_executor_finish_rejects_unresolved_spec_checkboxes(tmp_path: Path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    (work / "s.md").write_text("# Spec\n\n- [ ] Task A\n- [ ] Task B (manual)\n")
    turns = [
        [
            {"message": {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "function": {"name": "finish", "arguments": {"summary": "Done early."}}},
            ]}, "done": True, "done_reason": "stop", "prompt_eval_count": 100, "eval_count": 10},
        ],
        [
            {"message": {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c2", "function": {"name": "edit_file",
                                          "arguments": {"path": "s.md", "old_text": "- [ ] Task A",
                                                        "new_text": "- [x] Task A"}}},
                {"id": "c3", "function": {"name": "finish", "arguments": {"summary": "All ticked."}}},
            ]}, "done": True, "done_reason": "stop", "prompt_eval_count": 150, "eval_count": 20},
        ],
    ]
    with _FakeOllamaServer(turns) as server:
        monkeypatch.setenv("OLLAMA_API_BASE", server.url)
        ex = OllamaExecutor(ExecutorSpec(name="ollama", adapter="ollama", tier=Tier.LOCAL, model="qwen3-coder:30b"))
        result = ex.execute(_ollama_request(work), lambda line: None)

    assert result.exit_code == 0
    assert "All ticked." in result.summary
    assert "- [x] Task A" in (work / "s.md").read_text()
    tool_msgs = [m for m in server.chat_requests[1]["messages"] if m.get("role") == "tool"]
    assert "unresolved checkboxes" in tool_msgs[0]["content"].lower()


def test_ollama_executor_allows_rerunning_tests_after_edits(tmp_path: Path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    (work / "a.txt").write_text("v0\n")
    turns = [
        [
            {"message": {"role": "assistant", "content": "", "tool_calls": [
                {"id": f"e{i}", "function": {"name": "edit_file",
                                             "arguments": {"path": "a.txt", "old_text": f"v{i}",
                                                           "new_text": f"v{i+1}"}}},
                {"id": f"r{i}", "function": {"name": "run_command",
                                             "arguments": {"command": f"{sys.executable} -c \"pass\""}}},
            ]}, "done": True, "done_reason": "stop", "prompt_eval_count": 100, "eval_count": 20}
        ]
        for i in range(4)
    ] + [[
        {"message": {"role": "assistant", "content": "", "tool_calls": [
            {"id": "fin", "function": {"name": "finish", "arguments": {"summary": "Passed after 4 edits."}}},
        ]}, "done": True, "done_reason": "stop", "prompt_eval_count": 120, "eval_count": 10}
    ]]
    with _FakeOllamaServer(turns) as server:
        monkeypatch.setenv("OLLAMA_API_BASE", server.url)
        ex = OllamaExecutor(ExecutorSpec(name="ollama", adapter="ollama", tier=Tier.LOCAL, model="qwen3-coder:30b"))
        result = ex.execute(_ollama_request(work), LoopDetector(window=20, threshold=3))

    assert result.exit_code == 0
    assert result.killed_reason is None
    assert "Passed after 4 edits." in result.summary


def test_ollama_executor_finish_rejects_modified_spec_prose(tmp_path: Path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    orig = "# Spec\n\n- [ ] Build endpoint with PATCH support\n"
    (work / "s.md").write_text(orig)
    sha = normalized_spec_hash(orig)
    turns = [
        [
            {"message": {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "function": {"name": "edit_file",
                                          "arguments": {"path": "s.md",
                                                        "old_text": "- [ ] Build endpoint with PATCH support",
                                                        "new_text": "- [x] Build endpoint"}}},
                {"id": "c2", "function": {"name": "finish", "arguments": {"summary": "Shortened spec."}}},
            ]}, "done": True, "done_reason": "stop", "prompt_eval_count": 100, "eval_count": 10},
        ],
        [
            {"message": {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c3", "function": {"name": "edit_file",
                                          "arguments": {"path": "s.md",
                                                        "old_text": "- [x] Build endpoint",
                                                        "new_text": "- [x] Build endpoint with PATCH support"}}},
                {"id": "c4", "function": {"name": "finish", "arguments": {"summary": "Restored prose."}}},
            ]}, "done": True, "done_reason": "stop", "prompt_eval_count": 150, "eval_count": 20},
        ],
    ]
    with _FakeOllamaServer(turns) as server:
        monkeypatch.setenv("OLLAMA_API_BASE", server.url)
        ex = OllamaExecutor(ExecutorSpec(name="ollama", adapter="ollama", tier=Tier.LOCAL, model="qwen3-coder:30b"))
        result = ex.execute(_ollama_request(work, approved_spec_sha=sha), lambda line: None)

    assert result.exit_code == 0
    assert "Restored prose." in result.summary
    tool_msgs = [m for m in server.chat_requests[1]["messages"] if m.get("role") == "tool"]
    assert "modified beyond its checkboxes" in tool_msgs[1]["content"].lower()


def test_ollama_executor_compacts_old_tool_outputs_near_context_limit(tmp_path: Path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    (work / "big.txt").write_text(("x" * 75 + "\n") * 8)
    turns = [
        [
            {"message": {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "function": {"name": "read_file", "arguments": {"path": "big.txt"}}},
            ]}, "done": True, "done_reason": "stop", "prompt_eval_count": 800, "eval_count": 10},
        ],
        [
            {"message": {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c2", "function": {"name": "list_files", "arguments": {"path": "."}}},
            ]}, "done": True, "done_reason": "stop", "prompt_eval_count": 820, "eval_count": 10},
        ],
        [
            {"message": {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c3", "function": {"name": "list_files", "arguments": {"path": "."}}},
            ]}, "done": True, "done_reason": "stop", "prompt_eval_count": 830, "eval_count": 10},
        ],
        [
            {"message": {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c4", "function": {"name": "list_files", "arguments": {"path": "."}}},
            ]}, "done": True, "done_reason": "stop", "prompt_eval_count": 840, "eval_count": 10},
        ],
        [
            {"message": {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c5", "function": {"name": "finish", "arguments": {"summary": "Compacted cleanly."}}},
            ]}, "done": True, "done_reason": "stop", "prompt_eval_count": 500, "eval_count": 10},
        ],
    ]
    with _FakeOllamaServer(turns) as server:
        monkeypatch.setenv("OLLAMA_API_BASE", server.url)
        ex = OllamaExecutor(ExecutorSpec(
            name="ollama", adapter="ollama", tier=Tier.LOCAL, model="qwen3-coder:30b",
            args=("--num-ctx", "1000"),
        ))
        result = ex.execute(_ollama_request(work), lambda line: None)

    assert result.exit_code == 0
    assert "Compacted cleanly." in result.summary
    final_msgs = server.chat_requests[-1]["messages"]
    assert final_msgs[0]["role"] == "system"
    assert final_msgs[1]["role"] == "user"
    first_tool = next(m for m in final_msgs if m.get("role") == "tool")
    assert "earlier read_file output omitted" in first_tool["content"].lower()
    assert final_msgs[-1]["role"] == "tool"
    assert "big.txt" in final_msgs[-1]["content"]


def test_ollama_executor_finish_rejects_failing_checks(tmp_path: Path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    check_cmd = f'{sys.executable} -c "import pathlib, sys; sys.exit(0 if pathlib.Path(\'ok.txt\').exists() else 1)"'
    turns = [
        [
            {"message": {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "function": {"name": "finish", "arguments": {"summary": "Claiming done early."}}},
            ]}, "done": True, "done_reason": "stop", "prompt_eval_count": 100, "eval_count": 10},
        ],
        [
            {"message": {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c2", "function": {"name": "write_file", "arguments": {"path": "ok.txt", "content": "ok\n"}}},
                {"id": "c3", "function": {"name": "finish", "arguments": {"summary": "Actually verified."}}},
            ]}, "done": True, "done_reason": "stop", "prompt_eval_count": 150, "eval_count": 20},
        ],
    ]
    with _FakeOllamaServer(turns) as server:
        monkeypatch.setenv("OLLAMA_API_BASE", server.url)
        ex = OllamaExecutor(ExecutorSpec(name="ollama", adapter="ollama", tier=Tier.LOCAL, model="qwen3-coder:30b"))
        result = ex.execute(_ollama_request(work, checks=(check_cmd,)), lambda line: None)

    assert result.exit_code == 0
    assert "Actually verified." in result.summary
    tool_msgs = [m for m in server.chat_requests[1]["messages"] if m.get("role") == "tool"]
    assert "check failed" in tool_msgs[0]["content"].lower()


def test_ollama_executor_finish_skips_recheck_without_edits_and_hints_silent_failure(tmp_path: Path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    counter = work / "runs.txt"
    check_cmd = (
        f'{sys.executable} -c "import pathlib, sys; '
        'p=pathlib.Path(\'runs.txt\'); '
        'p.write_text((p.read_text() if p.exists() else \'\') + \'x\'); '
        'sys.exit(0 if pathlib.Path(\'ok.txt\').exists() else 1)"'
    )
    turns = [
        [
            {"message": {"role": "assistant", "content": "", "tool_calls": [
                {"id": "f1", "function": {"name": "finish", "arguments": {"summary": "Try 1."}}},
            ]}, "done": True, "done_reason": "stop", "prompt_eval_count": 100, "eval_count": 10},
        ],
        [
            {"message": {"role": "assistant", "content": "", "tool_calls": [
                {"id": "f2", "function": {"name": "finish", "arguments": {"summary": "Try 2 without edits."}}},
            ]}, "done": True, "done_reason": "stop", "prompt_eval_count": 120, "eval_count": 10},
        ],
        [
            {"message": {"role": "assistant", "content": "", "tool_calls": [
                {"id": "w1", "function": {"name": "write_file", "arguments": {"path": "ok.txt", "content": "ok\n"}}},
                {"id": "f3", "function": {"name": "finish", "arguments": {"summary": "Try 3 after edit."}}},
            ]}, "done": True, "done_reason": "stop", "prompt_eval_count": 140, "eval_count": 10},
        ],
    ]
    with _FakeOllamaServer(turns) as server:
        monkeypatch.setenv("OLLAMA_API_BASE", server.url)
        ex = OllamaExecutor(ExecutorSpec(name="ollama", adapter="ollama", tier=Tier.LOCAL, model="qwen3-coder:30b"))
        result = ex.execute(_ollama_request(work, checks=(check_cmd,)), lambda line: None)

    assert result.exit_code == 0
    assert counter.read_text() == "xx"
    tool_msgs = [m for m in server.chat_requests[2]["messages"] if m.get("role") == "tool"]
    assert "no output" in tool_msgs[0]["content"].lower()
    assert "no files were edited" in tool_msgs[1]["content"].lower()


def test_ollama_executor_compacts_old_write_edit_tool_call_args(tmp_path: Path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    huge = "line\n" * 100
    turns = [
        [
            {"message": {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "function": {"name": "write_file", "arguments": {"path": "big.go", "content": huge}}},
            ]}, "done": True, "done_reason": "stop", "prompt_eval_count": 800, "eval_count": 10},
        ],
        [
            {"message": {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c2", "function": {"name": "list_files", "arguments": {"path": "."}}},
            ]}, "done": True, "done_reason": "stop", "prompt_eval_count": 820, "eval_count": 10},
        ],
        [
            {"message": {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c3", "function": {"name": "list_files", "arguments": {"path": "."}}},
            ]}, "done": True, "done_reason": "stop", "prompt_eval_count": 830, "eval_count": 10},
        ],
        [
            {"message": {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c4", "function": {"name": "list_files", "arguments": {"path": "."}}},
            ]}, "done": True, "done_reason": "stop", "prompt_eval_count": 840, "eval_count": 10},
        ],
        [
            {"message": {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c5", "function": {"name": "finish", "arguments": {"summary": "Compacted args."}}},
            ]}, "done": True, "done_reason": "stop", "prompt_eval_count": 500, "eval_count": 10},
        ],
    ]
    with _FakeOllamaServer(turns) as server:
        monkeypatch.setenv("OLLAMA_API_BASE", server.url)
        ex = OllamaExecutor(ExecutorSpec(
            name="ollama", adapter="ollama", tier=Tier.LOCAL, model="qwen3-coder:30b",
            args=("--num-ctx", "1000"),
        ))
        result = ex.execute(_ollama_request(work), lambda line: None)

    assert result.exit_code == 0
    final_msgs = server.chat_requests[-1]["messages"]
    first_assistant = next(m for m in final_msgs if m.get("role") == "assistant")
    compacted_args = first_assistant["tool_calls"][0]["function"]["arguments"]
    assert compacted_args["path"] == "big.go"
    assert "omitted" in compacted_args["content"].lower()


def test_ollama_model_probe_catalog_vram_tok_s_and_tool_calling(monkeypatch):
    turns = [
        [
            {
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {
                            "id": "p1",
                            "function": {
                                "name": "write_file",
                                "arguments": {"path": "probe.txt", "content": "ok"},
                            },
                        }
                    ],
                },
                "done": True,
                "eval_count": 100,
                "eval_duration": 2_000_000_000,
            }
        ],
        [
            {
                "message": {"role": "assistant", "content": "I will write probe.txt manually."},
                "done": True,
                "eval_count": 50,
                "eval_duration": 1_000_000_000,
            }
        ],
    ]
    loaded = [
        {"name": "qwen3-coder:30b", "size": 20_000_000_000, "size_vram": 18_000_000_000},
        {"name": "plain:7b", "size": 8_000_000_000, "size_vram": 4_000_000_000},
    ]
    with _FakeOllamaServer(turns, loaded_models=loaded, tags=["qwen3-coder:30b", "plain:7b"]) as server:
        monkeypatch.setenv("OLLAMA_API_BASE", server.url)
        probe = OllamaModelProbe()
        assert probe.list_models() == ["qwen3-coder:30b", "plain:7b"]

        r1 = probe.probe("qwen3-coder:30b", num_ctx=32768)
        assert r1.reachable is True
        assert r1.tool_calling_ok is True
        assert r1.tok_per_s == pytest.approx(50.0)
        assert r1.vram_bytes == 18_000_000_000
        assert r1.total_bytes == 20_000_000_000
        assert r1.vram_ratio == pytest.approx(0.9)
        assert server.chat_requests[0]["stream"] is False
        assert server.chat_requests[0]["options"]["num_ctx"] == 32768
        assert any(
            c["model"] == "qwen3-coder:30b" and c.get("options", {}).get("num_ctx") == 32768
            for c in server.generate_calls
        )

        r2 = probe.probe("plain:7b", num_ctx=16384)
        assert r2.reachable is True
        assert r2.tool_calling_ok is False
        assert r2.vram_ratio == pytest.approx(0.5)


def test_ollama_model_probe_handles_unreachable_server(monkeypatch):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    monkeypatch.setenv("OLLAMA_API_BASE", f"http://127.0.0.1:{port}")
    probe = OllamaModelProbe()
    with pytest.raises(RuntimeError, match="unreachable"):
        probe.list_models()
    res = probe.probe("missing:7b")
    assert res.reachable is False
    assert res.tool_calling_ok is False
    assert res.error
