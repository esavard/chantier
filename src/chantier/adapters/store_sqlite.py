"""SQLite state store: one file, next to the factory, never in a target repo.

One connection per operation keeps it safe across the scheduler's worker
threads; WAL mode lets the web UI read while runs write.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from chantier.domain.model import (
    AttentionItem,
    AttentionKind,
    Phase,
    Run,
    RunOutcome,
    Stage,
    Ticket,
    TicketKind,
    Tier,
    Transition,
    Usage,
)
from chantier.domain.workflow import RevisionConflict

MIGRATIONS: list[str] = [
    # 1 - initial schema
    """
    CREATE TABLE tickets (
        id TEXT PRIMARY KEY,
        project TEXT NOT NULL,
        kind TEXT NOT NULL,
        title TEXT NOT NULL,
        spec_path TEXT NOT NULL,
        stage TEXT NOT NULL,
        depends_on TEXT NOT NULL DEFAULT '[]',
        revision INTEGER NOT NULL DEFAULT 0,
        attempt INTEGER NOT NULL DEFAULT 0,
        ladder_step INTEGER NOT NULL DEFAULT 0,
        rung_attempts INTEGER NOT NULL DEFAULT 0,
        fresh_start INTEGER NOT NULL DEFAULT 0,
        approved_spec_sha TEXT,
        branch TEXT,
        workflow_version INTEGER NOT NULL,
        feedback TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE INDEX tickets_project ON tickets(project);
    CREATE TABLE transitions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticket_id TEXT NOT NULL REFERENCES tickets(id),
        from_stage TEXT NOT NULL,
        to_stage TEXT NOT NULL,
        cause TEXT NOT NULL,
        actor TEXT NOT NULL,
        at TEXT NOT NULL
    );
    CREATE INDEX transitions_ticket ON transitions(ticket_id);
    CREATE TABLE runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ticket_id TEXT NOT NULL REFERENCES tickets(id),
        executor TEXT NOT NULL,
        tier TEXT NOT NULL,
        model TEXT NOT NULL,
        phase TEXT NOT NULL,
        attempt INTEGER NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        outcome TEXT,
        input_tokens INTEGER NOT NULL DEFAULT 0,
        output_tokens INTEGER NOT NULL DEFAULT 0,
        cache_read_tokens INTEGER NOT NULL DEFAULT 0,
        reported_cost_usd REAL,
        cost_usd REAL NOT NULL DEFAULT 0,
        detail TEXT NOT NULL DEFAULT '',
        log_path TEXT
    );
    CREATE INDEX runs_ticket ON runs(ticket_id);
    CREATE TABLE attention (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        kind TEXT NOT NULL,
        project TEXT NOT NULL,
        ticket_id TEXT,
        message TEXT NOT NULL,
        created_at TEXT NOT NULL,
        resolved_at TEXT
    );
    CREATE TABLE flags (
        name TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """,
]


class SqliteStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()
        self._migrate()

    # ------------------------------------------------------------------ plumbing

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _migrate(self) -> None:
        with self._conn() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            for number, script in enumerate(MIGRATIONS[version:], start=version + 1):
                conn.executescript(script)
                conn.execute(f"PRAGMA user_version = {number}")

    @property
    def schema_version(self) -> int:
        with self._conn() as conn:
            return conn.execute("PRAGMA user_version").fetchone()[0]

    # ------------------------------------------------------------------ tickets

    def get_ticket(self, ticket_id: str) -> Ticket | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
        return _ticket(row) if row else None

    def list_tickets(self, project: str | None = None) -> list[Ticket]:
        with self._conn() as conn:
            if project:
                rows = conn.execute("SELECT * FROM tickets WHERE project = ? ORDER BY created_at, id", (project,))
            else:
                rows = conn.execute("SELECT * FROM tickets ORDER BY created_at, id")
            return [_ticket(r) for r in rows.fetchall()]

    def insert_ticket(self, ticket: Ticket) -> None:
        now = _now()
        ticket.created_at = ticket.created_at or now
        ticket.updated_at = now
        with self._write_lock, self._conn() as conn:
            conn.execute(
                """INSERT INTO tickets (id, project, kind, title, spec_path, stage, depends_on, revision,
                   attempt, ladder_step, rung_attempts, fresh_start, approved_spec_sha, branch,
                   workflow_version, feedback, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (ticket.id, ticket.project, ticket.kind.value, ticket.title, ticket.spec_path,
                 ticket.stage.value, json.dumps(list(ticket.depends_on)), ticket.revision,
                 ticket.attempt, ticket.ladder_step, ticket.rung_attempts, int(ticket.fresh_start),
                 ticket.approved_spec_sha,
                 ticket.branch, ticket.workflow_version, ticket.feedback,
                 _iso(ticket.created_at), _iso(ticket.updated_at)),
            )

    def save_ticket(self, ticket: Ticket, expected_revision: int) -> Ticket:
        with self._write_lock, self._conn() as conn:
            cur = conn.execute(
                """UPDATE tickets SET title = ?, depends_on = ?, attempt = ?, ladder_step = ?,
                   rung_attempts = ?, fresh_start = ?, approved_spec_sha = ?, branch = ?, feedback = ?,
                   revision = revision + 1, updated_at = ?
                   WHERE id = ? AND revision = ?""",
                (ticket.title, json.dumps(list(ticket.depends_on)), ticket.attempt, ticket.ladder_step,
                 ticket.rung_attempts, int(ticket.fresh_start), ticket.approved_spec_sha, ticket.branch,
                 ticket.feedback, _iso(_now()), ticket.id, expected_revision),
            )
            if cur.rowcount != 1:
                raise RevisionConflict(f"{ticket.id}: revision {expected_revision} is stale")
            row = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket.id,)).fetchone()
        return _ticket(row)

    def transition(self, ticket: Ticket, to_stage: Stage, *, expected_revision: int, cause: str,
                   actor: str) -> Ticket:
        now = _iso(_now())
        with self._write_lock, self._conn() as conn:
            row = conn.execute("SELECT stage FROM tickets WHERE id = ? AND revision = ?",
                               (ticket.id, expected_revision)).fetchone()
            if row is None:
                raise RevisionConflict(f"{ticket.id}: revision {expected_revision} is stale")
            conn.execute(
                "UPDATE tickets SET stage = ?, revision = revision + 1, updated_at = ? WHERE id = ?",
                (to_stage.value, now, ticket.id),
            )
            conn.execute(
                "INSERT INTO transitions (ticket_id, from_stage, to_stage, cause, actor, at) VALUES (?, ?, ?, ?, ?, ?)",
                (ticket.id, row["stage"], to_stage.value, cause, actor, now),
            )
            fresh = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket.id,)).fetchone()
        return _ticket(fresh)

    def transitions(self, ticket_id: str) -> list[Transition]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM transitions WHERE ticket_id = ? ORDER BY id", (ticket_id,)).fetchall()
        return [Transition(r["ticket_id"], Stage(r["from_stage"]), Stage(r["to_stage"]), r["cause"],
                           r["actor"], _dt(r["at"])) for r in rows]

    # ------------------------------------------------------------------ runs

    def add_run(self, run: Run) -> Run:
        with self._write_lock, self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO runs (ticket_id, executor, tier, model, phase, attempt, started_at, log_path)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (run.ticket_id, run.executor, run.tier.value, run.model, run.phase.value, run.attempt,
                 _iso(run.started_at), run.log_path),
            )
            run.id = cur.lastrowid
        return run

    def finish_run(self, run: Run) -> None:
        with self._write_lock, self._conn() as conn:
            conn.execute(
                """UPDATE runs SET finished_at = ?, outcome = ?, input_tokens = ?, output_tokens = ?,
                   cache_read_tokens = ?, reported_cost_usd = ?, cost_usd = ?, detail = ? WHERE id = ?""",
                (_iso(run.finished_at), run.outcome.value if run.outcome else None, run.usage.input_tokens,
                 run.usage.output_tokens, run.usage.cache_read_tokens, run.usage.reported_cost_usd,
                 run.cost_usd, run.detail, run.id),
            )

    def list_runs(self, project: str | None = None, since: datetime | None = None) -> list[Run]:
        query = "SELECT runs.* FROM runs JOIN tickets ON tickets.id = runs.ticket_id WHERE 1 = 1"
        params: list = []
        if project:
            query += " AND tickets.project = ?"
            params.append(project)
        if since:
            query += " AND runs.started_at >= ?"
            params.append(_iso(since))
        with self._conn() as conn:
            return [_run(r) for r in conn.execute(query + " ORDER BY runs.id", params).fetchall()]

    def running_runs(self) -> list[Run]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM runs WHERE outcome IS NULL ORDER BY id").fetchall()
        return [_run(r) for r in rows]

    # ------------------------------------------------------------------ attention

    def raise_attention(self, item: AttentionItem) -> AttentionItem | None:
        with self._write_lock, self._conn() as conn:
            dup = conn.execute(
                """SELECT id FROM attention WHERE resolved_at IS NULL AND kind = ? AND project = ?
                   AND ticket_id IS ? AND message = ?""",
                (item.kind.value, item.project, item.ticket_id, item.message),
            ).fetchone()
            if dup:
                return None
            item.created_at = item.created_at or _now()
            cur = conn.execute(
                "INSERT INTO attention (kind, project, ticket_id, message, created_at) VALUES (?, ?, ?, ?, ?)",
                (item.kind.value, item.project, item.ticket_id, item.message, _iso(item.created_at)),
            )
            item.id = cur.lastrowid
        return item

    def list_attention(self, open_only: bool = True) -> list[AttentionItem]:
        where = " WHERE resolved_at IS NULL" if open_only else ""
        query = f"SELECT * FROM attention{where} ORDER BY id"
        with self._conn() as conn:
            return [
                AttentionItem(AttentionKind(r["kind"]), r["project"], r["message"], r["ticket_id"], r["id"],
                              _dt(r["created_at"]), _dt(r["resolved_at"]))
                for r in conn.execute(query).fetchall()
            ]

    def resolve_attention(self, item_id: int) -> None:
        with self._write_lock, self._conn() as conn:
            conn.execute("UPDATE attention SET resolved_at = ? WHERE id = ? AND resolved_at IS NULL",
                         (_iso(_now()), item_id))

    def resolve_attention_for(self, ticket_id: str) -> None:
        with self._write_lock, self._conn() as conn:
            conn.execute("UPDATE attention SET resolved_at = ? WHERE ticket_id = ? AND resolved_at IS NULL",
                         (_iso(_now()), ticket_id))

    # ------------------------------------------------------------------ flags

    def get_flag(self, name: str) -> str | None:
        with self._conn() as conn:
            row = conn.execute("SELECT value FROM flags WHERE name = ?", (name,)).fetchone()
        return row["value"] if row else None

    def set_flag(self, name: str, value: str | None) -> None:
        with self._write_lock, self._conn() as conn:
            if value is None:
                conn.execute("DELETE FROM flags WHERE name = ?", (name,))
            else:
                conn.execute("INSERT INTO flags (name, value) VALUES (?, ?) "
                             "ON CONFLICT(name) DO UPDATE SET value = excluded.value", (name, value))


def _ticket(r: sqlite3.Row) -> Ticket:
    return Ticket(
        id=r["id"], project=r["project"], kind=TicketKind(r["kind"]), title=r["title"],
        spec_path=r["spec_path"], stage=Stage(r["stage"]), depends_on=tuple(json.loads(r["depends_on"])),
        revision=r["revision"], attempt=r["attempt"], ladder_step=r["ladder_step"],
        rung_attempts=r["rung_attempts"], fresh_start=bool(r["fresh_start"]),
        approved_spec_sha=r["approved_spec_sha"], branch=r["branch"],
        workflow_version=r["workflow_version"], feedback=r["feedback"],
        created_at=_dt(r["created_at"]), updated_at=_dt(r["updated_at"]),
    )


def _run(r: sqlite3.Row) -> Run:
    return Run(
        id=r["id"], ticket_id=r["ticket_id"], executor=r["executor"], tier=Tier(r["tier"]), model=r["model"],
        phase=Phase(r["phase"]), attempt=r["attempt"], started_at=_dt(r["started_at"]),
        finished_at=_dt(r["finished_at"]), outcome=RunOutcome(r["outcome"]) if r["outcome"] else None,
        usage=Usage(r["input_tokens"], r["output_tokens"], r["cache_read_tokens"], r["reported_cost_usd"]),
        cost_usd=r["cost_usd"], detail=r["detail"], log_path=r["log_path"],
    )


def _now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None
