from datetime import UTC, datetime, timedelta

import pytest

from chantier.application.supervisor import LoopDetector, scope_violations
from chantier.domain.economics import Price, savings
from chantier.domain.model import Phase, Run, RunOutcome, Stage, Ticket, TicketKind, Tier, Usage
from chantier.domain.routing import RoutingPolicy
from chantier.domain.workflow import TransitionError, check_transition, find_cycle, unmet_dependencies
from chantier.specs import normalized_spec_hash, parse_spec

SPEC = """# spec_003_history

## 1. Spec
**Title:** Calculation history
**Scope:**
- show last 10 results

## 2. Context
**Relevant modules/paths:**
- `lib/history/` — new module
- `lib/app.dart`
**Depends on:** spec_001_basic_calculator, `docs/specs/spec_002_memory.md`

```markdown
- [ ] inside a fence: ignored
```

## 4. Requirements
- [x] REQ-001 store result — Acceptance: 3 + 4 = -> history shows 7
- [ ] REQ-002 clear history. NOT DONE: needs storage block
- [ ] (manual) check on a real phone
- [ ] REQ-003 limit to 10
"""


def ticket(**kw) -> Ticket:
    base = dict(id="p/t", project="p", kind=TicketKind.IMPLEMENT, title="t", spec_path="s.md")
    return Ticket(**{**base, **kw})


def test_parse_spec_reads_title_dependencies_scope_and_checkboxes():
    doc = parse_spec("spec_003_history", SPEC)
    assert doc.title == "Calculation history"
    assert doc.depends_on == ("spec_001_basic_calculator", "spec_002_memory")
    assert doc.scope_paths == ("lib/history/", "lib/app.dart")
    assert len(doc.checkboxes) == 4  # the fenced one is ignored
    assert [c.text[:7] for c in doc.unresolved] == ["REQ-003"]


def test_parse_spec_without_fields_falls_back_to_heading():
    doc = parse_spec("spec_009_x", "# Some feature\n\n- [ ] a\n")
    assert doc.title == "Some feature"
    assert doc.depends_on == ()
    assert doc.scope_paths == ()


def test_dependencies_none_means_no_dependency():
    assert parse_spec("s", "**Depends on:** none\n").depends_on == ()


def test_spec_hash_ignores_ticks_and_not_done_notes_but_not_text_changes():
    ticked = SPEC.replace("- [ ] REQ-003 limit to 10", "- [x] REQ-003 limit to 10")
    noted = SPEC.replace("- [ ] REQ-003 limit to 10", "- [ ] REQ-003 limit to 10 NOT DONE: no time")
    edited = SPEC.replace("limit to 10", "limit to 5")
    assert normalized_spec_hash(SPEC) == normalized_spec_hash(ticked) == normalized_spec_hash(noted)
    assert normalized_spec_hash(SPEC) != normalized_spec_hash(edited)


def test_workflow_rejects_illegal_transitions():
    with pytest.raises(TransitionError):
        check_transition(ticket(stage=Stage.DRAFT), Stage.RUNNING)
    check_transition(ticket(stage=Stage.READY), Stage.RUNNING)


def test_dependencies_and_cycles():
    a = ticket(id="a", depends_on=("b",))
    b = ticket(id="b", depends_on=("a",))
    assert find_cycle([a, b]) in (["a", "b", "a"], ["b", "a", "b"])
    assert find_cycle([ticket(id="a", depends_on=("b",)), ticket(id="b")]) is None
    assert unmet_dependencies(a, {"b": Stage.READY}) == ["b"]
    assert unmet_dependencies(a, {"b": Stage.DONE}) == []
    assert unmet_dependencies(ticket(id="c", depends_on=("ghost",)), {}) == ["ghost"]


def test_routing_climbs_the_ladder_after_attempts_on_a_rung():
    policy = RoutingPolicy({(TicketKind.IMPLEMENT, Phase.IMPLEMENT): ("frank", "claude")}, attempts_per_rung=2)
    t = ticket()
    assert policy.executor_for(t, Phase.IMPLEMENT) == "frank"
    assert policy.record_failure(t) is False
    assert policy.executor_for(t, Phase.IMPLEMENT) == "frank"
    assert policy.record_failure(t) is True and t.fresh_start
    assert policy.executor_for(t, Phase.IMPLEMENT) == "claude"
    policy.record_failure(t)
    policy.record_failure(t)
    assert policy.executor_for(t, Phase.IMPLEMENT) is None
    assert t.attempt == 4


def test_savings_count_only_local_work_that_was_not_redone_on_the_frontier():
    now = datetime.now(UTC)

    def run(ticket_id, tier, outcome, tokens, cost=0.0):
        return Run(ticket_id=ticket_id, executor="x", tier=tier, model="m", phase=Phase.IMPLEMENT,
                   attempt=1, started_at=now, finished_at=now + timedelta(seconds=60),
                   outcome=outcome, usage=Usage(input_tokens=tokens), cost_usd=cost)

    price = Price(input=3.0, output=15.0)
    report = savings([
        run("a", Tier.LOCAL, RunOutcome.SUCCEEDED, 1_000_000),   # saved: $3
        run("b", Tier.LOCAL, RunOutcome.KILLED, 1_000_000),      # escalated: waste
        run("b", Tier.FRONTIER, RunOutcome.SUCCEEDED, 100, cost=0.5),
        run("c", Tier.LOCAL, RunOutcome.FAILED_CHECKS, 1_000_000),  # retry, finished locally: neither
    ], price, escalated_tickets={"b"})
    assert report.estimated_savings_usd == 3.0
    assert report.local_waste_usd == 3.0
    assert report.frontier_spend_usd == 0.5
    assert report.local_runs == 3 and report.frontier_runs == 1


def test_loop_detector_fires_on_repeated_output_only():
    detector = LoopDetector(window=20, threshold=5)
    assert all(detector(f"working on step {i} of the plan") is None for i in range(4))
    assert detector("working on step 99 of the plan") is not None  # numbers are noise
    fresh = LoopDetector(window=20, threshold=5)
    assert all(fresh(f"different line {chr(65 + i)} here") is None for i in range(20))


def test_scope_violations():
    changed = ["lib/history/model.dart", "lib/app.dart", "test/history_test.dart", "pubspec.yaml"]
    assert scope_violations(changed, ("lib/history/", "lib/app.dart"), ("test/",)) == ["pubspec.yaml"]
    assert scope_violations(changed, (), ("test/",)) == []
