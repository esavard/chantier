"""Board: tickets, gates and transitions. The only place stages change."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import PurePosixPath

from chantier.application.ports import (
    EventSink,
    Notifier,
    StateStore,
    TrackerMirror,
    Workspace,
    fan_out,
)
from chantier.domain.model import (
    AttentionItem,
    AttentionKind,
    GateMode,
    ProjectSettings,
    Stage,
    Ticket,
    TicketKind,
)
from chantier.domain.workflow import check_transition, find_cycle
from chantier.specs import normalized_spec_hash, parse_spec


class BoardError(ValueError):
    pass


@dataclass
class SyncReport:
    added: list[str] = field(default_factory=list)
    unchanged: int = 0
    errors: list[str] = field(default_factory=list)


def ticket_id(project: str, stem: str) -> str:
    return f"{project}/{stem}"


class Board:
    def __init__(
        self,
        store: StateStore,
        workspace: Workspace,
        projects: Mapping[str, ProjectSettings],
        notifier: Notifier,
        mirror: TrackerMirror,
        sink: EventSink,
    ) -> None:
        self.store = store
        self.workspace = workspace
        self.projects = projects
        self.notifier = notifier
        self.mirror = mirror
        self.sink = sink

    # ------------------------------------------------------------------ queries

    def settings(self, project: str) -> ProjectSettings:
        try:
            return self.projects[project]
        except KeyError:
            raise BoardError(f"unknown project: {project}") from None

    def ticket(self, ticket_id_: str) -> Ticket:
        ticket = self.store.get_ticket(ticket_id_)
        if ticket is None:
            raise BoardError(f"unknown ticket: {ticket_id_}")
        return ticket

    # ------------------------------------------------------------------ intake

    def sync_project(self, project: str) -> SyncReport:
        """Import new specs from the deliverable repo's base branch.

        Greenfield and brownfield enter here the same way: a spec committed by a
        human (spec-interview) or merged from a plan ticket (architect).
        """
        settings = self.settings(project)
        self.workspace.sync(project, settings.repo, settings.base_branch)
        report = SyncReport()
        known = {t.spec_path for t in self.store.list_tickets(project)}
        for path in sorted(self.workspace.list_base_files(project, settings.base_branch, settings.specs_dir)):
            name = PurePosixPath(path).name
            if not (name.startswith("spec_") and name.endswith(".md")):
                continue
            if path in known:
                report.unchanged += 1
                continue
            text = self.workspace.read_base_file(project, settings.base_branch, path) or ""
            doc = parse_spec(PurePosixPath(path).stem, text)
            ticket = Ticket(
                id=ticket_id(project, doc.stem),
                project=project,
                kind=TicketKind.IMPLEMENT,
                title=doc.title,
                spec_path=path,
                depends_on=tuple(ticket_id(project, d) for d in doc.depends_on),
                branch=f"chantier/{doc.stem}",
                created_at=_now(),
            )
            self.store.insert_ticket(ticket)
            report.added.append(ticket.id)
            self._log("ticket_created", ticket)
            self._after_draft(ticket, text)
        if cycle := find_cycle(self.store.list_tickets(project)):
            report.errors.append("dependency cycle: " + " -> ".join(cycle))
        return report

    def request_plan(self, project: str) -> Ticket:
        """Greenfield entry: turn the blueprint into specs with a frontier planner."""
        settings = self.settings(project)
        self.workspace.sync(project, settings.repo, settings.base_branch)
        text = self.workspace.read_base_file(project, settings.base_branch, settings.blueprint_path)
        if text is None:
            raise BoardError(
                f"{settings.blueprint_path} not found on {settings.base_branch}: "
                "run the blueprint-interview skill and commit the blueprint first"
            )
        existing = [t for t in self.store.list_tickets(project) if t.kind == TicketKind.PLAN]
        number = len(existing) + 1
        ticket = Ticket(
            id=ticket_id(project, f"plan_{number:03d}"),
            project=project,
            kind=TicketKind.PLAN,
            title=f"Plan specs from {settings.blueprint_path}",
            spec_path=settings.blueprint_path,
            branch=f"chantier/plan_{number:03d}",
            created_at=_now(),
        )
        self.store.insert_ticket(ticket)
        self._log("ticket_created", ticket)
        self._after_draft(ticket, text)
        return ticket

    def _after_draft(self, ticket: Ticket, text: str) -> None:
        settings = self.settings(ticket.project)
        if settings.gates.spec_approval == GateMode.AUTO:
            self.approve(ticket.id, actor="policy", spec_text=text)
        else:
            self.raise_attention(
                AttentionKind.GATE_WAITING, ticket,
                f"spec waiting for approval: {ticket.title} ({ticket.spec_path})",
            )

    # ------------------------------------------------------------------ gates

    def approve(self, ticket_id_: str, actor: str = "human", spec_text: str | None = None) -> Ticket:
        """Pass the gate the ticket is waiting on: spec approval (DRAFT) or merge (AWAITING_MERGE)."""
        ticket = self.ticket(ticket_id_)
        settings = self.settings(ticket.project)
        if ticket.stage == Stage.DRAFT:
            if spec_text is None:
                spec_text = self.workspace.read_base_file(ticket.project, settings.base_branch, ticket.spec_path)
            if spec_text is None:
                raise BoardError(f"{ticket.spec_path} is not on {settings.base_branch}")
            ticket.approved_spec_sha = normalized_spec_hash(spec_text)
            ticket = self.store.save_ticket(ticket, ticket.revision)
            return self.move(ticket, Stage.READY, f"spec approved by {actor}", actor)
        if ticket.stage == Stage.AWAITING_MERGE:
            self.workspace.merge(ticket.project, ticket.branch or "", settings.base_branch)
            return self.move(ticket, Stage.DONE, f"merged by {actor}", actor)
        raise BoardError(f"{ticket.id} is {ticket.stage}: no gate to approve")

    def reject(self, ticket_id_: str, reason: str, actor: str = "human") -> Ticket:
        """Send work back. A rejected merge returns to READY with the reason as feedback."""
        ticket = self.ticket(ticket_id_)
        if ticket.stage == Stage.AWAITING_MERGE:
            ticket.feedback = f"Rejected at the merge gate by {actor}: {reason}"
            ticket = self.store.save_ticket(ticket, ticket.revision)
            return self.move(ticket, Stage.READY, f"merge rejected: {reason}", actor)
        if ticket.stage in (Stage.DRAFT, Stage.BLOCKED):
            return self.move(ticket, Stage.CANCELLED, f"cancelled: {reason}", actor)
        raise BoardError(f"{ticket.id} is {ticket.stage}: nothing to reject")

    def unblock(self, ticket_id_: str, actor: str = "human", reset_ladder: bool = False) -> Ticket:
        ticket = self.ticket(ticket_id_)
        if ticket.stage != Stage.BLOCKED:
            raise BoardError(f"{ticket.id} is {ticket.stage}, not blocked")
        if reset_ladder:
            ticket.ladder_step = 0
        ticket.rung_attempts = 0
        ticket = self.store.save_ticket(ticket, ticket.revision)
        return self.move(ticket, Stage.READY, "unblocked", actor)

    # ------------------------------------------------------------------ transitions

    def move(self, ticket: Ticket, to_stage: Stage, cause: str, actor: str) -> Ticket:
        check_transition(ticket, to_stage)
        moved = self.store.transition(
            ticket, to_stage, expected_revision=ticket.revision, cause=cause, actor=actor
        )
        # Whatever needed a human for this ticket is settled once it moves on.
        self.store.resolve_attention_for(moved.id)
        last = self.store.transitions(moved.id)[-1]
        fan_out([
            lambda: self.mirror.publish(moved, last),
            lambda: self.sink.emit("transition", {
                "ticket": moved.id, "from": last.from_stage.value, "to": last.to_stage.value,
                "cause": cause, "actor": actor,
            }),
        ])
        if to_stage == Stage.AWAITING_MERGE:
            settings = self.settings(moved.project)
            if settings.gates.merge == GateMode.HUMAN:
                self.raise_attention(
                    AttentionKind.GATE_WAITING, moved,
                    f"ready to merge: {moved.title} (branch {moved.branch})",
                )
        return moved

    def raise_attention(self, kind: AttentionKind, ticket: Ticket | None, message: str,
                        project: str | None = None) -> None:
        item = self.store.raise_attention(AttentionItem(
            kind=kind,
            project=project or (ticket.project if ticket else ""),
            message=message,
            ticket_id=ticket.id if ticket else None,
            created_at=_now(),
        ))
        if item is not None:
            fan_out([lambda: self.notifier.notify(item),
                     lambda: self.sink.emit("attention", {"kind": kind.value, "message": message})])

    def _log(self, kind: str, ticket: Ticket) -> None:
        fan_out([lambda: self.mirror.publish(ticket, None),
                 lambda: self.sink.emit(kind, {"ticket": ticket.id, "title": ticket.title})])


def _now() -> datetime:
    return datetime.now(UTC)
