# Roadmap and known limits

## Ports without a real adapter yet

- **External trackers** (`TrackerMirror`): Jira, GitHub Issues. Start outbound (mirror ticket stage and title), then intake (an issue becomes a spec request). Avoid two-way sync as long as possible.
- **Remote dashboard / cloud sync** (`EventSink`): today `events.jsonl`. A sink that posts events to a hosted dashboard plugs in here.
- **Rich TUI**: `chantier board --watch` is a live Rich board. A Textual app (keyboard navigation, approve/reject in place) would be another client of the same services.

## Next features

- **Frontier diagnosis on a supervisor signal**: when the watchdog kills a local run, optionally ask a frontier agent to read the log and choose between retry with a hint, escalate, or block. Today the ladder decides alone.
- **`Depends on` in `spec-interview`** (agent-skills), and a `Relevant modules/paths` reminder: both drive scheduling and scope checks.
- **Move `blueprint-interview` to agent-skills** once it has run on a real project.
- **App factory pack** (private, `$CHANTIER_HOME/packs/`): `checks: ["./factory verify <slug>"]`, prompts that hand off to `app-builder`, `app-brief-interview` as the greenfield entry.
- **Stacked branches**: today a dependent spec waits until its dependency is DONE (merged). With a human merge gate, stacking the dependent branch on the dependency's branch would keep the pipeline moving.
- **Telegram replies**: approve or reject from the notification.

## Known limits

- **CLI output formats drift.** Executor adapters parse Claude Code's `stream-json` and Codex's `--json` events tolerantly; unknown events are ignored and usage stays at zero rather than crashing. Check the token counts in `chantier metrics` after upgrading a CLI.
- **Headless permissions.** Claude Code and Codex need explicit permission flags in `args` to edit files and run tests without a human. Grant the minimum your checks need.
- **Auto-merge into a local non-bare repo** fails if the base branch is checked out, unless that repo sets `receive.denyCurrentBranch updateInstead`. Remote repos (GitHub, Gitea) have no such issue.
- **No authentication on the web UI.** It binds to localhost; put it behind a VPN or reverse proxy before exposing it.
- **Savings are an estimate.** See docs/ARCHITECTURE.md, Metrics.
