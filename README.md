# chantier — usine à code opiniâtre

*An opinionated, spec-driven code factory.* Humans decide **what** to build and approve it at two gates; agents build it. Local LLMs do the bulk of the work, frontier agents do the heavy lifting and take over when a local model goes off the rails, and every dollar spent (or saved) is accounted for.

```
 spec (human + interview skill)          board, gates, audit           deliverable repo
 ─────────────────────────────    ┌──────────────────────────────┐    ───────────────────
 greenfield: blueprint-interview ─┤  chantier                     │
      └─ chantier plan ─► specs   │   sync ─► DRAFT ─gate─► READY │─► branch chantier/spec_NNN
 brownfield: spec-interview ──────┤   DAG + seats ─► RUNNING      │
                                  │   watchdog ─► retry/escalate  │─► merge (gate, or auto)
                                  │   verify ─► AWAITING_MERGE    │
                                  └───────┬──────────────┬────────┘
                         local (Ollama) ◄─┘              └─► frontier (Claude Code, Codex)
```

## What it does

- **Spec-driven, both ways in.** Greenfield starts with the `blueprint-interview` skill and a frontier planner that turns the blueprint into specs with dependencies. Brownfield starts with `spec-interview` ([esavard/agent-skills](https://github.com/esavard/agent-skills)). Both produce the same spec format, and from there the pipeline is identical.
- **The deliverable is a separate repo.** No side-car. chantier works in its own clone and worktrees; the deliverable only ever receives branches.
- **Deterministic routing with escalation.** A ladder per ticket kind: `implement: [frank, claude]` means local first, frontier after two failed attempts. Escalation measures difficulty instead of guessing it.
- **A supervisor that catches a local model going in circles.** Loop detection, idle and hard timeouts, then retry or escalate. Frontier work starts from a clean branch, not from the local model's mess.
- **Verification, not claims.** After every attempt: the spec text is unchanged except its checkboxes, every checkbox is resolved (`[x]`, `(manual)` or `NOT DONE: <reason>`), changed files stay inside the spec's declared paths, and the project's own checks pass.
- **Auto-advance with gates where they matter.** Spec approval and merge are `human` or `auto` per project. Everything in between moves on its own. Parallel where the dependency graph allows, within each executor's seats.
- **Notifications only when a human is needed.** Gate waiting, escalation exhausted, budget reached. Console and Telegram.
- **Cost control and an honest savings figure.** Per-project daily frontier budget. Savings count only successful local work that was never redone on the frontier, minus GPU energy.
- **Upgradable without disturbing work in progress.** State lives in the factory (SQLite, migrations), tickets record their workflow version, and `chantier drain` finishes running work before an upgrade.
- **Ports for what comes next.** External trackers (Jira, GitHub Issues), remote dashboard sync, other executors: each is an adapter behind a port that already exists.

## Quick start

```bash
uv tool install --from git+https://github.com/esavard/chantier 'chantier[web]'   # or: uv sync --extra web
chantier init                        # writes ~/.chantier/config.yaml: edit executors, routing, pricing
chantier add calc --repo ~/code/calc --check "uv run pytest -q"
```

Brownfield (existing repo):

```bash
# in the deliverable repo, with Claude Code or Codex: spec-interview, then commit the spec
chantier sync calc                   # imports docs/specs/spec_*.md as DRAFT tickets
chantier approve calc/spec_004_history
chantier run                         # or: chantier serve --run   (web board on :8765)
```

Greenfield (new repo):

```bash
# in the new repo: run the blueprint-interview skill, commit docs/blueprint.md
chantier plan calc                   # creates ticket calc/plan_001 (DRAFT: the blueprint is its spec)
chantier approve calc/plan_001       # gate 1: the blueprint is final; the planner runs on the frontier
# the planner pushes branch chantier/plan_001 with the proposed specs; review them, then
chantier approve calc/plan_001       # gate 2: merge the specs; `chantier run` syncs and builds them
```

Everyday commands:

| Command | What |
|---|---|
| `chantier board [project] [--watch]` | kanban in the terminal |
| `chantier serve [--run]` | web board + HTTP API (localhost) |
| `chantier attention` | what needs a human |
| `chantier show <ticket>` | transitions, runs, feedback |
| `chantier approve / reject / unblock <ticket>` | gates |
| `chantier metrics [project]` | spend, local share, savings |
| `chantier drain` / `resume` | before / after upgrading the factory |

## Configuration

`~/.chantier/` (override with `CHANTIER_HOME`):

```
config.yaml              executors, routing ladders, pricing, energy, notifications
projects/<name>.yaml     repo, base_branch, pack, gates, checks, review, scope_mode, budget
packs/<name>/            private packs: defaults + prompt overrides (prompts/implement.md, ...)
chantier.db              state
workspaces/ logs/ events.jsonl
```

A project file:

```yaml
repo: ~/code/calc            # or a git URL
base_branch: main
pack: generic
gates: {spec_approval: human, merge: human}
checks: ["uv run pytest -q", "uv run lint-imports"]
review: false                # true: a frontier review run after verification
scope_mode: warn             # warn | enforce | off   (files outside the spec's declared paths)
frontier_budget_usd_per_day: 5
```

See [`src/chantier/config.example.yaml`](src/chantier/config.example.yaml) for executors and routing.

## Spec format

chantier reads the format produced by `spec-interview`. It needs three things from a spec, all optional except the checkboxes:

```markdown
## 2. Context
**Relevant modules/paths:** `lib/history/`, `lib/app.dart`     <- blast radius (scope check)
**Depends on:** spec_001_basic_calculator                       <- scheduling order

## 4. Requirements
- [ ] REQ-001 ... — Acceptance: ...                               <- verified checkboxes
```

`Depends on` is a chantier addition; specs without it simply have no dependencies.

## Documentation

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): layers, ports, ticket lifecycle, verification, metrics
- [docs/adr/0001-pivot-from-sovereignspecai.md](docs/adr/0001-pivot-from-sovereignspecai.md): why this repo exists and what came from where
- [docs/ROADMAP.md](docs/ROADMAP.md): ports without adapters yet, known limits
- [CONVENTIONS.md](CONVENTIONS.md): engineering rules of this repo

## Development

```bash
uv sync --extra web
uv run pytest -q          # unit + end-to-end scenarios on real git repos with scripted agents
uv run lint-imports       # architecture contracts (layers, pure domain)
```

MIT licensed.
