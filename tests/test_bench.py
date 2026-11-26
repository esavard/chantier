from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from chantier.adapters.integrations import ShellRunner
from chantier.adapters.workspace_git import GitWorkspace
from chantier.application.bench import BenchConfig, BenchError, Benchmarker
from chantier.application.ports import (
    ExecutionRequest,
    ExecutionResult,
    LineObserver,
    ModelProbeResult,
)
from chantier.domain.model import Phase, ProjectSettings, Ticket, Tier, Usage
from chantier.domain.routing import ExecutorSpec
from conftest import git


def _default_prompt(ticket: Ticket, settings: ProjectSettings, phase: Phase) -> str:
    if phase == Phase.REVIEW:
        return f"Review {ticket.spec_path}"
    fb = f"\nFeedback:\n{ticket.feedback}\n" if ticket.feedback else ""
    return f"Implement {ticket.spec_path}\nChecks: {settings.checks}{fb}"


@dataclass
class _FakeProbe:
    results: dict[str, ModelProbeResult]
    probed: list[tuple[str, int]] = field(default_factory=list)

    def list_models(self) -> list[str]:
        return list(self.results)

    def probe(self, model: str, *, num_ctx: int = 32768) -> ModelProbeResult:
        self.probed.append((model, num_ctx))
        return self.results[model]


class _ScriptedModelExecutor:
    def __init__(self, spec: ExecutorSpec, handler) -> None:
        self.spec = spec
        self._handler = handler

    def execute(self, request: ExecutionRequest, observe: LineObserver) -> ExecutionResult:
        return self._handler(self.spec.model, request, observe)


def _tick_all_boxes(spec_file: Path) -> None:
    lines = spec_file.read_text(encoding="utf-8").splitlines()
    out = [
        line.replace("- [ ] ", "- [x] ", 1) if "(manual)" not in line.lower() else line
        for line in lines
    ]
    spec_file.write_text("\n".join(out) + "\n", encoding="utf-8")


def test_bench_probe_only_screens_and_ranks(tmp_path: Path):
    probe = _FakeProbe(
        {
            "bad-tools:7b": ModelProbeResult("bad-tools:7b", True, False, 80.0, 4_000, 4_000, 1.0),
            "slow-cpu:27b": ModelProbeResult("slow-cpu:27b", True, True, 6.2, 12_000, 16_000, 0.75),
            "offline:30b": ModelProbeResult("offline:30b", False, False, 0.0, 0, 0, 0.0, error="down"),
            "fast-moe:35b": ModelProbeResult("fast-moe:35b", True, True, 58.7, 14_000, 14_000, 1.0),
            "faster-20b:20b": ModelProbeResult("faster-20b:20b", True, True, 95.1, 12_000, 12_000, 1.0),
        }
    )
    called_executors: list[str] = []

    def exec_factory(spec: ExecutorSpec):
        called_executors.append(spec.model)
        raise AssertionError("probe-only should not run worktree executors")

    bench = Benchmarker(
        probe=probe,
        executor_factory=exec_factory,
        workspace_factory=GitWorkspace,
        runner=ShellRunner(),
        projects={},
        prompts=_default_prompt,
    )
    report = bench.run(
        BenchConfig(
            probe_only=True,
            min_tok_s=15.0,
            min_vram_ratio=0.9,
            num_ctx=32768,
        )
    )

    assert report.mode == "probe"
    assert not called_executors
    assert report.recommended_model == "faster-20b:20b"
    assert "model: faster-20b:20b" in report.config_snippet

    by_model = {r.model: r for r in report.results}
    assert "tool" in (by_model["bad-tools:7b"].disqualified_reason or "").lower()
    assert "vram" in (by_model["slow-cpu:27b"].disqualified_reason or "").lower()
    assert "down" in (by_model["offline:30b"].disqualified_reason or "").lower()
    assert by_model["fast-moe:35b"].disqualified_reason is None
    assert [r.model for r in report.results[:2]] == ["faster-20b:20b", "fast-moe:35b"]


def test_bench_brownfield_isolation_and_ranking(env):
    # Initialize deliverable repo with a spec and check script; create production store ticket too.
    env.configure(
        {"local": ("good_agent.py", "local")},
        {"implement/implement": ["local"]},
        project={"checks": ["./check.sh"], "scope_mode": "enforce"},
    )
    (env.target / "src").mkdir(exist_ok=True)
    (env.target / "src" / "app.py").write_text("VALUE = 1\n")
    (env.target / "check.sh").write_text(
        "#!/usr/bin/env bash\n"
        "python3 -B -c 'import sys; sys.path.insert(0, \"src\"); import app; assert app.VALUE == 2'\n"
    )
    (env.target / "check.sh").chmod(0o755)
    git(env.target, "add", "-A")
    git(env.target, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "add app")
    env.write_spec("spec_001_update_value", "Update VALUE")
    env.write_spec("spec_002_second", "Second spec")

    app = env.app()
    app.board.sync_project("demo")
    before_tickets = [(t.id, t.stage, t.revision) for t in app.store.list_tickets("demo")]
    before_head = git(env.target, "rev-parse", "HEAD").strip()
    before_branches = git(env.target, "branch", "--list").strip()

    probe = _FakeProbe(
        {
            "model-a": ModelProbeResult("model-a", True, True, 45.0, 10_000, 10_000, 1.0),
            "model-b": ModelProbeResult("model-b", True, True, 60.0, 10_000, 10_000, 1.0),
        }
    )

    attempts_seen: dict[str, int] = {"model-a": 0, "model-b": 0}

    def handle(model: str, request: ExecutionRequest, observe: LineObserver) -> ExecutionResult:
        attempts_seen[model] += 1
        # model-a passes on attempt 2; model-b passes on attempt 1!
        if model == "model-b" or attempts_seen[model] >= 2:
            (request.workdir / "src" / "app.py").write_text("VALUE = 2\n")
        _tick_all_boxes(request.workdir / request.ticket.spec_path)
        return ExecutionResult(exit_code=0, usage=Usage(100, 50), summary="done")

    bench = Benchmarker(
        probe=probe,
        executor_factory=lambda s: _ScriptedModelExecutor(s, handle),
        workspace_factory=GitWorkspace,
        runner=ShellRunner(),
        projects=app.config.projects,
        prompts=_default_prompt,
    )
    report = bench.run(BenchConfig(project="demo", attempts=2, limit=1, num_ctx=16384))

    assert report.mode == "brownfield"
    assert report.target == "demo"
    assert report.recommended_model == "model-b"
    assert [r.model for r in report.results] == ["model-b", "model-a"]
    assert report.results[0].passed_count == 1
    assert report.results[0].total_attempts == 1
    assert len(report.results[0].specs) == 1
    assert report.results[1].passed_count == 1
    assert report.results[1].total_attempts == 2
    assert "model: model-b" in report.config_snippet
    assert '"--num-ctx", "16384"' in report.config_snippet

    # Deliverable repo and production StateStore are completely untouched.
    assert git(env.target, "rev-parse", "HEAD").strip() == before_head
    assert git(env.target, "branch", "--list").strip() == before_branches
    assert [(t.id, t.stage, t.revision) for t in app.store.list_tickets("demo")] == before_tickets
    assert (env.target / "src" / "app.py").read_text() == "VALUE = 1\n"


def test_bench_brownfield_explicit_specs_and_tiebreakers(env):
    env.configure(
        {"local": ("good_agent.py", "local")},
        {"implement/implement": ["local"]},
        project={"checks": ["true"]},
    )
    (env.target / "src").mkdir(exist_ok=True)
    git(env.target, "add", "-A")
    env.write_spec("spec_001_first", "First")
    env.write_spec("spec_002_second", "Second")
    app = env.app()

    probe = _FakeProbe(
        {
            "m-slower": ModelProbeResult("m-slower", True, True, 30.0, 9_000, 10_000, 0.9),
            "m-faster": ModelProbeResult("m-faster", True, True, 75.0, 10_000, 10_000, 1.0),
        }
    )

    def handle(model: str, request: ExecutionRequest, observe: LineObserver) -> ExecutionResult:
        (request.workdir / "src").mkdir(exist_ok=True)
        (request.workdir / "src" / "x.py").write_text("ok = True\n")
        _tick_all_boxes(request.workdir / request.ticket.spec_path)
        return ExecutionResult(exit_code=0, usage=Usage(10, 10), summary="done")

    bench = Benchmarker(
        probe=probe,
        executor_factory=lambda s: _ScriptedModelExecutor(s, handle),
        workspace_factory=GitWorkspace,
        runner=ShellRunner(),
        projects=app.config.projects,
        prompts=_default_prompt,
    )
    report = bench.run(BenchConfig(project="demo", specs=("spec_002_second",), attempts=1))
    assert [s.spec_stem for s in report.results[0].specs] == ["spec_002_second"]
    assert [r.model for r in report.results] == ["m-faster", "m-slower"]


def test_bench_brownfield_optional_review_retry(env):
    env.configure(
        {"local": ("good_agent.py", "local")},
        {"implement/implement": ["local"]},
        project={"checks": ["true"]},
    )
    (env.target / "src").mkdir(exist_ok=True)
    git(env.target, "add", "-A")
    env.write_spec("spec_001_mod", "Mod")
    app = env.app()

    probe = _FakeProbe({"m1": ModelProbeResult("m1", True, True, 50.0, 10_000, 10_000, 1.0)})
    review_calls = 0

    def handle_impl(model: str, request: ExecutionRequest, observe: LineObserver) -> ExecutionResult:
        (request.workdir / "src").mkdir(exist_ok=True)
        (request.workdir / "src" / "x.py").write_text(f"feedback={bool(request.ticket.feedback)}\n")
        _tick_all_boxes(request.workdir / request.ticket.spec_path)
        return ExecutionResult(exit_code=0, usage=Usage(10, 10), summary="impl")

    class _Reviewer:
        spec = ExecutorSpec(name="rev", adapter="command", tier=Tier.FRONTIER, model="ref")

        def execute(self, request: ExecutionRequest, observe: LineObserver) -> ExecutionResult:
            nonlocal review_calls
            review_calls += 1
            verdict = "VERDICT: CHANGES\nFix x" if review_calls == 1 else "VERDICT: PASS"
            return ExecutionResult(exit_code=0, usage=Usage(10, 5), summary=verdict)

    bench = Benchmarker(
        probe=probe,
        executor_factory=lambda s: _ScriptedModelExecutor(s, handle_impl),
        workspace_factory=GitWorkspace,
        runner=ShellRunner(),
        projects=app.config.projects,
        prompts=_default_prompt,
        review_executor=_Reviewer(),
    )
    report = bench.run(BenchConfig(project="demo", attempts=2, review=True))

    assert review_calls == 2
    assert report.results[0].passed_count == 1
    assert report.results[0].total_attempts == 2


class _GoFallbackRunner:
    """Falls back to structural verification when `go` is absent on the host PATH."""

    def __init__(self) -> None:
        self._shell = ShellRunner()

    def run(self, command: str, cwd: Path, timeout_s: int) -> tuple[int, str]:
        if command.startswith("go ") and shutil.which("go") is None:
            ok = (cwd / "calc" / "calc.go").is_file() and (cwd / "calc" / "calc_test.go").is_file()
            return (0, "go structural check ok") if ok else (1, "missing calc go files")
        return self._shell.run(command, cwd, timeout_s)


@pytest.mark.parametrize("stack", ["python", "go", "ts-svelte"])
def test_bench_greenfield_stacks(stack: str):
    probe = _FakeProbe({"m-good": ModelProbeResult("m-good", True, True, 55.0, 10_000, 10_000, 1.0)})

    def handle(model: str, request: ExecutionRequest, observe: LineObserver) -> ExecutionResult:
        wd = request.workdir
        if stack == "python":
            (wd / "src").mkdir(exist_ok=True)
            (wd / "tests").mkdir(exist_ok=True)
            (wd / "src" / "calc.py").write_text(
                "def add(a: int, b: int) -> int:\n    return a + b\n\n"
                "def divide(a: float, b: float) -> float:\n"
                "    if b == 0:\n        raise ValueError('zero')\n    return a / b\n"
            )
            (wd / "tests" / "test_calc.py").write_text(
                "import pytest\nfrom calc import add, divide\n\n"
                "def test_add():\n    assert add(2, 3) == 5\n\n"
                "def test_divide():\n    assert divide(6, 2) == 3\n"
                "    with pytest.raises(ValueError):\n        divide(1, 0)\n"
            )
        elif stack == "go":
            (wd / "calc").mkdir(exist_ok=True)
            (wd / "calc" / "calc.go").write_text(
                "package calc\n\nimport \"errors\"\n\n"
                "func Add(a, b int) int { return a + b }\n\n"
                "func Divide(a, b int) (int, error) {\n"
                "\tif b == 0 {\n\t\treturn 0, errors.New(\"zero\")\n\t}\n"
                "\treturn a / b, nil\n}\n"
            )
            (wd / "calc" / "calc_test.go").write_text(
                "package calc\n\nimport \"testing\"\n\n"
                "func TestDivide(t *testing.T) {\n"
                "\tgot, err := Divide(6, 2)\n"
                "\tif err != nil || got != 3 { t.Fatal(got, err) }\n"
                "\tif _, err := Divide(1, 0); err == nil { t.Fatal(\"expected error\") }\n"
                "}\n"
            )
        else:
            (wd / "src").mkdir(exist_ok=True)
            (wd / "tests").mkdir(exist_ok=True)
            (wd / "src" / "tasks.mjs").write_text(
                "export function countActive(tasks) {\n"
                "  return tasks.filter((t) => !t.done).length;\n"
                "}\n"
            )
            (wd / "tests" / "tasks.test.mjs").write_text(
                "import test from 'node:test';\nimport assert from 'node:assert/strict';\n"
                "import { countActive } from '../src/tasks.mjs';\n\n"
                "test('counts active', () => {\n"
                "  assert.equal(countActive([{ done: false }, { done: true }]), 1);\n"
                "});\n"
            )
        _tick_all_boxes(wd / request.ticket.spec_path)
        return ExecutionResult(exit_code=0, usage=Usage(50, 20), summary="done")

    bench = Benchmarker(
        probe=probe,
        executor_factory=lambda s: _ScriptedModelExecutor(s, handle),
        workspace_factory=GitWorkspace,
        runner=_GoFallbackRunner(),
        projects={},
        prompts=_default_prompt,
    )
    report = bench.run(BenchConfig(stack=stack, attempts=1))

    assert report.mode == "greenfield"
    assert report.target == stack
    assert report.recommended_model == "m-good"
    assert report.results[0].passed_count == 1


def test_bench_errors_for_missing_project_stack_or_models(env):
    env.configure({"local": ("good_agent.py", "local")}, {"implement/implement": ["local"]})
    app = env.app()
    probe = _FakeProbe({"m1": ModelProbeResult("m1", True, True, 50.0, 10_000, 10_000, 1.0)})

    bench = Benchmarker(
        probe=probe,
        executor_factory=lambda s: _ScriptedModelExecutor(s, lambda *a: ExecutionResult(exit_code=0)),
        workspace_factory=GitWorkspace,
        runner=ShellRunner(),
        projects=app.config.projects,
        prompts=_default_prompt,
    )
    with pytest.raises(BenchError, match="stack"):
        bench.run(BenchConfig(project="demo"))
    with pytest.raises(BenchError, match="unknown project"):
        bench.run(BenchConfig(project="missing"))
    with pytest.raises(BenchError, match="stack"):
        bench.run(BenchConfig(stack="rust"))

    empty_bench = Benchmarker(
        probe=_FakeProbe({}),
        executor_factory=lambda s: _ScriptedModelExecutor(s, lambda *a: ExecutionResult(exit_code=0)),
        workspace_factory=GitWorkspace,
        runner=ShellRunner(),
        projects=app.config.projects,
        prompts=_default_prompt,
    )
    with pytest.raises(BenchError, match="no candidate models"):
        empty_bench.run(BenchConfig(probe_only=True))
