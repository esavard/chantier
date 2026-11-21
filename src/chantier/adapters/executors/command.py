"""Generic executor: any command line. Useful for custom harnesses and for tests.

The prompt is written to `.chantier-prompt.md` in the worktree (excluded from
git) and passed in the CHANTIER_PROMPT_FILE, CHANTIER_SPEC and
CHANTIER_PHASE environment variables. Optional usage reporting: print a line
`CHANTIER_USAGE {"input_tokens": 1, "output_tokens": 2}`.
"""

from __future__ import annotations

import json

from chantier.adapters.executors.process import ProcessExecutor
from chantier.application.ports import ExecutionRequest
from chantier.domain.model import Usage

PROMPT_FILE = ".chantier-prompt.md"


class CommandExecutor(ProcessExecutor):
    def before_run(self, request: ExecutionRequest) -> None:
        (request.workdir / PROMPT_FILE).write_text(request.prompt, encoding="utf-8")
        self._env = {
            "CHANTIER_PROMPT_FILE": str(request.workdir / PROMPT_FILE),
            "CHANTIER_SPEC": request.ticket.spec_path,
            "CHANTIER_PHASE": request.phase.value,
            "CHANTIER_ATTEMPT": str(request.ticket.attempt + 1),
        }

    def environment(self) -> dict[str, str]:
        return getattr(self, "_env", {})

    def command(self, request: ExecutionRequest) -> list[str]:
        if not self.spec.args:
            raise ValueError(f"executor {self.spec.name}: 'args' must hold the command to run")
        return list(self.spec.args)

    def interpret(self, line: str, usage: Usage) -> str | None:
        if line.startswith("CHANTIER_USAGE "):
            data = json.loads(line.removeprefix("CHANTIER_USAGE "))
            usage.input_tokens += int(data.get("input_tokens", 0))
            usage.output_tokens += int(data.get("output_tokens", 0))
            usage.cache_read_tokens += int(data.get("cache_read_tokens", 0))
            if "cost_usd" in data:
                usage.reported_cost_usd = (usage.reported_cost_usd or 0) + float(data["cost_usd"])
            return None
        return line
