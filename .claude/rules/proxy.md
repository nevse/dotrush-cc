---
paths:
  - "plugins/dotrush/bin/**"
  - "plugins/dotrush/.lsp.json"
  - "plugins/dotrush/scripts/dotrush-diagnostics.sh"
  - "plugins/dotrush/scripts/dotrush-pick-project.sh"
  - "plugins/dotrush/scripts/summarize-diagnostics.py"
  - "plugins/dotrush/skills/dotrush-diagnostics/**"
  - "plugins/dotrush/skills/dotrush-pick-project/**"
---
# Proxy, injection and the diagnostics mirror

- `lsp-proxy.py` is stdlib-only Python 3.
- The proxy's stdout is the LSP stream to Claude Code. Nothing but frames may reach it; installer and tool output is
  captured and logged.
- Never send DotRush `dotrush/reloadWorkspace` before `load-completed` exists in the session dir: it races the first
  project load (analysis never starts, or every diagnostic appears twice).
  Inject `workspace/didChangeConfiguration` first; it replaces the whole `roslyn` section.
- The proxy's startup injection (`startup_config_inject`) is what lets DotRush load anything: its `initialize`
  waits for a `dotrush.roslyn` section, and Claude Code sends only the `.lsp.json` `settings`, which carry none.
  Put a default roslyn section in `.lsp.json` instead and it arrives after the persisted target and replaces it.
- `load-completed` does not mean a project loaded: DotRush completes a load that found several solutions, or none,
  with nothing loaded. `projects-loaded` (`0` at proxy start) tells them apart; readers treat a missing file as an
  older proxy.
- `load-completed` comes once per server, so it says nothing about a reload. The per-load signal is the end of
  DotRush's `$/progress` (it reports progress for workspace loads only): `workspace-loads` counts those, and
  `dotrush-pick-project.sh` waits on it. `projects-loaded` adds up across reloads; `last-load-projects`, written at
  that end, is what the last load loaded.
- The injector holds its own write end of `inject.fifo` (`open_fifo_for_reading`) so it never reaches EOF between
  writers. Keep it: a reader that closes and reopens silently drops what a writer sends in between (no EPIPE for
  lines written before the close).
- A bad injected line is logged and skipped, never allowed to end the injector thread (every later injection would
  be lost silently).
- `responses/` in the session dir is the request channel's capability marker (created at proxy start, never
  recreated later), and `edits/` (the saved rename plans) is cleared on every proxy start.
- A request-channel id becomes a file name: only a lowercase uuid after `dotrush-cc:` is written; anything else is
  dropped and logged.
- A proxy that predates a session-dir file is still running for users until they restart Claude Code. A reader of
  a new file names that case (`older proxy`) instead of failing.
- Claude Code is shown only the errors and warnings of a publish (`FORWARDED_SEVERITIES`); `diagnostics.json`
  keeps every severity. A publish with nothing to drop is forwarded byte for byte. Claude Code filters no severity
  itself (2.1.286: every severity, 10 per file, 30 per turn), so dropping the filter brings back IDE-style noise.
- DotRush signals no end of analysis. `dotrush-diagnostics.sh solution` waits for the publish count to move, then
  for quiet, and exit 3 means nothing arrived, which includes a clean solution.
- Every FIFO writer checks the proxy's pid and opens with `O_NONBLOCK` and no `O_CREAT`: a plain `> "$FIFO"`
  blocks on a dead proxy, and before the injector starts it creates a regular file there.
- Tests: `InjectorTests` and `DiagnosticsTests` in `tests/test_profile_reports.py`; the channel end to end in
  `tests/DotRushCli.Tests/E2E`.
