"""Verification: what the agent claims is not evidence; these checks are.

Runs after every implementation or planning attempt, on the ticket's worktree.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from chantier.application.ports import CommandRunner, Workspace
from chantier.application.supervisor import scope_violations
from chantier.domain.model import ProjectSettings, Stage, Ticket, TicketKind
from chantier.domain.workflow import find_cycle
from chantier.specs import normalized_spec_hash, parse_spec

OUTPUT_TAIL = 4000


@dataclass
class Verdict:
    failures: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures

    def feedback(self) -> str:
        return "\n".join(f"- {f}" for f in self.failures)


def verify(
    ticket: Ticket,
    settings: ProjectSettings,
    workspace: Workspace,
    runner: CommandRunner,
    worktree: Path,
    check_timeout_s: int = 1800,
) -> Verdict:
    verdict = Verdict()
    changed = workspace.changed_files(worktree, settings.base_branch)
    if not changed:
        verdict.failures.append("no changes: the attempt produced no diff")
        return verdict

    spec_text = workspace.read_file(worktree, ticket.spec_path)
    if spec_text is None:
        verdict.failures.append(f"{ticket.spec_path} was deleted")
        return verdict
    if ticket.approved_spec_sha and normalized_spec_hash(spec_text) != ticket.approved_spec_sha:
        verdict.failures.append(
            f"{ticket.spec_path} was modified beyond its checkboxes; the spec is read-only "
            "except ticking boxes and appending 'NOT DONE: <reason>'"
        )

    if ticket.kind == TicketKind.IMPLEMENT:
        _verify_implementation(ticket, settings, spec_text, changed, verdict)
    else:
        _verify_plan(ticket, settings, workspace, worktree, changed, verdict)

    for command in settings.checks:
        code, output = runner.run(command, worktree, check_timeout_s)
        if code != 0:
            verdict.failures.append(f"check failed (exit {code}): {command}\n{output[-OUTPUT_TAIL:]}")
    return verdict


def _verify_implementation(
    ticket: Ticket, settings: ProjectSettings, spec_text: str, changed: list[str], verdict: Verdict
) -> None:
    doc = parse_spec(Path(ticket.spec_path).stem, spec_text)
    for box in doc.unresolved:
        verdict.failures.append(
            f"unresolved checkbox line {box.line_no}: {box.text} "
            "(tick it if done and verified, else append 'NOT DONE: <reason>')"
        )
    if settings.scope_mode != "off":
        allow = (*settings.scope_allow, settings.specs_dir, settings.notes_file)
        outside = scope_violations(changed, doc.scope_paths, allow)
        if outside:
            message = "files changed outside the spec's declared scope: " + ", ".join(outside)
            (verdict.failures if settings.scope_mode == "enforce" else verdict.warnings).append(message)


def _verify_plan(
    ticket: Ticket,
    settings: ProjectSettings,
    workspace: Workspace,
    worktree: Path,
    changed: list[str],
    verdict: Verdict,
) -> None:
    specs_dir = settings.specs_dir.rstrip("/") + "/"
    new_specs = [f for f in changed
                 if f.startswith(specs_dir) and Path(f).name.startswith("spec_") and f.endswith(".md")]
    if not new_specs:
        verdict.failures.append(f"the plan produced no spec under {specs_dir}")
        return
    others = [f for f in changed if f not in new_specs]
    if others:
        verdict.failures.append("a plan may only add specs; also changed: " + ", ".join(others))

    docs = []
    for path in new_specs:
        text = workspace.read_file(worktree, path) or ""
        doc = parse_spec(Path(path).stem, text)
        docs.append(doc)
        if not doc.checkboxes:
            verdict.failures.append(f"{path}: no checkbox requirements")
    known = {d.stem for d in docs}
    for d in docs:
        for dep in d.depends_on:
            if dep not in known and workspace.read_file(worktree, f"{specs_dir}{dep}.md") is None:
                verdict.failures.append(f"{d.stem}: depends on unknown spec {dep}")
    graph = [
        Ticket(id=d.stem, project="", kind=TicketKind.IMPLEMENT, title=d.title, spec_path="",
               stage=Stage.DRAFT, depends_on=d.depends_on)
        for d in docs
    ]
    if cycle := find_cycle(graph):
        verdict.failures.append("dependency cycle: " + " -> ".join(cycle))
