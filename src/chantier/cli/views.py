"""Rich renderings shared by the CLI commands."""

from __future__ import annotations

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from chantier.application.bench import BenchReport
from chantier.application.metrics import MetricsReport
from chantier.domain.model import AttentionItem, Run, Stage, Ticket, Transition

COLUMNS = [
    (Stage.DRAFT, "Draft", "yellow"),
    (Stage.READY, "Ready", "cyan"),
    (Stage.RUNNING, "Running", "blue"),
    (Stage.REVIEWING, "Review", "blue"),
    (Stage.AWAITING_MERGE, "To merge", "magenta"),
    (Stage.BLOCKED, "Blocked", "red"),
    (Stage.DONE, "Done", "green"),
]


def board(tickets: list[Ticket], title: str) -> Table:
    table = Table(title=title, expand=True, show_lines=False)
    for _, label, color in COLUMNS:
        count = sum(1 for t in tickets if t.stage == _stage(label))
        table.add_column(f"{label} ({count})", style=color, overflow="fold")
    by_stage = {stage: [t for t in tickets if t.stage == stage] for stage, _, _ in COLUMNS}
    depth = max((len(v) for v in by_stage.values()), default=0)
    for i in range(depth):
        row = []
        for stage, _, _ in COLUMNS:
            items = by_stage[stage]
            row.append(_card(items[i]) if i < len(items) else "")
        table.add_row(*row)
    return table


def _stage(label: str) -> Stage:
    return next(s for s, lbl, _ in COLUMNS if lbl == label)


def _card(ticket: Ticket) -> Text:
    text = Text(ticket.id.split("/", 1)[-1], style="bold")
    if ticket.attempt:
        text.append(f"  try {ticket.attempt + 1}", style="dim")
    if ticket.ladder_step:
        text.append(f"  ↑{ticket.ladder_step}", style="bold red")
    return text


def ticket_detail(ticket: Ticket, transitions: list[Transition], runs: list[Run]) -> Group:
    head = Table.grid(padding=(0, 2))
    for key, value in [
        ("ticket", ticket.id), ("title", ticket.title), ("kind", ticket.kind.value),
        ("stage", ticket.stage.value), ("spec", ticket.spec_path), ("branch", ticket.branch or "-"),
        ("depends on", ", ".join(ticket.depends_on) or "-"),
        ("attempts", f"{ticket.attempt} (ladder step {ticket.ladder_step})"),
        ("revision", str(ticket.revision)), ("workflow", f"v{ticket.workflow_version}"),
    ]:
        head.add_row(Text(key, style="dim"), value)
    history = Table("when", "from", "to", "by", "cause", title="Transitions", expand=True)
    for t in transitions:
        history.add_row(t.at.strftime("%m-%d %H:%M"), t.from_stage.value, t.to_stage.value, t.actor, t.cause)
    run_table = Table("#", "phase", "executor", "outcome", "tokens", "cost $", "time", "detail",
                      title="Runs", expand=True)
    for r in runs:
        run_table.add_row(str(r.attempt), r.phase.value, r.executor, r.outcome.value if r.outcome else "running",
                          f"{r.usage.total_tokens:,}", f"{r.cost_usd:.3f}", f"{r.duration_s:.0f}s",
                          (r.detail or "").splitlines()[0][:80] if r.detail else "")
    parts = [Panel(head, title=ticket.title), history, run_table]
    if ticket.feedback:
        parts.append(Panel(ticket.feedback, title="Feedback for the next attempt", style="yellow"))
    return Group(*parts)


def attention(items: list[AttentionItem]) -> Table:
    table = Table("id", "kind", "ticket", "message", "since", title="Needs a human", expand=True)
    for item in items:
        table.add_row(str(item.id), item.kind.value, item.ticket_id or item.project,
                      item.message.splitlines()[0][:100],
                      item.created_at.strftime("%m-%d %H:%M") if item.created_at else "")
    return table


def metrics(report: MetricsReport) -> Group:
    s = report.savings
    summary = Table.grid(padding=(0, 2))
    for key, value in [
        ("frontier spend", f"${s.frontier_spend_usd:.2f}"),
        ("local runs", f"{s.local_runs} of {s.local_runs + s.frontier_runs} ({s.local_share:.0%})"),
        ("local tokens", f"{s.local_tokens:,}"),
        (f"local work valued at {report.reference_model}", f"${s.local_value_usd:.2f}"),
        ("local work redone on the frontier (waste)", f"${s.local_waste_usd:.2f}"),
        ("local energy", f"${s.energy_usd:.2f}"),
        ("estimated savings", f"${s.estimated_savings_usd:.2f}"),
    ]:
        summary.add_row(Text(key, style="dim"), Text(value, style="bold"))
    per = Table("executor", "tier", "runs", "succeeded", "tokens", "cost $", "time", expand=True)
    for e in report.by_executor:
        per.add_row(e.executor, e.tier.value, str(e.runs), str(e.succeeded), f"{e.tokens:,}",
                    f"{e.cost_usd:.2f}", f"{e.seconds / 60:.0f} min")
    note = Text("Estimate: a frontier model might have needed fewer tokens than the local one. "
                "Only successful local runs on tickets that never escalated count as savings.", style="dim")
    return Group(Panel(summary, title="Cost and savings"), per, note)


def bench(report: BenchReport) -> Group:
    table = Table(
        title=f"Ollama benchmark — {report.mode} ({report.target}, num_ctx={report.num_ctx})",
        expand=True,
    )
    table.add_column("#", no_wrap=True)
    table.add_column("model", overflow="fold")
    table.add_column("tools", no_wrap=True)
    table.add_column("tok/s", no_wrap=True)
    table.add_column("VRAM")
    table.add_column("passed", no_wrap=True)
    table.add_column("attempts", no_wrap=True)
    table.add_column("time", no_wrap=True)
    table.add_column("status", overflow="fold")
    for idx, res in enumerate(report.results, start=1):
        p = res.probe
        tools_txt = Text("yes", style="green") if p.tool_calling_ok else Text("no", style="red")
        tok_txt = f"{p.tok_per_s:.1f}" if p.reachable else "-"
        if p.total_bytes > 0:
            vram_gb = p.vram_bytes / 1_000_000_000
            total_gb = p.total_bytes / 1_000_000_000
            vram_txt = f"{p.vram_ratio:.0%} ({vram_gb:.1f}/{total_gb:.1f} GB)"
        elif p.reachable:
            vram_txt = f"{p.vram_ratio:.0%}"
        else:
            vram_txt = "-"
        passed_txt = "-" if report.mode == "probe" else f"{res.passed_count}/{res.total_specs}"
        attempts_txt = "-" if report.mode == "probe" else str(res.total_attempts)
        time_txt = "-" if report.mode == "probe" else f"{res.wall_time_s:.1f}s"

        if res.disqualified_reason:
            status_txt = Text(f"disqualified: {res.disqualified_reason}", style="red")
        elif res.model == report.recommended_model:
            status_txt = Text("recommended", style="bold green")
        elif report.mode == "probe" or res.passed_count > 0:
            status_txt = Text("ok", style="cyan")
        else:
            reason = next((s.failure_reason for s in res.specs if s.failure_reason), "failed specs")
            status_txt = Text(f"failed ({reason[:50]})", style="yellow")

        table.add_row(str(idx), res.model, tools_txt, tok_txt, vram_txt, passed_txt, attempts_txt, time_txt, status_txt)

    if report.config_snippet:
        footer = Panel(report.config_snippet, title="Recommended config.yaml snippet", style="green")
    else:
        footer = Text("No candidate model passed all benchmark criteria.", style="yellow")
    return Group(table, footer)
