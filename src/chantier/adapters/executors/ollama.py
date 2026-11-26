"""Native Ollama executor: drives local models directly via `/api/chat` tool calling.

Avoids external-CLI prompt bloat, fence mismatches, and worktree pollution by
talking straight to Ollama over stdlib `urllib` with workspace-confined tools.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError

from chantier.adapters.executors.process import kill_process
from chantier.application.ports import ExecutionRequest, ExecutionResult, LineObserver, ModelProbeResult
from chantier.domain.model import Phase, TicketKind, Usage
from chantier.domain.routing import ExecutorSpec
from chantier.specs import normalized_spec_hash, parse_spec

DEFAULT_NUM_CTX = 32768
DEFAULT_MAX_TURNS = 80
DEFAULT_TEMPERATURE = 0.2
MAX_LIST_FILES = 300
MAX_READ_CHARS = 10000
MAX_CMD_OUTPUT_CHARS = 5000
COMPACT_THRESHOLD_RATIO = 0.72
COMPACT_KEEP_RECENT = 6
COMPACT_MIN_CHARS = 240
SKIP_DIRS = frozenset({".git", "node_modules", ".svelte-kit", "__pycache__", ".venv", "dist", "build"})

_SYSTEM_PROMPT = (
    "You are an autonomous coding agent working inside the repository worktree. "
    "Use the provided tools (list_files, read_file, write_file, edit_file, run_command, finish) "
    "to inspect files, write code and tests, run checks, and tick completed checklist items.\n"
    "Rules:\n"
    "- Your working directory is already the repository root; always use relative paths inside it and never "
    "cd above it or into mirror/ or workspaces/.\n"
    "- Read the spec, CONVENTIONS.md, and existing manifests (go.mod, package.json, etc.) before editing; "
    "never run 'go mod init' or 'npm init' when a manifest already exists, and run language tools from the "
    "directory containing that manifest.\n"
    "- Never run git commit or git push.\n"
    "- Edit spec checkboxes with edit_file targeting ONLY '- [ ] ' -> '- [x] ' (or appending "
    "'NOT DONE: <reason>'). Pass exact full lines as old_text/new_text; never retype, shorten, or alter "
    "spec prose/instructions, and never run global sed on '[ ]'.\n"
    "- If the prompt includes prior verification or reviewer feedback, address EVERY finding explicitly "
    "and never revert to a design that was already rejected.\n"
    "- Before finishing, verify every rule in CONVENTIONS.md (formatting, line length <= 100 chars, "
    "allowed dependencies, clean type-check/linter output without new warnings, no leftover build "
    "binaries) and ensure every acceptance criterion in the spec has an explicit test assertion.\n"
    "- Once all required tests/checks pass and every applicable checkbox is ticked '- [x]', immediately "
    "call the 'finish' tool (or reply with no tool calls). Never call run_command just to print echo status "
    "text or re-run already-passing checks without making edits."
)

_TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files and directories relative to the workspace root.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative directory path (default '.')"},
                    "max_depth": {"type": "integer", "description": "Maximum directory depth (default 4)"},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a workspace file with 1-based line numbers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative file path"},
                    "start_line": {"type": "integer", "description": "First line to read (default 1)"},
                    "end_line": {"type": "integer", "description": "Last line to read (default 300)"},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a workspace file with full contents.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative file path"},
                    "content": {"type": "string", "description": "Complete file contents"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace a unique exact substring in an existing workspace file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative file path"},
                    "old_text": {"type": "string", "description": "Exact unique text to replace"},
                    "new_text": {"type": "string", "description": "Replacement text"},
                },
                "required": ["path", "old_text", "new_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Execute a shell command inside the workspace and return its exit code and output.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to run"},
                    "timeout_s": {"type": "integer", "description": "Timeout in seconds (default 120)"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "finish",
            "description": (
                "End the run and deliver your final summary once all code, tests, checks, "
                "and checklist updates are complete."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "Concise final summary of implemented changes and verified checks",
                    },
                },
                "required": ["summary"],
            },
        },
    },
]


def ollama_base_url() -> str:
    raw = os.environ.get("OLLAMA_API_BASE") or os.environ.get("OLLAMA_HOST") or "http://localhost:11434"
    url = raw.strip().rstrip("/")
    if url and "://" not in url:
        url = f"http://{url}"
    return url


def normalize_model_name(model: str) -> str:
    return model.removeprefix("ollama_chat/").removeprefix("ollama/")


def ensure_model_loaded(base_url: str, model: str, *, num_ctx: int | None = None) -> None:
    """Load `model` into VRAM, unloading other resident models first (single-GPU setups)."""
    name = normalize_model_name(model)
    loaded = _get_json(f"{base_url}/api/ps").get("models", [])
    if any(
        m.get("name") == name and (num_ctx is None or m.get("context_length") == num_ctx)
        for m in loaded
    ):
        return
    for resident in loaded:
        _post_json(f"{base_url}/api/generate", {"model": resident["name"], "keep_alive": 0, "prompt": ""})
    payload: dict[str, Any] = {"model": name, "keep_alive": -1, "prompt": ""}
    if num_ctx is not None:
        payload["options"] = {"num_ctx": num_ctx}
    _post_json(f"{base_url}/api/generate", payload)


class OllamaExecutor:
    """Executor backed directly by Ollama `/api/chat` structured tool calling."""

    def __init__(self, spec: ExecutorSpec) -> None:
        self.spec = spec
        self.num_ctx, self.max_turns, self.temperature = _parse_args(spec.args)

    @property
    def ollama_url(self) -> str:
        return ollama_base_url()

    @property
    def model_name(self) -> str:
        return normalize_model_name(self.spec.model)

    def execute(self, request: ExecutionRequest, observe: LineObserver) -> ExecutionResult:
        if not self.model_name:
            return ExecutionResult(exit_code=127, summary=f"executor {self.spec.name}: model is required")

        try:
            ensure_model_loaded(self.ollama_url, self.model_name)
        except HTTPError as exc:
            detail = _read_http_error(exc)
            code = 127 if exc.code == 404 else 1
            return ExecutionResult(exit_code=code, summary=f"ollama warmup HTTP {exc.code}: {detail}")
        except (URLError, TimeoutError, OSError) as exc:
            return ExecutionResult(exit_code=127, summary=f"ollama unreachable at {self.ollama_url}: {exc}")

        usage = Usage()
        texts: list[str] = []
        edits = 0
        checks_passed = False
        failed_check_edits: int | None = None
        last_prompt_tokens = 0
        started = last_activity = time.monotonic()
        deadline = started + self.spec.timeout_s
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": request.prompt},
        ]

        request.log_path.parent.mkdir(parents=True, exist_ok=True)
        with request.log_path.open("w", encoding="utf-8") as log:
            log.write(f"# ollama {self.model_name} @ {self.ollama_url} (num_ctx={self.num_ctx})\n")
            log.flush()

            for turn_idx in range(self.max_turns):
                now = time.monotonic()
                if now >= deadline:
                    return _killed(log, usage, texts, f"run exceeded {self.spec.timeout_s} s")

                if last_prompt_tokens >= int(self.num_ctx * COMPACT_THRESHOLD_RATIO):
                    compacted = _compact_messages(messages, keep_recent=COMPACT_KEEP_RECENT)
                    if compacted:
                        log.write(f"[chantier] compacted {compacted} older items (prompt_tokens={last_prompt_tokens})\n")
                        log.flush()

                payload = {
                    "model": self.model_name,
                    "messages": messages,
                    "tools": _TOOLS,
                    "stream": True,
                    "keep_alive": -1,
                    "options": {"num_ctx": self.num_ctx, "temperature": self.temperature},
                }
                socket_timeout = max(1.0, min(float(self.spec.idle_timeout_s), deadline - now))
                try:
                    content, tool_calls, last_activity, kill_reason, last_prompt_tokens = _stream_chat(
                        f"{self.ollama_url}/api/chat",
                        payload,
                        usage,
                        timeout_s=socket_timeout,
                        deadline=deadline,
                        idle_timeout_s=self.spec.idle_timeout_s,
                        last_activity=last_activity,
                        total_timeout_s=self.spec.timeout_s,
                    )
                except HTTPError as exc:
                    detail = _read_http_error(exc)
                    msg = f"ollama HTTP {exc.code}: {detail}"
                    log.write(f"\n[chantier] {msg}\n")
                    code = 127 if exc.code == 404 and usage.total_tokens == 0 else 1
                    return ExecutionResult(exit_code=code, usage=usage, summary=_tail([*texts, msg]))
                except (URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError) as exc:
                    msg = f"ollama request failed: {exc}"
                    log.write(f"\n[chantier] {msg}\n")
                    code = 127 if usage.total_tokens == 0 and isinstance(exc, URLError) else 1
                    return ExecutionResult(exit_code=code, usage=usage, summary=_tail([*texts, msg]))

                if kill_reason:
                    return _killed(log, usage, texts, kill_reason)

                assistant_msg: dict[str, Any] = {"role": "assistant", "content": content}
                if tool_calls:
                    assistant_msg["tool_calls"] = tool_calls
                messages.append(assistant_msg)

                if content.strip():
                    log.write(content.rstrip("\n") + "\n")
                    log.flush()
                    lines = [ln for ln in content.splitlines() if ln.strip()]
                    texts.extend(lines)
                    # On tool turns, observe only the leading line so multi-line recaps
                    # cannot flush LoopDetector's sliding window between identical tool calls.
                    for line in (lines[:1] if tool_calls else lines):
                        if reason := observe(line):
                            return _killed(log, usage, texts, reason)

                if not tool_calls:
                    if turn_idx < self.max_turns - 1:
                        pending_err, checks_passed, failed_check_edits = _check_finish_gates(
                            request,
                            edits=edits,
                            checks_passed=checks_passed,
                            failed_check_edits=failed_check_edits,
                            deadline=deadline,
                            idle_timeout_s=self.spec.idle_timeout_s,
                        )
                        if pending_err:
                            log.write(f"  -> {pending_err[:1200].rstrip()}\n")
                            log.flush()
                            messages.append({"role": "user", "content": pending_err})
                            continue
                    return ExecutionResult(exit_code=0, usage=usage, summary=_tail(texts))

                finished = False
                for call in tool_calls:
                    fn = call.get("function") or {}
                    name = str(fn.get("name") or "")
                    call_id = call.get("id")
                    raw_args = fn.get("arguments")
                    args, parse_err = _decode_arguments(raw_args)

                    args_json = json.dumps(args if parse_err is None else raw_args, sort_keys=True)[:300]
                    tool_line = f"tool {name} {args_json}"
                    observed_line = f"tool {name} [rev={edits}] {args_json}" if name == "run_command" else tool_line
                    log.write(tool_line + "\n")
                    log.flush()
                    texts.append(tool_line)
                    if reason := observe(observed_line):
                        return _killed(log, usage, texts, reason)

                    if parse_err is not None:
                        output = f"error: invalid arguments for {name}: {parse_err}"
                    elif name == "finish":
                        gate_err, checks_passed, failed_check_edits = _check_finish_gates(
                            request,
                            edits=edits,
                            checks_passed=checks_passed,
                            failed_check_edits=failed_check_edits,
                            deadline=deadline,
                            idle_timeout_s=self.spec.idle_timeout_s,
                        )
                        if gate_err:
                            output = gate_err
                        else:
                            summary = _str_arg(args, "summary").strip()
                            if summary:
                                texts.extend(ln for ln in summary.splitlines() if ln.strip())
                                log.write(summary.rstrip("\n") + "\n")
                            output = "finished"
                            finished = True
                    else:
                        output = _dispatch_tool(
                            name, args, request.workdir, deadline=deadline, idle_timeout_s=self.spec.idle_timeout_s
                        )
                        if name in ("write_file", "edit_file") and not output.startswith("error:"):
                            edits += 1
                            if _str_arg(args, "path") != request.ticket.spec_path:
                                checks_passed = False
                                failed_check_edits = None
                        elif name == "run_command":
                            cmd = _str_arg(args, "command").strip()
                            if len(request.checks) == 1 and cmd == request.checks[0].strip() and output.startswith("exit 0\n"):
                                checks_passed = True
                                failed_check_edits = None
                            else:
                                checks_passed = False
                    last_activity = time.monotonic()
                    log.write(f"  -> {output[:1200].rstrip()}\n")
                    log.flush()

                    tool_msg: dict[str, Any] = {"role": "tool", "tool_name": name, "content": output}
                    if call_id:
                        tool_msg["tool_call_id"] = call_id
                    messages.append(tool_msg)

                if finished:
                    return ExecutionResult(exit_code=0, usage=usage, summary=_tail(texts))

            msg = f"ollama exceeded {self.max_turns} turns without finishing"
            log.write(f"\n[chantier] {msg}\n")
            return ExecutionResult(exit_code=1, usage=usage, summary=_tail([*texts, msg]))


def _check_finish_gates(
    request: ExecutionRequest,
    *,
    edits: int,
    checks_passed: bool,
    failed_check_edits: int | None,
    deadline: float,
    idle_timeout_s: int,
) -> tuple[str | None, bool, int | None]:
    if spec_err := _finish_spec_error(request):
        return spec_err, checks_passed, failed_check_edits
    if not request.checks or checks_passed:
        return None, True if request.checks else checks_passed, None
    if failed_check_edits is not None and failed_check_edits == edits:
        return (
            "error: cannot finish — no files were edited since the last failed check; "
            "inspect and fix the root cause (code, imports, formatting, or tests) before calling finish again.",
            False,
            failed_check_edits,
        )
    if check_err := _finish_checks_error(request, deadline=deadline, idle_timeout_s=idle_timeout_s):
        return check_err, False, edits
    return None, True, None


def _finish_spec_error(request: ExecutionRequest) -> str | None:
    if request.phase != Phase.IMPLEMENT or request.ticket.kind != TicketKind.IMPLEMENT:
        return None
    if not request.ticket.spec_path:
        return None
    spec_file = request.workdir / request.ticket.spec_path
    if not spec_file.is_file():
        return None
    spec_text = spec_file.read_text(encoding="utf-8", errors="replace")
    if request.ticket.approved_spec_sha and normalized_spec_hash(spec_text) != request.ticket.approved_spec_sha:
        return (
            f"error: cannot finish — {request.ticket.spec_path} was modified beyond its checkboxes. "
            "Restore its exact original prose (change ONLY '- [ ] ' -> '- [x] ' or append "
            "'NOT DONE: <reason>'), then call finish again."
        )
    doc = parse_spec(spec_file.stem, spec_text)
    unresolved = [box.line_no for box in doc.unresolved]
    if unresolved:
        lines_str = ", ".join(f"line {n}" for n in unresolved[:8])
        return (
            f"error: cannot finish yet — unresolved checkboxes in "
            f"{request.ticket.spec_path} ({lines_str}). Edit ONLY '- [ ] ' -> '- [x] ' "
            "with edit_file (or append 'NOT DONE: <reason>'), then call finish again."
        )
    return None


def _finish_checks_error(request: ExecutionRequest, *, deadline: float, idle_timeout_s: int) -> str | None:
    if request.phase != Phase.IMPLEMENT or request.ticket.kind != TicketKind.IMPLEMENT:
        return None
    for command in request.checks:
        out = _tool_run_command(
            request.workdir,
            command,
            idle_timeout_s,
            deadline=deadline,
            idle_timeout_s=idle_timeout_s,
        )
        if not out.startswith("exit 0\n"):
            body = out.split("\n", 1)[1].strip() if "\n" in out else ""
            hint = "" if body else (
                "(the command failed with no output — read the check script and run each sub-command "
                "individually, e.g. gofmt -l ., linter, type-check, tests)\n"
            )
            return (
                f"error: cannot finish — check failed ({command}):\n{out}\n{hint}"
                "Fix the root cause (check manifests like go.mod/package.json, imports, formatting, and tests) "
                "and verify the check passes before calling finish again."
            )
    return None


def _compact_messages(messages: list[dict[str, Any]], keep_recent: int) -> int:
    cutoff = len(messages) - keep_recent
    if cutoff <= 2:
        return 0
    compacted = 0
    for idx in range(2, cutoff):
        msg = messages[idx]
        role = msg.get("role")
        if role == "tool":
            content = str(msg.get("content") or "")
            if len(content) <= COMPACT_MIN_CHARS or content.startswith("[earlier "):
                continue
            tool_name = msg.get("tool_name") or "tool"
            msg["content"] = f"[earlier {tool_name} output omitted ({len(content)} chars); re-read if needed]"
            compacted += 1
        elif role == "assistant":
            for call in msg.get("tool_calls") or ():
                compacted += _compact_tool_call_args(call)
    return compacted


def _compact_tool_call_args(call: dict[str, Any]) -> int:
    fn = call.get("function") or {}
    name = str(fn.get("name") or "")
    if name not in ("write_file", "edit_file"):
        return 0
    raw_args = fn.get("arguments")
    parsed_from_str = False
    if isinstance(raw_args, dict):
        args = raw_args
    elif isinstance(raw_args, str) and len(raw_args) > COMPACT_MIN_CHARS:
        try:
            loaded = json.loads(raw_args)
        except json.JSONDecodeError:
            return 0
        if not isinstance(loaded, dict):
            return 0
        args = loaded
        parsed_from_str = True
    else:
        return 0

    changed = 0
    for key in ("content", "old_text", "new_text"):
        val = args.get(key)
        if isinstance(val, str) and len(val) > COMPACT_MIN_CHARS and not val.startswith("[earlier "):
            args[key] = f"[earlier {name} {key} omitted ({len(val)} chars)]"
            changed += 1
    if changed and parsed_from_str:
        fn["arguments"] = json.dumps(args)
    return changed


def _parse_args(args: tuple[str, ...]) -> tuple[int, int, float]:
    num_ctx = DEFAULT_NUM_CTX
    max_turns = DEFAULT_MAX_TURNS
    temperature = DEFAULT_TEMPERATURE
    i = 0
    while i < len(args):
        flag = args[i]
        if flag in ("--num-ctx", "--max-turns", "--temperature") and i + 1 < len(args):
            value = args[i + 1]
            if flag == "--num-ctx":
                num_ctx = int(value)
            elif flag == "--max-turns":
                max_turns = int(value)
            else:
                temperature = float(value)
            i += 2
            continue
        i += 1
    return num_ctx, max_turns, temperature


def _stream_chat(
    url: str,
    payload: dict[str, Any],
    usage: Usage,
    *,
    timeout_s: float,
    deadline: float,
    idle_timeout_s: int,
    last_activity: float,
    total_timeout_s: int,
) -> tuple[str, list[dict[str, Any]], float, str | None, int]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    content_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    prompt_tokens = 0

    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        for raw in resp:
            now = time.monotonic()
            if now - last_activity > idle_timeout_s:
                return "", [], last_activity, f"no output for {idle_timeout_s} s", prompt_tokens
            if now >= deadline:
                return "", [], last_activity, f"run exceeded {total_timeout_s} s", prompt_tokens
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            last_activity = now
            chunk = json.loads(line)
            msg = chunk.get("message") or {}
            if text := msg.get("content"):
                content_parts.append(str(text))
            if calls := msg.get("tool_calls"):
                tool_calls.extend(calls)
            if chunk.get("done"):
                prompt_tokens = int(chunk.get("prompt_eval_count") or 0)
                cached = int(chunk.get("prompt_eval_cached_count") or 0)
                usage.input_tokens += max(0, prompt_tokens - cached)
                usage.cache_read_tokens += cached
                usage.output_tokens += int(chunk.get("eval_count") or 0)
                break
    return "".join(content_parts), tool_calls, last_activity, None, prompt_tokens


def _decode_arguments(raw: Any) -> tuple[dict[str, Any], str | None]:
    if raw is None:
        return {}, None
    if isinstance(raw, dict):
        return raw, None
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            return {}, str(exc)
        return (parsed, None) if isinstance(parsed, dict) else ({}, "expected JSON object")
    return {}, f"unexpected arguments type: {type(raw).__name__}"


def _resolve_workspace_path(workdir: Path, rel_path: str) -> tuple[Path | None, str | None]:
    root = workdir.resolve()
    rel = Path(rel_path or ".")
    if rel.is_absolute():
        return None, f"error: path '{rel_path}' is outside the workspace"
    target = (root / rel).resolve()
    if not target.is_relative_to(root):
        return None, f"error: path '{rel_path}' is outside the workspace"
    rel_parts = target.relative_to(root).parts
    if ".git" in rel_parts:
        return None, f"error: refusing to access .git path '{rel_path}'"
    return target, None


def _int_arg(args: dict[str, Any], key: str, default: int) -> int:
    val = args.get(key)
    if val is None:
        return default
    if isinstance(val, bool):
        raise ValueError(f"invalid integer for '{key}': {val!r}")
    try:
        return int(val)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid integer for '{key}': {val!r}") from exc


def _str_arg(args: dict[str, Any], key: str, default: str = "") -> str:
    val = args.get(key)
    return default if val is None else str(val)


def _dispatch_tool(
    name: str, args: dict[str, Any], workdir: Path, *, deadline: float, idle_timeout_s: int
) -> str:
    try:
        if name == "list_files":
            return _tool_list_files(workdir, _str_arg(args, "path", "."), _int_arg(args, "max_depth", 4))
        if name == "read_file":
            return _tool_read_file(
                workdir, _str_arg(args, "path"), _int_arg(args, "start_line", 1), _int_arg(args, "end_line", 300)
            )
        if name == "write_file":
            return _tool_write_file(workdir, _str_arg(args, "path"), _str_arg(args, "content"))
        if name == "edit_file":
            return _tool_edit_file(
                workdir, _str_arg(args, "path"), _str_arg(args, "old_text"), _str_arg(args, "new_text")
            )
        if name == "run_command":
            return _tool_run_command(
                workdir, _str_arg(args, "command"), _int_arg(args, "timeout_s", 120),
                deadline=deadline, idle_timeout_s=idle_timeout_s,
            )
    except (TypeError, ValueError, OSError) as exc:
        return f"error: {exc}"
    return f"error: unknown tool '{name}'"


def _tool_list_files(workdir: Path, rel_path: str, max_depth: int) -> str:
    target, err = _resolve_workspace_path(workdir, rel_path)
    if err or target is None:
        return err or "error: invalid path"
    if not target.exists():
        return f"error: path '{rel_path}' does not exist"
    if target.is_file():
        return target.relative_to(workdir.resolve()).as_posix()

    root = workdir.resolve()
    base_depth = len(target.relative_to(root).parts)
    entries: list[str] = []
    for current, dirnames, filenames in os.walk(target):
        cur_path = Path(current)
        depth = len(cur_path.relative_to(root).parts) - base_depth
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        if depth >= max_depth:
            dirnames[:] = []
        for d in dirnames:
            entries.append((cur_path / d).relative_to(root).as_posix() + "/")
            if len(entries) >= MAX_LIST_FILES:
                return "\n".join(entries) + f"\n... (truncated at {MAX_LIST_FILES} entries)"
        for f in sorted(filenames):
            entries.append((cur_path / f).relative_to(root).as_posix())
            if len(entries) >= MAX_LIST_FILES:
                return "\n".join(entries) + f"\n... (truncated at {MAX_LIST_FILES} entries)"
    return "\n".join(entries) if entries else "(empty)"


def _tool_read_file(workdir: Path, rel_path: str, start_line: int, end_line: int) -> str:
    target, err = _resolve_workspace_path(workdir, rel_path)
    if err or target is None:
        return err or "error: invalid path"
    if not target.is_file():
        return f"error: file '{rel_path}' does not exist"
    lines = target.read_text(encoding="utf-8", errors="replace").splitlines()
    total = len(lines)
    start = max(1, start_line)
    end = min(total, max(start, end_line))
    if start > total:
        return f"(file has {total} lines; start_line={start} is past EOF)"
    body = "\n".join(f"{idx}: {lines[idx - 1]}" for idx in range(start, end + 1))
    if len(body) > MAX_READ_CHARS:
        body = body[:MAX_READ_CHARS] + "\n... [truncated; pass a smaller line range]"
    return f"({rel_path}: lines {start}-{end} of {total})\n{body}"


def _tool_write_file(workdir: Path, rel_path: str, content: str) -> str:
    target, err = _resolve_workspace_path(workdir, rel_path)
    if err or target is None:
        return err or "error: invalid path"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"Wrote {rel_path} ({len(content)} bytes)"


def _tool_edit_file(workdir: Path, rel_path: str, old_text: str, new_text: str) -> str:
    target, err = _resolve_workspace_path(workdir, rel_path)
    if err or target is None:
        return err or "error: invalid path"
    if not target.is_file():
        return f"error: file '{rel_path}' does not exist"
    if not old_text:
        return "error: old_text must not be empty"
    text = target.read_text(encoding="utf-8")
    count = text.count(old_text)
    if count == 0:
        return f"error: old_text not found in '{rel_path}'"
    if count > 1:
        return f"error: old_text matched {count} times in '{rel_path}'; provide more context"
    target.write_text(text.replace(old_text, new_text, 1), encoding="utf-8")
    return f"Edited {rel_path}"


def _tool_run_command(
    workdir: Path, command: str, timeout_s: int, *, deadline: float, idle_timeout_s: int
) -> str:
    if not command.strip():
        return "error: empty command"
    sanitized = command.replace(str(workdir.resolve()), ".")
    if any(forbidden in sanitized for forbidden in ("/workspaces/", "/mirror", "../..")):
        return "error: command must stay inside the workspace root; use relative paths"
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        return "error: run timeout exceeded"
    cap = max(1, min(timeout_s, int(remaining), idle_timeout_s))
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "CI": "1"}
    proc = subprocess.Popen(
        command,
        shell=True,
        cwd=workdir,
        env=env,
        text=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        out, _ = proc.communicate(timeout=cap)
        code = proc.returncode
    except subprocess.TimeoutExpired as exc:
        kill_process(proc)
        partial = exc.output.decode("utf-8", errors="replace") if isinstance(exc.output, bytes) else (exc.output or "")
        return f"error: command timed out after {cap} s\n{_clip(partial, MAX_CMD_OUTPUT_CHARS)}"
    return f"exit {code}\n{_clip(out or '', MAX_CMD_OUTPUT_CHARS)}"


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    head = limit // 5
    tail = limit - head
    return text[:head] + "\n... [truncated] ...\n" + text[-tail:]


def _tail(lines: list[str]) -> str:
    return "\n".join(lines[-40:])


def _killed(log, usage: Usage, texts: list[str], reason: str) -> ExecutionResult:
    log.write(f"\n[chantier] killed: {reason}\n")
    log.flush()
    return ExecutionResult(exit_code=1, usage=usage, summary=_tail(texts), killed_reason=reason)


def _read_http_error(exc: HTTPError) -> str:
    try:
        return exc.read().decode("utf-8", errors="replace")[:500]
    except OSError:
        return str(exc)


def _get_json(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=10) as resp:
        return json.loads(resp.read())


def _post_json(url: str, payload: dict[str, Any]) -> None:
    _post_json_response(url, payload)


def _post_json_response(url: str, payload: dict[str, Any], timeout_s: int = 300) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else {}


class OllamaModelProbe:
    """Discovers local Ollama models and probes tool calling, VRAM residency, and throughput."""

    def __init__(self, base_url: str | None = None) -> None:
        self._base_url = base_url

    @property
    def ollama_url(self) -> str:
        return (self._base_url or ollama_base_url()).rstrip("/")

    def list_models(self) -> list[str]:
        try:
            data = _get_json(f"{self.ollama_url}/api/tags")
        except HTTPError as exc:
            raise RuntimeError(f"ollama /api/tags HTTP {exc.code}: {_read_http_error(exc)}") from exc
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"ollama unreachable at {self.ollama_url}: {exc}") from exc
        models: list[str] = []
        for entry in data.get("models") or ():
            name = str(entry.get("name") or entry.get("model") or "").strip()
            if name and name not in models:
                models.append(name)
        return models

    def probe(self, model: str, *, num_ctx: int = DEFAULT_NUM_CTX) -> ModelProbeResult:
        name = normalize_model_name(model)
        if not name:
            return ModelProbeResult(model=model, reachable=False, tool_calling_ok=False, error="empty model name")
        try:
            ensure_model_loaded(self.ollama_url, name, num_ctx=num_ctx)
            payload = {
                "model": name,
                "messages": [
                    {
                        "role": "user",
                        "content": (
                            "Call the `write_file` tool now to write `probe.txt` with content `ok`. "
                            "Do not reply with prose; only emit the tool call."
                        ),
                    }
                ],
                "tools": _TOOLS,
                "stream": False,
                "keep_alive": -1,
                "options": {"num_ctx": num_ctx, "temperature": 0.0},
            }
            resp = _post_json_response(f"{self.ollama_url}/api/chat", payload)
            ps = _get_json(f"{self.ollama_url}/api/ps")
        except HTTPError as exc:
            return ModelProbeResult(
                model=name,
                reachable=False,
                tool_calling_ok=False,
                error=f"HTTP {exc.code}: {_read_http_error(exc)}",
            )
        except (URLError, TimeoutError, OSError, json.JSONDecodeError, ValueError) as exc:
            return ModelProbeResult(
                model=name,
                reachable=False,
                tool_calling_ok=False,
                error=f"ollama error: {exc}",
            )

        vram_bytes, total_bytes, vram_ratio = _probe_vram(ps, name)
        return ModelProbeResult(
            model=name,
            reachable=True,
            tool_calling_ok=_probe_has_write_file(resp),
            tok_per_s=_probe_tok_per_s(resp),
            vram_bytes=vram_bytes,
            total_bytes=total_bytes,
            vram_ratio=vram_ratio,
        )


def _probe_has_write_file(resp: dict[str, Any]) -> bool:
    msg = resp.get("message") or {}
    for call in msg.get("tool_calls") or ():
        fn = call.get("function") or {}
        if fn.get("name") == "write_file":
            args, err = _decode_arguments(fn.get("arguments"))
            if err is None and _str_arg(args, "path").strip() and "content" in args:
                return True
    return False


def _probe_tok_per_s(resp: dict[str, Any]) -> float:
    eval_count = int(resp.get("eval_count") or 0)
    eval_ns = int(resp.get("eval_duration") or 0)
    if eval_count > 0 and eval_ns > 0:
        return round(eval_count * 1_000_000_000 / eval_ns, 2)
    return 0.0


def _probe_vram(ps: dict[str, Any], name: str) -> tuple[int, int, float]:
    for entry in ps.get("models") or ():
        entry_name = normalize_model_name(str(entry.get("name") or entry.get("model") or ""))
        if entry_name == name or entry_name == f"{name}:latest":
            total = int(entry.get("size") or 0)
            vram = int(entry.get("size_vram") or 0)
            ratio = (vram / total) if total > 0 else 0.0
            return vram, total, ratio
    return 0, 0, 0.0
