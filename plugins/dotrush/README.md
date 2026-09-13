# dotrush — DotRush C# LSP for Claude Code

Wires the [DotRush](https://github.com/JaneySprings/DotRush) Roslyn language server into Claude Code's
`LSP` tool, and puts a stdio **proxy** in front of it so you can inject custom LSP messages into the
*running* server — on-demand diagnostics, and live solution reconfigure/reload without a restart.

Release notes are in [`CHANGELOG.md`](CHANGELOG.md).

## Contents

| File | Role |
|------|------|
| `.claude-plugin/plugin.json` | plugin manifest; declares the `csharp` LSP server via `.lsp.json` |
| `.lsp.json` | maps `.cs/.csx/.cshtml` → `bin/lsp-proxy.py`; wires portable `${CLAUDE_PLUGIN_ROOT}`/`${CLAUDE_PLUGIN_DATA}` paths + a 15 min startup timeout (a first run may build from source) |
| `bin/lsp-proxy.py` | stdio man-in-the-middle: verbatim forwarding + custom-message injection + auto-install-on-first-run (stdlib-only Python 3) |
| `dotrush-version.json` | pins the DotRush repository and the one tag or commit both the server and the profiling tools come from |
| `scripts/install-dotrush.sh` | installs the DotRush server or profiling tools at the pinned ref: the release bundles when the release ships them, otherwise a build from source (logic shared in `scripts/dotrush-install.sh`) |
| `scripts/dotrush-profile.sh` | runs DotRush's own `dotnet-trace`/`dotnet-gcdump` at the pinned ref, installed exactly as the server is, then collects and reports bounded CPU traces or GC dumps |
| `scripts/summarize-speedscope.py` | derives full-name exclusive/inclusive managed-CPU rankings from Speedscope output |
| `scripts/analyze-gcdump.py` | streams the heap graph DotRush's `dotnet-gcdump --format Json` writes: exact per-type bytes, the largest retained objects with their dominator chain, and snapshot diffs (backs `heap-report`/`heap-diff`) |
| `scripts/dotrush-diagnostics.sh` | injects `dotrush/solutionDiagnostics` into this session's server, waits for the results the proxy captures, and reports them |
| `scripts/summarize-diagnostics.py` | summarizes the proxy's `diagnostics.json`: counts by severity and code, errors first, hints hidden unless asked |
| `skills/dotrush-pick-project/` | picks the `.sln/.slnx/.csproj` DotRush loads for the session and applies it live |
| `skills/dotrush-diagnostics/` | runs whole-solution compiler analysis and reports errors and warnings |
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

Not supported: **call hierarchy** (`prepareCallHierarchy`/`incomingCalls`/`outgoingCalls`) — DotRush (as of the
pinned 2026.09) has no call-hierarchy handler. Use `findReferences` instead.

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
"$PLUGIN/scripts/dotrush-diagnostics.sh" where          # session dir, proxy, chosen project, publish count
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
- **`python3` not found when the LSP starts** → ensure `python3` is on the PATH Claude Code launches with,
  or set the `.lsp.json` `command` to your interpreter explicitly.
- Disable the proxy's logging by setting `DOTRUSH_PROXY_LOG=""`.

