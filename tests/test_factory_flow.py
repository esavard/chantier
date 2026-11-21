"""End-to-end scenarios on a real git deliverable repo, with scripted agents."""

from __future__ import annotations

from chantier.application.metrics import build_report
from chantier.domain.model import AttentionKind, RunOutcome, Stage, Tier

from conftest import git

LOCAL_THEN_FRONTIER = {"implement/implement": ["local", "frontier"], "plan/plan": ["frontier"]}


def run_until_idle(app, max_ticks: int = 20):
    reports = []
    for _ in range(max_ticks):
        report = app.scheduler.tick()
        reports.append(report)
        app.scheduler.wait(timeout=120)
        if not report.dispatched and not report.merged:
            break
    return reports


def test_brownfield_spec_goes_from_human_approval_to_delivered_branch(env):
    env.configure({"local": ("good_agent.py", "local"), "frontier": ("good_agent.py", "frontier")},
                  LOCAL_THEN_FRONTIER)
    env.write_spec("spec_001_parser", "Parser")
    app = env.app()

    report = app.board.sync_project("demo")
    assert report.added == ["demo/spec_001_parser"]
    ticket = app.store.get_ticket("demo/spec_001_parser")
    assert ticket.stage == Stage.DRAFT
    assert [a.kind for a in app.store.list_attention()] == [AttentionKind.GATE_WAITING]

    # Nothing runs before the human approves the spec.
    assert app.scheduler.tick().dispatched == []

    app.board.approve("demo/spec_001_parser")
    run_until_idle(app)

    ticket = app.store.get_ticket("demo/spec_001_parser")
    assert ticket.stage == Stage.AWAITING_MERGE
    # The deliverable received a branch, and nothing else (no side-car, no factory files).
    assert "chantier/spec_001_parser" in git(env.target, "branch", "--list", "chantier/*")
    files = git(env.target, "ls-tree", "-r", "--name-only", "chantier/spec_001_parser").split()
    assert sorted(files) == ["README.md", "docs/specs/spec_001_parser.md", "src/spec_001_parser.py"]
    runs = app.store.list_runs("demo")
    assert [(r.executor, r.outcome) for r in runs] == [("local", RunOutcome.SUCCEEDED)]
    assert runs[0].usage.input_tokens == 12000

    # The human merges in the deliverable repo; the factory sees it through git.
    git(env.target, "-c", "user.name=t", "-c", "user.email=t@t", "merge", "-q", "--no-ff",
        "-m", "merge", "chantier/spec_001_parser")
    reports = run_until_idle(app)
    assert reports[0].merged == ["demo/spec_001_parser"]
    assert app.store.get_ticket("demo/spec_001_parser").stage == Stage.DONE
    assert app.store.list_attention() == []
    stages = [t.to_stage for t in app.store.transitions("demo/spec_001_parser")]
    assert stages == [Stage.READY, Stage.RUNNING, Stage.AWAITING_MERGE, Stage.DONE]


def test_dependencies_order_work_and_auto_gates_flow_without_a_human(env):
    env.configure({"local": ("good_agent.py", "local")}, {"implement/implement": ["local"]},
                  project={"gates": {"spec_approval": "auto", "merge": "auto"}})
    env.write_spec("spec_001_model", "Model")
    env.write_spec("spec_002_view", "View", deps="spec_001_model")
    app = env.app()
    app.board.sync_project("demo")

    first = app.scheduler.tick()
    assert first.dispatched == [("demo/spec_001_model", "local")]
    assert first.waiting == {"demo/spec_002_view": "waiting on demo/spec_001_model"}
    app.scheduler.wait(timeout=120)
    run_until_idle(app)

    assert app.store.get_ticket("demo/spec_001_model").stage == Stage.DONE
    assert app.store.get_ticket("demo/spec_002_view").stage == Stage.DONE
    merged = git(env.target, "log", "--oneline", "main")
    assert "Merge chantier/spec_001_model" in merged and "Merge chantier/spec_002_view" in merged
    assert app.store.list_attention() == []


def test_looping_local_agent_is_killed_then_escalated_to_the_frontier(env):
    env.configure({"local": ("looping_agent.py", "local"), "frontier": ("good_agent.py", "frontier")},
                  LOCAL_THEN_FRONTIER, project={"gates": {"spec_approval": "auto"}})
    env.write_spec("spec_001_parser", "Parser")
    app = env.app()
    app.board.sync_project("demo")
    run_until_idle(app)

    ticket = app.store.get_ticket("demo/spec_001_parser")
    assert ticket.stage == Stage.AWAITING_MERGE
    runs = app.store.list_runs("demo")
    assert [(r.executor, r.outcome) for r in runs] == [
        ("local", RunOutcome.KILLED), ("local", RunOutcome.KILLED), ("frontier", RunOutcome.SUCCEEDED)]
    assert "loop detected" in runs[0].detail
    causes = [t.cause for t in app.store.transitions(ticket.id)]
    assert "escalated to frontier" in causes

    report = build_report(app.store, "ref", app.config.prices, "demo")
    assert report.savings.estimated_savings_usd == 0.0     # nothing finished locally
    assert report.savings.local_waste_usd > 0              # the looping runs are counted as waste
    assert report.savings.frontier_spend_usd > 0


def test_spec_tampering_is_caught_and_the_ladder_ends_with_a_human(env):
    env.configure({"local": ("tampering_agent.py", "local")}, {"implement/implement": ["local"]},
                  project={"gates": {"spec_approval": "auto"}}, attempts_per_rung=1)
    env.write_spec("spec_001_parser", "Parser")
    app = env.app()
    app.board.sync_project("demo")
    run_until_idle(app)

    ticket = app.store.get_ticket("demo/spec_001_parser")
    assert ticket.stage == Stage.BLOCKED
    run = app.store.list_runs("demo")[0]
    assert run.outcome == RunOutcome.FAILED_CHECKS
    assert "modified beyond its checkboxes" in run.detail
    kinds = [a.kind for a in app.store.list_attention()]
    assert AttentionKind.ESCALATION_EXHAUSTED in kinds


def test_project_checks_failures_feed_the_next_attempt(env):
    env.configure({"local": ("good_agent.py", "local")}, {"implement/implement": ["local"]},
                  project={"gates": {"spec_approval": "auto"}, "checks": ["test -f MISSING_FILE"]},
                  attempts_per_rung=2)
    env.write_spec("spec_001_parser", "Parser")
    app = env.app()
    app.board.sync_project("demo")
    app.scheduler.tick()
    app.scheduler.wait(timeout=120)

    ticket = app.store.get_ticket("demo/spec_001_parser")
    assert ticket.stage == Stage.READY and ticket.attempt == 1
    assert "check failed (exit 1): test -f MISSING_FILE" in ticket.feedback
    prompt = app.scheduler.prompts(ticket, __import__("chantier.domain.model", fromlist=["Phase"]).Phase.IMPLEMENT,
                                   env.tmp)
    assert "test -f MISSING_FILE" in prompt and "attempt 2" in prompt


def test_frontier_budget_holds_frontier_work_and_tells_the_human(env):
    env.configure({"frontier": ("good_agent.py", "frontier")}, {"implement/implement": ["frontier"]},
                  project={"gates": {"spec_approval": "auto"}, "frontier_budget_usd_per_day": 0.01})
    env.write_spec("spec_001_a", "A")
    env.write_spec("spec_002_b", "B")
    app = env.app()
    app.board.sync_project("demo")
    first = app.scheduler.tick()
    app.scheduler.wait(timeout=120)
    assert len(first.dispatched) == 1   # one frontier seat
    second = app.scheduler.tick()       # the first run cost more than the budget
    assert second.dispatched == []
    assert "budget" in next(iter(second.waiting.values()))
    assert AttentionKind.BUDGET_EXCEEDED in [a.kind for a in app.store.list_attention()]


def test_greenfield_plan_produces_specs_that_enter_the_same_pipeline(env):
    env.configure({"frontier": ("planner_agent.py", "frontier"), "local": ("good_agent.py", "local")},
                  {"plan/plan": ["frontier"], "implement/implement": ["local"]},
                  project={"gates": {"spec_approval": "auto", "merge": "auto"}})
    (env.target / "docs").mkdir()
    (env.target / "docs" / "blueprint.md").write_text("# Ledger app\nBounded context: Ledger.\n")
    git(env.target, "add", "-A")
    git(env.target, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "blueprint")
    app = env.app()

    plan = app.board.request_plan("demo")
    assert plan.id == "demo/plan_001"
    run_until_idle(app)
    assert app.store.get_ticket(plan.id).stage == Stage.DONE

    report = app.board.sync_project("demo")
    assert report.added == ["demo/spec_001_ledger", "demo/spec_002_report"]
    assert app.store.get_ticket("demo/spec_002_report").depends_on == ("demo/spec_001_ledger",)
    run_until_idle(app)
    assert {t.stage for t in app.store.list_tickets("demo")} == {Stage.DONE}
    plan_run = [r for r in app.store.list_runs("demo") if r.ticket_id == plan.id][0]
    assert plan_run.tier == Tier.FRONTIER and plan_run.cost_usd == 0.21  # cost reported by the CLI wins


def test_merge_gate_rejection_sends_work_back_with_the_reason(env):
    env.configure({"local": ("good_agent.py", "local")}, {"implement/implement": ["local"]},
                  project={"gates": {"spec_approval": "auto"}})
    env.write_spec("spec_001_parser", "Parser")
    app = env.app()
    app.board.sync_project("demo")
    run_until_idle(app)
    app.board.reject("demo/spec_001_parser", "naming does not follow CONVENTIONS.md")
    ticket = app.store.get_ticket("demo/spec_001_parser")
    assert ticket.stage == Stage.READY
    assert "naming does not follow" in ticket.feedback
    assert ticket.fresh_start is False  # keeps the delivered work as a starting point


def test_restart_recovers_runs_left_open(env):
    env.configure({"local": ("good_agent.py", "local")}, {"implement/implement": ["local"]},
                  project={"gates": {"spec_approval": "auto"}})
    env.write_spec("spec_001_parser", "Parser")
    app = env.app()
    app.board.sync_project("demo")
    ticket = app.store.get_ticket("demo/spec_001_parser")
    app.board.move(ticket, Stage.RUNNING, "simulated crash", "test")
    from datetime import UTC, datetime

    from chantier.domain.model import Phase, Run
    app.store.add_run(Run(ticket_id=ticket.id, executor="local", tier=Tier.LOCAL, model="m",
                          phase=Phase.IMPLEMENT, attempt=1, started_at=datetime.now(UTC)))

    fresh = env.app()  # a new process
    assert fresh.scheduler.recover() == ["demo/spec_001_parser"]
    assert fresh.store.get_ticket(ticket.id).stage == Stage.READY
    assert fresh.store.running_runs() == []


def test_drain_stops_new_dispatch(env):
    env.configure({"local": ("good_agent.py", "local")}, {"implement/implement": ["local"]},
                  project={"gates": {"spec_approval": "auto"}})
    env.write_spec("spec_001_parser", "Parser")
    app = env.app()
    app.board.sync_project("demo")
    app.store.set_flag("drain", "1")
    assert app.scheduler.tick().dispatched == []
    app.store.set_flag("drain", None)
    assert len(app.scheduler.tick().dispatched) == 1
    app.scheduler.wait(timeout=120)
