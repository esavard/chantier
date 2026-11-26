# spec_001_ollama_executor

## 1. Spec
**Title:** Native Ollama tool-calling executor and local-loop safeguards
**Goal:** Replace external-harness friction for local Ollama execution with a native stdlib `/api/chat` tool-calling adapter (`adapter: ollama`), fix `LoopDetector` false positives on numbered tool/command invocations, and clarify checkbox editing in `implement.md`.

## 2. Context
**Project:** chantier
**Relevant modules/paths:**
- `src/chantier/adapters/executors/ollama.py`
- `src/chantier/adapters/executors/agents.py`
- `src/chantier/bootstrap.py`
- `src/chantier/application/supervisor.py`
- `src/chantier/prompts/implement.md`
- `src/chantier/domain/routing.py`
- `src/chantier/config.example.yaml`
- `docs/ARCHITECTURE.md`
- `tests/test_domain.py`
- `tests/test_executors.py`
**Depends on:** none

## 3. Requirements
- [x] Register `OllamaExecutor` in `bootstrap.ADAPTERS` under `"ollama"` implementing the `Executor` port using stdlib `urllib` against `$OLLAMA_API_BASE` (`/api/chat` with streaming NDJSON and structured `tool_calls`).
- [x] Reuse single-GPU VRAM warmup (`/api/ps` + `/api/generate` `keep_alive`) before runs, and return `exit_code=127` when the Ollama endpoint is unreachable or the model is missing so the scheduler blocks cleanly without burning ladder attempts.
- [x] Provide workspace-confined tools (`list_files`, `read_file`, `write_file`, `edit_file`, `run_command`) that reject path escapes outside `request.workdir` and access into `.git`, run commands in their own process group, and kill subprocess trees on timeout or supervisor kill.
- [x] Enforce `timeout_s`, `idle_timeout_s`, and `observe()` watchdog callbacks per streamed chunk/tool call, log turns and tool calls to `request.log_path`, and record `input_tokens`, `cache_read_tokens` (`prompt_eval_cached_count`), and `output_tokens` (`eval_count`).
- [x] Fix `LoopDetector` in `src/chantier/application/supervisor.py` so `tool ` and `command ` lines preserve digits (distinct line numbers/ranges do not trigger false kills) while identical repeated tool/command lines and prose number-noise detection continue to fire.
- [x] Clarify `src/chantier/prompts/implement.md` so agents only toggle checklist prefixes (`- [ ] ` -> `- [x] `) and never alter `[ ]` inside prose or instructions.

## 4. Definition of Done
- [x] Failing tests written first for `LoopDetector` and `OllamaExecutor`, then green.
- [x] `uv run pytest -q` passes.
- [x] `uv run lint-imports` passes.
- [x] `uvx pyright --pythonpath .venv/bin/python src tests` passes with zero errors.
