"""Automatic local model benchmark: probe screening + isolated worktree validation."""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path

from chantier.application.ports import (
    CommandRunner,
    ExecutionRequest,
    Executor,
    ModelProbe,
    ModelProbeResult,
    Workspace,
)
from chantier.application.supervisor import LoopDetector
from chantier.application.verification import verify
from chantier.domain.model import Phase, ProjectSettings, Stage, Ticket, TicketKind, Tier
from chantier.domain.routing import ExecutorSpec
from chantier.specs import normalized_spec_hash

GREENFIELD_STACKS: tuple[str, ...] = ("go", "python", "ts-svelte")
DEFAULT_NUM_CTX = 32768


class BenchError(RuntimeError):
    pass


@dataclass
class BenchConfig:
    project: str | None = None
    models: tuple[str, ...] = ()
    specs: tuple[str, ...] = ()
    stack: str | None = None
    num_ctx: int = DEFAULT_NUM_CTX
    attempts: int = 2
    limit: int = 2
    probe_only: bool = False
    review: bool = False
    min_tok_s: float = 10.0
    min_vram_ratio: float = 0.9


@dataclass
class BenchSpecResult:
    spec_stem: str
    passed: bool
    attempts: int
    wall_time_s: float
    failure_reason: str | None = None


@dataclass
class BenchModelResult:
    model: str
    probe: ModelProbeResult
    specs: list[BenchSpecResult] = field(default_factory=list)
    passed_count: int = 0
    total_specs: int = 0
    total_attempts: int = 0
    wall_time_s: float = 0.0
    disqualified_reason: str | None = None

    @property
    def pass_rate(self) -> float:
        return (self.passed_count / self.total_specs) if self.total_specs > 0 else 0.0


@dataclass
class BenchReport:
    mode: str
    target: str
    num_ctx: int
    results: list[BenchModelResult]
    recommended_model: str | None = None
    config_snippet: str = ""

    def to_dict(self) -> dict:
        data = asdict(self)
        for item, res in zip(data["results"], self.results, strict=True):
            item["pass_rate"] = round(res.pass_rate, 4)
        return data


@dataclass(frozen=True)
class _StackTemplate:
    checks: tuple[str, ...]
    spec_path: str
    files: dict[str, str]


class Benchmarker:
    """Two-stage benchmark: fast HTTP probe screening + ephemeral git worktree evaluation."""

    def __init__(
        self,
        *,
        probe: ModelProbe,
        executor_factory: Callable[[ExecutorSpec], Executor],
        workspace_factory: Callable[[Path], Workspace],
        runner: CommandRunner,
        projects: dict[str, ProjectSettings],
        prompts: Callable[[Ticket, ProjectSettings, Phase], str],
        review_executor: Executor | None = None,
    ) -> None:
        self.probe = probe
        self.executor_factory = executor_factory
        self.workspace_factory = workspace_factory
        self.runner = runner
        self.projects = projects
        self.prompts = prompts
        self.review_executor = review_executor

    def needs_greenfield_stack(self, project: str | None) -> bool:
        if project is None:
            return True
        settings = self.projects.get(project)
        if settings is None:
            raise BenchError(f"unknown project '{project}'")
        with tempfile.TemporaryDirectory(prefix="chantier-bench-check-") as tmp:
            ws = self.workspace_factory(Path(tmp) / "ws")
            ws.sync(settings.name, settings.repo, settings.base_branch)
            return not _list_project_specs(ws, settings)

    def run(self, config: BenchConfig) -> BenchReport:
        _validate_config(config, self.projects, self.review_executor)
        candidates = self._resolve_candidates(config.models)
        mode, target, brownfield_specs = self._resolve_target(config)

        probe_map = {model: self.probe.probe(model, num_ctx=config.num_ctx) for model in candidates}
        model_results: dict[str, BenchModelResult] = {}
        eligible: list[str] = []
        for model in candidates:
            pr = probe_map[model]
            reason = _screen_probe(pr, config.min_tok_s, config.min_vram_ratio)
            model_results[model] = BenchModelResult(
                model=model,
                probe=pr,
                disqualified_reason=reason,
            )
            if reason is None:
                eligible.append(model)

        if mode != "probe" and eligible:
            self._run_worktrees(
                config=config,
                mode=mode,
                target=target,
                brownfield_specs=brownfield_specs,
                eligible=eligible,
                model_results=model_results,
            )

        ranked = sorted(model_results.values(), key=_rank_key)
        winner = next(
            (r for r in ranked if r.disqualified_reason is None and (mode == "probe" or r.passed_count > 0)),
            None,
        )
        snippet = _format_config_snippet(winner.model, config.num_ctx) if winner else ""
        return BenchReport(
            mode=mode,
            target=target,
            num_ctx=config.num_ctx,
            results=ranked,
            recommended_model=winner.model if winner else None,
            config_snippet=snippet,
        )

    def _resolve_candidates(self, explicit: tuple[str, ...]) -> list[str]:
        if explicit:
            models = list(dict.fromkeys(m.strip() for m in explicit if m.strip()))
        else:
            try:
                models = list(dict.fromkeys(self.probe.list_models()))
            except (RuntimeError, OSError) as exc:
                raise BenchError(str(exc)) from exc
        if not models:
            raise BenchError("no candidate models found (install a model in Ollama or pass --models)")
        return models

    def _resolve_target(self, config: BenchConfig) -> tuple[str, str, list[str]]:
        if config.probe_only:
            return "probe", config.project or config.stack or "ollama", []
        if config.project and not config.stack:
            settings = self.projects[config.project]
            with tempfile.TemporaryDirectory(prefix="chantier-bench-inspect-") as tmp:
                ws = self.workspace_factory(Path(tmp) / "ws")
                ws.sync(settings.name, settings.repo, settings.base_branch)
                all_specs = _list_project_specs(ws, settings)
            if not all_specs:
                raise BenchError(
                    f"project '{config.project}' has no specs under {settings.specs_dir}/; "
                    f"pass --stack {'|'.join(GREENFIELD_STACKS)} for greenfield benchmarking"
                )
            selected = _select_specs(all_specs, config.specs, config.limit)
            return "brownfield", config.project, selected
        if config.stack:
            return "greenfield", config.stack, []
        raise BenchError(
            f"greenfield mode requires --stack {'|'.join(GREENFIELD_STACKS)} "
            "(or pass a registered <project> name)"
        )

    def _run_worktrees(
        self,
        *,
        config: BenchConfig,
        mode: str,
        target: str,
        brownfield_specs: list[str],
        eligible: list[str],
        model_results: dict[str, BenchModelResult],
    ) -> None:
        with tempfile.TemporaryDirectory(prefix="chantier-bench-") as tmp_str:
            tmp_root = Path(tmp_str)
            if mode == "brownfield":
                settings = self.projects[target]
                spec_paths = brownfield_specs
            else:
                settings, spec_path = self._prepare_greenfield_repo(tmp_root / "template", target, config.review)
                spec_paths = [spec_path]

            for idx, model in enumerate(eligible):
                ws = self.workspace_factory(tmp_root / f"ws-{idx}")
                ws.sync(settings.name, settings.repo, settings.base_branch)
                spec = ExecutorSpec(
                    name=f"bench-{idx}",
                    adapter="ollama",
                    tier=Tier.LOCAL,
                    model=model,
                    args=("--num-ctx", str(config.num_ctx)),
                    timeout_s=1800,
                    idle_timeout_s=300,
                )
                executor = self.executor_factory(spec)
                logs_dir = tmp_root / "logs" / f"m-{idx}"
                spec_results: list[BenchSpecResult] = []
                for spec_path in spec_paths:
                    spec_results.append(
                        self._evaluate_spec(
                            ws=ws,
                            settings=settings,
                            spec_path=spec_path,
                            executor=executor,
                            attempts=config.attempts,
                            review=config.review,
                            logs_dir=logs_dir,
                        )
                    )
                res = model_results[model]
                res.specs = spec_results
                res.total_specs = len(spec_results)
                res.passed_count = sum(1 for s in spec_results if s.passed)
                res.total_attempts = sum(s.attempts for s in spec_results)
                res.wall_time_s = round(sum(s.wall_time_s for s in spec_results), 3)

    def _prepare_greenfield_repo(self, repo_dir: Path, stack: str, review: bool) -> tuple[ProjectSettings, str]:
        template = _greenfield_template(stack)
        repo_dir.mkdir(parents=True, exist_ok=True)
        for rel_path, content in template.files.items():
            target = repo_dir / rel_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        commands = [
            "git init -q -b main",
            "git add -A",
            'git -c user.name=chantier -c user.email=chantier@localhost commit -qm "bench init"',
        ]
        for cmd in commands:
            code, out = self.runner.run(cmd, repo_dir, 60)
            if code != 0:
                raise BenchError(f"failed to initialize temporary {stack} repo ({cmd}): {out.strip()}")
        settings = ProjectSettings(
            name=f"bench-{stack}",
            repo=str(repo_dir),
            base_branch="main",
            checks=template.checks,
            review=review,
            scope_mode="enforce",
        )
        return settings, template.spec_path

    def _evaluate_spec(
        self,
        *,
        ws: Workspace,
        settings: ProjectSettings,
        spec_path: str,
        executor: Executor,
        attempts: int,
        review: bool,
        logs_dir: Path,
    ) -> BenchSpecResult:
        spec_stem = Path(spec_path).stem
        raw_spec = ws.read_base_file(settings.name, settings.base_branch, spec_path) or ""
        ticket = Ticket(
            id=f"{settings.name}/{spec_stem}",
            project=settings.name,
            kind=TicketKind.IMPLEMENT,
            title=spec_stem,
            spec_path=spec_path,
            branch=f"chantier-bench/{spec_stem}",
            stage=Stage.RUNNING,
            approved_spec_sha=normalized_spec_hash(raw_spec) if raw_spec else None,
        )
        worktree = ws.prepare_worktree(settings.name, ticket, settings.base_branch, reset=True)
        started = time.monotonic()
        last_failure: str | None = None

        for attempt in range(1, attempts + 1):
            ticket.attempt = attempt - 1
            request = ExecutionRequest(
                ticket=ticket,
                phase=Phase.IMPLEMENT,
                workdir=worktree,
                prompt=self.prompts(ticket, settings, Phase.IMPLEMENT),
                log_path=logs_dir / f"{spec_stem}-implement-{attempt}.log",
                checks=settings.checks,
            )
            result = executor.execute(request, LoopDetector())
            ws.commit_all(worktree, f"chantier-bench: {ticket.id} attempt {attempt}")

            if result.killed_reason:
                last_failure = f"killed: {result.killed_reason}"
                ticket.feedback = f"Previous attempt was stopped by the supervisor: {result.killed_reason}"
                continue
            if result.exit_code != 0:
                last_failure = f"exit {result.exit_code}: {result.summary.splitlines()[-1] if result.summary else ''}"
                ticket.feedback = f"Previous attempt exited with code {result.exit_code}."
                continue

            verdict = verify(ticket, settings, ws, self.runner, worktree)
            if not verdict.ok:
                last_failure = verdict.failures[0] if verdict.failures else "verification failed"
                ticket.feedback = "Verification failed on the previous attempt:\n" + verdict.feedback()
                continue

            if review and self.review_executor is not None:
                rev_req = ExecutionRequest(
                    ticket=ticket,
                    phase=Phase.REVIEW,
                    workdir=worktree,
                    prompt=self.prompts(ticket, settings, Phase.REVIEW),
                    log_path=logs_dir / f"{spec_stem}-review-{attempt}.log",
                )
                rev_res = self.review_executor.execute(rev_req, lambda line: None)
                verdict_line = _last_verdict(rev_res.summary)
                if rev_res.exit_code != 0 or rev_res.killed_reason or verdict_line != "PASS":
                    last_failure = rev_res.killed_reason or "reviewer requested changes"
                    ticket.feedback = "The reviewer requested changes:\n" + rev_res.summary[-4000:]
                    continue

            return BenchSpecResult(
                spec_stem=spec_stem,
                passed=True,
                attempts=attempt,
                wall_time_s=round(time.monotonic() - started, 3),
            )

        return BenchSpecResult(
            spec_stem=spec_stem,
            passed=False,
            attempts=attempts,
            wall_time_s=round(time.monotonic() - started, 3),
            failure_reason=last_failure or "failed verification",
        )


def _validate_config(
    config: BenchConfig,
    projects: dict[str, ProjectSettings],
    review_executor: Executor | None,
) -> None:
    if config.project and config.project not in projects:
        raise BenchError(f"unknown project '{config.project}'")
    if config.stack is not None and config.stack not in GREENFIELD_STACKS:
        raise BenchError(f"unknown stack '{config.stack}' (choose from: {', '.join(GREENFIELD_STACKS)})")
    if config.attempts < 1:
        raise BenchError("--attempts must be at least 1")
    if config.limit < 1:
        raise BenchError("--limit must be at least 1")
    if config.num_ctx < 512:
        raise BenchError("--num-ctx must be at least 512")
    if config.review and review_executor is None:
        raise BenchError("--review requires an `implement/review` frontier executor configured in config.yaml")


def _list_project_specs(ws: Workspace, settings: ProjectSettings) -> list[str]:
    files = ws.list_base_files(settings.name, settings.base_branch, settings.specs_dir)
    prefix = settings.specs_dir.rstrip("/") + "/"
    return sorted(
        f for f in files if f.startswith(prefix) and Path(f).name.startswith("spec_") and f.endswith(".md")
    )


def _select_specs(all_specs: list[str], requested: tuple[str, ...], limit: int) -> list[str]:
    if not requested:
        return all_specs[:limit]
    wanted = {item.strip() for item in requested if item.strip()}
    selected = [
        path
        for path in all_specs
        if Path(path).stem in wanted or Path(path).name in wanted or path in wanted
    ]
    if not selected:
        raise BenchError(f"none of the requested specs matched: {', '.join(sorted(wanted))}")
    return selected


def _screen_probe(probe: ModelProbeResult, min_tok_s: float, min_vram_ratio: float) -> str | None:
    if not probe.reachable:
        return probe.error or "model unreachable"
    if not probe.tool_calling_ok:
        return "failed structured write_file tool-calling probe"
    if min_vram_ratio > 0 and probe.vram_ratio < min_vram_ratio:
        return f"VRAM ratio {probe.vram_ratio:.0%} < {min_vram_ratio:.0%}"
    if min_tok_s > 0 and probe.tok_per_s < min_tok_s:
        return f"throughput {probe.tok_per_s:.1f} tok/s < {min_tok_s:.1f}"
    return None


def _rank_key(result: BenchModelResult) -> tuple:
    eligible = 0 if result.disqualified_reason is None else 1
    attempts_key = result.total_attempts if result.passed_count > 0 else 10**9
    return (
        eligible,
        -result.passed_count,
        -result.pass_rate,
        attempts_key,
        -round(result.probe.tok_per_s, 3),
        round(result.wall_time_s, 3),
        -round(result.probe.vram_ratio, 4),
        result.model,
    )


def _format_config_snippet(model: str, num_ctx: int) -> str:
    return (
        "executors:\n"
        "  ollama:\n"
        "    adapter: ollama\n"
        "    tier: local\n"
        f"    model: {model}\n"
        "    seats: 1\n"
        f'    args: ["--num-ctx", "{num_ctx}"]\n'
        "    idle_timeout_s: 600\n"
        "    timeout_s: 3600"
    )


def _last_verdict(text: str) -> str | None:
    for line in reversed(text.strip().splitlines()):
        cleaned = line.strip().upper()
        if cleaned.startswith("VERDICT:"):
            value = cleaned.removeprefix("VERDICT:").strip()
            return "PASS" if value.startswith("PASS") else "CHANGES"
    return None


def _python_check_cmd() -> str:
    return "pytest -q" if shutil.which("pytest") else f"{sys.executable} -m pytest -q"


def _greenfield_template(stack: str) -> _StackTemplate:
    if stack == "python":
        return _StackTemplate(
            checks=(_python_check_cmd(),),
            spec_path="docs/specs/spec_001_calculator.md",
            files={
                "pyproject.toml": (
                    '[project]\nname = "bench-python"\nversion = "0.1.0"\n'
                    'requires-python = ">=3.12"\n\n'
                    '[tool.pytest.ini_options]\ntestpaths = ["tests"]\npythonpath = ["src"]\n'
                ),
                "src/__init__.py": "",
                "tests/__init__.py": "",
                "docs/specs/spec_001_calculator.md": (
                    "# spec_001_calculator\n\n"
                    "## 1. Spec\n"
                    "**Title:** Calculator module\n"
                    "**Goal:** Add a small calculator module with unit tests.\n\n"
                    "## 2. Context\n"
                    "**Relevant modules/paths:** `src/`, `tests/`\n"
                    "**Depends on:** none\n\n"
                    "## 3. Requirements\n"
                    "- [ ] Implement `add(a: int, b: int) -> int` and `divide(a: float, b: float) -> float` "
                    "(raising `ValueError` when `b == 0`) in `src/calc.py`.\n"
                    "- [ ] Add pytest tests in `tests/test_calc.py` covering `add`, `divide`, and division by zero.\n\n"
                    "## 4. Definition of Done\n"
                    "- [ ] Test suite passes cleanly.\n"
                ),
            },
        )
    if stack == "go":
        return _StackTemplate(
            checks=("go test ./...",),
            spec_path="docs/specs/spec_001_calculator.md",
            files={
                "go.mod": "module bench\n\ngo 1.22\n",
                "docs/specs/spec_001_calculator.md": (
                    "# spec_001_calculator\n\n"
                    "## 1. Spec\n"
                    "**Title:** Go calculator package\n"
                    "**Goal:** Add a small Go calculator package with unit tests.\n\n"
                    "## 2. Context\n"
                    "**Relevant modules/paths:** `calc/`\n"
                    "**Depends on:** none\n\n"
                    "## 3. Requirements\n"
                    "- [ ] Implement `Add(a, b int) int` and `Divide(a, b int) (int, error)` "
                    "(returning an error when `b == 0`) in `calc/calc.go` (`package calc`).\n"
                    "- [ ] Add Go unit tests in `calc/calc_test.go` covering `Add`, `Divide`, and division by zero.\n\n"
                    "## 4. Definition of Done\n"
                    "- [ ] `go test ./...` passes cleanly.\n"
                ),
            },
        )
    return _StackTemplate(
        checks=("node --test",),
        spec_path="docs/specs/spec_001_task_counter.md",
        files={
            "package.json": json.dumps(
                {
                    "name": "bench-ts-svelte",
                    "version": "0.1.0",
                    "private": True,
                    "type": "module",
                    "scripts": {"test": "node --test"},
                },
                indent=2,
            )
            + "\n",
            "docs/specs/spec_001_task_counter.md": (
                "# spec_001_task_counter\n\n"
                "## 1. Spec\n"
                "**Title:** Task counter helper\n"
                "**Goal:** Add a small ESM task-filtering module with `node:test` unit tests.\n\n"
                "## 2. Context\n"
                "**Relevant modules/paths:** `src/`, `tests/`\n"
                "**Depends on:** none\n\n"
                "## 3. Requirements\n"
                "- [ ] Export `countActive(tasks)` in `src/tasks.mjs` returning the count of items with `!t.done`.\n"
                "- [ ] Add `node:test` unit tests in `tests/tasks.test.mjs` verifying active and completed tasks.\n\n"
                "## 4. Definition of Done\n"
                "- [ ] `node --test` passes cleanly.\n"
            ),
        },
    )
