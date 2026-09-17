# dotrush-cc

Claude Code marketplace with one plugin, `plugins/dotrush`: a stdio proxy (`bin/lsp-proxy.py`) in front of the
DotRush C# language server, bash scripts, skills, and a C# CLI (`tools/DotRushCli`).

## Tests

- `python3 -m unittest tests.test_profile_reports` — proxy, installer, report tools, diagnostics script. The
  diagnostics tests build the CLI once with `dotnet`, so they need the .NET 10 SDK.
- `dotnet test tests/DotRushCli.Tests` — CLI, wrapper and `SkillCommandTests` (every `dotrush-cli.sh …` line in a
  `SKILL.md` must match a subcommand and flags in `Program`'s usage table; update both together).
- `DOTRUSH_E2E=1 dotnet test tests/DotRushCli.Tests --filter Category=E2E` — against the installed DotRush server;
  skipped without the variable. See `tests/DotRushCli.Tests/E2E/README.md`.

## Conventions

- Run the CLI only through `scripts/dotrush-cli.sh`: it supplies `DOTRUSH_DATA_DIR` (always overwriting it with the
  data dir derived by `dotrush_data_dir`), and the CLI never derives it. Tests that run the wrapper point
  `CLAUDE_PLUGIN_DATA` at their temp data dir.
- CLI commands take a `CommandContext` (env, cwd, stdout, stderr) and are tested in-process through
  `Program.Run(...)`; never read the process environment or `Console` in a command, so tests run in parallel.
- Changing any file under `plugins/dotrush/tools/` changes the source hash, so the wrapper builds a new CLI.
- Never send DotRush `dotrush/reloadWorkspace` before `load-completed` exists in the session dir: it races the first
  project load (analysis never starts, or every diagnostic appears twice).
- DotRush returns Roslyn's minimal rename edits (`Greeter` → `Welcomer` arrives as `Greet` → `Welcom`); compare
  whole identifiers on disk, not range text, and check the identifier both before and after the edit.
- Session lookup lives only in the CLI (`Session.Find`). Bash callers run `dotrush-cli.sh session --dir`; never
  re-scan `ws/` from a script.
- A request line must reach the FIFO in one write (`PIPE_BUF`), and .NET cannot open a FIFO non-blocking, so the
  open and write run on a background task with a 2 s timeout; check the proxy's pid first.
- The proxy's injector holds its own write end of `inject.fifo` (`open_fifo_for_reading`) so it never reaches EOF
  between writers. Keep it: a reader that closes and reopens silently drops what a writer sends in between (no
  EPIPE for lines written before the close). On EPIPE the CLI resends the whole batch, never from the failed line.
- `responses/` in the session dir is the request channel's capability marker (created at proxy start, never
  recreated later), and `edits/` — the saved rename plans — is cleared on every proxy start.
