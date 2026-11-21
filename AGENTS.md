# AGENTS.md

chantier is a spec-driven code factory (Python, hexagonal). Read `CONVENTIONS.md` and `docs/ARCHITECTURE.md` before changing code.

## Commands
```
uv sync --extra web
uv run pytest -q
uv run lint-imports
```

## Spec workflow overrides
- Specs for chantier live in `docs/specs/`.
- Definition of Done always includes: `uv run pytest -q` and `uv run lint-imports` pass.

## Hard rules
- Stage changes only through `Board.move`. Adapters only through ports. Domain stays pure.
- Never write factory files into a deliverable repo in code or tests.
- Never commit, branch or push unless asked.
