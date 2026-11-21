"""Cost control and the savings report."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from chantier.application.ports import StateStore
from chantier.domain.economics import Price, SavingsReport, savings
from chantier.domain.model import Phase, Run, Tier


@dataclass(frozen=True)
class ExecutorStats:
    executor: str
    tier: Tier
    runs: int
    succeeded: int
    tokens: int
    cost_usd: float
    seconds: float


@dataclass(frozen=True)
class MetricsReport:
    savings: SavingsReport
    by_executor: list[ExecutorStats]
    reference_model: str


def build_report(store: StateStore, reference_model: str, prices: Mapping[str, Price],
                 project: str | None = None) -> MetricsReport:
    runs = store.list_runs(project)
    reference = prices.get(reference_model)
    if reference is None:
        raise KeyError(f"no price for reference model '{reference_model}' in the pricing table")
    escalated = {
        r.ticket_id for r in runs
        if r.tier == Tier.FRONTIER and r.phase in (Phase.IMPLEMENT, Phase.PLAN)
        and any(o.ticket_id == r.ticket_id and o.tier == Tier.LOCAL for o in runs)
    }
    return MetricsReport(
        savings=savings(runs, reference, escalated),
        by_executor=_by_executor(runs),
        reference_model=reference_model,
    )


def _by_executor(runs: list[Run]) -> list[ExecutorStats]:
    groups: dict[str, list[Run]] = {}
    for run in runs:
        groups.setdefault(run.executor, []).append(run)
    return [
        ExecutorStats(
            executor=name,
            tier=rs[0].tier,
            runs=len(rs),
            succeeded=sum(1 for r in rs if r.outcome and r.outcome.value == "succeeded"),
            tokens=sum(r.usage.total_tokens for r in rs),
            cost_usd=round(sum(r.cost_usd for r in rs), 4),
            seconds=round(sum(r.duration_s for r in rs), 1),
        )
        for name, rs in sorted(groups.items())
    ]
