from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest
import yaml

from chantier.bootstrap import App, build

AGENTS = Path(__file__).parent / "agents"

SPEC = """# {stem}

> Point-in-time record (ADR style).

## 1. Spec
**Title:** {title}
**Goal:** test goal

## 2. Context
**Relevant modules/paths:** `src/`
**Depends on:** {deps}

## 4. Requirements
- [ ] Requirement A — Acceptance: module exists
- [ ] Requirement B — Acceptance: module importable

## 8. Definition of Done
- [ ] Tests pass
- [ ] (manual) Smoke test on device
"""


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@dataclass
class Env:
    home: Path
    target: Path
    tmp: Path

    def app(self) -> App:
        return build(self.home)

    def write_spec(self, stem: str, title: str = "Feature", deps: str = "none", commit: bool = True) -> None:
        path = self.target / "docs" / "specs" / f"{stem}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(SPEC.format(stem=stem, title=title, deps=deps))
        if commit:
            git(self.target, "add", "-A")
            git(self.target, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", f"spec {stem}")

    def configure(self, executors: dict[str, tuple[str, str]], ladders: dict[str, list[str]],
                  project: dict | None = None, attempts_per_rung: int = 2, idle_timeout_s: int = 30) -> None:
        config = {
            "executors": {
                name: {
                    "adapter": "command",
                    "tier": tier,
                    "model": "ref" if tier == "frontier" else "local-model",
                    "args": [sys.executable, str(AGENTS / script)],
                    "idle_timeout_s": idle_timeout_s,
                    "timeout_s": 60,
                }
                for name, (script, tier) in executors.items()
            },
            "routing": {"attempts_per_rung": attempts_per_rung, "ladders": ladders},
            "pricing": {"reference_model": "ref", "models": {"ref": {"input": 3.0, "output": 15.0}}},
            "energy": {"gpu_watts": 0, "kwh_price": 0},
        }
        (self.home / "config.yaml").write_text(yaml.safe_dump(config))
        proj = {"repo": str(self.target), "base_branch": "main", **(project or {})}
        (self.home / "projects").mkdir(exist_ok=True)
        (self.home / "projects" / "demo.yaml").write_text(yaml.safe_dump(proj))


@pytest.fixture
def env(tmp_path: Path) -> Env:
    target = tmp_path / "deliverable"
    target.mkdir()
    git(target, "init", "-q", "-b", "main")
    # Lets chantier push the base branch into this non-bare repo (auto-merge tests).
    git(target, "config", "receive.denyCurrentBranch", "updateInstead")
    (target / "README.md").write_text("deliverable\n")
    git(target, "add", "-A")
    git(target, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init")
    home = tmp_path / "home"
    home.mkdir()
    return Env(home=home, target=target, tmp=tmp_path)
