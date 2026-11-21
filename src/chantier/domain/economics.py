"""Cost and savings accounting.

The savings figure is meant to survive a sceptic, so it is deliberately
conservative:

* Local tokens are valued at a reference frontier price ("what these tokens
  would have cost on the frontier").
* Only successful local runs on tickets that never escalated count as savings. Local runs
  whose ticket later escalated to a frontier executor are counted as waste: the
  frontier paid for the work anyway.
* Local energy (GPU watts x run time x kWh price) is subtracted.

It is still an estimate: a frontier model might have used fewer tokens than the
local one. The report says so.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from chantier.domain.model import Run, RunOutcome, Tier, Usage


@dataclass(frozen=True)
class Price:
    """USD per million tokens."""

    input: float
    output: float
    cache_read: float = 0.0

    def cost(self, usage: Usage) -> float:
        return (
            usage.input_tokens * self.input
            + usage.output_tokens * self.output
            + usage.cache_read_tokens * self.cache_read
        ) / 1_000_000


@dataclass(frozen=True)
class EnergyModel:
    gpu_watts: float = 0.0
    kwh_price: float = 0.0

    def cost(self, seconds: float) -> float:
        return self.gpu_watts / 1000 * seconds / 3600 * self.kwh_price


def frontier_run_cost(usage: Usage, price: Price | None) -> float:
    """Prefer what the CLI reported; otherwise compute from the price table."""
    if usage.reported_cost_usd is not None:
        return usage.reported_cost_usd
    return price.cost(usage) if price else 0.0


@dataclass(frozen=True)
class SavingsReport:
    frontier_spend_usd: float
    local_runs: int
    frontier_runs: int
    local_tokens: int
    local_value_usd: float          # local tokens of locally-finished tickets, at reference price
    local_waste_usd: float          # local tokens of tickets that escalated anyway
    energy_usd: float
    estimated_savings_usd: float

    @property
    def local_share(self) -> float:
        total = self.local_runs + self.frontier_runs
        return self.local_runs / total if total else 0.0


def savings(runs: Iterable[Run], reference: Price, escalated_tickets: set[str]) -> SavingsReport:
    """`escalated_tickets`: tickets that ever ran a frontier IMPLEMENT/PLAN attempt."""
    frontier_spend = local_value = local_waste = energy = 0.0
    local_runs = frontier_runs = local_tokens = 0
    for run in runs:
        if run.tier == Tier.FRONTIER:
            frontier_runs += 1
            frontier_spend += run.cost_usd
            continue
        local_runs += 1
        local_tokens += run.usage.total_tokens
        energy += run.cost_usd
        value = reference.cost(run.usage)
        if run.ticket_id in escalated_tickets:
            local_waste += value
        elif run.outcome == RunOutcome.SUCCEEDED:
            local_value += value
        # Failed local attempts on a ticket that still finished locally are
        # neither saved nor wasted: a frontier run might have needed retries too.
    return SavingsReport(
        frontier_spend_usd=round(frontier_spend, 4),
        local_runs=local_runs,
        frontier_runs=frontier_runs,
        local_tokens=local_tokens,
        local_value_usd=round(local_value, 4),
        local_waste_usd=round(local_waste, 4),
        energy_usd=round(energy, 4),
        estimated_savings_usd=round(local_value - energy, 4),
    )
