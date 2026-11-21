"""Composition root: the only place that knows every adapter."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from chantier.adapters.config_yaml import ConfigError, FactoryConfig, PromptLibrary, load_config
from chantier.adapters.executors.agents import AiderOllamaExecutor, ClaudeCodeExecutor, CodexExecutor
from chantier.adapters.executors.command import CommandExecutor
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
from chantier.application.board import Board
from chantier.application.scheduler import Scheduler

ADAPTERS = {
    "claude_code": ClaudeCodeExecutor,
    "codex": CodexExecutor,
    "aider_ollama": AiderOllamaExecutor,
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
    executors = {}
    for name, spec in config.executors.items():
        try:
            executors[name] = ADAPTERS[spec.adapter](spec)
        except KeyError:
            raise ConfigError(f"executors.{name}: unknown adapter '{spec.adapter}' "
                              f"(known: {', '.join(sorted(ADAPTERS))})") from None
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
