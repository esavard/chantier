"""`chantier` command line."""

from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

from rich.console import Console
from rich.live import Live

from chantier.adapters.config_yaml import ConfigError, factory_home, write_project
from chantier.adapters.workspace_git import GitError
from chantier.application.board import BoardError
from chantier.application.metrics import build_report
from chantier.application.scheduler import DRAIN_FLAG
from chantier.bootstrap import App, build
from chantier.cli import views

console = Console()
EXAMPLE_CONFIG = Path(__file__).resolve().parent.parent / "config.example.yaml"


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args) or 0
    except (BoardError, ConfigError, GitError) as exc:
        console.print(f"[red]error:[/] {exc}")
        return 1
    except KeyboardInterrupt:
        return 130


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="chantier", description="Opinionated, spec-driven code factory.")
    sub = p.add_subparsers(required=True, metavar="command")

    def cmd(name: str, func, help_: str) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help_, description=help_)
        sp.set_defaults(func=func)
        return sp

    cmd("init", cmd_init, "create the factory home with an example config")
    sp = cmd("add", cmd_add, "register a deliverable repository as a project")
    sp.add_argument("name")
    sp.add_argument("--repo", required=True, help="git URL or local path of the deliverable")
    sp.add_argument("--base", default="main", help="base branch (default: main)")
    sp.add_argument("--pack", default="generic")
    sp.add_argument("--check", action="append", default=[], help="verification command (repeatable)")
    cmd("projects", cmd_projects, "list projects")
    sp = cmd("sync", cmd_sync, "import new specs from the deliverables' base branches")
    sp.add_argument("project", nargs="?")
    sp = cmd("plan", cmd_plan, "greenfield: ask the planner to turn the blueprint into specs")
    sp.add_argument("project")
    sp = cmd("board", cmd_board, "kanban of tickets")
    sp.add_argument("project", nargs="?")
    sp.add_argument("--watch", action="store_true", help="refresh every few seconds")
    sp = cmd("show", cmd_show, "ticket details, transitions and runs")
    sp.add_argument("ticket")
    sp = cmd("approve", cmd_approve, "pass the gate a ticket waits on (spec approval or merge)")
    sp.add_argument("ticket")
    sp = cmd("reject", cmd_reject, "reject a merge (work goes back) or cancel a draft/blocked ticket")
    sp.add_argument("ticket")
    sp.add_argument("reason")
    sp = cmd("unblock", cmd_unblock, "send a blocked ticket back to READY")
    sp.add_argument("ticket")
    sp.add_argument("--reset-ladder", action="store_true", help="start again from the first executor")
    sp = cmd("attention", cmd_attention, "what needs a human")
    sp.add_argument("--all", action="store_true")
    sp = cmd("resolve", cmd_resolve, "dismiss an attention item")
    sp.add_argument("id", type=int)
    sp = cmd("tick", cmd_tick, "one scheduling cycle")
    sp.add_argument("--wait", action="store_true", help="wait for dispatched runs to finish")
    sp = cmd("run", cmd_run, "run the factory: sync and schedule in a loop")
    sp.add_argument("--interval", type=int, default=30, help="seconds between cycles")
    cmd("drain", cmd_drain, "finish running work but dispatch nothing new (before an upgrade)")
    cmd("resume", cmd_resume, "leave drain mode")
    sp = cmd("metrics", cmd_metrics, "costs and savings")
    sp.add_argument("project", nargs="?")
    sp = cmd("serve", cmd_serve, "web UI and HTTP API")
    sp.add_argument("--host", default="127.0.0.1")
    sp.add_argument("--port", type=int, default=8765)
    sp.add_argument("--run", action="store_true", help="also run the factory loop in this process")
    sp.add_argument("--interval", type=int, default=30)
    return p


# ---------------------------------------------------------------------- setup

def cmd_init(args) -> int:
    home = factory_home()
    home.mkdir(parents=True, exist_ok=True)
    (home / "projects").mkdir(exist_ok=True)
    (home / "packs").mkdir(exist_ok=True)
    config = home / "config.yaml"
    if config.exists():
        console.print(f"{config} already exists; left untouched.")
    else:
        config.write_text(EXAMPLE_CONFIG.read_text())
        console.print(f"Created {config}. Edit executors, routing and pricing, then `chantier add`.")
    return 0


def cmd_add(args) -> int:
    data = {"repo": args.repo, "base_branch": args.base, "pack": args.pack}
    if args.check:
        data["checks"] = args.check
    path = write_project(factory_home(), args.name, data)
    console.print(f"Project written to {path} (edit it for gates, checks, review, budget).")
    app = build()
    report = app.board.sync_project(args.name)
    console.print(f"{len(report.added)} spec(s) imported.")
    return 0


def cmd_projects(args) -> int:
    app = build()
    for name, s in app.config.projects.items():
        console.print(f"[bold]{name}[/]  {s.repo}  ({s.base_branch}, pack {s.pack}, "
                      f"gates spec={s.gates.spec_approval.value} merge={s.gates.merge.value})")
    return 0


# ---------------------------------------------------------------------- intake and gates

def cmd_sync(args) -> int:
    app = build()
    for project in [args.project] if args.project else list(app.config.projects):
        report = app.board.sync_project(project)
        console.print(f"{project}: {len(report.added)} new, {report.unchanged} known")
        for tid in report.added:
            console.print(f"  + {tid}")
        for error in report.errors:
            console.print(f"  [red]{error}[/]")
    return 0


def cmd_plan(args) -> int:
    ticket = build().board.request_plan(args.project)
    console.print(f"Plan ticket {ticket.id} created ({ticket.stage.value}).")
    return 0


def cmd_approve(args) -> int:
    ticket = build().board.approve(args.ticket)
    console.print(f"{ticket.id} -> {ticket.stage.value}")
    return 0


def cmd_reject(args) -> int:
    ticket = build().board.reject(args.ticket, args.reason)
    console.print(f"{ticket.id} -> {ticket.stage.value}")
    return 0


def cmd_unblock(args) -> int:
    ticket = build().board.unblock(args.ticket, reset_ladder=args.reset_ladder)
    console.print(f"{ticket.id} -> {ticket.stage.value}")
    return 0


# ---------------------------------------------------------------------- views

def cmd_board(args) -> int:
    app = build()

    def render():
        title = f"chantier — {args.project or 'all projects'}"
        if app.store.get_flag(DRAIN_FLAG) == "1":
            title += "  [draining]"
        return views.board(app.store.list_tickets(args.project), title)

    if not args.watch:
        console.print(render())
        return 0
    with Live(render(), console=console, refresh_per_second=1, screen=True) as live:
        while True:
            time.sleep(3)
            live.update(render())


def cmd_show(args) -> int:
    app = build()
    ticket = app.board.ticket(args.ticket)
    runs = [r for r in app.store.list_runs(ticket.project) if r.ticket_id == ticket.id]
    console.print(views.ticket_detail(ticket, app.store.transitions(ticket.id), runs))
    return 0


def cmd_attention(args) -> int:
    console.print(views.attention(build().store.list_attention(open_only=not args.all)))
    return 0


def cmd_resolve(args) -> int:
    build().store.resolve_attention(args.id)
    return 0


def cmd_metrics(args) -> int:
    app = build()
    report = build_report(app.store, app.config.reference_model, app.config.prices, args.project)
    console.print(views.metrics(report))
    return 0


# ---------------------------------------------------------------------- running

def cmd_tick(args) -> int:
    app = build()
    app.scheduler.recover()
    report = app.scheduler.tick()
    _print_tick(report)
    if args.wait:
        app.scheduler.wait()
    return 0


def cmd_run(args) -> int:
    app = build()
    console.print(f"chantier running; cycle every {args.interval}s. Ctrl-C to stop "
                  "(running agents finish their attempt first).")
    try:
        factory_loop(app, args.interval, threading.Event())
    finally:
        app.scheduler.shutdown()
    return 0


def factory_loop(app: App, interval: int, stop: threading.Event) -> None:
    for tid in app.scheduler.recover():
        console.print(f"recovered {tid} after restart")
    while not stop.is_set():
        for project in app.config.projects:
            try:
                report = app.board.sync_project(project)
                for tid in report.added:
                    console.print(f"+ {tid}")
            except Exception as exc:  # noqa: BLE001 - an unreachable repo must not stop the others
                console.print(f"[red]{project}: sync failed: {exc}[/]")
        try:
            _print_tick(app.scheduler.tick())
        except Exception as exc:  # noqa: BLE001 - e.g. a human moved a ticket mid-tick; next cycle re-reads
            console.print(f"[yellow]tick skipped: {exc}[/]")
        stop.wait(interval)


def cmd_drain(args) -> int:
    build().store.set_flag(DRAIN_FLAG, "1")
    console.print("Draining: running attempts finish, nothing new starts. `chantier resume` to leave.")
    return 0


def cmd_resume(args) -> int:
    build().store.set_flag(DRAIN_FLAG, None)
    console.print("Resumed.")
    return 0


def cmd_serve(args) -> int:
    try:
        import uvicorn

        from chantier.api.app import create_app
    except ImportError:
        console.print("[red]The web UI needs the 'web' extra:[/] uv sync --extra web")
        return 1
    app = build()
    stop = threading.Event()
    if args.run:
        threading.Thread(target=factory_loop, args=(app, args.interval, stop), daemon=True).start()
    try:
        uvicorn.run(create_app(app), host=args.host, port=args.port, log_level="warning")
    finally:
        stop.set()
    return 0


def _print_tick(report) -> None:
    for tid in report.merged:
        console.print(f"[green]✓ merged[/] {tid}")
    for tid, executor in report.dispatched:
        console.print(f"[blue]→ {tid}[/] on {executor}")


if __name__ == "__main__":
    sys.exit(main())
