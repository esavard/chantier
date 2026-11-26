# ADR-001: Native Ollama Tool-Calling Execution and 16 GB VRAM Model Selection

- **Status:** Accepted
- **Date:** 2026-11-26
- **Deciders:** Eric Savard
- **Context hardware:** Remote Ollama host (`frank`) with NVIDIA GeForce RTX 5060 Ti 16 GB VRAM (`15.929 GiB` usable), `num_ctx = 32768`
- **Benchmark workload:** `todo-demo` (13-spec greenfield full-stack app: Go DDD/CQRS backend + SvelteKit/Vitest static frontend, gated by `scripts/check.sh` and frontier `agy-pro` code review on every spec)

## Context

`chantier` routes `implement/implement` tickets to a local Ollama executor first and escalates to a frontier executor (`agy-pro`) only after `attempts_per_rung` failures, while running `plan/plan` and `implement/review` on frontier. To make local-first routing practical on a single 16 GB consumer GPU, two questions had to be answered empirically:

1. **Harness choice:** Does an external CLI wrapper (`aider_ollama`) suffice, or does `chantier` need a native `/api/chat` tool-calling adapter (`OllamaExecutor`)?
2. **Model and quantization fit:** Which local coding model fits 100% inside 16 GB VRAM at `num_ctx=32768` (zero CPU/RAM offloading) while maintaining high spec-completion rates and fast iteration?

Seven sandbox iterations (`v1`–`v7`) were run against `todo-demo` to measure progression from baseline to the current architecture.

## Iteration Comparison (`v1`–`v7`)

> **Scope note:** `v1`, `v2`, and `v7` executed all 13 specs end-to-end. `v3`–`v5` were focused regression benchmarks on the 3 hardest backend specs (`spec_006`, `spec_008`, `spec_009`), and `v6` tested the 4 remaining integration/UI specs (`spec_007`, `spec_010`, `spec_012`, `spec_013`). USD estimates below apply [`src/chantier/config.example.yaml`](../../src/chantier/config.example.yaml) reference rates (`$3.0 / $15.0 / $0.15` per 1M input/output/cache-read tokens, `180 W` GPU @ `$0.15/kWh`) because `agy-pro` and the sandbox configs reported `$0.00`.

| Iter | Adapter | Local Model | Scope (`ran_impl`) | `attempts` / `timeout_s` | Local Wins | Escalated | Local Runs (ok / err / kill) | Local Tokens | Local Time | Frontier Spend (est.) | Local Value / Waste (est.) |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **v1** | `aider_ollama` | `qwen3-coder:30b` | 13 / 13 (full) | 3 / 3600s | **1 / 13 (7.7%)** | 12 | 46 (14 / 6 / 5) | 19.83 M | 221.5m | `$7.30` | `$0.21` / `$13.24` |
| **v2** | Native `OllamaExecutor` | `qwen3-coder:30b` | 13 / 13 (full) | 3 / 3600s | **5 / 13 (38.5%)** | 8 | 34 (21 / 3 / 0) | 19.62 M | 161.5m | `$7.05` | `$1.23` / `$9.17` |
| **v3** | Native `OllamaExecutor` | `qwen3-coder:30b` | 3 / 13 (subset) | 3 / 900s | **0 / 3 (0.0%)** | 3 | 9 (2 / 3 / 4) | 9.95 M | 64.3m | `$1.77` | `$0.00` / `$4.32` |
| **v4** | Native `OllamaExecutor` | `qwen3.8:27b-q4_K_M` | 3 / 13 (subset) | 4 / 1200s | **2 / 3 (66.7%)** | 1 | 7 (2 / 5 / 0) | 0.46 M | 101.7m | `$0.84` | `$0.18` / `$0.29` |
| **v5** | Native `OllamaExecutor` | `qwen3.6:35b-a3b-q4_K_M` | 3 / 13 (subset) | 4 / 1200s | **2 / 3 (66.7%)** | 1 | 8 (6 / 0 / 2) | 4.04 M | 64.9m | `$1.68` | `$0.98` / `$3.04` |
| **v6** | Native `OllamaExecutor` | `qwen3.6:35b-a3b-q4_K_M` | 4 / 13 (subset) | 4 / 1200s | **3 / 4 (75.0%)** | 1 | 18 (15 / 3 / 0) | 4.87 M | 106.5m | `$3.01` | `$4.80` / `$1.12` |
| **v7** | Native `OllamaExecutor` | `Qwen3.6-35B-A3B:UD-IQ3_XXS` | 13 / 13 (full) | 4 / 1200s | **11 / 13 (84.6%)** | 2 | 31 (26 / 2 / 3) | 13.23 M | ~180m active (565.9m raw¹) | **`$5.50`** | **`$7.60` / `$9.27`** |

¹ *During `v7`, the host suspended for ~385 min (`00:21`–`06:46`) mid-run on `spec_013` attempt 2; active GPU time across the entire 13-spec build was ~180 min (`~3 h`, `~$0.08` electricity at `180 W` / `$0.15/kWh`, or `~$0.03` at observed `140 W` / Quebec utility rates).*

## Pre-`v7` Candidate Screening (`chantier bench`)

Using the two-stage `chantier bench` workflow ([`spec_002_ollama_bench.md`](../specs/spec_002_ollama_bench.md)) at `num_ctx=32768` and `--min-vram-ratio 0.9`:

| Candidate | Tool Calling | Speed (`tok/s`) | VRAM Fit (`num_ctx=32768`) | Python Greenfield Bench | Status |
|---|---:|---:|---|---|---|
| `hf.co/unsloth/Qwen3.6-35B-A3B-GGUF:UD-IQ3_XXS` | yes | **108.6–109.8** | **100% (13.9 / 13.9 GB)** | 1/1 (`27.7s`) | **recommended** |
| `gpt-oss:20b` | yes | 98.1–98.5 | 100% (12.8 / 12.8 GB) | 1/1 (`63.6s`) | ok (verbose reasoning slows wall time) |
| `hf.co/unsloth/Qwen3.6-27B-GGUF:UD-IQ3_XXS` | yes | 31.0–31.1 | 100% (14.2 / 14.2 GB) | 1/1 (`50.9s`) | ok (dense 27B ~3.5x slower) |
| `devstral-small-2` | yes | 7.2 | 88% (14.5 / 16.4 GB) | — | disqualified (`88% < 90%`) |

## Key Findings

1. **Harness > model on `qwen3-coder:30b` alone (`v1` → `v2`):** Replacing `aider_ollama` with native `OllamaExecutor` (`b8e0d59`: direct `/api/chat` tools, in-loop finish gates on spec integrity/checkboxes/`checks`, and context compaction) boosted full-project local completion from **7.7% (1/13)** to **38.5% (5/13)** with the exact same model, eliminating edit-fence errors and killed loops.
2. **100% VRAM fit dominates wall-clock throughput (`v4` vs `v5`/`v7`):** Dense `27b-q4_K_M` (`v4`) achieved 2/3 accuracy on hard specs but spilled past 16 GB VRAM (`101.7m` for just `457k` tokens). MoE `Qwen3.6-35B-A3B` (`v5`–`v7`) activates only ~3B parameters per token, and `UD-IQ3_XXS` (`13.9 GB` total with `32k` context) fits **100% in VRAM**, sustaining **~109 tok/s** and `86%` prompt-cache reuse (`11.33 M / 13.23 M` tokens).
3. **Full end-to-end validation (`v7`):** On a clean 13-spec run, `Qwen3.6-35B-A3B:UD-IQ3_XXS` completed **11 / 13 specs (84.6%)** purely locally (**5 on attempt 1**, **26 / 31** local runs succeeded), passing `agy-pro` reviews (`13 pass / 15 reject` across iterative review feedback) and full deliverable checks (`go test`, `svelte-check`, `vitest` 35/35, `vite build`).
4. **Economics & remaining waste:** With frontier reviews on every spec plus 2 escalations (`spec_009_http_api` and `spec_013_task_page_ui`), `v7` delivered **~50–60% estimated API savings** (`~$7.52` net local value vs `~$5.50` frontier spend, for `~$0.08` electricity). However, `attempts_per_rung: 4` generated `~$9.27` in `local_waste_usd` on the two specs that ultimately escalated on attempt 5.

## Decision

1. **Standardize on native `OllamaExecutor`** with `num_ctx=32768`, `idle_timeout_s=600`, and `timeout_s=1200` as the sole Ollama execution path.
2. **Require 100% (or >=90%) VRAM residency at target `num_ctx`** via `chantier bench --min-vram-ratio 0.9` before committing a local model to a factory run; favor MoE `35B-A3B` dynamic quantizations (`UD-IQ3_XXS`) over dense ~27B Q4 models on 16 GB GPUs.
3. **Keep `attempts_per_rung` tight (2–3)** in production configs (vs `4` in `v4`–`v7`) to cap wasted tokens/time on tickets that structurally require frontier escalation (complex HTTP handlers or multi-file UI test harnesses).
4. **Maintain industry-representative pricing and energy defaults** in [`src/chantier/config.example.yaml`](../../src/chantier/config.example.yaml) (`gpu_watts: 180`, `kwh_price: 0.15`, non-zero reference frontier model rates) so `chantier metrics` reports meaningful cost and savings signals out of the box.

## Consequences

- **Positive:** Validated >84% local spec completion on a 16 GB GPU at ~50–60% lower API cost without relaxing frontier code-review or verification gates.
- **Negative / Trade-offs:** `IQ3_XXS` dynamic quantization sacrifices some single-shot reliability on complex integration specs (`spec_009`, `spec_013`) compared to larger models, making deterministic ladder escalation to frontier essential. Long wall-clock runs on remote Ollama hosts remain sensitive to client laptop sleep (`CLOCK_MONOTONIC` pauses in-flight idle timeouts until wake-up).
