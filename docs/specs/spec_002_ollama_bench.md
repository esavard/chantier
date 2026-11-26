# spec_002_ollama_bench

## 1. Spec
**Title:** Automatic Ollama model benchmark for greenfield and brownfield repositories
**Goal:** Add a `chantier bench` command that discovers or accepts candidate Ollama models, screens them for tool-calling compliance, VRAM residency, and throughput (`tok/s`), validates passing candidates end-to-end on ephemeral git worktrees (using brownfield project specs or a user-selected greenfield stack micro-spec), and outputs a ranked recommendation with a ready-to-use `config.yaml` snippet.
**Scope:**
- Two-stage benchmark pipeline (`probe` + `worktree validation`) in `application` behind ports (`ModelProbe`, `Executor`, `CommandRunner`).
- Brownfield mode targeting an existing registered project (`<project>`) on ephemeral temporary clones/worktrees without mutating the project's `StateStore` tickets or pushing branches/factory files into the deliverable repo.
- Greenfield mode targeting a user-selected language/stack micro-spec suite (`--stack go|python|ts-svelte`; when omitted on an empty/new project or standalone run, interactively prompt the user for the target stack in a TTY or require `--stack` in non-interactive mode) on a temporary initialized git repository.
- CLI command `chantier bench` with Rich table output, optional `--json` output, and recommended `executors.ollama` YAML block, plus usage documentation in `README.md`.
**Out of scope:**
- Automatic rewriting of `$CHANTIER_HOME/config.yaml` without human review.
- Benchmarking cloud/frontier executors or modifying `WORKFLOW_VERSION` / SQLite schema.

## 2. Context
**Project:** chantier
**Relevant modules/paths:**
- `src/chantier/application/bench.py`
- `src/chantier/application/ports.py`
- `src/chantier/adapters/executors/ollama.py`
- `src/chantier/bootstrap.py`
- `src/chantier/cli/main.py`
- `src/chantier/cli/views.py`
- `README.md`
- `docs/ROADMAP.md`
- `tests/test_bench.py`
- `tests/test_executors.py`
- `tests/test_cli_and_api.py`
**Depends on:** spec_001_ollama_executor

## 3. Requirements
- [ ] Define a `ModelProbe` port (and probe result dataclasses `ModelProbeResult`, `BenchSpecResult`, `BenchReport`) in `src/chantier/application/ports.py` / `src/chantier/application/bench.py` so `application` stays decoupled from `urllib` and Ollama HTTP details.
- [ ] Implement Ollama catalog discovery and probing in `src/chantier/adapters/executors/ollama.py`: query `/api/tags` when `--models` is omitted, warm up each candidate at the requested `num_ctx` (default `32768`) via `ensure_model_loaded`, read `size_vram` / `size` from `/api/ps`, measure real `eval_count / eval_duration` throughput (`tok/s`), and verify structured `write_file` tool-call emission on a minimal synthetic prompt.
- [ ] In `src/chantier/application/bench.py`, screen probe results before running expensive worktree tasks: mark models that fail structured `write_file` tool calling or drop below the minimum GPU VRAM ratio / throughput threshold (`--min-tok-s`, `--min-vram-ratio`) as disqualified with an explicit reason, while still reporting them in `BenchReport`.
- [ ] Support **brownfield** benchmarking when given a registered `<project>` containing specs (`docs/specs/spec_*.md`): clone/checkout the project's `base_branch` into a temporary directory under `tmp`/factory scratch, select up to `--limit` (default `2`) specs (or explicit `--specs`), run each candidate model for up to `--attempts` (default `2`) using `OllamaExecutor` + `verify()` (spec hash, checkboxes, scope, project `checks`) and optional frontier review (`--review`), and clean up temporary clones without ever writing factory files or branches to the deliverable repo or mutating production `StateStore` tickets.
- [ ] Support **greenfield** benchmarking via `--stack <go|python|ts-svelte>` when no `<project>` is provided or when the target project has no implementation specs yet: if `--stack` is omitted in an interactive TTY (and `--probe-only` is not set), prompt the user to choose their target stack so the benchmark runs a representative example; in non-interactive runs (`--json` or non-TTY) without `--stack`, exit with a clear error asking for `--stack`. Generate a minimal temporary git repository with the chosen stack's manifest, check script, and self-contained micro-spec (`add module/endpoint + unit test`), and evaluate each candidate model against `verify()` + optional `--review`.
- [ ] Rank candidate models deterministically in `BenchReport`: (1) passed specs count / local pass rate, (2) fewer total attempts used, (3) higher throughput (`tok/s`) / lower total wall time, (4) higher GPU VRAM residency ratio; print a recommended `executors.ollama` YAML block for the top passing model.
- [ ] Expose `chantier bench [project] [--models M1,M2] [--specs S1,S2] [--stack STACK] [--num-ctx 32768] [--attempts 2] [--limit 2] [--probe-only] [--review] [--min-tok-s N] [--min-vram-ratio R] [--json]` in `src/chantier/cli/main.py` and render the benchmark table + recommendation in `src/chantier/cli/views.py`, wired through `src/chantier/bootstrap.py`.
- [ ] Document `chantier bench` (brownfield, greenfield, and `--probe-only` usage examples) in `README.md`, and remove the implemented item from `docs/ROADMAP.md`.

## 4. Definition of Done
- [ ] Unit tests in `tests/test_bench.py` cover probe screening, brownfield worktree isolation (deliverable repo and `StateStore` untouched), greenfield stack micro-specs, deterministic ranking, and `--probe-only` / `--json` modes using temporary git repos and fake/scripted executors (no live Ollama server required).
- [ ] Adapter tests in `tests/test_executors.py` cover `/api/tags`, `/api/ps` VRAM calculation, `tok/s` measurement, and tool-call probe pass/fail parsing.
- [ ] CLI tests in `tests/test_cli_and_api.py` verify `chantier bench` argument parsing, interactive greenfield stack prompting vs non-interactive `--stack` requirement, and output formatting.
- [ ] `uv run pytest -q` passes.
- [ ] `uv run lint-imports` passes.
- [ ] `uvx pyright --pythonpath .venv/bin/python src tests` passes with zero errors.
