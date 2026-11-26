"""Ports: what the application needs from the outside world.

Every integration (state store, executors, git, notifications, external
trackers, dashboard sync) is an adapter behind one of these protocols. None of
them exists in the domain; swapping Jira for GitHub Issues, or SQLite for a
cloud store, never touches the scheduler.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol

from chantier.domain.model import (
    AttentionItem,
    Phase,
    Run,
    Stage,
    Ticket,
    Transition,
    Usage,
)
from chantier.domain.routing import ExecutorSpec


class StateStore(Protocol):
    """Source of truth for factory state. Lives with the factory, never in the target repo."""

    def get_ticket(self, ticket_id: str) -> Ticket | None: ...
    def list_tickets(self, project: str | None = None) -> list[Ticket]: ...
    def insert_ticket(self, ticket: Ticket) -> None: ...

    def save_ticket(self, ticket: Ticket, expected_revision: int) -> Ticket:
        """Persist non-stage fields. Raises RevisionConflict on a stale revision."""
        ...

    def transition(
        self, ticket: Ticket, to_stage: Stage, *, expected_revision: int, cause: str, actor: str
    ) -> Ticket:
        """Change stage atomically with an audit record. Raises RevisionConflict."""
        ...

    def transitions(self, ticket_id: str) -> list[Transition]: ...

    def add_run(self, run: Run) -> Run: ...
    def finish_run(self, run: Run) -> None: ...
    def list_runs(self, project: str | None = None, since: datetime | None = None) -> list[Run]: ...
    def running_runs(self) -> list[Run]: ...

    def raise_attention(self, item: AttentionItem) -> AttentionItem | None:
        """Insert unless an identical unresolved item exists; returns None if deduplicated."""
        ...

    def list_attention(self, open_only: bool = True) -> list[AttentionItem]: ...
    def resolve_attention(self, item_id: int) -> None: ...
    def resolve_attention_for(self, ticket_id: str) -> None: ...

    def get_flag(self, name: str) -> str | None: ...
    def set_flag(self, name: str, value: str | None) -> None: ...


@dataclass
class ExecutionRequest:
    ticket: Ticket
    phase: Phase
    workdir: Path
    prompt: str
    log_path: Path
    checks: tuple[str, ...] = ()


@dataclass
class ExecutionResult:
    exit_code: int
    usage: Usage = field(default_factory=Usage)
    summary: str = ""
    killed_reason: str | None = None  # set when the watchdog stopped the process


class LineObserver(Protocol):
    """Fed every output line of a running executor. Returns a reason to kill, or None."""

    def __call__(self, line: str) -> str | None: ...


class Executor(Protocol):
    spec: ExecutorSpec

    def execute(self, request: ExecutionRequest, observe: LineObserver) -> ExecutionResult: ...


class Workspace(Protocol):
    """Git operations on the deliverable repository, from the factory's own clone."""

    def sync(self, project: str, repo: str, base_branch: str) -> Path:
        """Ensure an up-to-date mirror clone; return its path (base branch checked out)."""
        ...

    def read_base_file(self, project: str, base_branch: str, path: str) -> str | None: ...
    def list_base_files(self, project: str, base_branch: str, directory: str) -> list[str]: ...

    def prepare_worktree(self, project: str, ticket: Ticket, base_branch: str, reset: bool = False) -> Path:
        """Worktree on the ticket's branch: created from base, reused for a retry,
        or reset to base when `reset` (a fresh start after an escalation)."""
        ...

    def commit_all(self, worktree: Path, message: str) -> bool: ...
    def changed_files(self, worktree: Path, base_branch: str) -> list[str]: ...
    def diff(self, worktree: Path, base_branch: str) -> str: ...
    def read_file(self, worktree: Path, path: str) -> str | None: ...
    def deliver(self, project: str, worktree: Path, branch: str) -> None:
        """Push the ticket branch to the deliverable repository."""
        ...

    def merge(self, project: str, branch: str, base_branch: str) -> None:
        """Merge the ticket branch into base and push (used when the merge gate is auto)."""
        ...

    def is_merged(self, project: str, branch: str, base_branch: str) -> bool:
        """True once the branch tip is reachable from the remote base branch."""
        ...

    def remove_worktree(self, project: str, ticket: Ticket) -> None: ...


class CommandRunner(Protocol):
    def run(self, command: str, cwd: Path, timeout_s: int) -> tuple[int, str]: ...


class Notifier(Protocol):
    """Tells the human in charge, only when a human is required."""

    def notify(self, item: AttentionItem) -> None: ...


class TrackerMirror(Protocol):
    """Outbound mirror to an external tracker (Jira, GitHub Issues...). The factory stays the owner of state."""

    def publish(self, ticket: Ticket, transition: Transition | None) -> None: ...


class EventSink(Protocol):
    """Outbound event stream for a remote dashboard or cloud sync."""

    def emit(self, kind: str, payload: dict) -> None: ...


Clock = Callable[[], datetime]


def fan_out(targets: Iterable[Callable[[], None]]) -> list[Exception]:
    """Run best-effort side effects; an outage of one integration never blocks the factory."""
    errors = []
    for target in targets:
        try:
            target()
        except Exception as exc:  # noqa: BLE001 - integrations are best effort by design
            errors.append(exc)
    return errors
