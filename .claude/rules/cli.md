---
paths:
  - "plugins/dotrush/tools/**"
  - "plugins/dotrush/scripts/dotrush-cli.sh"
  - "plugins/dotrush/skills/dotrush-rename/**"
  - "tests/DotRushCli.Tests/**"
---
# DotRushCli and its wrapper

- Run the CLI only through `scripts/dotrush-cli.sh`: it supplies `DOTRUSH_DATA_DIR` (always overwriting it with the
  data dir derived by `dotrush_data_dir`), and the CLI never derives it. Tests that run the wrapper point
  `CLAUDE_PLUGIN_DATA` at their temp data dir.
- CLI commands take a `CommandContext` (env, cwd, stdout, stderr) and are tested in-process through
  `Program.Run(...)`; never read the process environment or `Console` in a command, so tests run in parallel.
- Exit codes are 0 success, 1 error, 2 usage, 3 timeout; errors go to stderr through `CliErrors` with the
  `dotrush-cli:` prefix. An IO failure is exit 1, never a stack trace.
- `Program`'s usage table is the CLI's contract: `SkillCommandTests` checks every `dotrush-cli.sh …` line and every
  quoted CLI message in a `SKILL.md` against it and the CLI's string literals. Update both together.
- Changing any file under `plugins/dotrush/tools/` changes the source hash, so the wrapper builds a new CLI.
- Session lookup lives only in the CLI (`Session.Find`). Bash callers run `dotrush-cli.sh session --dir`; never
  re-scan `ws/` from a script.
- A request line must reach the FIFO in one write (`PIPE_BUF`), and .NET cannot open a FIFO non-blocking, so the
  open and write run on a background task with a 2 s timeout; check the proxy's pid first.
- On EPIPE the CLI resends the whole batch, never from the failed line.
- DotRush returns Roslyn's minimal rename edits (`Greeter` → `Welcomer` arrives as `Greet` → `Welcom`); compare
  whole identifiers on disk, not range text, and check the identifier both before and after the edit.
- `rename apply` refuses before replacing the first file, or names exactly which files changed. Keep every new
  refusal ahead of the first move.
- E2E tests (`[Trait("Category", "E2E")]`) skip without `DOTRUSH_E2E=1`; see `tests/DotRushCli.Tests/E2E/README.md`.
