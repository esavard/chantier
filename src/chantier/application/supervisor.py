"""Supervisor: detect a local agent that is going off the rails.

Cheap deterministic signals first; they cost nothing and catch the common
failures of local models (tool-call loops, repeating the same edit forever).
Time-based signals (idle, hard timeout) are enforced by the executor runner,
which knows the process clock.

When a signal fires, the run is killed and the ticket follows the routing
ladder: retry on the same rung, then escalate to a frontier executor, then
ask the human.
"""

from __future__ import annotations

import re
from collections import Counter, deque
from dataclasses import dataclass
from fnmatch import fnmatch

_NOISE = re.compile(r"\d+(\.\d+)?|0x[0-9a-f]+|\s+", re.IGNORECASE)


@dataclass
class LoopDetector:
    """Kills a run when the same meaningful output line keeps coming back.

    `window` recent lines are kept; if one normalized line appears `threshold`
    times in that window, the agent is looping.
    """

    window: int = 60
    threshold: int = 8
    min_length: int = 12

    def __post_init__(self) -> None:
        self._recent: deque[str] = deque(maxlen=self.window)
        self._counts: Counter[str] = Counter()

    def __call__(self, line: str) -> str | None:
        key = _NOISE.sub(" ", line.strip().lower()).strip()
        if len(key) < self.min_length:
            return None
        if len(self._recent) == self._recent.maxlen:
            old = self._recent[0]
            self._counts[old] -= 1
        self._recent.append(key)
        self._counts[key] += 1
        if self._counts[key] >= self.threshold:
            return f"loop detected: same output {self._counts[key]} times in {self.window} lines: {line.strip()[:120]}"
        return None


def scope_violations(changed: list[str], scope_paths: tuple[str, ...], allow: tuple[str, ...]) -> list[str]:
    """Changed files outside the spec's declared modules/paths (its blast radius).

    A spec that declares no paths has no measurable scope: nothing is reported.
    """
    if not scope_paths:
        return []
    allowed = tuple(p.rstrip("/") for p in (*scope_paths, *allow))
    return [f for f in changed if not any(_within(f, p) for p in allowed)]


def _within(path: str, scope: str) -> bool:
    if any(ch in scope for ch in "*?["):
        return fnmatch(path, scope)
    return path == scope or path.startswith(scope + "/")
