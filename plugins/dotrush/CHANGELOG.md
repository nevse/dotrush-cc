# Changelog

### 0.7.3
- `dotrush-profile.sh trace-diff <baseline> <current> [count] [baseline-thread current-thread]` compares two CPU
  captures: functions ranked by how much their share of each capture's own managed CPU moved, in percentage
  points, with both raw weights and both totals. If either capture has no sample tagged managed, both are compared
  by time on stack. The CPU skill uses it for before/after questions.
- `dotrush-profile.sh ps` adds each process's elapsed time, main assembly and command line (its tail when long),
  and takes `--filter <text>`: processes started through the `dotnet` host were identical rows before.
- `dotrush-pick-project` applies a project or user instruction that already names the solution instead of always
  asking, and chooses `restoreProjectsBeforeLoading` (`false` for a built checkout or an unreachable NuGet feed).
- The proxy's injector no longer dies on a JSON line nested too deeply for Python 3.9's parser, which silently
  dropped every later injection until a restart; a failed FIFO open no longer leaks its write descriptor.
- A test checks that every CLI error message a skill quotes is still in the CLI sources.

### 0.7.2
- `dotrush-profile.sh trace --launch [duration] [output-dir] -- <command...>` starts the command under
  `dotnet-trace` and traces it from startup until it exits or the duration ends, so a microbenchmark or a single
  test no longer needs a background run, a `ps` search and a timed attach. It prints the command's exit code as
  `EXIT=`, and refuses `dotnet test`, `dotnet run` and other SDK commands: the .NET processes they start inherit the
  suspended diagnostic port and hang. The CPU skill launches when the workload is short-lived, running a
  Microsoft.Testing.Platform test project's executable with its filter for a single test.

### 0.7.1
- The CPU report no longer discards a capture whose samples are all tagged unmanaged. .NET 9 and 10.0.0–10.0.3 on
  macOS arm64 tag every sample that way, even in a managed loop, and the report built its rankings from the
  untagged gaps between events (0.09 ms of a 360 s capture). When no sample in a capture is tagged as managed, the
  report now ranks the managed frames above the tag by time on stack, adds `ManagedOnStackTime` and a `Warning`
  line, and the CPU skill says how to read it. A capture with at least one managed sample is ranked as before.
- The report opens with `Runtime`, the target's .NET version read from the `.nettrace` (`unknown` when there is
  none), and has a thread table: stack changes, weight and top function per thread. `trace-report` takes an
  optional thread id to rank that thread alone (`summarize-speedscope.py --thread`), and reads the runtime from the
  `.nettrace` next to a `.speedscope.json` it is given.

### 0.7.0
- Added `dotrush-rename`, a semantic rename of a C# symbol across the loaded solution through DotRush's Roslyn
  rename. Claude locates the symbol with the `LSP` tool, runs `rename preview`, shows the per-file edit counts and
  the diff, and runs `rename apply` only after you confirm. Apply prepares every file before replacing any, so a
  refusal (a file changed since the preview, a temp file that cannot be written) changes nothing; if replacing them
  fails part way it names exactly which files changed and which did not, and drops the plan. It keeps BOM, line
  endings and file mode, and sends `textDocument/didOpen` for each changed file so DotRush re-reads it from disk.
  Files outside the workspace or under `bin`/`obj` need `--outside-workspace`. Overloads, strings, comments and
  file names are not renamed.
- The proxy has a request channel: a response whose id is `dotrush-cc:<uuid>` goes to `responses/<uuid>.json` in
  the session dir instead of to Claude Code, and an id with a non-uuid suffix is dropped. `responses/` is created
  empty at proxy start and marks a proxy with the channel; `edits/`, where rename plans are saved, is cleared at
  start, so a server restart discards unapplied plans. The injector now opens the FIFO once and holds a write end
  of it itself, so it no longer reaches end of file and reopens between writers; lines a writer sent while the
  injector was reopening used to be lost without an error. Messages are still one JSON-RPC message per line.
- Added a C# CLI, `tools/DotRushCli`, run through `scripts/dotrush-cli.sh`: `session [--dir]`,
  `request <method> <params-json> [--timeout N]`, `rename preview <file> <line> <column> <NewName> [--timeout N]`
  and `rename apply <plan-id> [--outside-workspace]`. Exit status 0 success, 1 error, 2 usage, 3 timeout; a request
  that times out is cancelled with `$/cancelRequest`. The wrapper builds the CLI on first use with the .NET 10 SDK
  into `${CLAUDE_PLUGIN_DATA}/cli/<source-hash>/`, one build per plugin version, from `tools/` so a repository's
  `global.json` or `Directory.Build.*` does not affect it. `DOTRUSH_CLI_DIR` runs a ready build instead.
- `dotrush-diagnostics.sh` and `dotrush-pick-project` find the session dir with `dotrush-cli.sh session --dir`.
  The fallback without a matching session id now considers only per-workspace dirs, so it no longer takes another
  session's `sess-*` dir recorded for the same workspace, and it resolves symlinked paths. `dotrush-pick-project`
  looks only in this plugin's data dir rather than in every plugin's. `where` prints the CLI's `session` output,
  which adds a `channel:` line. Readiness checks and their messages are unchanged, including against 0.6.x
  proxies; the lookup's error is now prefixed `dotrush-cli:`. Both paths therefore need the .NET 10 SDK and a
  sha256 tool (`shasum` or `sha256sum`): the first call builds the CLI once per plugin version (`building the
  DotRush CLI` on stderr), and a failed build makes `where`, `solution`, `report` and project picking fail with the
  tail of `cli-build.log`.

### 0.6.1
- Corrected the 0.6.0 claim that Claude Code does not show the model diagnostics published outside an edit. Checked
  in a live session: after a solution run it surfaced diagnostics for a file it had never opened, but only the ones
  it had not shown before, hints included. The captured `diagnostics.json` stays the complete current set, and the
  skill now says how the two relate.

### 0.6.0
- Added `dotrush-diagnostics`, which runs DotRush's whole-solution compiler analysis in the session's server and
  reports counts by severity and code and each error and warning with its location (`scripts/dotrush-diagnostics.sh`,
  `scripts/summarize-diagnostics.py`).
- The proxy mirrors every `textDocument/publishDiagnostics` into `diagnostics.json` in the session dir (latest list
  per file, publish count and time). DotRush signals no end of analysis, so this file is how the results are read
  and when they are complete.
- `dotrush-pick-project` no longer sends `dotrush/reloadWorkspace` to a server that has not completed its first
  load. DotRush's `initialize` waits for a configuration and starts its code-analysis worker only after that load;
  a reload sent with the configuration raced it, and the session then either published no diagnostics at all or
  loaded the project twice and reported each diagnostic twice. The proxy
  now creates `load-completed` in the session dir on `dotrush/loadCompleted`, and the skill reloads only when it
  exists.

### 0.5.2
- DotRush is pinned to the 2026.09 release, republished with `DotRush.Bundle.LanguageServer.zip` and
  `DotRush.Bundle.Diagnostics.zip`, so both components are downloaded instead of built. The installer looks for
  those names; the release no longer ships per-platform server bundles.
- The server bundle has no native launcher, so the proxy starts `DotRush.dll` through the `dotnet` host. A build
  from source publishes the server the way DotRush's own `server` task does, without a runtime identifier or
  `_dotrush.config.json`.

### 0.5.1
- The profiling skills now install their tools beside the language server. Claude Code passes
  `CLAUDE_PLUGIN_DATA` to the server but not to the Bash commands a skill runs, so 0.5.0 put the tools in
  `~/.cache/dotrush-cc` and `dotrush-profile.sh tools` reported the server as not installed. Without the
  variable, the scripts now derive the plugin's data directory from where the plugin is installed.

### 0.5.0
- DotRush is pinned in `dotrush-version.json` to one `ref`, a release tag or a commit. The language server and the
  profiling tools are installed from it the same way and from the same place: the release's server and diagnostics
  bundles when the release ships both, otherwise both built from source at that ref. The pin is a commit after
  2026.09; the server was pinned to 2026.07 before. Each install records its ref in `.dotrush-ref`, and the proxy
  reinstalls a server at another ref, swapping it in whole and keeping the previous server if that fails. `.lsp.json`
  now allows 15 minutes for a start that builds.
- The profiling skills now run DotRush's own `dotnet-trace` and `dotnet-gcdump` instead of the NuGet tools. The NuGet
  install, `DOTRUSH_TRACE_TOOL`, `DOTRUSH_GCDUMP_TOOL` and `DOTRUSH_RELEASE` are gone (use `DOTRUSH_REF`), and so are
  the NuGet tools 0.4.0 left in `diagnostics-tools`. `DOTRUSH_DIAGNOSTICS_DIR` now names a ready directory of the
  tools, and `tools` shows the pin and what is installed.
- `heap` collects with `dotnet-gcdump --format Json` and prints `GCDUMP_JSON=` after `GCDUMP=`. The memory
  reports read that heap graph instead of parsing `dotnet-gcdump report` text, so per-type bytes are exact
  and `heap-report` lists the largest retained objects with the dominator chain that keeps each alive.
  `heap-diff` ranks exact per-type byte deltas alongside count deltas. This needs a DotRush build whose
  `dotnet-gcdump` has `--format Json`; without one the heap commands fail instead of falling back.
- `scripts/analyze-gcdump.py` replaces `scripts/compare-heapstats.py` and streams the JSON, so a large heap's
  per-object arrays are never loaded whole. `heap-report` now prints the report rather than a file path, and
  0.4.0 `.heapstat.txt` files are refused; their `.gcdump` files still work and are converted on first use.

### 0.4.0
- Added `dotrush-profile-cpu` for bounded `dotnet-trace` capture, Speedscope conversion, and top-method reports.
- Added `dotrush-profile-memory` for `dotnet-gcdump` capture, heap statistics, and baseline/current comparison.
- Added a shared lazy-installing profiler helper; profiling tools and artifacts stay outside your repository
  (`$DOTRUSH_PROFILE_OUTPUT_DIR` → `${CLAUDE_PLUGIN_DATA}/profiles` → the user cache).
- Added `scripts/compare-heapstats.py` behind `heap-diff`, and `tests/test_profile_reports.py` covering both
  report tools. `heap-diff` ranks per-type **object counts**; it reports no per-type byte delta, because
  `dotnet-gcdump report` prints one sampled object size per type rather than a total.
- The CPU report heads with `Threads`, `WallClockDuration` (the capture window) and `SampledThreadTime`,
  `ManagedSampledTime` and `UnmanagedOrBlockedTime` (each summed across threads). Method names print
  without their IL parameter lists unless two rows would otherwise read identically.
- Trace durations are validated as `hh:mm:ss` or `dd:hh:mm:ss` with `hh` 00-23, `mm`/`ss` 00-59, and must be
  greater than zero. `dotnet-trace` parses `--duration` with `TimeSpan.Parse`, which reads `00:30` as 30
  minutes and `24:00:00` as 24 days, and treats a zero duration as no limit at all.
- The `trace` and `heap` commands print only `TRACE=`/`SPEEDSCOPE=`/`REPORT=`/`GCDUMP=` lines on stdout; the
  diagnostic tools' own output goes to stderr.

### 0.3.0
- **Per-session runtime state** — target/FIFO/log now live under `${CLAUDE_PLUGIN_DATA}/ws/sess-<session-id>/`
  (keyed on `AGTERM_SESSION_ID`) instead of per project dir. Fixes projects **meshing** across parallel
  sessions launched from one folder — the git-worktree workflow, where every session shares
  `CLAUDE_PROJECT_DIR` and formerly clobbered one shared choice/FIFO.
- Stale session dirs are **pruned** automatically on server start (dir whose recorded proxy `pid` is gone).
- Falls back to the old per-workspace scoping (keyed on project-dir hash, choice persists across restarts)
  when no session id is present (headless/CI). Trade-off: with a session id, a fresh session re-picks its
  project.
- `dotrush-pick-project` skill locates its dir by session id; docs updated.

### 0.2.0
- **`dotrush-pick-project` skill** — interactively pick the C# project/solution DotRush loads (via
  `AskUserQuestion`), applied live with no `dotrush.config.json`; asked only if not chosen before.
- **Per-workspace runtime state** — target/FIFO/log now live under `${CLAUDE_PLUGIN_DATA}/ws/<hash>/`,
  so concurrent Claude sessions on different projects no longer collide.
- The proxy **replays the persisted project choice at startup** (`didChangeConfiguration`), so the chosen
  solution auto-loads each session — no config file in your repo.
- Docs: verify via `/plugin` (current Claude Code has **no `/lsp` command**); added a multiple-sessions section.

### 0.1.0
- Initial release. DotRush C# LSP wired into Claude Code via a stdio **proxy**; **auto-downloads** the
  DotRush server (official GitHub release) for your OS/arch on first use; custom LSP-message **injection**
  (FIFO) with on-demand diagnostics and live reconfigure/reload.
