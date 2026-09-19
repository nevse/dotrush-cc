---
paths:
  - "plugins/dotrush/bin/**"
  - "plugins/dotrush/.lsp.json"
  - "plugins/dotrush/scripts/dotrush-diagnostics.sh"
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
- DotRush signals no end of analysis. `dotrush-diagnostics.sh solution` waits for the publish count to move, then
  for quiet, and exit 3 means nothing arrived, which includes a clean solution.
- Tests: `InjectorTests` and `DiagnosticsTests` in `tests/test_profile_reports.py`; the channel end to end in
  `tests/DotRushCli.Tests/E2E`.
