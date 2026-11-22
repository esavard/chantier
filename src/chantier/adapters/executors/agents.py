"""Executor adapters for real agent CLIs.

Output formats of these CLIs change between versions. Parsing is tolerant:
an unknown event is logged and ignored, and usage that cannot be read stays
at zero (the metrics report then undercounts, it never crashes a run).
Extra flags come from the executor's `args` in the factory config, so a CLI
change is a config edit, not a code change.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from urllib.error import URLError

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
        item = event.get("item") or {}
        if event.get("type") == "item.completed":
            if item.get("type") == "agent_message":
                return item.get("text")
            if item.get("type") == "command_execution":
                return f"command {item.get('command')}"
        return None


_AIDER_TOKENS = re.compile(r"Tokens:\s*([\d.,]+)(k?)\s*sent,\s*([\d.,]+)(k?)\s*received", re.IGNORECASE)


class AiderOllamaExecutor(ProcessExecutor):
    """aider driving a local Ollama model (SovereignSpecAI's execution layer).

    The spec file is passed as an editable file so the agent can tick its boxes.
    The Ollama host comes from OLLAMA_API_BASE (aider's own variable), so the GPU
    can be another machine on the network.
    """

    @property
    def ollama_url(self) -> str:
        return os.environ.get("OLLAMA_API_BASE", "http://localhost:11434").rstrip("/")

    def command(self, request: ExecutionRequest) -> list[str]:
        cmd = [
            "aider", "--model", self.spec.model, "--message", request.prompt,
            "--yes-always", "--no-auto-commits", "--no-pretty", "--no-stream",
        ]
        return cmd + list(self.spec.args) + [request.ticket.spec_path]

    def environment(self) -> dict[str, str]:
        return {"OLLAMA_API_BASE": self.ollama_url, "AIDER_NO_COLORS": "1", "PYTHONUNBUFFERED": "1"}

    def before_run(self, request: ExecutionRequest) -> None:
        """Load the model in VRAM, unloading any other one first (16 GB cards hold one model)."""
        name = self.spec.model.split("/", 1)[-1]
        try:
            loaded = _post_get(f"{self.ollama_url}/api/ps").get("models", [])
        except URLError:
            return  # let aider report the real connection error
        if any(m.get("name") == name for m in loaded):
            return
        for model in loaded:
            _post(f"{self.ollama_url}/api/generate", {"model": model["name"], "keep_alive": 0, "prompt": ""})
        _post(f"{self.ollama_url}/api/generate", {"model": name, "keep_alive": -1, "prompt": ""})

    def interpret(self, line: str, usage: Usage) -> str | None:
        if m := _AIDER_TOKENS.search(line):
            usage.input_tokens += _count(m.group(1), m.group(2))
            usage.output_tokens += _count(m.group(3), m.group(4))
            return None
        return line


def _count(number: str, suffix: str) -> int:
    value = float(number.replace(",", ""))
    return int(value * 1000) if suffix.lower() == "k" else int(value)


def _json(line: str) -> dict | None:
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def _post_get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read())


def _post(url: str, payload: dict) -> None:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=300) as resp:
        resp.read()
