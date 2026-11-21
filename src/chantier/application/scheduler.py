"""Scheduler: picks ready tickets, routes them, runs attempts, follows the ladder.

One `tick()` reconciles merges, then dispatches every ready ticket whose
dependencies are done, as long as its executor has a free seat and the
frontier budget allows it. Parallelism comes from the dependency graph and
from mixing tiers (one local run while a frontier run works elsewhere).
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from chantier.application.board import Board
from chantier.application.ports import (
    CommandRunner,
    ExecutionRequest,
    ExecutionResult,
    Executor,
)
from chantier.application.supervisor import LoopDetector
from chantier.application.verification import Verdict, verify
from chantier.domain.economics import EnergyModel, Price, frontier_run_cost
from chantier.domain.model import (
    AttentionKind,
    GateMode,
    Phase,
    Run,
    RunOutcome,
    Stage,
    Ticket,
    TicketKind,
    Tier,
)
from chantier.domain.routing import RoutingPolicy
from chantier.domain.workflow import RevisionConflict, unmet_dependencies

DRAIN_FLAG = "drain"
EXECUTOR_MISSING_EXIT = 127

PromptBuilder = Callable[[Ticket, Phase, Path], str]


@dataclass
class TickReport:
    merged: list[str] = field(default_factory=list)
    dispatched: list[tuple[str, str]] = field(default_factory=list)  # (ticket, executor)
    waiting: dict[str, str] = field(default_factory=dict)            # ticket -> reason
    recovered: list[str] = field(default_factory=list)


class Scheduler:
    def __init__(
        self,
        board: Board,
        executors: Mapping[str, Executor],
        routing: RoutingPolicy,
        runner: CommandRunner,
        prompts: PromptBuilder,
        logs_dir: Path,
        prices: Mapping[str, Price],
        energy: EnergyModel,
        max_workers: int = 8,
    ) -> None:
        self.board = board
        self.store = board.store
        self.workspace = board.workspace
        self.executors = executors
        self.routing = routing
        self.runner = runner
        self.prompts = prompts
        self.logs_dir = logs_dir
        self.prices = prices
        self.energy = energy
        self._pool = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="chantier")
        self._lock = threading.Lock()
        self._busy: dict[str, int] = {}
        self._futures: dict[str, Future] = {}

    # ------------------------------------------------------------------ lifecycle

    def recover(self) -> list[str]:
        """After a factory restart: runs left open have no process anymore."""
        recovered = []
        with self._lock:
            active = set(self._futures)
        for run in self.store.running_runs():
            if run.ticket_id in active:
                continue
            run.finished_at = _now()
            run.outcome = RunOutcome.ERROR
            run.detail = "factory restarted during the run"
            self.store.finish_run(run)
            ticket = self.store.get_ticket(run.ticket_id)
            if ticket and ticket.stage in (Stage.RUNNING, Stage.REVIEWING):
                self.board.move(ticket, Stage.READY, "recovered after factory restart", "scheduler")
                recovered.append(ticket.id)
        return recovered

    def wait(self, timeout: float | None = None) -> None:
        with self._lock:
            futures = list(self._futures.values())
        for future in futures:
            future.result(timeout=timeout)

    def shutdown(self) -> None:
        self._pool.shutdown(wait=True)

    @property
    def draining(self) -> bool:
        return self.store.get_flag(DRAIN_FLAG) == "1"

    # ------------------------------------------------------------------ tick

    def tick(self) -> TickReport:
        report = TickReport()
        self._reconcile_merges(report)
        if self.draining:
            report.waiting["*"] = "draining: no new dispatch"
            return report
        tickets = self.store.list_tickets()
        stages = {t.id: t.stage for t in tickets}
        for ticket in sorted(tickets, key=lambda t: (t.created_at or _now(), t.id)):
            if ticket.stage != Stage.READY:
                continue
            with self._lock:
                if ticket.id in self._futures:
                    continue
            if unmet := unmet_dependencies(ticket, stages):
                report.waiting[ticket.id] = "waiting on " + ", ".join(unmet)
                continue
            phase = Phase.PLAN if ticket.kind == TicketKind.PLAN else Phase.IMPLEMENT
            name = self.routing.executor_for(ticket, phase)
            if name is None:
                self._block(ticket, AttentionKind.ESCALATION_EXHAUSTED,
                            "every executor on the routing ladder failed; human needed")
                continue
            executor = self.executors.get(name)
            if executor is None:
                self._block(ticket, AttentionKind.EXECUTOR_ERROR, f"executor '{name}' is not configured")
                continue
            if executor.spec.tier == Tier.FRONTIER and not self._budget_allows(ticket.project):
                report.waiting[ticket.id] = "frontier budget exceeded today"
                continue
            if not self._reserve(name, executor.spec.seats):
                report.waiting[ticket.id] = f"no free seat on {name}"
                continue
            try:
                ticket = self.board.move(ticket, Stage.RUNNING, f"dispatched to {name}", "scheduler")
            except RevisionConflict:
                self._release(name)
                continue
            stages[ticket.id] = Stage.RUNNING
            report.dispatched.append((ticket.id, name))
            future = self._pool.submit(self._attempt_guarded, ticket, phase, name)
            with self._lock:
                self._futures[ticket.id] = future
        return report

    # ------------------------------------------------------------------ attempt

    def _attempt_guarded(self, ticket: Ticket, phase: Phase, name: str) -> None:
        try:
            self._attempt(ticket, phase, name)
        except Exception as exc:  # noqa: BLE001 - never lose a ticket in RUNNING
            fresh = self.store.get_ticket(ticket.id)
            if fresh and fresh.stage in (Stage.RUNNING, Stage.REVIEWING):
                self._block(fresh, AttentionKind.EXECUTOR_ERROR, f"internal error: {exc!r}")
        finally:
            self._release(name)
            with self._lock:
                self._futures.pop(ticket.id, None)

    def _attempt(self, ticket: Ticket, phase: Phase, name: str) -> None:
        settings = self.board.settings(ticket.project)
        executor = self.executors[name]
        # A ticket that just climbed a rung starts from base: the stronger executor
        # should not inherit the mess of the one that failed.
        worktree = self.workspace.prepare_worktree(
            ticket.project, ticket, settings.base_branch, reset=ticket.fresh_start
        )
        if ticket.fresh_start:
            ticket.fresh_start = False
            ticket = self.store.save_ticket(ticket, ticket.revision)
        result, run = self._execute(ticket, phase, executor, worktree)

        self.workspace.commit_all(
            worktree, f"chantier: {ticket.id} attempt {ticket.attempt + 1} ({name})"
        )

        if result.exit_code == EXECUTOR_MISSING_EXIT and result.killed_reason is None:
            self._finish(run, RunOutcome.ERROR, result.summary)
            self._block(ticket, AttentionKind.EXECUTOR_ERROR, f"{name} could not start: {result.summary}")
            return
        if result.killed_reason:
            self._finish(run, RunOutcome.KILLED, result.killed_reason)
            self._fail(ticket, f"Previous attempt was stopped by the supervisor: {result.killed_reason}")
            return
        if result.exit_code != 0:
            self._finish(run, RunOutcome.ERROR, f"exit {result.exit_code}: {result.summary}")
            self._fail(ticket, f"Previous attempt exited with code {result.exit_code}.")
            return

        verdict: Verdict = verify(ticket, settings, self.workspace, self.runner, worktree)
        for warning in verdict.warnings:
            self.board.raise_attention(AttentionKind.SCOPE_WARNING, ticket, warning)
        if not verdict.ok:
            self._finish(run, RunOutcome.FAILED_CHECKS, verdict.feedback())
            self._fail(ticket, "Verification failed on the previous attempt:\n" + verdict.feedback())
            return
        self._finish(run, RunOutcome.SUCCEEDED, result.summary)

        if settings.review and ticket.kind == TicketKind.IMPLEMENT:
            ticket = self.board.move(self._fresh(ticket), Stage.REVIEWING, "verified; review requested", "scheduler")
            if not self._review(ticket, worktree):
                return
        self._deliver(self._fresh(ticket), worktree)

    def _execute(self, ticket: Ticket, phase: Phase, executor: Executor, worktree: Path) -> tuple[ExecutionResult, Run]:
        stem = ticket.id.split("/")[-1]
        name = f"{stem}-{phase.value}-{ticket.attempt + 1}-{executor.spec.name}.log"
        log_path = self.logs_dir / ticket.project / name
        log_path.parent.mkdir(parents=True, exist_ok=True)
        run = self.store.add_run(Run(
            ticket_id=ticket.id, executor=executor.spec.name, tier=executor.spec.tier,
            model=executor.spec.model, phase=phase, attempt=ticket.attempt + 1,
            started_at=_now(), log_path=str(log_path),
        ))
        request = ExecutionRequest(
            ticket=ticket, phase=phase, workdir=worktree,
            prompt=self.prompts(ticket, phase, worktree), log_path=log_path,
        )
        observer = LoopDetector() if executor.spec.tier == Tier.LOCAL else (lambda line: None)
        result = executor.execute(request, observer)
        run.usage = result.usage
        run.finished_at = _now()
        if executor.spec.tier == Tier.FRONTIER:
            run.cost_usd = frontier_run_cost(result.usage, self.prices.get(executor.spec.model))
        else:
            run.cost_usd = self.energy.cost(run.duration_s)
        return result, run

    def _review(self, ticket: Ticket, worktree: Path) -> bool:
        name = self.routing.executor_for(ticket, Phase.REVIEW)
        executor = self.executors.get(name or "")
        if executor is None:
            self._block(ticket, AttentionKind.EXECUTOR_ERROR, f"review executor '{name}' is not configured")
            return False
        result, run = self._execute(ticket, Phase.REVIEW, executor, worktree)
        verdict_line = _last_verdict(result.summary)
        if result.exit_code != 0 or result.killed_reason or verdict_line is None:
            self._finish(run, RunOutcome.ERROR, result.killed_reason or "review produced no VERDICT line")
            self._block(ticket, AttentionKind.EXECUTOR_ERROR, "review run failed; human needed")
            return False
        if verdict_line == "PASS":
            self._finish(run, RunOutcome.SUCCEEDED, "review passed")
            return True
        self._finish(run, RunOutcome.REJECTED, result.summary[-4000:])
        self._fail(ticket, "The reviewer requested changes:\n" + result.summary[-4000:])
        return False

    def _deliver(self, ticket: Ticket, worktree: Path) -> None:
        settings = self.board.settings(ticket.project)
        self.workspace.deliver(ticket.project, worktree, ticket.branch or "")
        ticket.feedback = ""
        ticket = self.store.save_ticket(ticket, ticket.revision)
        ticket = self.board.move(ticket, Stage.AWAITING_MERGE,
                                 f"verified; branch {ticket.branch} delivered", "scheduler")
        if settings.gates.merge == GateMode.AUTO:
            self.workspace.merge(ticket.project, ticket.branch or "", settings.base_branch)
            self.board.move(ticket, Stage.DONE, "merged automatically (merge gate: auto)", "policy")
            self.workspace.remove_worktree(ticket.project, ticket)

    # ------------------------------------------------------------------ outcomes

    def _fail(self, ticket: Ticket, feedback: str) -> None:
        ticket = self._fresh(ticket)
        escalated = self.routing.record_failure(ticket)
        ticket.feedback = feedback
        ticket = self.store.save_ticket(ticket, ticket.revision)
        phase = Phase.PLAN if ticket.kind == TicketKind.PLAN else Phase.IMPLEMENT
        if self.routing.executor_for(ticket, phase) is None:
            self._block(ticket, AttentionKind.ESCALATION_EXHAUSTED,
                        f"all executors failed after {ticket.attempt} attempts. Last feedback:\n{feedback[-1500:]}")
            return
        cause = "escalated to " + str(self.routing.executor_for(ticket, phase)) if escalated else "retry"
        self.board.move(ticket, Stage.READY, cause, "supervisor")

    def _block(self, ticket: Ticket, kind: AttentionKind, message: str) -> None:
        ticket = self._fresh(ticket)
        if ticket.stage != Stage.BLOCKED:
            ticket = self.board.move(ticket, Stage.BLOCKED, message.splitlines()[0][:200], "supervisor")
        self.board.raise_attention(kind, ticket, message)

    def _finish(self, run: Run, outcome: RunOutcome, detail: str) -> None:
        run.outcome = outcome
        run.detail = detail
        run.finished_at = run.finished_at or _now()
        self.store.finish_run(run)

    def _fresh(self, ticket: Ticket) -> Ticket:
        return self.store.get_ticket(ticket.id) or ticket

    # ------------------------------------------------------------------ merges, seats, budget

    def _reconcile_merges(self, report: TickReport) -> None:
        """Git is evidence: a branch a human merged elsewhere closes its ticket."""
        synced: set[str] = set()
        for ticket in self.store.list_tickets():
            if ticket.stage != Stage.AWAITING_MERGE or not ticket.branch:
                continue
            settings = self.board.settings(ticket.project)
            if ticket.project not in synced:
                self.workspace.sync(ticket.project, settings.repo, settings.base_branch)
                synced.add(ticket.project)
            if self.workspace.is_merged(ticket.project, ticket.branch, settings.base_branch):
                self.board.move(ticket, Stage.DONE, "branch found merged into base", "scheduler")
                self.workspace.remove_worktree(ticket.project, ticket)
                report.merged.append(ticket.id)

    def _reserve(self, name: str, seats: int) -> bool:
        with self._lock:
            if self._busy.get(name, 0) >= seats:
                return False
            self._busy[name] = self._busy.get(name, 0) + 1
            return True

    def _release(self, name: str) -> None:
        with self._lock:
            self._busy[name] = max(0, self._busy.get(name, 0) - 1)

    def _budget_allows(self, project: str) -> bool:
        budget = self.board.settings(project).frontier_budget_usd_per_day
        if budget is None:
            return True
        since = datetime.now(UTC) - timedelta(days=1)
        spent = sum(r.cost_usd for r in self.store.list_runs(project, since) if r.tier == Tier.FRONTIER)
        if spent < budget:
            return True
        self.board.raise_attention(
            AttentionKind.BUDGET_EXCEEDED, None,
            f"frontier budget reached for {project}: ${spent:.2f} spent in 24 h (budget ${budget:.2f})",
            project=project,
        )
        return False


def _last_verdict(text: str) -> str | None:
    for line in reversed(text.strip().splitlines()):
        line = line.strip().upper()
        if line.startswith("VERDICT:"):
            value = line.removeprefix("VERDICT:").strip()
            return "PASS" if value.startswith("PASS") else "CHANGES"
    return None


def _now() -> datetime:
    return datetime.now(UTC)
