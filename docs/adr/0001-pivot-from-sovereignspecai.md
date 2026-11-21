# ADR 0001: Pivot from SovereignSpecAI to chantier

> Point-in-time record, written 2026-11-21.

## Context

Three projects carried parts of the same vision:

- **SovereignSpecAI**: a local-first orchestrator (Ollama + aider) with a Kanban state machine in folders, an architect that decomposes a Mermaid DDD/EDD blueprint, developer/reviewer agents, human gates. Strong on greenfield, awkward on existing repos: it runs as a side-car cloned inside the project, writes `specs/` and `architecture/` into it, and every stage needs a human command. No parallelism, no supervision of a local model that loops. "Sovereign" (nothing leaves the machine) stopped being true once frontier agents took the heavy lifting.
- **agent-skills**: `spec-conventions-interview`, `spec-interview`, `spec-implement` + `check_spec.py`. The method that works in production, feature by feature, on existing repos.
- **app-factory-template** (private): the most mature specialization of that method (apps produced as separate repos, `verify` as the done gate, spec integrity, skills pinned with `skills-lock.json`, repository overrides in `AGENTS.md`).

## Decision

Start a new repository, **chantier**, as the single orchestration engine. Keep SovereignSpecAI as is, to be archived. Layering:

1. **Method**: agent-skills stays the single source of truth for interview skills and the spec format. New generic skills (`blueprint-interview`) live in `skills/` here until they are moved there.
2. **Engine**: chantier. Knows no stack.
3. **Packs**: specializations (defaults, checks, prompt overrides). The app factory becomes a private pack; nothing private enters this repo.

## What came from where

| chantier | Origin |
|---|---|
| Kanban state machine, two human gates, rework with feedback | SovereignSpecAI pipeline (`01_backlog` ... `05_done`), now in SQLite with audited transitions |
| DDD/EDD decomposition rules in `prompts/plan.md` (event before command, one node per task, Mermaid prefixes) | SovereignSpecAI `agents/architect_plan.md`, `architect_task.md` |
| Implementation and review prompts (KISS, scope discipline, spec names are the contract, test rules, PASS/CHANGES verdict) | SovereignSpecAI `agents/developer.md`, `reviewer.md`, `rework.md` |
| Per-role models, one model at a time in VRAM (`keep_alive` unload/load) | SovereignSpecAI `core/model_manager.py` → `AiderOllamaExecutor.before_run` |
| aider invocation and artifact hygiene (`.aider*` never reaches a deliverable) | SovereignSpecAI `core/aider_runner.py`, `git_utils.py` → git `info/exclude` in the factory's clone |
| `pre_review_checks` | SovereignSpecAI `factory_config.yaml` → project `checks` |
| Blueprint format (stack, constraints, structure, node legend, Mermaid domain model) | SovereignSpecAI `architecture-example/project_blueprint.md` → `blueprint-interview` output |
| Spec format, checkbox protocol, `(manual)` / `NOT DONE:` rule | agent-skills `spec-interview`, `spec-implement/scripts/check_spec.py` → `chantier.specs` |
| Spec read-only except checkboxes (integrity hash) | app-factory `verify` ("spec modified beyond checkboxes") |
| One-question interview discipline, defaults with stated provenance, assumptions to validate | agent-skills + app-factory `app-brief-interview` → `blueprint-interview` |
| Deliverable as its own repo, never the factory's | app-factory (`apps/<slug>` are separate git repos) |
| Packs, pinned skills, repository overrides | app-factory `factory.yaml` presets, `skills-lock.json`, `AGENTS.md` "Spec workflow overrides" |
| Governed transitions, decision ≠ authorization, attention inbox, supervisor findings | ideas from mastra-ai/softwarefactory-template (operating model only, no code) |

## Consequences

- Greenfield and brownfield share the pipeline from "spec on the base branch" onward; only the entry skill differs.
- Factory upgrades never touch deliverables; in-flight tickets carry their workflow version; `drain` before upgrading.
- The `Depends on` field is a chantier addition to the spec format. Proposed follow-up: add it to `spec-interview` in agent-skills.
- aider remains the local execution layer for now (cecli migration blocked upstream); it sits behind the `Executor` port, so switching is an adapter.
