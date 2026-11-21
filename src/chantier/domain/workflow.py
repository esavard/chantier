"""The ticket state machine. Every stage change goes through `check_transition`."""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from chantier.domain.model import TERMINAL_STAGES, Stage, Ticket

ALLOWED: Mapping[Stage, frozenset[Stage]] = {
    Stage.DRAFT: frozenset({Stage.READY, Stage.CANCELLED}),
    Stage.READY: frozenset({Stage.RUNNING, Stage.BLOCKED, Stage.CANCELLED}),
    Stage.RUNNING: frozenset({Stage.READY, Stage.REVIEWING, Stage.AWAITING_MERGE, Stage.BLOCKED}),
    Stage.REVIEWING: frozenset({Stage.READY, Stage.AWAITING_MERGE, Stage.BLOCKED}),
    Stage.AWAITING_MERGE: frozenset({Stage.DONE, Stage.READY, Stage.BLOCKED}),
    Stage.BLOCKED: frozenset({Stage.READY, Stage.CANCELLED}),
    Stage.DONE: frozenset(),
    Stage.CANCELLED: frozenset(),
}


class TransitionError(ValueError):
    pass


class RevisionConflict(RuntimeError):
    """The ticket changed since it was read. Re-read and reassess; never retry blindly."""


def check_transition(ticket: Ticket, to_stage: Stage) -> None:
    if to_stage not in ALLOWED[ticket.stage]:
        raise TransitionError(f"{ticket.id}: {ticket.stage} -> {to_stage} is not allowed")


def unmet_dependencies(ticket: Ticket, stages_by_id: Mapping[str, Stage]) -> list[str]:
    """Dependencies that are not DONE. An unknown dependency counts as unmet."""
    return [dep for dep in ticket.depends_on if stages_by_id.get(dep) != Stage.DONE]


def find_cycle(tickets: Iterable[Ticket]) -> list[str] | None:
    """Return one dependency cycle as a list of ids, or None."""
    graph = {t.id: list(t.depends_on) for t in tickets}
    WHITE, GREY, BLACK = 0, 1, 2
    color = dict.fromkeys(graph, WHITE)
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        color[node] = GREY
        stack.append(node)
        for nxt in graph.get(node, []):
            if nxt not in graph:
                continue
            if color[nxt] == GREY:
                return stack[stack.index(nxt):] + [nxt]
            if color[nxt] == WHITE and (cycle := visit(nxt)):
                return cycle
        stack.pop()
        color[node] = BLACK
        return None

    for node in graph:
        if color[node] == WHITE and (cycle := visit(node)):
            return cycle
    return None


def is_terminal(stage: Stage) -> bool:
    return stage in TERMINAL_STAGES
