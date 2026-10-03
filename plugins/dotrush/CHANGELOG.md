# Changelog

### 0.8.14
- `dotrush-cli.sh session` (and `dotrush-diagnostics.sh where`) counts the projects of the last load: after a switch
  it said `load: completed (9 projects)` for a 4-project load followed by a 5-project one. The proxy writes that
  count to `last-load-projects` in the session dir when a load ends; with an older proxy the line shows the total
  as before.

### 0.8.13
- `dotrush-pick-project.sh apply` waits for DotRush to finish the load it started and prints `loaded: N projects`,
  instead of the `dotrush-pick-project` skill waiting "a few seconds" before checking for symbols. It waits up to
  90 seconds (`DOTRUSH_PICK_PROJECT_TIMEOUT`) and exits 3 while the load is still running; the new
  `dotrush-pick-project.sh wait` keeps waiting. A load that loaded no project, or a server that stopped or
  restarted while loading, exits 1. The proxy counts finished loads in `workspace-loads` in the session dir, from
  the end of DotRush's load progress, which comes for a reload too; a proxy that predates it is not waited on.
  Errors from the script start with `dotrush-pick-project:`.
- A project picked while the server's first load is still running now loads. That load started with the previous
  choice, and the script sent no reload (it would race the load), so the new choice waited for a restart. The
  script now sends the reload once the first load completes, and waits for that one.

### 0.8.12
- DotRush is pinned to commit `618212b` (30 September 2026), up from `1b94204`. It fixes generic method
  completions that inserted an empty `<>`, and adds built-in analyzers (async void, empty catch, `is null`
  checks), their code fixes and six refactorings, so diagnostics can report those findings. Not a release
  either, so the server and the profiling tools are built from source on first use.

### 0.8.11
- The `dotrush-diagnostics` skill no longer calls analyzer findings after a file was opened "usually info": they
  came as hints too (`IDE0005`), which a plain `report` only counts, so the skill now says to pass `--hints` to see
  them. It also says that such a `report` holds at most the last file analyzed, so its `No diagnostics.` is about
  that file, not the solution, and that `CS8019` and `IDE0005` on one `using` are two findings, not a double load.

### 0.8.10
- The diagnostics summary always prints its `By code` table, over every diagnostic, hints included. A hint-only
  result printed none without `--hints`, though the `dotrush-diagnostics` skill says the table covers everything
  and asks for the most frequent codes.
- `dotrush-diagnostics.sh` errors start with `dotrush-diagnostics:`; they read `dotrush-install:`, from the shared
  install helper.
- The `dotrush-diagnostics` skill gives a polling command for `load: not completed`, documents exit statuses 1 and
  2 and `DOTRUSH_DIAGNOSTICS_QUIET`, says both `solution` arguments are optional and go in either order, and says
  that a `report` after a file was opened can hold analyzer rules (`CA…`, `IDE…`) a solution run does not.

### 0.8.9
- With a session id, the session lookup falls back to an old per-workspace runtime dir only when its proxy is
  still running. Before this session's server started, `where` showed a dead per-workspace dir from an earlier
  plugin version instead: `proxy: not running`, `publishes: no capture (older proxy)`, which the
  `dotrush-diagnostics` skill reads as "restart Claude Code". It now says `no DotRush language server has
  started in this session`, and the skill says to run an LSP operation. Without a session id nothing changes.
- `dotrush-diagnostics.sh report` and `solution` take `--hints`, before or after the count, and refuse an extra
  argument. `report N --hints` was silently ignored, while the summary said to pass `--hints`. The skill's re-read
  step uses `report 500 --hints` instead of a summarizer command that needed the session dir copied from `where`.
- "DotRush has not finished loading" now tells to wait and re-check `where`. It also offered `dotrush-pick-project`,
  which no longer applies: since 0.8.5 the load completes even when no project is found, which `where` and the
  refusal for an empty load report separately.

### 0.8.8
- The `dotrush-diagnostics` skill's re-read command passes `--count 500`; without it the summarizer lists 50 rows,
  so asking for the hints showed 50 of 151. The skill now says what `--count` does and that `report N` and
  `solution N` pass it.
- The skill says that paths, not positions, are relative to the workspace, and that a file outside it (a NuGet
  package's) keeps its absolute path; its example of a multi-project label uses full project names, as the
  summary prints them.

### 0.8.7
- The diagnostics summary also merges a diagnostic that several projects report for one file, such as the
  `Microsoft.NET.Test.Sdk` `Program.cs` every test project compiles, and names them after the message
  (`[Core.Tests(net10.0, net11.0), Tests]`). It was listed once per project, unlabelled, which read like the double
  load the `dotrush-diagnostics` skill warns about.
- `dotrush-diagnostics.sh where` (and `dotrush-cli.sh session`) print how many projects DotRush loaded:
  `load: completed (8 projects)`. With no project chosen, that is the only sign of what DotRush found on its own.
- The `dotrush-diagnostics` skill says that `solution` exits 0 with errors too, that `Publishes:` is a running
  total, and that Claude Code's `new-diagnostics` block lists a multi-targeted project's error once per framework.
  It no longer says that block includes hints, which the proxy stopped forwarding in 0.8.3.

### 0.8.6
- The diagnostics summary lists a diagnostic once when a multi-targeted project reports it per framework, with the
  frameworks after the message (`bad [net10.0, net11.0]`), and counts it once. Before, each copy was listed and
  counted, and the `dotrush-diagnostics` skill read "every diagnostic twice" as a double load and told the user to
  restart Claude Code. A copy repeated for the same framework is still listed twice, so that sign of a double load
  keeps working.
- `Files:` counts every file with a diagnostic. It counted only the files with listed rows, so a solution with
  only hints read `Files: 0` next to `Hints: 205`, and `--hints` changed it to 96.
- `dotrush-diagnostics.sh solution` no longer prints "no project chosen for this session; DotRush analyzes only
  what it loaded on its own" when no project was picked: since 0.8.5 DotRush loads a single solution itself, and a
  load with nothing in it is refused before the run.

### 0.8.5
- A workspace with one solution (or one project) loads without `dotrush-pick-project`, as the README always said.
  DotRush's `initialize` waits for a configuration with a `dotrush.roslyn` section before it loads anything, and
  Claude Code sends only the `.lsp.json` `settings`, which have none, so with no project chosen nothing ever
  loaded and every query returned "No symbols found". The proxy now sends an empty section at startup when no
  project is chosen and no `dotrush.config.json` configures DotRush, and DotRush looks for the solution itself.
- The proxy counts the projects DotRush reports loaded in `projects-loaded` in the session dir. With several
  solutions DotRush now completes its load with nothing loaded. `dotrush-diagnostics.sh solution` and the CLI's
  rename and request commands then refuse right away with `DotRush loaded no project in this session` and name
  `dotrush-pick-project`, instead of the diagnostics run waiting out its timeout, and `where` shows
  `load: completed with no project`.

### 0.8.4
- `dotnet` is also looked for in the standard install dirs (`~/.dotnet`, `/usr/local/share/dotnet`,
  `/opt/homebrew/bin`, `/usr/share/dotnet`, `/usr/lib/dotnet`) after `PATH` and `DOTNET_ROOT`, by the proxy, the
  installer, the CLI wrapper and the profiling helper. A Claude Code started with a GUI environment (the Dock, the
  desktop app, a terminal's launcher) has no `dotnet` on `PATH`, and the proxy exited 127, so Claude Code gave up
  on the server after its restarts with only "crashed with exit code 127". When the host is found off `PATH`, the
  proxy puts it on `PATH` and in `DOTNET_ROOT` for the installer and the server, so a build from source and the
  MSBuild that DotRush starts find the same SDK. The proxy also honors `DOTRUSH_DOTNET` now, as the scripts did.

### 0.8.3
- The proxy forwards only the errors and warnings of each `textDocument/publishDiagnostics` to Claude Code (and
  diagnostics without a severity, which Claude Code takes for errors). Claude Code filters no severity itself, so
  style suggestions such as IDE0058, IDE0130 or CA1305 arrived in a `<new-diagnostics>` block after every LSP call,
  even on files Claude had only read, and could use up its cap of 30 diagnostics per turn before another file's
  errors. `diagnostics.json`, and so `dotrush-diagnostics`, still holds every severity.

### 0.8.2
- The three profiling skills say how to look at a capture without uploading it. DotRush's VS Code extension
  already registers the viewers, and the helper's artifacts match their file-name patterns: `*.speedscope.json`
  and `*.nettrace.json` open in its `dotrush.traceView` as flame graphs, `*.gcdump.json` in its
  `dotrush.memoryView` as a heap-graph browser. Without VS Code, `npx speedscope <TRACE.speedscope.json>`
  renders that trace viewer from a local page, and the same for a `.nettrace.json`; a `.gcdump.json` has none,
  which the memory skill now says outright so no agent goes hunting for one. The CPU skill also says how to
  read a flame graph: pick a working thread first, Left Heavy for where the time went and
  Time Order for when, `CPU_TIME` as the sampler's leaf marker, and block width as sampled time on stack rather
  than the duration of one call. Before, the skills only forbade uploading a trace and named no local viewer, so
  the flame graph had to be improvised each time.

### 0.8.1
- `dotrush-pick-project` saves and applies the choice through `scripts/dotrush-pick-project.sh apply <path>
  [--no-restore]` instead of `printf ... > "$FIFO"` commands the agent filled in. The script builds the JSON from
  its arguments, so a path with a quote, backslash or apostrophe no longer breaks the command or its JSON, and the
  reload URI is percent-encoded. It writes the FIFO only when the session's proxy is running and the FIFO exists,
  without blocking and without creating a file; otherwise it saves `target.json`, which the proxy applies when it
  starts. Before, a dead proxy hung the skill's Bash call, and a redirect before the injector started left a
  regular file at `inject.fifo`.
- The proxy replaces a regular file it finds at its session dir's `inject.fifo` with the FIFO. Read as the FIFO,
  such a file reached end of file on every pass and its lines went to DotRush again and again in a tight loop. A
  `DOTRUSH_INJECT_FIFO` naming an existing file or directory that is not a FIFO is left alone and disables injection.
- Profiles with no output dir go to `profiles/` in the plugin data dir, as documented, and no longer to
  `~/.cache/dotrush-cc/profiles`: `CLAUDE_PLUGIN_DATA` is not set in a skill's Bash, so the dir is now derived the
  same way as the tools' install dir.
- The proxy reads large frames in linear time (the buffer was copied on every 64 KB read) and no longer parses
  every server response as JSON only to build a log line it then discarded.

### 0.8.0
- New skill `dotrush-profile-allocations` answers "what allocates": it captures a trace with `--profile gc-verbose`
  and runs `dotrush-profile.sh alloc-report <trace> [count] [--focus <function|type> [--depth N]]`, which ranks
  allocated MB by type, by the function that allocated and by inclusive caller, with the samples behind each row,
  plus `AllocatedMB` and `AllocationRate`. `--focus` on a type prints who allocates it; on a function, its callers
  and the types it allocates below it. The figures are estimates from `GCAllocationTick`, one sample per ~100 KB.
- DotRush is pinned to commit `1b94204` (one past the 2026.09 release), whose `dotnet-trace convert --format Json`
  writes the allocation profile `alloc-report` reads. It has no release, so the server and the profiling tools are
  built from source on first use (a few minutes, `git` and a .NET SDK). `alloc-report` refuses a build without the
  format, and `tools` shows `trace-json=`.

### 0.7.6
- `dotrush-profile.sh trace-report <trace> [count] [thread-id] --focus <function> [--depth N]` replaces the thread
  table and rankings with two trees for one function: its callers, and what it calls with its own time as
  `(self)`. The name resolves by whole signature, then without its parameters, then by trailing `Type.Method` or
  `Method`, then as a substring; an ambiguous name is refused with the candidates. `--depth` caps each tree (8 by
  default), `count` caps the rows per level, and rows under 1% of the function's time are folded.

### 0.7.5
- Claude Code processes started from one agterm tab (background jobs included) no longer share a runtime dir.
  They all inherit the tab's `AGTERM_SESSION_ID`, so their language servers overwrote each other's `pid`,
  request responses and saved rename plans, and read one injection FIFO: a request could be answered by
  another job's server. The dir is now keyed on the session id and the launching Claude Code process, and the
  tools look it up by `CLAUDE_PID`. `DOTRUSH_SESSION_ID` is still used as given.

### 0.7.4
- `trace` and `trace --launch` take `--profile`, `--providers` and `--buffersize` anywhere before `--`, so a capture
  can add GC or allocation events (for example `gc-verbose`). `dotnet-sampled-thread-time`, which the CPU report
  is built from, is added to any `--profile` without it, and `--providers` alone keeps the default pair.
- With TPL task events in a trace, `AWAIT_TIME` counts as blocked time, `STARTING TASK` and `UNKNOWN_ASYNC` are no
  longer ranked as functions, and the report and the diff warn that stacks are stitched.

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
