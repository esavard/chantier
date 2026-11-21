"""HTTP API and web board. The web UI and the CLI are two clients of the same services.

Bind to localhost by default: there is no authentication (single user, home lab).
Put it behind your own reverse proxy / VPN before exposing it.
"""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel

from chantier.application.board import BoardError
from chantier.application.metrics import build_report
from chantier.application.scheduler import DRAIN_FLAG
from chantier.bootstrap import App
from chantier.domain.model import AttentionItem, Run, Ticket, Transition
from chantier.domain.workflow import RevisionConflict, TransitionError

STATIC = Path(__file__).parent / "static"


class Reason(BaseModel):
    reason: str


def create_app(app: App) -> FastAPI:
    api = FastAPI(title="chantier", version="0.1.0")

    def guard(fn):
        try:
            return fn()
        except BoardError as exc:
            raise HTTPException(404 if "unknown" in str(exc) else 409, str(exc)) from exc
        except (TransitionError, RevisionConflict) as exc:
            raise HTTPException(409, str(exc)) from exc

    @api.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @api.get("/api/projects")
    def projects() -> list[dict]:
        return [
            {"name": s.name, "repo": s.repo, "base_branch": s.base_branch, "pack": s.pack,
             "gates": {"spec_approval": s.gates.spec_approval.value, "merge": s.gates.merge.value},
             "budget_usd_per_day": s.frontier_budget_usd_per_day}
            for s in app.config.projects.values()
        ]

    @api.get("/api/tickets")
    def tickets(project: str | None = None) -> list[dict]:
        return [_ticket(t) for t in app.store.list_tickets(project)]

    @api.get("/api/tickets/{project}/{stem}")
    def ticket(project: str, stem: str) -> dict:
        t = guard(lambda: app.board.ticket(f"{project}/{stem}"))
        runs = [r for r in app.store.list_runs(project) if r.ticket_id == t.id]
        return {**_ticket(t), "feedback": t.feedback,
                "transitions": [_transition(x) for x in app.store.transitions(t.id)],
                "runs": [_run(r) for r in runs]}

    @api.post("/api/tickets/{project}/{stem}/approve")
    def approve(project: str, stem: str) -> dict:
        return _ticket(guard(lambda: app.board.approve(f"{project}/{stem}", actor="human (web)")))

    @api.post("/api/tickets/{project}/{stem}/reject")
    def reject(project: str, stem: str, body: Reason) -> dict:
        return _ticket(guard(lambda: app.board.reject(f"{project}/{stem}", body.reason, actor="human (web)")))

    @api.post("/api/tickets/{project}/{stem}/unblock")
    def unblock(project: str, stem: str) -> dict:
        return _ticket(guard(lambda: app.board.unblock(f"{project}/{stem}", actor="human (web)")))

    @api.get("/api/attention")
    def attention(all: bool = False) -> list[dict]:
        return [_attention(a) for a in app.store.list_attention(open_only=not all)]

    @api.get("/api/metrics")
    def metrics(project: str | None = None) -> dict:
        try:
            report = build_report(app.store, app.config.reference_model, app.config.prices, project)
        except KeyError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"savings": {**asdict(report.savings), "local_share": report.savings.local_share},
                "reference_model": report.reference_model,
                "by_executor": [{**asdict(e), "tier": e.tier.value} for e in report.by_executor]}

    @api.get("/api/status")
    def status() -> dict:
        return {"draining": app.store.get_flag(DRAIN_FLAG) == "1",
                "running": [r.ticket_id for r in app.store.running_runs()]}

    return api


def _ticket(t: Ticket) -> dict:
    return {"id": t.id, "project": t.project, "kind": t.kind.value, "title": t.title,
            "spec_path": t.spec_path, "stage": t.stage.value, "depends_on": list(t.depends_on),
            "attempt": t.attempt, "ladder_step": t.ladder_step, "branch": t.branch,
            "revision": t.revision, "updated_at": t.updated_at.isoformat() if t.updated_at else None}


def _transition(t: Transition) -> dict:
    return {"from": t.from_stage.value, "to": t.to_stage.value, "cause": t.cause, "actor": t.actor,
            "at": t.at.isoformat()}


def _run(r: Run) -> dict:
    return {"executor": r.executor, "tier": r.tier.value, "model": r.model, "phase": r.phase.value,
            "attempt": r.attempt, "outcome": r.outcome.value if r.outcome else None,
            "tokens": r.usage.total_tokens, "cost_usd": r.cost_usd, "seconds": r.duration_s,
            "detail": r.detail, "started_at": r.started_at.isoformat()}


def _attention(a: AttentionItem) -> dict:
    return {"id": a.id, "kind": a.kind.value, "project": a.project, "ticket": a.ticket_id,
            "message": a.message, "created_at": a.created_at.isoformat() if a.created_at else None}
