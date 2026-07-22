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
| `scripts/install-dotrush.sh` | downloads the DotRush server bundle for this OS/arch into `${CLAUDE_PLUGIN_DATA}/server` |

## The server auto-installs

On first C# LSP use, the proxy checks `${CLAUDE_PLUGIN_DATA}/server/DotRush`. If missing, it runs
`install-dotrush.sh`, which downloads `DotRush.Bundle.Server_<os>-<arch>.zip` from the official
GitHub release and extracts it there. One-time, ~48 MB. Supported: `darwin`/`linux`/`win32` × `arm64`/`x64`.

Manual / re-install (e.g. to pin a version):
```bash
DOTRUSH_RELEASE=2026.07 bash "$PLUGIN/scripts/install-dotrush.sh" "$DATA/server" --force
```
Requires `curl` + `unzip`. Override the release with `DOTRUSH_RELEASE`, or point at an existing
server binary with `DOTRUSH_REAL_BIN` (env, set in your Claude settings or `.lsp.json`).

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
- **Server didn't download** → check `curl`/`unzip` exist; run `install-dotrush.sh` manually; inspect `proxy.log`.
- **`python3` not found when the LSP starts** → ensure `python3` is on the PATH Claude Code launches with,
  or set the `.lsp.json` `command` to your interpreter explicitly.
- Disable the proxy's logging by setting `DOTRUSH_PROXY_LOG=""`.

## Changelog

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
