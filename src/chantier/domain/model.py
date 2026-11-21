"""Core domain model: projects, tickets, runs, attention items.

Pure data and rules. No I/O here (enforced by import-linter).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

# Bumped when the workflow (stages, gates, transition rules) changes in a way an
# in-flight ticket could notice. Each ticket records the version it started with,
# so upgrading the factory never silently changes the rules under a running ticket.
WORKFLOW_VERSION = 1


class Stage(StrEnum):
    DRAFT = "draft"                    # spec exists, waiting for the spec-approval gate
    READY = "ready"                    # approved; waits for its dependencies and a free seat
    RUNNING = "running"                # an executor is working on it
    REVIEWING = "reviewing"            # optional review run on the verified branch
    AWAITING_MERGE = "awaiting_merge"  # branch delivered; waiting for the merge gate
    DONE = "done"
    BLOCKED = "blocked"                # needs a human: escalation exhausted, error, budget
    CANCELLED = "cancelled"


TERMINAL_STAGES = frozenset({Stage.DONE, Stage.CANCELLED})


class TicketKind(StrEnum):
    PLAN = "plan"            # greenfield: blueprint -> specs (output is spec files)
    IMPLEMENT = "implement"  # one spec -> code


class Phase(StrEnum):
    """What a run does. Routing is keyed on (kind, phase)."""

    PLAN = "plan"
    IMPLEMENT = "implement"
    REVIEW = "review"


class GateMode(StrEnum):
    HUMAN = "human"
    AUTO = "auto"


class Tier(StrEnum):
    LOCAL = "local"
    FRONTIER = "frontier"


class RunOutcome(StrEnum):
    SUCCEEDED = "succeeded"        # executor finished and verification passed
    FAILED_CHECKS = "failed_checks"
    KILLED = "killed"              # stopped by the supervisor watchdog
    ERROR = "error"                # executor could not run / crashed
    REJECTED = "rejected"          # review verdict asked for changes


class AttentionKind(StrEnum):
    GATE_WAITING = "gate_waiting"
    ESCALATION_EXHAUSTED = "escalation_exhausted"
    BUDGET_EXCEEDED = "budget_exceeded"
    SCOPE_WARNING = "scope_warning"
    EXECUTOR_ERROR = "executor_error"


@dataclass(frozen=True)
class Gates:
    spec_approval: GateMode = GateMode.HUMAN
    merge: GateMode = GateMode.HUMAN


@dataclass(frozen=True)
class ProjectSettings:
    """Per-project settings, loaded from the project's YAML file outside the target repo."""

    name: str
    repo: str                       # git URL or local path of the deliverable repository
    base_branch: str = "main"
    pack: str = "generic"
    specs_dir: str = "docs/specs"
    blueprint_path: str = "docs/blueprint.md"
    gates: Gates = field(default_factory=Gates)
    checks: tuple[str, ...] = ()    # commands run in the worktree; all must exit 0
    review: bool = False            # run a review phase after verification
    scope_mode: str = "warn"        # "warn" | "enforce" | "off"
    scope_allow: tuple[str, ...] = ("docs/", "test/", "tests/")
    frontier_budget_usd_per_day: float | None = None
    notes_file: str = "NOTES.md"


@dataclass
class Ticket:
    id: str                         # "<project>/<spec stem>" e.g. "calc/spec_003_history"
    project: str
    kind: TicketKind
    title: str
    spec_path: str                  # path inside the target repo
    stage: Stage = Stage.DRAFT
    depends_on: tuple[str, ...] = ()  # ticket ids
    revision: int = 0
    attempt: int = 0                # implementation attempts so far (all executors)
    ladder_step: int = 0            # position on the routing ladder (escalation level)
    rung_attempts: int = 0          # failed attempts on the current rung
    fresh_start: bool = False       # next attempt restarts from base (set on escalation)
    approved_spec_sha: str | None = None  # hash of the spec text (checkboxes normalized) at approval
    branch: str | None = None
    workflow_version: int = WORKFLOW_VERSION
    feedback: str = ""              # verification/review feedback for the next attempt
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class Transition:
    ticket_id: str
    from_stage: Stage
    to_stage: Stage
    cause: str
    actor: str                      # "human", "scheduler", "supervisor", executor name...
    at: datetime


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    reported_cost_usd: float | None = None  # what the provider/CLI reported, if anything

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_read_tokens


@dataclass
class Run:
    ticket_id: str
    executor: str
    tier: Tier
    model: str
    phase: Phase
    attempt: int
    started_at: datetime
    id: int | None = None
    finished_at: datetime | None = None
    outcome: RunOutcome | None = None
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0           # money actually spent (frontier) or energy (local)
    detail: str = ""                # why it failed / summary
    log_path: str | None = None

    @property
    def duration_s(self) -> float:
        if self.finished_at is None:
            return 0.0
        return (self.finished_at - self.started_at).total_seconds()


@dataclass
class AttentionItem:
    kind: AttentionKind
    project: str
    message: str
    ticket_id: str | None = None
    id: int | None = None
    created_at: datetime | None = None
    resolved_at: datetime | None = None
