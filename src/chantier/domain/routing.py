"""Routing: deterministic rules decide which executor runs which phase.

No LLM decides routing. Each (kind, phase) has a ladder of executors; a ticket
starts on the first rung and climbs one rung each time it exhausts the attempts
allowed on the current rung. Escalation measures real difficulty instead of
guessing it up front.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from chantier.domain.model import Phase, Ticket, TicketKind, Tier


@dataclass(frozen=True)
class ExecutorSpec:
    name: str
    adapter: str                     # "claude_code" | "codex" | "aider_ollama" | "fake"
    tier: Tier
    model: str = ""
    seats: int = 1                   # concurrent runs allowed (local GPU: usually 1)
    args: tuple[str, ...] = ()       # extra CLI arguments, adapter-specific
    timeout_s: int = 3600            # hard cap on a run
    idle_timeout_s: int = 600        # no output for this long -> killed


@dataclass(frozen=True)
class RoutingPolicy:
    ladders: dict[tuple[TicketKind, Phase], tuple[str, ...]] = field(default_factory=dict)
    attempts_per_rung: int = 2

    def ladder(self, kind: TicketKind, phase: Phase) -> tuple[str, ...]:
        try:
            return self.ladders[(kind, phase)]
        except KeyError:
            raise KeyError(f"no routing ladder for {kind}/{phase}") from None

    def executor_for(self, ticket: Ticket, phase: Phase) -> str | None:
        """Executor for the ticket's current rung, or None if the ladder is exhausted."""
        ladder = self.ladder(ticket.kind, phase)
        if phase == Phase.REVIEW:
            return ladder[0] if ladder else None
        return ladder[ticket.ladder_step] if ticket.ladder_step < len(ladder) else None

    def record_failure(self, ticket: Ticket) -> bool:
        """Count a failed attempt on the current rung; climb a rung when it is exhausted.

        Returns True if the ticket escalated.
        """
        ticket.attempt += 1
        ticket.rung_attempts += 1
        if ticket.rung_attempts >= self.attempts_per_rung:
            ticket.ladder_step += 1
            ticket.rung_attempts = 0
            ticket.fresh_start = True
            return True
        return False
