"""Shared machinery: run an agent CLI as a subprocess under the watchdog.

Every output line is logged, parsed for usage by the adapter, and offered to
the supervisor's observer. The runner enforces the time-based signals itself:
no output for `idle_timeout_s`, or a run longer than `timeout_s`.
"""

from __future__ import annotations

import os
import queue
import signal
import subprocess
import threading
import time
from collections.abc import Sequence
from pathlib import Path

from chantier.application.ports import ExecutionRequest, ExecutionResult, LineObserver
from chantier.domain.model import Usage
from chantier.domain.routing import ExecutorSpec


class ProcessExecutor:
    """Subclasses build the command and interpret output lines."""

    def __init__(self, spec: ExecutorSpec) -> None:
        self.spec = spec

    # ------------------------------------------------------------------ hooks

    def command(self, request: ExecutionRequest) -> list[str]:
        raise NotImplementedError

    def environment(self) -> dict[str, str]:
        return {}

    def before_run(self, request: ExecutionRequest) -> None:
        """Optional preparation (e.g. load a local model in VRAM)."""

    def interpret(self, line: str, usage: Usage) -> str | None:
        """Update `usage` from a line; return the human-meaningful text it carries, if any."""
        return line

    # ------------------------------------------------------------------ run

    def execute(self, request: ExecutionRequest, observe: LineObserver) -> ExecutionResult:
        try:
            self.before_run(request)
        except Exception as exc:  # noqa: BLE001
            return ExecutionResult(exit_code=127, summary=f"preparation failed: {exc}")
        return run_process(
            self.command(request), request.workdir, request.log_path,
            env={**os.environ, **self.environment()},
            timeout_s=self.spec.timeout_s, idle_timeout_s=self.spec.idle_timeout_s,
            interpret=self.interpret, observe=observe,
        )


def run_process(
    command: Sequence[str],
    cwd: Path,
    log_path: Path,
    env: dict[str, str],
    timeout_s: int,
    idle_timeout_s: int,
    interpret,
    observe: LineObserver,
) -> ExecutionResult:
    usage = Usage()
    texts: list[str] = []
    try:
        proc = subprocess.Popen(
            list(command), cwd=cwd, env=env, text=True, bufsize=1,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=True,  # own process group: the watchdog kills the whole tree
        )
    except FileNotFoundError as exc:
        return ExecutionResult(exit_code=127, summary=f"executable not found: {exc.filename}")

    lines: queue.Queue[str | None] = queue.Queue()

    def pump() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.put(line)
        lines.put(None)

    threading.Thread(target=pump, daemon=True).start()
    started = last_output = time.monotonic()
    killed: str | None = None
    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"$ {' '.join(command[:3])} ...\n")
        while True:
            try:
                line = lines.get(timeout=1.0)
            except queue.Empty:
                line = ""
            now = time.monotonic()
            if line is None:
                break
            if line:
                last_output = now
                log.write(line)
                log.flush()
                text = interpret(line.rstrip("\n"), usage)
                if text:
                    texts.append(text)
                    if reason := observe(text):
                        killed = reason
            if killed is None and now - last_output > idle_timeout_s:
                killed = f"no output for {idle_timeout_s} s"
            if killed is None and now - started > timeout_s:
                killed = f"run exceeded {timeout_s} s"
            if killed:
                log.write(f"\n[chantier] killed: {killed}\n")
                _kill(proc)
                break
    exit_code = proc.wait()
    return ExecutionResult(
        exit_code=exit_code,
        usage=usage,
        summary="\n".join(texts[-40:]),
        killed_reason=killed,
    )


def _kill(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=10)
    except (ProcessLookupError, subprocess.TimeoutExpired):
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
