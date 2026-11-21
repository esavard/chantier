# CONVENTIONS.md

Engineering rules for chantier itself. Draft written at the pivot (2026-11-21); review it with `spec-conventions-interview`.

## 1. Architecture

- Hexagonal. Layers, top to bottom: `cli` → `api` → `bootstrap` → `adapters` → `application` → `specs` → `domain`. A layer imports only from layers below it. `uv run lint-imports` enforces it.
- `chantier.domain` is pure: no I/O, no `sqlite3`, `subprocess`, `yaml`, `rich`, `fastapi`.
- Every integration is an adapter behind a port in `application/ports.py`. New integration = new adapter, never a branch in the scheduler.
- `bootstrap.py` is the only module that instantiates adapters.

## 2. Domain rules

- A ticket changes stage only through `Board.move` (legal transition + revision check + audit row). Never update `stage` anywhere else.
- Routing is deterministic configuration. No LLM decides which executor runs.
- An agent's claim is not evidence. Done means verified by chantier (spec integrity, checkboxes, scope, checks).
- Nothing from the factory is ever written into a deliverable repo except branches and merges.

## 3. Testing

- `uv run pytest -q` must pass. Domain logic: unit tests. Orchestration: end-to-end scenarios on real temporary git repos with scripted agents (`tests/agents/`), no mocks of git.
- A bug fix starts with a failing scenario or unit test.
- Never delete, skip or weaken a test to get green.

## 4. Dependencies

- Runtime: standard library, PyYAML, Rich. The web UI (FastAPI, uvicorn) is an optional extra.
- Integrations with external services (Telegram, Ollama) use `urllib` unless a library removes real complexity.
- New runtime dependency: justify it in the PR.

## 5. Style

- Python 3.12+, type hints, dataclasses. Line length 120. English for code, comments and docs.
- Comments explain why, not what. No dead code.

## 6. Process

- One spec per change in `docs/specs/` for anything beyond a fix (yes, chantier can build chantier).
- Breaking change to the workflow (stages, gates, transition rules): bump `WORKFLOW_VERSION` and add a store migration if needed.
