"""Small adapters: command runner, notifiers, tracker mirror, event sinks."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from chantier.domain.model import AttentionItem, Ticket, Transition


class ShellRunner:
    def run(self, command: str, cwd: Path, timeout_s: int) -> tuple[int, str]:
        try:
            proc = subprocess.run(command, shell=True, cwd=cwd, capture_output=True, text=True,
                                  timeout=timeout_s)
        except subprocess.TimeoutExpired:
            return 124, f"timed out after {timeout_s} s"
        return proc.returncode, proc.stdout + proc.stderr


# ---------------------------------------------------------------------- notifiers

class ConsoleNotifier:
    def notify(self, item: AttentionItem) -> None:
        print(f"[chantier] attention ({item.kind.value}) {item.ticket_id or item.project}: {item.message}",
              file=sys.stderr)


class TelegramNotifier:
    """Sends attention items to a Telegram chat through a bot.

    Token and chat id come from environment variables named in the config,
    never from the config file itself.
    """

    def __init__(self, token: str, chat_id: str, web_url: str | None = None) -> None:
        self.url = f"https://api.telegram.org/bot{token}/sendMessage"
        self.chat_id = chat_id
        self.web_url = web_url

    def notify(self, item: AttentionItem) -> None:
        text = f"🏗 chantier — {item.kind.value}\n{item.ticket_id or item.project}\n{item.message[:3500]}"
        if self.web_url:
            text += f"\n{self.web_url}"
        data = urllib.parse.urlencode({"chat_id": self.chat_id, "text": text}).encode()
        with urllib.request.urlopen(urllib.request.Request(self.url, data=data), timeout=15) as resp:
            resp.read()


class MultiNotifier:
    def __init__(self, notifiers: list) -> None:
        self.notifiers = notifiers

    def notify(self, item: AttentionItem) -> None:
        for notifier in self.notifiers:
            try:
                notifier.notify(item)
            except Exception as exc:  # noqa: BLE001 - a dead channel must not stop the others
                print(f"[chantier] notifier {type(notifier).__name__} failed: {exc}", file=sys.stderr)


# ---------------------------------------------------------------------- outbound integrations

class NullMirror:
    """No external tracker. Jira / GitHub Issues adapters implement the same `publish`."""

    def publish(self, ticket: Ticket, transition: Transition | None) -> None:
        return None


class NullSink:
    def emit(self, kind: str, payload: dict) -> None:
        return None


class JsonlEventSink:
    """Append-only event log: the simplest base for a remote dashboard or cloud sync."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)

    def emit(self, kind: str, payload: dict) -> None:
        record = {"at": datetime.now(UTC).isoformat(), "kind": kind, **payload}
        with self._lock, self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
