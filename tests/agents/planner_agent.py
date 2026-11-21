"""Scripted planner: writes two specs, the second depending on the first."""

from pathlib import Path

SPEC = """# {stem}

## 1. Spec
**Title:** {title}

## 2. Context
**Relevant modules/paths:** `src/`
**Depends on:** {deps}

## 4. Requirements
- [ ] {title} works — Acceptance: module exists
"""

specs = Path("docs/specs")
specs.mkdir(parents=True, exist_ok=True)
(specs / "spec_001_ledger.md").write_text(SPEC.format(stem="spec_001_ledger", title="Ledger aggregate", deps="none"))
(specs / "spec_002_report.md").write_text(
    SPEC.format(stem="spec_002_report", title="Report read model", deps="spec_001_ledger"))
print('CHANTIER_USAGE {"input_tokens": 30000, "output_tokens": 8000, "cost_usd": 0.21}')
