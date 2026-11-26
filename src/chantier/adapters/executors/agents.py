"""Executor adapters for real agent CLIs.

Output formats of these CLIs change between versions. Parsing is tolerant:
an unknown event is logged and ignored, and usage that cannot be read stays
at zero (the metrics report then undercounts, it never crashes a run).
Extra flags come from the executor's `args` in the factory config, so a CLI
change is a config edit, not a code change.
"""

from __future__ import annotations

import json

from chantier.adapters.executors.process import ProcessExecutor
from chantier.application.ports import ExecutionRequest
from chantier.domain.model import Usage


class ClaudeCodeExecutor(ProcessExecutor):
    """Claude Code in headless mode (`claude -p`), with stream-json output.

    Goes through the user's Claude Code installation and plan, not the raw API.
    """

    def command(self, request: ExecutionRequest) -> list[str]:
        cmd = ["claude", "-p", request.prompt, "--output-format", "stream-json", "--verbose"]
        if self.spec.model:
            cmd += ["--model", self.spec.model]
        return cmd + list(self.spec.args)

    def interpret(self, line: str, usage: Usage) -> str | None:
        event = _json(line)
        if event is None:
            return line
        kind = event.get("type")
        if kind == "assistant":
            parts = []
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    parts.append(f"tool {block.get('name')} {json.dumps(block.get('input'), sort_keys=True)[:300]}")
            return "\n".join(p for p in parts if p) or None
        if kind == "result":
            u = event.get("usage") or {}
            usage.input_tokens += int(u.get("input_tokens", 0)) + int(u.get("cache_creation_input_tokens", 0))
            usage.output_tokens += int(u.get("output_tokens", 0))
            usage.cache_read_tokens += int(u.get("cache_read_input_tokens", 0))
            if event.get("total_cost_usd") is not None:
                usage.reported_cost_usd = float(event["total_cost_usd"])
            return event.get("result") or None
        return None


class CodexExecutor(ProcessExecutor):
    """OpenAI Codex CLI (`codex exec --json`)."""

    def command(self, request: ExecutionRequest) -> list[str]:
        cmd = ["codex", "exec", "--json", "-C", str(request.workdir)]
        if self.spec.model:
            cmd += ["-m", self.spec.model]
        return cmd + list(self.spec.args) + [request.prompt]

    def interpret(self, line: str, usage: Usage) -> str | None:
        event = _json(line)
        if event is None:
            return line
        if event.get("type") == "turn.completed":
            u = event.get("usage") or {}
            cached = int(u.get("cached_input_tokens", 0))
            usage.input_tokens += max(0, int(u.get("input_tokens", 0)) - cached)
            usage.cache_read_tokens += cached
            usage.output_tokens += int(u.get("output_tokens", 0))
            return None
        if event.get("type") in ("error", "turn.failed"):
            # Without this the reason of a failed run (auth, quota) would vanish from the summary.
            message = event.get("message") or (event.get("error") or {}).get("message")
            return f"codex error: {message}" if message else None
        item = event.get("item") or {}
        if event.get("type") == "item.completed":
            if item.get("type") == "agent_message":
                return item.get("text")
            if item.get("type") == "command_execution":
                return f"command {item.get('command')}"
        return None


class AntigravityExecutor(ProcessExecutor):
    """Google Antigravity CLI (`agy -p --output-format stream-json`)."""

    def command(self, request: ExecutionRequest) -> list[str]:
        cmd = ["agy", "--output-format", "stream-json"]
        if self.spec.model:
            cmd += ["--model", self.spec.model]
        return cmd + list(self.spec.args) + ["-p", request.prompt]

    def interpret(self, line: str, usage: Usage) -> str | None:
        event = _json(line)
        if event is None:
            return line
        kind = event.get("event")
        if kind == "step_update":
            step = event.get("step_update") or {}
            if step.get("step_type") == "tool" and step.get("state") == "ACTIVE":
                info = step.get("tool_info") or {}
                name = step.get("tool_name") or info.get("name")
                params = info.get("parameters")
                return f"tool {name} {json.dumps(params, sort_keys=True)[:300]}"
            return None
        if kind == "result":
            res = event.get("result") or {}
            u = res.get("usage") or {}
            usage.input_tokens += int(u.get("input_tokens", 0))
            usage.output_tokens += int(u.get("output_tokens", 0))
            usage.cache_read_tokens += int(u.get("cache_read_tokens", 0))
            if res.get("total_cost_usd") is not None:
                usage.reported_cost_usd = float(res["total_cost_usd"])
            if res.get("status") == "ERROR" and res.get("error"):
                return res["error"]
            return res.get("response") or None
        return None


def _json(line: str) -> dict | None:
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None
