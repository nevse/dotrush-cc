# Architecture

One plugin, `plugins/dotrush`, with three runtime parts that share no memory and talk only through files in a
per-session dir: the proxy Claude Code starts, the C# CLI the skills run, and the DotRush server itself.

```
Claude Code ──stdio── bin/lsp-proxy.py ──stdio── dotnet DotRush.dll
                          ▲        │
              inject.fifo │        └──▶ responses/<uuid>.json, diagnostics.json, load-completed
                          │
skills ──▶ scripts/*.sh ──▶ scripts/dotrush-cli.sh ──▶ DotRushCli.dll
```

User-facing behavior is in `plugins/dotrush/README.md`; this file covers how the parts fit and what is load-bearing.

## Components

- **Manifests.** `.claude-plugin/marketplace.json` lists the plugin; `plugins/dotrush/.claude-plugin/plugin.json`
  carries the version and points at `.lsp.json` and `skills/`. `.lsp.json` starts the proxy for `.cs/.csx/.cshtml`
  with `DOTRUSH_SERVER_DIR`, `DOTRUSH_INSTALL_SCRIPT`, `DOTRUSH_DATA_DIR` (`${CLAUDE_PLUGIN_DATA}`) and
  `DOTRUSH_WORKSPACE`, and a 15-minute startup timeout because a first start may build DotRush from source.
- **Proxy** (`bin/lsp-proxy.py`, stdlib-only Python 3). One per Claude Code session. Owns the session dir, installs
  the server when the pin moved, and runs four daemon threads beside the main server→client pump: client→server
  pump, stderr pump, FIFO injector, diagnostics flusher. `_stdin_lock` serializes the three writers into DotRush's
  stdin (client pump, injector, startup replay), always at frame boundaries.
- **CLI** (`tools/DotRushCli`, `net10.0`, no packages). `session`, `request`, `rename preview|apply`.
  `Program.Run` dispatches through one usage table; every command takes a `CommandContext`.
  - `Session` finds this session's dir and checks readiness (`RequireChannel`).
  - `LspChannel` does the request channel: FIFO writes, response polling, cancel on timeout.
  - `RenameCommand` validates the name, checks each edit against the disk, and saves and executes plans.
  - `WorkspaceEditApplier` owns `SourceDocument` (Roslyn's line splitting, UTF-16 positions), diffs, and the
    all-or-nothing write.
- **Wrapper** (`scripts/dotrush-cli.sh`). Builds the CLI into `cli/<sha256 of tools/>/` under a lock, prunes builds
  unused for a day, exports `DOTRUSH_DATA_DIR`, and `exec`s the dll.
- **Installer** (`scripts/dotrush-install.sh`, sourced; `install-dotrush.sh` is the entry point). Installs the
  `server` or `diagnostics` component at the one ref in `dotrush-version.json`. Also owns `dotrush_data_dir`, the
  data-dir derivation every script uses.
- **Diagnostics** (`scripts/dotrush-diagnostics.sh`, `summarize-diagnostics.py`). Injects
  `dotrush/solutionDiagnostics` and reads the proxy's `diagnostics.json`.
- **Profiling** (`scripts/dotrush-profile.sh` and its Python reporters `summarize-speedscope.py`,
  `analyze-gcdump.py`, `list-dotnet-processes.py`). Independent of the proxy and the session: it runs DotRush's
  `dotnet-trace`/`dotnet-gcdump` from the `diagnostics` component against a user process.
- **Skills** (`skills/*/SKILL.md`). Prompts that run the scripts above; they hold no logic of their own.

## Data dir

`${CLAUDE_PLUGIN_DATA}`, which Claude Code sets for the language server but not for a skill's Bash commands, so
`dotrush_data_dir` derives the same path from the plugin's install location.

```
server/            DotRush.dll, .dotrush-ref, .dotrush-source      server.lock, server-build.log beside it
diagnostics/       dotnet-trace, dotnet-gcdump at the same ref      diagnostics.lock, diagnostics-build.log
cli/<hash>/        DotRushCli.dll                                  cli-artifacts/<hash>/, cli.lock, cli-build.log
profiles/          default profiling output
ws/<key>/          one session dir per proxy
```

The session key is `sess-` + sha1(`AGTERM_SESSION_ID:<proxy's parent pid>`)[:12]. The parent pid is the
launching Claude Code process, which the CLI sees as `CLAUDE_PID`. `DOTRUSH_SESSION_ID` is used as given. Without
either id, the key is sha1 of the workspace path: the legacy per-workspace dir, whose `target.json` survives
restarts. A proxy start prunes `sess-*` dirs whose `pid` is dead.

| Entry | Written by | Read by | Lifetime |
|---|---|---|---|
| `workspace.txt`, `pid`, `session.txt`, `claude-pid` | proxy at start | `Session.Find`, pruning | per proxy |
| `inject.fifo` | proxy creates; CLI, `dotrush-pick-project.sh`, diagnostics script write | proxy injector | reused |
| `proxy.log` | proxy | humans, skills on failure | appended |
| `target.json` | `dotrush-pick-project.sh` | proxy replays it at start | per session |
| `load-completed` | proxy on `dotrush/loadCompleted` | CLI, diagnostics script, `dotrush-pick-project.sh` | removed at proxy start |
| `diagnostics.json` | proxy, coalesced every 0.5 s | diagnostics script | reset at proxy start |
| `responses/` | proxy creates empty; `<uuid>.json` via temp + rename | CLI deletes after reading; stale after 10 min | recreated at proxy start only |
| `edits/` | `rename preview`: `<plan>.json` (sha256 and URI per file), `<plan>.diff` | `rename apply` | removed at proxy start |

## Flows

- **LSP traffic.** Frames are forwarded byte for byte. Before any client traffic the proxy replays `target.json` as
  `workspace/didChangeConfiguration`: DotRush's `initialize` waits for a configuration, so the chosen project loads
  with no `dotrush.config.json`.
- **Injection.** One JSON-RPC message per FIFO line; the injector adds `jsonrpc`, frames it and writes it under
  `_stdin_lock`. Notifications only; requests go through the channel.
- **Request channel.** The CLI picks the id `dotrush-cc:<uuid>`, so the proxy keeps no request table. The
  server→client pump substring-checks each frame for the prefix and writes a matching response to
  `responses/<uuid>.json` instead of forwarding it. The CLI's `RequireChannel` checks, in order: proxy pid alive,
  `responses/` present, `load-completed` present. It then writes the line and polls every 20 ms; on timeout it
  sends `$/cancelRequest` and drops a response that lands within a second.
- **Diagnostics.** The script records `publishes`, injects `dotrush/solutionDiagnostics`, and waits until the count
  moves and publishing has been quiet for 2 s: DotRush signals no completion, so a clean solution exits 3 on timeout.
- **Rename.** `preview` sends `textDocument/rename`, reads `WorkspaceEdit.changes`, checks that each edited
  identifier reads as the old name before and the new name after, and saves a plan with a sha256 per file.
  `apply` re-hashes every file and refuses any change. It writes each file to `*.dotrush-cc.tmp` (BOM, line endings
  and mode kept), moves them all into place, then sends one `didOpen` batch under the URIs DotRush used.
- **Install.** On start the proxy compares `server/.dotrush-ref` with the pin and runs the installer when they
  differ, capturing its output so none reaches the LSP stdout. The installer downloads release bundles when the
  ref's release ships both, otherwise builds both components from source. It stages beside the target and swaps
  whole under `<target>.lock`, so a failed install leaves the previous server running.
- **CLI build.** The wrapper hashes `tools/` (sans `bin/obj`); a new hash means a new build dir, so sessions on
  different plugin versions never rebuild over each other. The build runs from `tools/`, whose empty `Directory.*`
  files stop MSBuild importing the user's repo settings.

## Tests

| Layer | Suite |
|---|---|
| proxy (injector, channel routing), installer, diagnostics script, profiling reporters | `tests/test_profile_reports.py` |
| CLI commands in-process, wrapper, skill command lines | `tests/DotRushCli.Tests` (`FakeProxy` stands in for the proxy) |
| CLI + real proxy + real DotRush | `tests/DotRushCli.Tests/E2E`, only with `DOTRUSH_E2E=1` |

## Load-bearing fragile points

- `dotrush/reloadWorkspace` before `load-completed` races DotRush's first load: analysis never starts, or every
  diagnostic arrives twice. Only restarting Claude Code recovers.
- FIFO semantics: the injector's own held write end is what keeps lines from vanishing between writers; a line
  must fit `PIPE_BUF` (512 bytes on macOS) and go in one write; .NET cannot open a FIFO non-blocking.
- Roslyn sends minimal rename edits (`Greeter` → `Welcomer` arrives as `Greet` → `Welcom`), so range text never
  identifies a rename.
- A request-channel id becomes a file name, so the proxy accepts only a lowercase uuid suffix.
- The proxy's stdout is the LSP stream: nothing but frames may reach it.
- `dotnet-trace --duration` goes through `TimeSpan.Parse`, which reads `00:30` as 30 minutes.
- Known and accepted: lock reclaim in `dotrush_lock` is not atomic (`docs/backlog/lock-reclaim-is-not-atomic.md`).
