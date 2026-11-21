"""Factory configuration: YAML files in the factory home, never in a deliverable.

    $CHANTIER_HOME/                   (default ~/.chantier)
      config.yaml                     executors, routing, pricing, energy, notifications
      projects/<name>.yaml            one file per deliverable repository
      packs/<name>/pack.yaml          private packs (optional prompts/ overrides)
      chantier.db                     state (SQLite)
      workspaces/  logs/  events.jsonl
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml

from chantier.domain.economics import EnergyModel, Price
from chantier.domain.model import Gates, GateMode, Phase, ProjectSettings, Ticket, TicketKind, Tier
from chantier.domain.routing import ExecutorSpec, RoutingPolicy

PACKAGE_DIR = Path(__file__).resolve().parent.parent
BUILTIN_PACKS = PACKAGE_DIR / "packs"
BUILTIN_PROMPTS = PACKAGE_DIR / "prompts"


class ConfigError(ValueError):
    pass


@dataclass
class FactoryConfig:
    home: Path
    executors: dict[str, ExecutorSpec] = field(default_factory=dict)
    routing: RoutingPolicy = field(default_factory=RoutingPolicy)
    prices: dict[str, Price] = field(default_factory=dict)
    reference_model: str = ""
    energy: EnergyModel = field(default_factory=EnergyModel)
    telegram: dict[str, str] = field(default_factory=dict)
    web_url: str | None = None
    projects: dict[str, ProjectSettings] = field(default_factory=dict)
    project_packs: dict[str, Path] = field(default_factory=dict)  # project -> pack dir

    @property
    def db_path(self) -> Path:
        return self.home / "chantier.db"


def factory_home() -> Path:
    return Path(os.environ.get("CHANTIER_HOME", "~/.chantier")).expanduser()


def load_config(home: Path | None = None) -> FactoryConfig:
    home = home or factory_home()
    raw = _read(home / "config.yaml")
    config = FactoryConfig(home=home)
    for name, data in (raw.get("executors") or {}).items():
        config.executors[name] = ExecutorSpec(
            name=name,
            adapter=_required(data, "adapter", f"executors.{name}"),
            tier=Tier(_required(data, "tier", f"executors.{name}")),
            model=str(data.get("model", "")),
            seats=int(data.get("seats", 1)),
            args=tuple(str(a) for a in data.get("args", [])),
            timeout_s=int(data.get("timeout_s", 3600)),
            idle_timeout_s=int(data.get("idle_timeout_s", 600)),
        )
    routing = raw.get("routing") or {}
    ladders = {}
    for key, ladder in (routing.get("ladders") or {}).items():
        kind, _, phase = key.partition("/")
        try:
            ladders[(TicketKind(kind), Phase(phase or kind))] = tuple(ladder)
        except ValueError as exc:
            raise ConfigError(f"routing.ladders: bad key '{key}' (expected <kind>/<phase>)") from exc
        for name in ladder:
            if name not in config.executors:
                raise ConfigError(f"routing.ladders.{key}: unknown executor '{name}'")
    config.routing = RoutingPolicy(ladders=ladders, attempts_per_rung=int(routing.get("attempts_per_rung", 2)))
    pricing = raw.get("pricing") or {}
    config.prices = {m: Price(float(p.get("input", 0)), float(p.get("output", 0)), float(p.get("cache_read", 0)))
                     for m, p in (pricing.get("models") or {}).items()}
    config.reference_model = str(pricing.get("reference_model", ""))
    energy = raw.get("energy") or {}
    config.energy = EnergyModel(float(energy.get("gpu_watts", 0)), float(energy.get("kwh_price", 0)))
    config.telegram = (raw.get("notifications") or {}).get("telegram") or {}
    config.web_url = (raw.get("web") or {}).get("url")
    for file in sorted((home / "projects").glob("*.yaml")):
        settings, pack_dir = load_project(file, home)
        config.projects[settings.name] = settings
        config.project_packs[settings.name] = pack_dir
    return config


def load_project(file: Path, home: Path) -> tuple[ProjectSettings, Path]:
    data = _read(file)
    name = file.stem
    pack_name = str(data.get("pack", "generic"))
    pack_dir = find_pack(pack_name, home)
    merged = {**(_read(pack_dir / "pack.yaml").get("defaults") or {})}
    for key, value in data.items():
        if key == "gates" and isinstance(value, dict):
            merged["gates"] = {**(merged.get("gates") or {}), **value}
        else:
            merged[key] = value
    gates = merged.get("gates") or {}
    try:
        settings = ProjectSettings(
            name=name,
            repo=str(_required(merged, "repo", file.name)),
            base_branch=str(merged.get("base_branch", "main")),
            pack=pack_name,
            specs_dir=str(merged.get("specs_dir", "docs/specs")).rstrip("/"),
            blueprint_path=str(merged.get("blueprint_path", "docs/blueprint.md")),
            gates=Gates(GateMode(gates.get("spec_approval", "human")), GateMode(gates.get("merge", "human"))),
            checks=tuple(str(c) for c in merged.get("checks", [])),
            review=bool(merged.get("review", False)),
            scope_mode=str(merged.get("scope_mode", "warn")),
            scope_allow=tuple(str(p) for p in merged.get("scope_allow", ())),
            frontier_budget_usd_per_day=(float(merged["frontier_budget_usd_per_day"])
                                         if merged.get("frontier_budget_usd_per_day") is not None else None),
            notes_file=str(merged.get("notes_file", "NOTES.md")),
        )
    except ValueError as exc:
        raise ConfigError(f"{file}: {exc}") from exc
    if settings.scope_mode not in ("warn", "enforce", "off"):
        raise ConfigError(f"{file}: scope_mode must be warn, enforce or off")
    return settings, pack_dir


def find_pack(name: str, home: Path) -> Path:
    for root in (home / "packs", BUILTIN_PACKS):
        if (root / name / "pack.yaml").is_file():
            return root / name
    raise ConfigError(f"pack '{name}' not found in {home / 'packs'} or the built-in packs")


def write_project(home: Path, name: str, data: dict) -> Path:
    path = home / "projects" / f"{name}.yaml"
    if path.exists():
        raise ConfigError(f"project '{name}' already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


class PromptLibrary:
    """Prompt templates: the project's pack may override the built-in ones."""

    def __init__(self, config: FactoryConfig, workspace) -> None:
        self.config = config
        self.workspace = workspace

    def template(self, project: str, name: str) -> str:
        pack_dir = self.config.project_packs.get(project)
        for root in ([pack_dir / "prompts"] if pack_dir else []) + [BUILTIN_PROMPTS]:
            if (root / f"{name}.md").is_file():
                return (root / f"{name}.md").read_text(encoding="utf-8")
        raise ConfigError(f"prompt template '{name}' not found")

    def __call__(self, ticket: Ticket, phase: Phase, worktree: Path) -> str:
        settings = self.config.projects[ticket.project]
        feedback = ""
        if ticket.feedback and phase != Phase.REVIEW:
            feedback = (f"\nThis is attempt {ticket.attempt + 1}. Fix what the factory reported on the "
                        f"previous attempt before anything else:\n{ticket.feedback}\n")
        checks = ""
        if settings.checks:
            checks = "- The factory will run these checks; they must pass: " + "; ".join(
                f"`{c}`" for c in settings.checks) + "\n"
        values = {
            "spec_path": ticket.spec_path,
            "specs_dir": settings.specs_dir,
            "notes_file": settings.notes_file,
            "base_ref": f"origin/{settings.base_branch}",
            "today": date.today().isoformat(),
            "feedback_section": feedback,
            "checks_section": checks,
        }
        return self.template(ticket.project, phase.value).format_map(_Defaulting(values))


class _Defaulting(dict):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _read(path: Path) -> dict:
    if not path.is_file():
        return {}
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a mapping")
    return data


def _required(data: dict, key: str, where: str):
    if key not in data or data[key] in (None, ""):
        raise ConfigError(f"{where}: '{key}' is required")
    return data[key]
