"""Read the spec format produced by `spec-interview` (esavard/agent-skills).

Chantier only reads what it needs to schedule and verify a spec:
title, dependencies, declared scope (blast radius) and checkboxes.
The spec stays the contract; chantier never rewrites it.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import PurePosixPath

CHECKBOX = re.compile(r"^(\s*(?:[-*+]|\d+[.)])\s+\[)([ xX])(\]\s*)(.*)$")
FENCE = re.compile(r"^\s*(```|~~~)")
MANUAL_TAG = re.compile(r"\(manual\)", re.IGNORECASE)
NOT_DONE = re.compile(r"NOT DONE:\s*(\S.{2,})")
FIELD = re.compile(r"^\*\*(?P<name>[^*:]+):\*\*\s*(?P<value>.*)$")
BACKTICKED = re.compile(r"`([^`]+)`")
NONE_VALUES = {"", "none", "n/a", "-", "aucune", "aucun"}


@dataclass(frozen=True)
class Checkbox:
    line_no: int
    checked: bool
    text: str

    @property
    def resolved(self) -> bool:
        return self.checked or bool(MANUAL_TAG.search(self.text) or NOT_DONE.search(self.text))


@dataclass(frozen=True)
class SpecDoc:
    stem: str
    title: str
    depends_on: tuple[str, ...]
    scope_paths: tuple[str, ...]
    checkboxes: tuple[Checkbox, ...]

    @property
    def unresolved(self) -> list[Checkbox]:
        return [c for c in self.checkboxes if not c.resolved]


def parse_spec(stem: str, text: str) -> SpecDoc:
    lines = _outside_fences(text)
    fields = _fields(lines)
    title = fields.get("title", [""])[0].strip() or _first_heading(lines) or stem
    return SpecDoc(
        stem=stem,
        title=title,
        depends_on=tuple(_spec_refs(fields.get("depends on", []))),
        scope_paths=tuple(_paths(fields.get("relevant modules/paths", []))),
        checkboxes=tuple(_checkboxes(text)),
    )


def normalized_spec_hash(text: str) -> str:
    """Hash of the spec with every checkbox unticked and NOT DONE notes removed.

    Agents may tick boxes and append `NOT DONE: <reason>`; anything else they
    change in the spec makes this hash differ (spec integrity, borrowed from the
    app factory's `verify`).
    """
    out = []
    in_fence = False
    for line in text.splitlines():
        if FENCE.match(line):
            in_fence = not in_fence
        if not in_fence and (m := CHECKBOX.match(line)):
            body = re.sub(r"\s*NOT DONE:.*$", "", m.group(4)).rstrip()
            line = f"{m.group(1)} {m.group(3)}{body}"
        out.append(line.rstrip())
    return hashlib.sha256("\n".join(out).strip().encode()).hexdigest()


def _outside_fences(text: str) -> list[tuple[int, str]]:
    result, in_fence = [], False
    for no, line in enumerate(text.splitlines(), start=1):
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence:
            result.append((no, line))
    return result


def _checkboxes(text: str) -> list[Checkbox]:
    return [
        Checkbox(no, m.group(2) in "xX", m.group(4).strip())
        for no, line in _outside_fences(text)
        if (m := CHECKBOX.match(line))
    ]


def _first_heading(lines: list[tuple[int, str]]) -> str:
    for _, line in lines:
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def _fields(lines: list[tuple[int, str]]) -> dict[str, list[str]]:
    """`**Name:** value` plus the bullet lines that follow it."""
    fields: dict[str, list[str]] = {}
    current: list[str] | None = None
    for _, line in lines:
        stripped = line.strip()
        if m := FIELD.match(stripped):
            current = fields.setdefault(m.group("name").strip().lower(), [])
            current.append(m.group("value"))
        elif current is not None and stripped.startswith(("- ", "* ")) and not CHECKBOX.match(line):
            current.append(stripped[2:])
        elif stripped.startswith("#") or (current is not None and not stripped):
            current = None
    return fields


def _tokens(values: list[str]) -> list[str]:
    tokens: list[str] = []
    for value in values:
        for part in value.split(","):
            if ticks := BACKTICKED.findall(part):
                tokens.extend(ticks)
                continue
            part = re.split(r"\s+[—–-]\s+|\s+\(", part.strip())[0].strip()
            if part.lower() not in NONE_VALUES:
                tokens.append(part)
    return [t.strip() for t in tokens if t.strip()]


def _spec_refs(values: list[str]) -> list[str]:
    refs = []
    for token in _tokens(values):
        stem = PurePosixPath(token).name.removesuffix(".md")
        if stem and stem.lower() not in NONE_VALUES and stem not in refs:
            refs.append(stem)
    return refs


def _paths(values: list[str]) -> list[str]:
    paths = []
    for token in _tokens(values):
        token = token.removeprefix("./")
        if ("/" in token or "." in token) and " " not in token and token not in paths:
            paths.append(token)
    return paths
