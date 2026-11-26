"""Composition root: the only place that knows every adapter."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from chantier.adapters.config_yaml import ConfigError, FactoryConfig, PromptLibrary, load_config
from chantier.adapters.executors.agents import (
    AntigravityExecutor,
    ClaudeCodeExecutor,
    CodexExecutor,
)
from chantier.adapters.executors.command import CommandExecutor
from chantier.adapters.executors.ollama import OllamaExecutor, OllamaModelProbe
from chantier.adapters.integrations import (
    ConsoleNotifier,
    JsonlEventSink,
    MultiNotifier,
    NullMirror,
    ShellRunner,
    TelegramNotifier,
)
from chantier.adapters.store_sqlite import SqliteStore
from chantier.adapters.workspace_git import GitWorkspace
from chantier.application.bench import Benchmarker
from chantier.application.board import Board
from chantier.application.ports import Executor
from chantier.application.scheduler import Scheduler
from chantier.domain.model import Phase, TicketKind

ADAPTERS = {
    "claude_code": ClaudeCodeExecutor,
    "codex": CodexExecutor,
    "antigravity": AntigravityExecutor,
    "ollama": OllamaExecutor,
    "command": CommandExecutor,
}


@dataclass
class App:
    config: FactoryConfig
    store: SqliteStore
    board: Board
    scheduler: Scheduler


def build(home: Path | None = None) -> App:
    config = load_config(home)
    store = SqliteStore(config.db_path)
    workspace = GitWorkspace(config.home / "workspaces")
    notifiers: list = [ConsoleNotifier()]
    if config.telegram:
        token = os.environ.get(config.telegram.get("token_env", "CHANTIER_TELEGRAM_TOKEN"), "")
        chat = os.environ.get(config.telegram.get("chat_id_env", "CHANTIER_TELEGRAM_CHAT_ID"), "")
        if token and chat:
            notifiers.append(TelegramNotifier(token, chat, config.web_url))
    board = Board(
        store=store,
        workspace=workspace,
        projects=config.projects,
        notifier=MultiNotifier(notifiers),
        mirror=NullMirror(),
        sink=JsonlEventSink(config.home / "events.jsonl"),
    )
    executors = _build_executors(config)
    scheduler = Scheduler(
        board=board,
        executors=executors,
        routing=config.routing,
        runner=ShellRunner(),
        prompts=PromptLibrary(config, workspace),
        logs_dir=config.home / "logs",
        prices=config.prices,
        energy=config.energy,
    )
    return App(config=config, store=store, board=board, scheduler=scheduler)


def build_benchmarker(home: Path | None = None) -> Benchmarker:
    """Wire the Ollama model benchmark without initializing or mutating SqliteStore."""
    config = load_config(home)
    review_executor: Executor | None = None
    review_ladder = config.routing.ladders.get((TicketKind.IMPLEMENT, Phase.REVIEW), ())
    if review_ladder:
        rev_name = review_ladder[0]
        rev_spec = config.executors[rev_name]
        review_executor = ADAPTERS[rev_spec.adapter](rev_spec)
    return Benchmarker(
        probe=OllamaModelProbe(),
        executor_factory=OllamaExecutor,
        workspace_factory=GitWorkspace,
        runner=ShellRunner(),
        projects=config.projects,
        prompts=PromptLibrary(config, None).render,
        review_executor=review_executor,
    )


def _build_executors(config: FactoryConfig) -> dict[str, Executor]:
    executors: dict[str, Executor] = {}
    for name, spec in config.executors.items():
        try:
            executors[name] = ADAPTERS[spec.adapter](spec)
        except KeyError:
            raise ConfigError(
                f"executors.{name}: unknown adapter '{spec.adapter}' (known: {', '.join(sorted(ADAPTERS))})"
            ) from None
    return executors
