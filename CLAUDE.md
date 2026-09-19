# dotrush-cc

Claude Code marketplace with one plugin, `plugins/dotrush`: a stdio proxy (`bin/lsp-proxy.py`) in front of the
DotRush C# language server, bash scripts, skills, and a C# CLI (`tools/DotRushCli`).
Read `ARCHITECTURE.md` for the parts, the session dir and the fragile points before changing the proxy or the CLI.
`plugins/dotrush/README.md` is the user reference; the root `README.md` is the pitch, install and usage examples.

## Tests

- `python3 -m unittest tests.test_profile_reports` — proxy, installer, report tools, diagnostics script. The
  diagnostics tests build the CLI once with `dotnet`, so they need the .NET 10 SDK.
- `dotnet test tests/DotRushCli.Tests` — CLI, wrapper and `SkillCommandTests`.
- `DOTRUSH_E2E=1 dotnet test tests/DotRushCli.Tests --filter Category=E2E` — against the installed DotRush server;
  skipped without the variable. See `tests/DotRushCli.Tests/E2E/README.md`.

## Shipping a change

- A user-visible change bumps `version` in `plugins/dotrush/.claude-plugin/plugin.json`, adds an entry to
  `plugins/dotrush/CHANGELOG.md`, and updates `plugins/dotrush/README.md` (and the root `README.md` when its usage
  examples or file list change). A new skill also goes into both READMEs' file lists.
- All Python here runs on macOS's `/usr/bin/python3`, which is 3.9: no `match`, and `X | None` annotations only
  under `from __future__ import annotations`.
- Skills are prompts an agent executes with a shell: a stale flag or message in a `SKILL.md` is a bug, not a doc
  nit. `.revmux/profile.md` ranks what counts as a real failure here.
- Multi-task features start as a plan in `docs/plans/` (`planning:make`) and move to `docs/plans/completed/` after
  merge. Real but deferred work goes to `docs/backlog/`, one file per item (`workflow:backlog`).

## Path-scoped rules

Read the matching `.claude/rules/*.md` before working in its area; each lists its paths in frontmatter.

- `proxy.md`: `lsp-proxy.py`, `.lsp.json`, FIFO injection, the request channel's proxy side, the diagnostics mirror,
  project picking.
- `cli.md`: `DotRushCli`, its wrapper, the request channel's CLI side, rename, CLI tests.
- `install.md`: the pin, server and tools installation, data-dir derivation, locks.
- `profiling.md`: `dotrush-profile.sh`, its Python reporters, the profiling skills.
