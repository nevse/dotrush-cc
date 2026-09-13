# dotrush — DevExpress C# LSP for Claude Code

Wires the [DotRush](https://github.com/JaneySprings/DotRush) Roslyn language server into Claude Code's
`LSP` tool, and puts a stdio **proxy** in front of it so you can inject custom LSP messages into the
*running* server — on-demand diagnostics, and live solution reconfigure/reload without a restart.

## Contents

| File | Role |
|------|------|
| `.claude-plugin/plugin.json` | plugin manifest; declares the `csharp` LSP server via `.lsp.json` |
| `.lsp.json` | maps `.cs/.csx/.cshtml` → `bin/lsp-proxy.py`; wires portable `${CLAUDE_PLUGIN_ROOT}`/`${CLAUDE_PLUGIN_DATA}` paths + a 180 s startup timeout (first-run download) |
| `bin/lsp-proxy.py` | stdio man-in-the-middle: verbatim forwarding + custom-message injection + auto-install-on-first-run (stdlib-only Python 3) |
| `dotrush-version.json` | pins the DotRush repository and the one tag or commit both the server and the profiling tools come from |
| `scripts/install-dotrush.sh` | installs the DotRush server or profiling tools at the pinned ref: the release bundles when the release ships them, otherwise a build from source (logic shared in `scripts/dotrush-install.sh`) |
| `scripts/dotrush-profile.sh` | runs DotRush's own `dotnet-trace`/`dotnet-gcdump` at the pinned ref, installed exactly as the server is, then collects and reports bounded CPU traces or GC dumps |
| `scripts/summarize-speedscope.py` | derives full-name exclusive/inclusive managed-CPU rankings from Speedscope output |
| `scripts/analyze-gcdump.py` | streams the heap graph DotRush's `dotnet-gcdump --format Json` writes: exact per-type bytes, the largest retained objects with their dominator chain, and snapshot diffs (backs `heap-report`/`heap-diff`) |
| `skills/dotrush-pick-project/` | picks the `.sln/.slnx/.csproj` DotRush loads for the session and applies it live |
| `skills/dotrush-profile-cpu/` | attaches `dotnet-trace`, creates Speedscope plus top-method artifacts, and guides evidence-based analysis |
| `skills/dotrush-profile-memory/` | collects `dotnet-gcdump` snapshots, reports per-type bytes and retention chains, and compares snapshots for managed-memory growth |

## The server auto-installs

The DotRush version this plugin version uses is pinned in `dotrush-version.json`, one ref for everything:

```json
{
  "repository": "JaneySprings/DotRush",
  "ref": "2026.09"
}
```

`ref` is a release tag or a full commit SHA. The language server and the profiling tools are both installed from
it by `install-dotrush.sh`, the same way and from the same place:

- a **release tag** whose GitHub release ships `DotRush.Bundle.LanguageServer.zip` and
  `DotRush.Bundle.Diagnostics.zip`: both bundles are downloaded (`curl` + `unzip`);
- **anything else**, a commit or a release missing either bundle: both are built from source at that ref (`git` +
  a .NET 10 SDK, a few minutes per component). Both are `dotnet publish`ed as DotRush's `build.cake` does
  before its `pack` step zips them into those bundles.

The pin is the 2026.09 release, republished on 13 September 2026 with both bundles and a `dotnet-gcdump` that has
`--format Json`, so nothing is built.

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

The session id comes from `AGTERM_SESSION_ID` (unique per Claude session). On terminals that don't set one
(headless/CI), the plugin falls back to per-**workspace** scoping keyed on the project-dir hash — the older
behavior, where the project choice also persists across restarts. Per-session scoping trades that
cross-restart persistence for isolation: a fresh session re-picks its project (parallel worktree sessions
otherwise share `CLAUDE_PROJECT_DIR` and clobber one shared choice).

## Capabilities (via the Claude Code `LSP` tool)

Work: `documentSymbol`, `workspaceSymbol` (needs a non-empty query), `hover`, `goToDefinition`,
`findReferences`, `goToImplementation`. Cross-project results require the containing projects to be
loaded (target a solution, not a single `.csproj`).

Not supported: **call hierarchy** (`prepareCallHierarchy`/`incomingCalls`/`outgoingCalls`) — this DotRush
build registers no call-hierarchy handler. Use `findReferences` instead.

## Profiling .NET applications

The plugin mirrors DotRush's profiling split with two Claude skills:

- Ask Claude to **profile CPU**, find a hot path, investigate high CPU/latency, or invoke
  `dotrush-profile-cpu`. It attaches `dotnet-trace` for a bounded interval and creates a `.nettrace`, a
  `.speedscope.json`, and a text top-method report.
- Ask Claude to **profile managed memory**, investigate a suspected leak, compare heap snapshots, or invoke
  `dotrush-profile-memory`. It collects a `.gcdump` together with its heap graph as `.gcdump.json`, reports
  exact per-type bytes and the largest retained objects with the dominator chain that keeps each alive, and
  ranks per-type byte and object-count deltas between a baseline and a later snapshot.

Both skills use `scripts/dotrush-profile.sh`, which runs DotRush's own build of `dotnet-trace` and
`dotnet-gcdump` ([JaneySprings/diagnostics](https://github.com/JaneySprings/diagnostics)) at the pinned ref. The
tools are installed into `${CLAUDE_PLUGIN_DATA}/diagnostics` on first use exactly as the server is (see
[The server auto-installs](#the-server-auto-installs)); a failed build keeps its log in
`${CLAUDE_PLUGIN_DATA}/diagnostics-build.log`. They run as `dotnet <tool>.dll`, so a .NET runtime must be on
`PATH` or in `DOTNET_ROOT`. `dotrush-profile.sh tools` shows the pin, whether it installs from a release or builds,
and the state of the server and the tools, installing nothing. `DOTRUSH_DIAGNOSTICS_DIR` uses a ready directory of
the tools instead.

The memory reports need a `dotnet-gcdump` with `--format Json`; the heap commands refuse a pinned build without it.
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

## Injecting custom LSP messages (the proxy)

The proxy forwards Claude ⇄ DotRush verbatim and injects newline-delimited JSON-RPC written to a FIFO,
at frame boundaries under a lock — so injection never desyncs request/response pairing.

The FIFO + log live in a **per-session** dir (`${CLAUDE_PLUGIN_DATA}/ws/sess-<hash>/`) so concurrent
sessions never collide. Find the ones for the *current* session by matching the recorded session id
(falling back to the workspace path when no session id is set):
```bash
ROOT="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/plugins/data"
SID="${DOTRUSH_SESSION_ID:-$AGTERM_SESSION_ID}"
HIT=""
[ -n "$SID" ] && HIT=$(grep -lFx "$SID" "$ROOT"/*/ws/sess-*/session.txt 2>/dev/null | head -1)
[ -z "$HIT" ] && HIT=$(grep -lFx "$PWD" "$ROOT"/*/ws/*/workspace.txt 2>/dev/null | head -1)
WSDIR=$(dirname "$HIT")
FIFO="$WSDIR/inject.fifo"     # inject here
LOG="$WSDIR/proxy.log"        # INJECT events + server->client traffic
```

Inject (`jsonrpc:"2.0"` auto-added):
```bash
echo '{"method":"$/setTrace","params":{"value":"verbose"}}' > "$FIFO"
tail -f "$LOG"
```

Prefer **notifications** (no `id`, fire-and-forget, silently ignored if unknown). A **request** (with `id`)
makes DotRush reply, and that unsolicited response flows back to Claude Code — only inject one if the
client tolerates it.

### DotRush-specific injectable notifications

| Method | Params | Effect |
|--------|--------|--------|
| `dotrush/solutionDiagnostics` | `{}` | analyze the whole solution → burst of `textDocument/publishDiagnostics` (Claude Code surfaces the new diagnostics automatically) |
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
- Inject `didChangeConfiguration` **before** `reloadWorkspace` (FIFO delivery is in order).

## Troubleshooting

- **Every query returns "No symbols found"** → no project loaded. Run the **`dotrush-pick-project`** skill to pick a `.sln/.slnx/.csproj` (or add `dotrush.config.json`).
- **Server or profiling tools didn't install** → run `dotrush-profile.sh tools` to see the pin and whether it downloads
  or builds. A download needs `curl`/`unzip`; a build needs `git` and a .NET SDK and keeps its log in
  `${CLAUDE_PLUGIN_DATA}/<component>-build.log`. Run `install-dotrush.sh server` (or `diagnostics`) by hand and inspect `proxy.log`.
- **`python3` not found when the LSP starts** → ensure `python3` is on the PATH Claude Code launches with,
  or set the `.lsp.json` `command` to your interpreter explicitly.
- Disable the proxy's logging by setting `DOTRUSH_PROXY_LOG=""`.

## Changelog

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
