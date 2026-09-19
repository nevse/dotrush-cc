# dotrush — DotRush C# LSP for Claude Code

Wires the [DotRush](https://github.com/JaneySprings/DotRush) Roslyn language server into Claude Code's
`LSP` tool, and puts a stdio **proxy** in front of it so you can inject custom LSP messages into the
*running* server — on-demand diagnostics, semantic solution-wide renames, and live solution reconfigure/reload
without a restart.

Release notes are in [`CHANGELOG.md`](CHANGELOG.md).

## Contents

| File | Role |
|------|------|
| `.claude-plugin/plugin.json` | plugin manifest; declares the `csharp` LSP server via `.lsp.json` |
| `.lsp.json` | maps `.cs/.csx/.cshtml` → `bin/lsp-proxy.py`; wires portable `${CLAUDE_PLUGIN_ROOT}`/`${CLAUDE_PLUGIN_DATA}` paths + a 15 min startup timeout (a first run may build from source) |
| `bin/lsp-proxy.py` | stdio man-in-the-middle: verbatim forwarding + custom-message injection + request-channel responses to files + auto-install-on-first-run (stdlib-only Python 3) |
| `tools/DotRushCli/` | the plugin's C# CLI (`net10.0`, no packages): `session`, `request`, `rename preview`, `rename apply` |
| `scripts/dotrush-cli.sh` | runs the CLI, building it on first use into `${CLAUDE_PLUGIN_DATA}/cli/<source-hash>/` |
| `dotrush-version.json` | pins the DotRush repository and the one tag or commit both the server and the profiling tools come from |
| `scripts/install-dotrush.sh` | installs the DotRush server or profiling tools at the pinned ref: the release bundles when the release ships them, otherwise a build from source (logic shared in `scripts/dotrush-install.sh`) |
| `scripts/dotrush-profile.sh` | runs DotRush's own `dotnet-trace`/`dotnet-gcdump` at the pinned ref, installed exactly as the server is, then collects and reports bounded CPU and allocation traces or GC dumps |
| `scripts/summarize-speedscope.py` | derives full-name exclusive/inclusive managed-CPU rankings from Speedscope output, and with `--allocations` allocated MB by type and function from `dotnet-trace --format Json` output |
| `scripts/analyze-gcdump.py` | streams the heap graph DotRush's `dotnet-gcdump --format Json` writes: exact per-type bytes, the largest retained objects with their dominator chain, and snapshot diffs (backs `heap-report`/`heap-diff`) |
| `scripts/dotrush-diagnostics.sh` | injects `dotrush/solutionDiagnostics` into this session's server, waits for the results the proxy captures, and reports them |
| `scripts/summarize-diagnostics.py` | summarizes the proxy's `diagnostics.json`: counts by severity and code, errors first, hints hidden unless asked |
| `skills/dotrush-pick-project/` | picks the `.sln/.slnx/.csproj` DotRush loads for the session and applies it live |
| `skills/dotrush-diagnostics/` | runs whole-solution compiler analysis and reports errors and warnings |
| `skills/dotrush-rename/` | renames a C# symbol across the loaded solution: diff preview, then, after you confirm, an apply that checks every file before replacing any |
| `skills/dotrush-profile-cpu/` | attaches `dotnet-trace` or launches the target under it, creates Speedscope plus top-method artifacts, and guides evidence-based analysis |
| `skills/dotrush-profile-allocations/` | captures a trace with allocation sampling and reports allocated bytes by type, allocating function and call path |
| `skills/dotrush-profile-memory/` | collects `dotnet-gcdump` snapshots, reports per-type bytes and retention chains, and compares snapshots for managed-memory growth |

## The server auto-installs

The DotRush version this plugin version uses is pinned in `dotrush-version.json`, one ref for everything:

```json
{
  "repository": "JaneySprings/DotRush",
  "ref": "1b942045447104061b30b1b5f135a0e28e07ef6c"
}
```

`ref` is a release tag or a full commit SHA. The language server and the profiling tools are both installed from
it by `install-dotrush.sh`, the same way and from the same place:

- a **release tag** whose GitHub release ships `DotRush.Bundle.LanguageServer.zip` and
  `DotRush.Bundle.Diagnostics.zip`: both bundles are downloaded (`curl` + `unzip`);
- **anything else**, a commit or a release missing either bundle: both are built from source at that ref (`git` +
  a .NET 10 SDK, a few minutes per component). Both are `dotnet publish`ed as DotRush's `build.cake` does
  before its `pack` step zips them into those bundles.

The pin is DotRush commit `1b94204` (19 September 2026), one commit past the 2026.09 release: its `dotnet-trace`
has `--format Json`, whose allocation profile `alloc-report` reads. It has no release yet, so both components are
built from source on first use; move the pin to the next release tag once it ships, and the bundles are downloaded
again.

The bundles are platform-neutral and the server has no native launcher, so the proxy starts it as
`dotnet DotRush.dll`, which needs a .NET 10 or newer runtime on `PATH` or in `DOTNET_ROOT`. On C# LSP start the
proxy compares `${CLAUDE_PLUGIN_DATA}/server/.dotrush-ref` with the pin. When the server is
missing or at another ref, it runs the installer, which prepares the new server beside the old one and swaps it in
whole; if that fails, the previous server keeps running. A build makes that first start take minutes, so
`.lsp.json` allows 15. Concurrent sessions wait for one install rather than repeating it, and a failed build keeps
its log in `${CLAUDE_PLUGIN_DATA}/server-build.log`.

Manual / re-install:
```bash
bash "$PLUGIN/scripts/install-dotrush.sh" server "$DATA/server" --force
bash "$PLUGIN/scripts/install-dotrush.sh" diagnostics "$DATA/diagnostics" --force
```
Override the ref with `DOTRUSH_REF` and the repository with `DOTRUSH_REPO`, or point at an existing server (`DotRush.dll` or a
native launcher) with `DOTRUSH_REAL_BIN` (env, set in your Claude settings or `.lsp.json`); a server named by `DOTRUSH_REAL_BIN` is
never installed over.

## Point DotRush at your project

DotRush loads a project only when the workspace resolves to a single `.sln/.slnx/.csproj`. In a
monorepo/multi-project root it finds many and **loads nothing** — every query returns "No symbols found".

**Recommended — the `dotrush-pick-project` skill (interactive, no config file).** Ask Claude to *"set up the
DotRush project"* (or invoke the `dotrush-pick-project` skill). It finds the `.sln/.slnx/.csproj` candidates in
your workspace, **asks which one to use**, applies it live (no restart), and remembers the choice for this
session — stored in the plugin's data dir (`target.json`) and replayed on the session's LSP restarts.
**Nothing is written into your repo.** It asks once per session; a fresh Claude session picks again (see
[Multiple sessions / projects / worktrees](#multiple-sessions--projects--worktrees)).

**Alternative — `dotrush.config.json`.** If you prefer a file, create it in your working directory:
```json
{ "dotrush": { "roslyn": { "projectOrSolutionFiles": ["/abs/path/to/YourSolution.sln"], "restoreProjectsBeforeLoading": true } } }
```
Read at server **startup**; restart Claude Code after editing (or use live reload, below).

## Verify it loaded

There is **no `/lsp` command** in current Claude Code. To check:
- Open **`/plugin` → Installed → `dotrush`** — it lists the `csharp` LSP server. The **Errors** tab shows
  start-up failures (missing `python3`, download errors, etc.).
- Or just use it: ask Claude to "find references to <symbol>" / "go to definition" in a `.cs` file. Real
  results = it's working. "No symbols" = no project loaded → run `dotrush-pick-project`.

## Multiple sessions / projects / worktrees

Each Claude Code session spawns its own DotRush server, and all runtime state (chosen project, FIFO, log)
is scoped **per session** under `${CLAUDE_PLUGIN_DATA}/ws/sess-<hash-of-session-id>/`. So every session —
even several launched from the **same** folder, e.g. one per git worktree — gets its own project choice and
injection FIFO, and they never collide or mesh. Dirs from ended sessions are pruned automatically on the
next server start.

The session id comes from `AGTERM_SESSION_ID`. Every Claude Code process started from one agterm tab,
background jobs included, inherits the same id, so the key also includes the launching Claude Code process
(the server's parent, which the tools see as `CLAUDE_PID`). On terminals that don't set one
(headless/CI), the plugin falls back to per-**workspace** scoping keyed on the project-dir hash — the older
behavior, where the project choice also persists across restarts. Per-session scoping trades that
cross-restart persistence for isolation: a fresh session re-picks its project (parallel worktree sessions
otherwise share `CLAUDE_PROJECT_DIR` and clobber one shared choice).

The plugin's tools find the current session's dir with `dotrush-cli.sh session --dir`: the `sess-*` dir recording
this session id (and, for an agterm id, this Claude Code process), else a dir whose `workspace.txt` is `CLAUDE_PROJECT_DIR` (or the working directory) among the
per-workspace dirs only. Another session's `sess-*` dir is never picked, even for the same workspace.

## Capabilities (via the Claude Code `LSP` tool)

Work: `documentSymbol`, `workspaceSymbol` (needs a non-empty query), `hover`, `goToDefinition`,
`findReferences`, `goToImplementation`. Cross-project results require the containing projects to be
loaded (target a solution, not a single `.csproj`).

Not supported: **call hierarchy** (`prepareCallHierarchy`/`incomingCalls`/`outgoingCalls`) — DotRush (as of the
pinned ref) has no call-hierarchy handler. Use `findReferences` instead.

## Profiling .NET applications

The plugin mirrors DotRush's profiling split with three Claude skills:

- Ask Claude to **profile CPU**, find a hot path, investigate high CPU/latency, or invoke
  `dotrush-profile-cpu`. It attaches `dotnet-trace` for a bounded interval, or launches a short-lived program
  or an executable test project under it (`trace --launch … -- <command>`), and creates a `.nettrace`, a
  `.speedscope.json`, and a text top-method report. `trace-report --focus <function>` shows one function's
  callers and callees as trees with their share of the time. For a before/after change, `trace-diff` ranks functions by
  how much their share of each capture's managed CPU moved. `--profile`, `--providers` and `--buffersize` add
  events to a capture (for example `gc-verbose`) while the thread-time sampler behind the report stays on. `ps` lists attachable processes with their elapsed
  time, main assembly and command line, and `--filter` narrows the list.
- Ask Claude **what allocates**, about allocation rate or GC pressure, or invoke `dotrush-profile-allocations`. It
  captures a trace with `--profile gc-verbose`, whose `GCAllocationTick` events sample one allocation per ~100 KB
  with its type and stack, and `alloc-report` ranks allocated MB by type, by the function that allocated and by
  inclusive caller, with the samples behind each row. `--focus` takes a type (who allocates it) or a function (its
  callers, and the types it allocates below it). It converts the `.nettrace` with `dotnet-trace --format Json`.
- Ask Claude to **profile managed memory**, investigate a suspected leak, compare heap snapshots, or invoke
  `dotrush-profile-memory`. It collects a `.gcdump` together with its heap graph as `.gcdump.json`, reports
  exact per-type bytes and the largest retained objects with the dominator chain that keeps each alive, and
  ranks per-type byte and object-count deltas between a baseline and a later snapshot.

All three skills use `scripts/dotrush-profile.sh`, which runs DotRush's own build of `dotnet-trace` and
`dotnet-gcdump` ([JaneySprings/diagnostics](https://github.com/JaneySprings/diagnostics)) at the pinned ref. The
tools are installed into `${CLAUDE_PLUGIN_DATA}/diagnostics` on first use exactly as the server is (see
[The server auto-installs](#the-server-auto-installs)); a failed build keeps its log in
`${CLAUDE_PLUGIN_DATA}/diagnostics-build.log`. They run as `dotnet <tool>.dll`, so a .NET runtime must be on
`PATH` or in `DOTNET_ROOT`. `dotrush-profile.sh tools` shows the pin, whether it installs from a release or builds,
and the state of the server and the tools, installing nothing. `DOTRUSH_DIAGNOSTICS_DIR` uses a ready directory of
the tools instead.

The memory reports need a `dotnet-gcdump` with `--format Json`, and `alloc-report` a `dotnet-trace` with it; each
refuses a pinned build without it (`tools` shows `gcdump-json=` and `trace-json=`).
Artifacts never default into your repository: without an explicit output directory they go to
`$DOTRUSH_PROFILE_OUTPUT_DIR`, else `${CLAUDE_PLUGIN_DATA}/profiles`, else
`${XDG_CACHE_HOME:-~/.cache}/dotrush-cc/profiles`. Claude or the user can always supply one instead.

Trace durations must be given as `hh:mm:ss` (or `dd:hh:mm:ss`), with `hh` 00-23 and `mm`/`ss` 00-59.
Both bounds exist because `dotnet-trace` binds `--duration` with `TimeSpan.Parse`, which reinterprets
any field that runs past its range rather than rejecting it: `00:30` is `hh:mm` (30 minutes, not 30
seconds) and `24:00:00` is `dd:hh:mm` (24 days, not one). For a day or more, use the day field —
`01:00:00:00`.

`dotnet-gcdump` triggers a full generation 2 GC and can pause the target, so the memory skill requires an
explicit impact check before attaching to production or another latency-sensitive process. Neither skill
uploads profiling artifacts to external viewers.

## Solution diagnostics

Ask Claude for **solution diagnostics** ("what compiler errors does the solution have?") or invoke
`dotrush-diagnostics`. It runs DotRush's whole-solution compiler analysis in the session's server and reports
counts by severity, the most frequent codes, and the errors and warnings with their locations. Nothing is built.

DotRush sends analysis results only as `textDocument/publishDiagnostics` notifications to the client. Claude Code
does show the model newly published diagnostics, including for files it never opened, but only the ones it has not
shown before, hints included, and with no sign of when the analysis finished. So the proxy mirrors every publish
into `diagnostics.json` in the session dir: the latest list per file, a publish count and the time of the last one.
That file is the complete current set the skill reports from. `scripts/dotrush-diagnostics.sh solution` records the count, injects the request, and waits until the
count moves and publishing has been quiet for two seconds, because DotRush signals no completion.

```bash
"$PLUGIN/scripts/dotrush-diagnostics.sh" where          # dir, workspace, proxy, load, target, publishes, channel
"$PLUGIN/scripts/dotrush-diagnostics.sh" solution 100   # analyze, wait, list up to 100 diagnostics
"$PLUGIN/scripts/dotrush-diagnostics.sh" report         # last published results, no analysis
```

What to expect:
- Only **compiler** diagnostics (with suppressors applied); analyzer packages are not part of a solution run.
- Hints are counted but not listed; `summarize-diagnostics.py --hints` lists them.
- DotRush publishes only files with diagnostics or that just lost them, so a clean solution publishes nothing and
  `solution` exits 3 after `DOTRUSH_DIAGNOSTICS_TIMEOUT` (default 300 s) — as does an analysis still running.
- Any later analysis replaces the set: once Claude Code opens or edits a file, DotRush re-analyzes that document
  and clears the others, so run `solution` again for the whole view. An edit during the analysis cancels it.

## Semantic rename

Ask Claude to **rename a C# symbol** ("rename `Greeter` to `Welcomer`") or invoke `dotrush-rename`. DotRush
resolves the symbol with Roslyn, so only references to that one symbol change; same-named identifiers elsewhere are
left alone. Claude finds the symbol's position with the `LSP` tool, previews the rename, shows you the summary and
diff, and applies it only after you confirm.

```bash
"$PLUGIN/scripts/dotrush-cli.sh" rename preview src/Greeter.cs 3 14 Welcomer   # 1-based line and column
"$PLUGIN/scripts/dotrush-cli.sh" rename apply 3f9a0c1b2d4e                      # the id from preview's plan: line
```

- `preview` sends `textDocument/rename` through the [request channel](#requests-the-request-channel), checks that
  every edited range on disk still lies in an identifier with the old name, and saves a plan in the session dir
  (`edits/<plan-id>.json`, with a sha256 per file, and the full diff in `edits/<plan-id>.diff`). It prints
  `N edits in M files`, one line per file, the first 200 diff lines, `diff:` and `plan:`. A new name must be a valid
  C# identifier; a reserved keyword needs `@`. `--timeout N` (default 60 s) bounds the wait for DotRush.
- `apply` re-checks every file's hash and refuses if any changed since the preview, writing nothing. It writes each
  file to a temp file (keeping BOM, line endings and file mode; a symlink's target is written), then moves all of
  them into place, and sends `textDocument/didOpen` for each changed file so DotRush re-reads it from disk. It prints
  `renamed <Old> to <New>: N edits in M files` and the changed paths. Every refusal happens before the first file is
  replaced; if a move itself fails (permissions, a full disk), it reports which files changed and which did not,
  drops the plan and exits 1.
- Files outside the workspace root or under a `bin`/`obj` directory are marked `(outside workspace)` in the
  preview; `apply` refuses them unless given `--outside-workspace`.
- Plans belong to the server that produced them: a server restart clears `edits/`.

Limits come from DotRush's rename: overloads, text in strings and comments, and file names are not renamed (a class
in a same-named file keeps that file name), and only documents in loaded projects change. Claude Code does not
learn of the writes by itself, so the skill re-reads the changed files before editing them further.

## The plugin CLI

`scripts/dotrush-cli.sh` runs `tools/DotRushCli`. On first use it builds the CLI with the .NET 10 SDK (a few
seconds, `building the DotRush CLI` on stderr) into `${CLAUDE_PLUGIN_DATA}/cli/<hash of the tool sources>/`, so
sessions on different plugin versions keep separate builds. The build runs from `tools/`, whose empty
`Directory.*` files and location keep your repository's `global.json` and `Directory.Build.*` out of it. Concurrent
sessions wait for one build, a failed build prints the end of `${CLAUDE_PLUGIN_DATA}/cli-build.log`, and builds for
other sources are removed after a day unused. `DOTRUSH_CLI_DIR` runs a ready `DotRushCli.dll` from that directory
and builds nothing.

```bash
"$PLUGIN/scripts/dotrush-cli.sh" session           # dir, workspace, proxy, load, target, publishes, channel
"$PLUGIN/scripts/dotrush-cli.sh" session --dir     # just this session's dir (exit 1 when there is none)
"$PLUGIN/scripts/dotrush-cli.sh" request textDocument/hover '{"textDocument":{"uri":"file:///abs/A.cs"},"position":{"line":2,"character":14}}'
"$PLUGIN/scripts/dotrush-cli.sh" help
```

Exit status: 0 success, 1 error (a JSON-RPC error prints as `code: message`), 2 usage, 3 timeout. Errors go to
stderr prefixed with `dotrush-cli:`. `help`, `-h` and `--help` print the table above.

- `--timeout N` takes seconds above 0 and at most 86400 (24 h), once per command; anything else is exit 2 with
  `--timeout needs a number of seconds greater than 0` (or `--timeout may be given only once`).
- `<params-json>` must be a JSON object or array.
- The wrapper needs `shasum` or `sha256sum` to hash the sources, and builds and runs with `DOTRUSH_DOTNET` when that
  is set, else `dotnet` from `PATH` or `DOTNET_ROOT`. It always exports `DOTRUSH_DATA_DIR` as the data dir it derives
  (`CLAUDE_PLUGIN_DATA` when set), overwriting an inherited value.

## Injecting custom LSP messages (the proxy)

The proxy forwards Claude ⇄ DotRush verbatim and injects newline-delimited JSON-RPC written to a FIFO,
at frame boundaries under a lock — so injection never desyncs request/response pairing.

The FIFO + log live in a **per-session** dir (`${CLAUDE_PLUGIN_DATA}/ws/sess-<hash>/`) so concurrent
sessions never collide. Find the ones for the *current* session with the plugin CLI (see
[Multiple sessions / projects / worktrees](#multiple-sessions--projects--worktrees) for how it matches):
```bash
WSDIR=$("$PLUGIN/scripts/dotrush-cli.sh" session --dir)
FIFO="$WSDIR/inject.fifo"     # inject here
LOG="$WSDIR/proxy.log"        # INJECT events + server->client traffic
```

Inject (`jsonrpc:"2.0"` auto-added):
```bash
echo '{"method":"$/setTrace","params":{"value":"verbose"}}' > "$FIFO"
tail -f "$LOG"
```

The injector opens the FIFO once for the life of the proxy and holds a write end of it itself, so it never sees
end of file when a writer closes: writers can come and go without a line falling into a reopen. Write each message
as one line in a single `write` (a line up to `PIPE_BUF`, 512 bytes on macOS, cannot interleave with another
writer's). A writer that dies half way through a line leaves that partial line in the pipe, and it spoils the next
writer's first line (logged as `INJECT skipped (bad JSON)`).

Echo into the FIFO only **notifications** (no `id`, fire-and-forget, silently ignored if unknown). Send requests
through the request channel below, so their responses come back to you rather than to Claude Code.

### Requests: the request channel

A response whose `id` is the string `dotrush-cc:<uuid>` (a lowercase GUID) is not forwarded to Claude Code: the
proxy writes its JSON-RPC body verbatim to `responses/<uuid>.json` in the session dir (via a temp file and a
rename). Claude Code's integer ids never match, so the proxy keeps no request table; a `dotrush-cc:` id whose suffix
is not a uuid is dropped and logged, so an id never becomes a path. The proxy creates `responses/` empty at start,
and its presence marks a proxy that has the channel; it also removes `edits/`, the saved rename plans.

`dotrush-cli.sh request <method> <params-json> [--timeout N]` does the whole exchange: it writes the request line to
the FIFO, waits for the response file (default 60 s), prints the `result` as JSON on stdout and deletes the file.
A JSON-RPC error prints `code: message` and exits 1. On timeout it injects `$/cancelRequest` for the id, deletes a
response that arrives within a second, and exits 3. Response files older than 10 minutes are removed when a
`request` or `rename preview` starts. Before sending, it requires a running proxy, `responses/`, and `load-completed`, and
names the next step when one is missing (run a C# LSP operation, restart Claude Code, or pick a project). A write
that fails with a broken pipe (the proxy's read end is gone) is retried once, with the whole batch through a new
open; `rename apply` sends all its `didOpen` notifications as one such batch. The CLI has no delivery receipt for a
notification: a line is lost only if the proxy exits before reading it, and then DotRush is gone with it.

### DotRush-specific injectable notifications

| Method | Params | Effect |
|--------|--------|--------|
| `dotrush/solutionDiagnostics` | `{}` | analyze the whole solution → burst of `textDocument/publishDiagnostics`; Claude Code surfaces the ones it has not shown yet, and the proxy captures all of them in `diagnostics.json` (see [Solution diagnostics](#solution-diagnostics)) |
| `dotrush/documentDiagnostics` | `DidOpenTextDocumentParams` | analyze a single document |
| `dotrush/reloadWorkspace` | `{"workspaceFolders":[{"uri","name"}]}` | clear caches, re-run project load |
| `workspace/didChangeConfiguration` | `{"settings":{"dotrush":{"roslyn":{…}}}}` | replace the roslyn config (see live reload) |

## Live reconfigure + reload (no restart)

Switch target solution live by injecting a config change then a reload (`$FIFO` = this workspace's FIFO,
found as above):

```bash
echo '{"method":"workspace/didChangeConfiguration","params":{"settings":{"dotrush":{"roslyn":{"projectOrSolutionFiles":["/abs/Other.sln"],"restoreProjectsBeforeLoading":false}}}}}' > "$FIFO"
echo '{"method":"dotrush/reloadWorkspace","params":{"workspaceFolders":[{"uri":"file:///abs/workspace","name":"ws"}]}}' > "$FIFO"
```

Notes (learned while verifying this):
- `didChangeConfiguration` **replaces the entire roslyn section** — include every setting you care about.
- A reload emits `dotrush/projectLoaded` per project but **not** `dotrush/loadCompleted` (that fires only
  on initial init). Wait on `projectLoaded` + memory settling.
- **Don't reload a server that has not completed its first load.** DotRush's `initialize` waits for a
  configuration, then loads the project and only then starts its code-analysis worker and sends
  `dotrush/loadCompleted`. On a server started with no project, `didChangeConfiguration` alone loads it; adding
  `reloadWorkspace` races that load, and DotRush either never starts analysis or loads the project twice (every
  diagnostic reported twice). The proxy creates `load-completed` in the session
  dir on `dotrush/loadCompleted`, so reload only when that file exists (`dotrush-pick-project` does this).
- Inject `didChangeConfiguration` **before** `reloadWorkspace` (FIFO delivery is in order).

## Troubleshooting

- **Every query returns "No symbols found"** → no project loaded. Run the **`dotrush-pick-project`** skill to pick a `.sln/.slnx/.csproj` (or add `dotrush.config.json`).
- **Navigation works but no diagnostics ever arrive** (`dotrush-diagnostics.sh where` shows `load: not completed`
  long after `projectLoaded`) → the project was loaded with a `dotrush/reloadWorkspace` before DotRush's first load
  completed, so its analysis worker never started. The same race can instead load the project twice, which shows
  as every diagnostic listed twice. Restart Claude Code either way; the chosen project is replayed at startup.
- **Server or profiling tools didn't install** → run `dotrush-profile.sh tools` to see the pin and whether it downloads
  or builds. A download needs `curl`/`unzip`; a build needs `git` and a .NET SDK and keeps its log in
  `${CLAUDE_PLUGIN_DATA}/<component>-build.log`. Run `install-dotrush.sh server` (or `diagnostics`) by hand and inspect `proxy.log`.
- **Rename says "the running DotRush proxy predates the request channel"** (`session` shows
  `channel: unavailable (older proxy)`) → the language server started before the plugin was updated. Restart Claude
  Code. Diagnostics and project picking still work with the older proxy.
- **The CLI does not build** → `dotrush-cli.sh` needs a .NET 10 SDK (and `shasum` or `sha256sum`) and prints the end
  of `${CLAUDE_PLUGIN_DATA}/cli-build.log`; run `dotrush-cli.sh help` to retry. Rename, `dotrush-diagnostics.sh`
  (`where`, `solution`, `report`) and `dotrush-pick-project` all go through this wrapper, so a build that fails
  stops all three, not only rename.
- **`python3` not found when the LSP starts** → ensure `python3` is on the PATH Claude Code launches with,
  or set the `.lsp.json` `command` to your interpreter explicitly.
- Disable the proxy's logging by setting `DOTRUSH_PROXY_LOG=""`.

