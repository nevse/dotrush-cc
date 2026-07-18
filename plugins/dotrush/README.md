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

**Recommended — the `dotrush-setup` skill (interactive, no config file).** Ask Claude to *"set up the
DotRush project"* (or invoke the `dotrush-setup` skill). It finds the `.sln/.slnx/.csproj` candidates in
your workspace, **asks which one to use**, applies it live (no restart), and remembers the choice — stored
in the plugin's data dir (`target.json`) and auto-replayed at startup on future sessions. **Nothing is
written into your repo.** If you never picked one, it asks; once picked, it doesn't ask again.

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
  results = it's working. "No symbols" = no project loaded → run `dotrush-setup`.

## Capabilities (via the Claude Code `LSP` tool)

Work: `documentSymbol`, `workspaceSymbol` (needs a non-empty query), `hover`, `goToDefinition`,
`findReferences`, `goToImplementation`. Cross-project results require the containing projects to be
loaded (target a solution, not a single `.csproj`).

Not supported: **call hierarchy** (`prepareCallHierarchy`/`incomingCalls`/`outgoingCalls`) — this DotRush
build registers no call-hierarchy handler. Use `findReferences` instead.

## Injecting custom LSP messages (the proxy)

The proxy forwards Claude ⇄ DotRush verbatim and injects newline-delimited JSON-RPC written to a FIFO,
at frame boundaries under a lock — so injection never desyncs request/response pairing.

Find the FIFO + log (they live in the plugin's data dir):
```bash
find ~/.claude/plugins -name inject.fifo   # -> $DOTRUSH_INJECT_FIFO
find ~/.claude/plugins -name proxy.log     # logs INJECT events + server->client traffic
```

Inject (`jsonrpc:"2.0"` auto-added):
```bash
FIFO=$(find ~/.claude/plugins -name inject.fifo | head -1)
echo '{"method":"$/setTrace","params":{"value":"verbose"}}' > "$FIFO"
tail -f "$(find ~/.claude/plugins -name proxy.log | head -1)"
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

Switch target solution live by injecting a config change then a reload:

```bash
FIFO=$(find ~/.claude/plugins -name inject.fifo | head -1)
echo '{"method":"workspace/didChangeConfiguration","params":{"settings":{"dotrush":{"roslyn":{"projectOrSolutionFiles":["/abs/Other.sln"],"restoreProjectsBeforeLoading":false}}}}}' > "$FIFO"
echo '{"method":"dotrush/reloadWorkspace","params":{"workspaceFolders":[{"uri":"file:///abs/workspace","name":"ws"}]}}' > "$FIFO"
```

Notes (learned while verifying this):
- `didChangeConfiguration` **replaces the entire roslyn section** — include every setting you care about.
- A reload emits `dotrush/projectLoaded` per project but **not** `dotrush/loadCompleted` (that fires only
  on initial init). Wait on `projectLoaded` + memory settling.
- Inject `didChangeConfiguration` **before** `reloadWorkspace` (FIFO delivery is in order).

## Troubleshooting

- **Every query returns "No symbols found"** → no project loaded. Run the **`dotrush-setup`** skill to pick a `.sln/.slnx/.csproj` (or add `dotrush.config.json`).
- **Server didn't download** → check `curl`/`unzip` exist; run `install-dotrush.sh` manually; inspect `proxy.log`.
- **`python3` not found when the LSP starts** → ensure `python3` is on the PATH Claude Code launches with,
  or set the `.lsp.json` `command` to your interpreter explicitly.
- Disable the proxy's logging by setting `DOTRUSH_PROXY_LOG=""`.
