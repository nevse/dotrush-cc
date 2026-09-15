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
  whole identifiers on disk, not range text.
