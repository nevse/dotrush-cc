---
worth: yes
where: plugins/dotrush/bin/lsp-proxy.py:149
added: 2026-09-13
---
# no skill for on-demand solution diagnostics

Running DotRush's whole-solution analysis today means finding the session FIFO by hand and echoing
`{"method":"dotrush/solutionDiagnostics","params":{}}` into it (plugin README, "Injecting custom LSP
messages"). A `dotrush-solution-diagnostics` skill would reduce that to "run DotRush solution diagnostics"
and report the findings grouped by severity and file.

Deferred rather than written as a thin FIFO wrapper, because the skill has nothing to read back yet:

- `pump_server_to_client` logs only `brief(body)`, so `proxy.log` has method names, not the diagnostics.
  The proxy needs to capture `textDocument/publishDiagnostics` payloads, e.g. latest per URI into
  `$WSDIR/diagnostics.json` (an empty `diagnostics` array clears a URI).
- DotRush 2026.09 `WorkspaceDiagnosticsHandler` is a notification that calls
  `RequestDiagnosticsPublishing` and returns; nothing signals that analysis finished. The skill must wait
  until publishes stop arriving for a few seconds (scaled to solution size), and say that this is a
  heuristic.
- Whether Claude Code surfaces diagnostics published without an edit is unverified; the plugin README's
  "surfaces the new diagnostics automatically" claim should be checked or removed alongside this.
- `CompilerDiagnosticsScope` and analyzer settings decide what a solution run reports; check the defaults
  before promising analyzer warnings.

Verify end to end on a small project with a known warning (a .NET 10 SDK is available locally), add proxy
capture tests to `tests/test_profile_reports.py`, and list the skill in both READMEs.
