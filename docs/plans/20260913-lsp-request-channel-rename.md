# LSP request channel and semantic rename

## Overview
- Add a request channel to the DotRush proxy so plugin tooling can send LSP **requests** to the session's
  DotRush server and read the responses itself, without the responses reaching Claude Code. Today the FIFO
  injector only suits notifications: an injected request's response flows back to Claude Code.
- Build a C# CLI (`DotRushCli`) on top of it with `session`, `request`, `rename preview` and `rename apply`.
- Ship a `dotrush-rename` skill: semantic, solution-wide renames through Roslyn with a diff preview and an
  all-or-nothing apply after the user confirms. Roslyn resolves symbols, so it does not hit same-named
  identifiers the way a text search-and-replace does.
- Replace the session-directory **lookup** duplicated across `dotrush-diagnostics.sh` and the
  `dotrush-pick-project` skill with one `session` command. Readiness checks stay per caller, so those skills keep
  their current behaviour and messages, including against 0.6.x proxies.
- Releases as plugin 0.7.0. Code actions / quick fixes are the next version on the same channel (out of scope).

## Context (from discovery)
- Files/components involved:
  - `plugins/dotrush/bin/lsp-proxy.py` — stdio MITM (Python, stdlib). `pump_server_to_client` forwards server
    frames, records `publishDiagnostics` (`DiagnosticsStore` → `$WSDIR/diagnostics.json`) and writes
    `$WSDIR/load-completed` on `dotrush/loadCompleted`; `ensure_workspace_dir` prepares `$WSDIR` at startup;
    `injector` forwards FIFO lines to the server.
  - `plugins/dotrush/scripts/dotrush-diagnostics.sh` (`session_dir`, `require_live_proxy`, `where`, `report`),
    `plugins/dotrush/skills/dotrush-diagnostics/SKILL.md` (documents `where` output and messages),
    `plugins/dotrush/skills/dotrush-pick-project/SKILL.md` (bash lookup snippet).
  - `plugins/dotrush/scripts/dotrush-install.sh` — `dotrush_data_dir` (derives the data dir from the plugin's
    install path because Claude Code does not set `CLAUDE_PLUGIN_DATA` for a skill's Bash), `dotrush_dotnet`
    (honours `DOTRUSH_DOTNET`), `dotrush_fail`, lock via `mkdir "$target.lock"` with PID takeover, staging dir +
    `mv` swap.
  - `tests/test_profile_reports.py` — 40 Python unittest tests; `DiagnosticsTests` (session fixture, `run_driver`)
    covers `dotrush-diagnostics.sh`; `test_skills_find_the_servers_data_dir_without_claude_plugin_data` shows how
    to test from a copied plugin tree.
- Related patterns found:
  - Per-session runtime dir `${DATA}/ws/sess-<sha1(session id)[:12]>/` with `session.txt`, `workspace.txt`,
    `pid`, `inject.fifo`, `proxy.log`, `target.json`, `diagnostics.json`, `load-completed`; a workspace-hash dir
    (no `sess-` prefix) when the proxy has no session id.
  - Components installed on first use under the plugin data dir with a lock, a staging dir swapped in whole,
    and a marker deciding whether to reinstall.
  - Error messages name the next action ("run any C# LSP operation first", "restart Claude Code").
- Dependencies identified:
  - .NET 10 SDK (already required). Measured locally: a package-free `net10.0` console project builds in
    ~0.6 s warm; `dotnet X.dll` starts in ~0.03 s. The `dotnet` host resolves `global.json` from the current
    directory, and MSBuild imports `Directory.Build.*` / `Directory.Packages.props` from parent directories.
  - DotRush 2026.09 (source read):
    - `textDocument/rename` returns `WorkspaceEdit { changes: uri → TextEdit[] }`; `prepareRename` unsupported;
      options exclude overloads, strings, comments and file renames; null/empty result when no symbol is at the
      position; positions are UTF-16 over Roslyn `SourceText` lines.
    - The server never sends `workspace/applyEdit`. Its LSP framework types ids as `StringOrInt`.
    - `textDocument/didOpen` re-reads the document **from disk** (`UpdateDocument(path)` ignores the sent text);
      `didChange` takes the client's full text. There is no `workspace/didChangeWatchedFiles`; DotRush's own
      `FileSystemWatcher` watches `FileName | DirectoryName | Size`, so an in-place edit that keeps the file size
      goes unnoticed.

## Development Approach
- **testing approach**: TDD — write the failing tests for each task first, then the code that makes them pass
- complete each task fully before moving to the next
- make small, focused changes
- **CRITICAL: every task MUST include new/updated tests** for code changes in that task
  - tests are not optional - they are a required part of the checklist
  - write unit tests for new functions/methods
  - write unit tests for modified functions/methods
  - add new test cases for new code paths
  - update existing test cases if behavior changes
  - tests cover both success and error scenarios
- **CRITICAL: all tests must pass before starting next task** - no exceptions
- **CRITICAL: update this plan file when scope changes during implementation**
- run tests after each change
- maintain backward compatibility: notifications injected through the FIFO, `dotrush-diagnostics` (including
  against 0.6.x proxies) and `dotrush-pick-project` keep working with their current messages
- language split: new code and its tests are C# (CLI, the bash wrapper's tests, e2e). Python changes are limited
  to the proxy, its tests, and adapting the existing `DiagnosticsTests` fixture to the CLI lookup

## Testing Strategy
- **unit tests (Python)**: `python3 -m unittest tests.test_profile_reports` — proxy routing, plus the existing
  diagnostics/installer/profiling tests, which stay green.
- **unit tests (C#)**: `dotnet test tests/DotRushCli.Tests` — xUnit v3. The CLI's commands are tested in-process
  through `Program.Run(args, env, cwd, stdout, stderr)`, never through process-wide environment variables or
  `Console`, so test classes can run in parallel. The bash wrapper is tested by running `bash` from xUnit against
  a copied plugin tree with a stub `dotnet` (`DOTRUSH_DOTNET`), plus one test with the real SDK.
- **e2e tests**: no UI. Real-server checks are in the same project under `[Trait("Category", "E2E")]` and skip
  unless `DOTRUSH_E2E=1`: `DOTRUSH_E2E=1 dotnet test tests/DotRushCli.Tests --filter Category=E2E`. The fixture
  starts the proxy from the checkout (`DOTRUSH_REAL_BIN` → installed DotRush server, temp `DOTRUSH_DATA_DIR`,
  `DOTRUSH_SESSION_ID`, pre-written `target.json`) in a temp demo project, acts as a minimal LSP client that
  `didOpen`s the files it renames (as Claude Code does), and waits for `load-completed`. At least one e2e call
  goes through the real `dotrush-cli.sh` with a real build.

## Progress Tracking
- mark completed items with `[x]` immediately when done
- add newly discovered tasks with ➕ prefix
- document issues/blockers with ⚠️ prefix
- update plan if implementation deviates from original scope
- keep plan in sync with actual work done

## Solution Overview
- **Proxy (Python, minimal):** a response whose string id starts with `dotrush-cc:` is written to
  `$WSDIR/responses/<uuid>.json` instead of being forwarded. The caller picks the id, so the proxy keeps no request
  table, and Claude Code's integer ids never match. `responses/` is created fresh at proxy start and is the
  capability marker for the request channel; `edits/` (saved rename plans) is cleared at start.
- **CLI (C#):** `plugins/dotrush/tools/DotRushCli/`, `net10.0`, no package references, `System.Text.Json`.
  `scripts/dotrush-cli.sh` builds it on first use into `$(dotrush_data_dir)/cli/<source-hash>/` — a folder per
  source hash, so sessions on different plugin versions never rebuild over each other — then runs
  `dotnet DotRushCli.dll`.
- **Lookup vs readiness:** `session` / `session --dir` only find the session dir and print its state (exit 1 only
  when there is no dir). `request` and `rename` require a live proxy, `responses/` and `load-completed`.
  `dotrush-diagnostics.sh` uses the CLI for lookup and `where`, and keeps its own readiness checks and messages.
- **Rename:** `preview` asks DotRush for the edit, checks that every edited range on disk still holds the old
  name, saves a plan with a sha256 per touched file and the full diff, and prints a per-file summary with the start
  of the diff. `apply` re-checks every hash, writes all files (keeping BOM, line endings and file mode), then
  sends `textDocument/didOpen` for each changed file so DotRush re-reads it from disk.
- **Skill:** Claude finds the symbol position with the LSP tool, previews, shows the summary and diff, applies
  only after the user confirms, then re-reads the changed files.
- Order change from the brainstorm: the proxy routing comes first, because the request channel's readiness check
  needs `responses/`.

## Technical Details
- **Request line** written to `inject.fifo` (unchanged injector), as a single `write` of the whole line including
  `\n`: `{"jsonrpc":"2.0","id":"dotrush-cc:<uuid>","method":"…","params":{…}}`. `<uuid>` is a lowercase GUID
  matching `^[0-9a-f-]{36}$`; the proxy drops and logs any `dotrush-cc:` response whose suffix does not match, so
  an id never becomes a path. Lines longer than `PIPE_BUF` (512 bytes on macOS) can interleave with a concurrent
  writer; the channel's own requests stay short.
- **Proxy routing:** only frames without `method` whose body contains `"dotrush-cc:` are parsed; the rest are
  forwarded untouched. A parsed frame is routed only if `id` is a string with the prefix; unparseable frames and
  other ids are forwarded verbatim.
- **Response file** `$WSDIR/responses/<uuid>.json`: the JSON-RPC response body verbatim, written to
  `<uuid>.json.<pid>.tmp` then `os.replace`d.
- **Cancel** on timeout: `{"jsonrpc":"2.0","method":"$/cancelRequest","params":{"id":"dotrush-cc:<uuid>"}}`, then
  wait up to 1 s and delete the response file if it appears. On start, `request`/`rename` delete
  `responses/*.json` older than 10 minutes.
- **Data dir:** the wrapper passes `DOTRUSH_DATA_DIR="$(dotrush_data_dir)"`; the CLI never derives it. No path in
  the wrapper uses `${CLAUDE_PLUGIN_DATA}` directly.
- **Session lookup (C#):** session id = `DOTRUSH_SESSION_ID` or `AGTERM_SESSION_ID`.
  1. With a session id: a `ws/sess-*/session.txt` whose line equals it.
  2. Otherwise, or when nothing matched: a `ws/*/workspace.txt` equal to `CLAUDE_PROJECT_DIR` or the cwd, among
     dirs **without** the `sess-` prefix only, so another session's dir is never picked.
  3. Several matches: prefer one whose `pid` is alive, then the newest `pid` file.
  This narrows today's `dotrush-diagnostics.sh` fallback, which could match any session's `workspace.txt`
  (changelog note).
- **`session` output:** `dir`, `workspace`, `proxy: running (pid N)` / `proxy: not running`, `load: completed` /
  `load: not completed (no project loaded yet, or still loading)`, `target`, `publishes` — the exact lines
  `dotrush-diagnostics.sh where` prints today, plus `channel: available` / `channel: unavailable (older proxy)`.
  `session --dir` prints only the directory. Both exit 0 whenever a dir is found.
- **Channel readiness** (`request`, `rename`), in order: dir found; `pid` alive ("the DotRush language server for
  this session is not running; run any C# LSP operation to start it"); `responses/` exists ("the running DotRush
  proxy predates the request channel; restart Claude Code"); `load-completed` exists ("DotRush has not finished
  loading a project in this session; choose one with dotrush-pick-project, or wait for the load to finish and
  retry").
- **FIFO write:** .NET cannot open a FIFO with `O_NONBLOCK`, and opening for write blocks until a reader exists.
  The proxy's injector holds it open while alive, so check the PID first, then open and write on a background task
  with a 2 s timeout that fails with "cannot write to the proxy's FIFO".
- **Exit codes:** 0 success; 1 error (including a JSON-RPC error response, printed as `code: message`); 2 usage;
  3 timeout (matches `dotrush-diagnostics.sh`).
- **CLI entry point:** `static int Run(string[] args, IReadOnlyDictionary<string, string?> env, string cwd,
  TextWriter stdout, TextWriter stderr)`; `Main` passes the real process values.
- **Paths:** URIs → paths with `new Uri(uri).LocalPath` (decodes `%20`, non-ASCII). Every comparison (workspace
  guard, plan keys, hash lookups) uses fully resolved real paths on both sides, so macOS `/var` vs `/private/var`
  and symlinked checkouts compare equal.
- **Plan files:** `$WSDIR/edits/<plan-id>.json` =
  `{"workspace": "...", "oldName": "...", "newName": "...", "changes": {realPath: [TextEdit]}, "files": {realPath: sha256hex}}`
  and `$WSDIR/edits/<plan-id>.diff` (full unified diff). `plan-id` is 12 lowercase hex chars, validated with
  `^[0-9a-f]{12}$` before any path is built.
- **Text handling:** read bytes; detect and keep a UTF-8 BOM; decode strictly as UTF-8 and refuse a file that is
  not valid UTF-8. Line breaks for position mapping are the ones Roslyn `SourceText` uses: `\r\n`, `\n`, `\r`,
  U+0085, U+2028, U+2029. Edits per file sorted by start descending; overlapping edits rejected.
- **Preview consistency check:** the requested position must land on an identifier character (clear error
  otherwise); that identifier is `oldName`. Every returned range's text on disk must equal `oldName`, `@oldName`,
  or `oldName` without an `Attribute` suffix / `oldNameAttribute`; otherwise refuse with "DotRush's view of
  <file> differs from disk; preview again after the file is saved".
- **Writing:** symlinked files are resolved to their target and the target is written. For each file write
  `<target>.dotrush-cc.tmp` with the original Unix file mode (`File.GetUnixFileMode` / `SetUnixFileMode`); if any
  temp write fails, delete all temps and change nothing. Then `File.Move(tmp, target, overwrite: true)` per file;
  if a move fails, stop and report which files were changed and which were not (exit 1).
- **After apply:** send one `textDocument/didOpen` notification per changed file
  (`{"textDocument":{"uri":…,"languageId":"csharp","version":0,"text":""}}`); DotRush reloads the document from
  disk. Needed because DotRush's watcher misses same-size edits; no workspace reload.
- **Workspace guard:** files outside the session's resolved workspace root, or with a `bin` or `obj` path segment,
  are marked `(outside workspace)` in the preview and refused by `apply` unless `--outside-workspace`.
- **Identifier validation:** `^@?[\p{L}\p{Nl}_][\p{L}\p{Nl}\p{Nd}\p{Mn}\p{Mc}\p{Pc}\p{Cf}]*$`; a reserved C# keyword
  is valid only with `@`; contextual keywords (`var`, `record`, `async`) are valid as they are.
- **Preview output:** `N edits in M files`, one line per file (`path: K edits`, `(outside workspace)` where it
  applies), the first 200 diff lines, `diff: <path to .diff>`, `plan: <plan-id>`. Hunks are built from the known
  edits (changed lines ± 3 context lines, overlapping hunks merged), headers `--- a/<rel>` / `+++ b/<rel>`.
- **Build wrapper `scripts/dotrush-cli.sh`:**
  - `tools="$plugin/tools"`, `data="$(dotrush_data_dir)"`.
  - hash = `shasum -a 256` over the sorted file list and contents of `tools/` excluding any `bin/` or `obj/`
    directory; build dir `"$data/cli/$hash"`.
  - if `DOTRUSH_CLI_DIR` is set, use it and skip building.
  - if `"$data/cli/$hash/DotRushCli.dll"` is missing: under `"$data/cli.lock"` (PID takeover, as the installer),
    re-check, then in a subshell `cd "$tools"` run `dotnet build DotRushCli/DotRushCli.csproj -c Release --nologo
    --artifacts-path "$data/cli-artifacts/$hash" -o "$data/cli/$hash.new.$$"` logging to `"$data/cli-build.log"`;
    on success `mv` the staging dir into place; on failure print the log tail and exit 1; then remove other hash
    dirs under `cli/` not modified in the last 24 hours.
  - `exec dotnet "$data/cli/$hash/DotRushCli.dll" "$@"` with `DOTRUSH_DATA_DIR` exported.
  - `plugins/dotrush/tools/` holds empty `Directory.Build.props`, `Directory.Build.targets` and
    `Directory.Packages.props` so MSBuild imports nothing from the user's parent directories, and building from
    `tools/` avoids the user repo's `global.json`.

## What Goes Where
- **Implementation Steps** (`[ ]` checkboxes): tasks achievable within this codebase - code changes, tests, documentation updates
- **Post-Completion** (no checkboxes): items requiring external action - manual testing, changes in consuming projects, deployment configs, third-party verifications

## Implementation Steps

### Task 1: Route `dotrush-cc:` responses to files in the proxy

**Files:**
- Modify: `plugins/dotrush/bin/lsp-proxy.py`
- Modify: `tests/test_profile_reports.py`

- [x] write failing tests: a `dotrush-cc:<uuid>` response is absent from the forwarded stdout and present in `responses/<uuid>.json`; an integer-id response, an unprefixed string id, an integer-id response whose result text contains `dotrush-cc:`, and an unparseable frame containing `dotrush-cc:` are all forwarded verbatim
- [x] write failing tests: a `dotrush-cc:` id with a non-uuid suffix (`../x`, empty) is neither forwarded nor written
- [x] write failing test: workspace dir setup creates an empty `responses/` (removing stale files) and removes `edits/`
- [x] implement routing in `pump_server_to_client` with an atomic write helper
- [x] implement `responses/` / `edits/` preparation in `ensure_workspace_dir`
- [x] run tests - `python3 -m unittest tests.test_profile_reports` must pass before task 2

### Task 2: Create the CLI project, test project and build wrapper

**Files:**
- Create: `plugins/dotrush/tools/Directory.Build.props`
- Create: `plugins/dotrush/tools/Directory.Build.targets`
- Create: `plugins/dotrush/tools/Directory.Packages.props`
- Create: `plugins/dotrush/tools/DotRushCli/DotRushCli.csproj`
- Create: `plugins/dotrush/tools/DotRushCli/Program.cs`
- Create: `plugins/dotrush/scripts/dotrush-cli.sh`
- Create: `tests/DotRushCli.Tests/DotRushCli.Tests.csproj`
- Create: `tests/DotRushCli.Tests/ProgramTests.cs`
- Create: `tests/DotRushCli.Tests/WrapperTests.cs`
- Modify: `.gitignore`

- [x] write failing xUnit tests through `Program.Run`: no arguments and an unknown command print usage to stderr and return 2
- [x] write failing `WrapperTests` (copied plugin tree under a fake `.claude/plugins/cache/dotrush-cc/dotrush/0.7.0`, stub `dotnet` script via `DOTRUSH_DOTNET` that records calls and fakes `build`): first run builds into `<derived data dir>/cli/<hash>/` **without `CLAUDE_PLUGIN_DATA` set**; a second run does not build; changing a copied source builds a new hash dir; a failing build prints the log tail, exits 1 and leaves no hash dir; `DOTRUSH_CLI_DIR` skips building; the build runs with cwd `tools/`
- [x] write failing real-SDK wrapper test: run from a directory whose `global.json` pins a missing SDK; the build succeeds and leaves no `bin/` or `obj/` inside the copied `tools/` tree
- [x] create the empty `Directory.*` files, `DotRushCli.csproj` (`net10.0`, Nullable, ImplicitUsings, InvariantGlobalization, no packages) and `Program.cs` with `Run(...)`, command dispatch, usage text and exit codes
- [x] create `dotrush-cli.sh` per Technical Details, reusing `dotrush-install.sh` helpers
- [x] create the xUnit v3 test project referencing `DotRushCli.csproj`; add `.gitignore` entries scoped to `plugins/dotrush/tools/**/bin/`, `plugins/dotrush/tools/**/obj/`, `tests/DotRushCli.Tests/bin/`, `tests/DotRushCli.Tests/obj/`
- [x] run tests - `dotnet test tests/DotRushCli.Tests` and the Python suite must pass before task 3

### Task 3: Implement session lookup and the `session` command

**Files:**
- Create: `plugins/dotrush/tools/DotRushCli/Session.cs`
- Modify: `plugins/dotrush/tools/DotRushCli/Program.cs`
- Create: `tests/DotRushCli.Tests/SessionTests.cs`

- [x] write failing lookup tests (temp data dirs): match by session id; without a session id match `workspace.txt` in a non-`sess-` dir; a session id that matches nothing never picks another session's `sess-*` dir; with several matches the live-pid dir wins
- [x] write failing tests that lookup succeeds in unready states: `session --dir` returns the dir and exit 0 with a dead pid, without `responses/`, and without `load-completed`; no dir → "no DotRush language server has started in this session (looked in …); run any C# LSP operation first", exit 1
- [x] write failing test for `session` output: the six `where` lines in today's wording plus the `channel:` line, for running/not running, completed/not completed, target/none, capture/no capture
- [x] write failing tests for channel readiness (used later by `request`/`rename`): dead pid, missing `responses/`, missing `load-completed` — each message and exit 1
- [x] implement `Session.cs` (lookup, state, channel readiness) and the `session` / `session --dir` commands
- [x] run tests - C# and Python suites must pass before task 4

### Task 4: Switch `dotrush-diagnostics` and `dotrush-pick-project` to the CLI lookup

**Files:**
- Modify: `plugins/dotrush/scripts/dotrush-diagnostics.sh`
- Modify: `plugins/dotrush/skills/dotrush-diagnostics/SKILL.md`
- Modify: `plugins/dotrush/skills/dotrush-pick-project/SKILL.md`
- Modify: `tests/test_profile_reports.py`

- [x] adapt the `DiagnosticsTests` fixture first: build the CLI once per class (`dotnet build … -o <tmp>`) and pass `DOTRUSH_CLI_DIR`; keep every assertion. Expected output changes: none for `test_solution_injects_the_request_and_reports_what_the_server_publishes_next`, `test_solution_refuses_a_session_whose_proxy_is_gone_or_predates_capture`, `test_solution_refuses_to_wait_on_a_server_that_never_completed_a_load` and `test_without_a_session_dir_it_says_to_start_the_language_server`; `where` gains a `channel:` line
- [x] write failing tests: `report` works with a dead proxy; `solution` works against a session dir without `responses/` (0.6.x proxy); lookup ignores another session's `sess-*` dir with the same workspace
- [x] replace `session_dir` in `dotrush-diagnostics.sh` with `dotrush-cli.sh session --dir`, and `where` with `dotrush-cli.sh session`; keep `require_live_proxy` (pid, `diagnostics.json`, `load-completed`) and its messages unchanged
- [x] replace the bash lookup snippet in the pick-project skill with `WSDIR=$("${CLAUDE_PLUGIN_ROOT}/scripts/dotrush-cli.sh" session --dir)`, keeping the "start the C# LSP first" guidance for exit 1
- [x] update the diagnostics skill's `where` description for the `channel:` line
- [x] run tests - C# and Python suites must pass before task 5

### Task 5: Implement the request channel and the `request` command

**Files:**
- Create: `plugins/dotrush/tools/DotRushCli/LspChannel.cs`
- Modify: `plugins/dotrush/tools/DotRushCli/Program.cs`
- Create: `tests/DotRushCli.Tests/FakeProxy.cs`
- Create: `tests/DotRushCli.Tests/LspChannelTests.cs`

- [x] write `FakeProxy` test helper: temp session dir, `mkfifo`, a reader task that records lines and answers by writing `responses/<uuid>.json`; cleanup opens the FIFO for reading to release any writer stuck in `open`
- [x] write failing tests: result JSON printed and response file deleted; JSON-RPC error → `code: message`, exit 1; no response before `--timeout` → a `$/cancelRequest` line with the same id reaches the FIFO and a late response file is deleted, exit 3; invalid `<params-json>` → exit 2 with nothing written; stale `responses/*.json` older than 10 minutes removed on start
- [x] write failing test: a FIFO without a reader fails within the open timeout instead of hanging
- [x] implement `LspChannel` (id generation, single-write request line, background FIFO write with timeout, response polling, cancel and cleanup)
- [x] implement `request <method> <params-json> [--timeout N]` (default 60 s) after channel readiness
- [x] run tests - C# and Python suites must pass before task 6

### Task 6: Add the e2e fixture and the request-channel checks

**Files:**
- Create: `tests/DotRushCli.Tests/E2E/DotRushServerFixture.cs`
- Create: `tests/DotRushCli.Tests/E2E/RequestChannelE2ETests.cs`
- Create: `tests/DotRushCli.Tests/E2E/README.md`

- [x] write the fixture: skip unless `DOTRUSH_E2E=1`; create a demo project (class `Greeter` in `Greeter.cs`, used from `App.cs`) in a temp dir; start `lsp-proxy.py` with `DOTRUSH_REAL_BIN`, temp `DOTRUSH_DATA_DIR`, `DOTRUSH_SESSION_ID` and `target.json`; act as a minimal LSP client (initialize, initialized, answer server requests with `null`, record every frame); `didOpen` both source files; wait for `load-completed`
- [x] write e2e test: `request textDocument/hover` on `Greeter` returns a result mentioning it, and the fixture's client received no frame with a `dotrush-cc:` id
- [x] write e2e test through the real `dotrush-cli.sh` (real build into a temp data dir): `session` reports `proxy: running`, `load: completed`, `channel: available`
- [x] document how to run the e2e tests and their prerequisites (installed DotRush server, .NET 10 SDK) in `E2E/README.md`
- [x] run `DOTRUSH_E2E=1 dotnet test tests/DotRushCli.Tests --filter Category=E2E` and the unit suites - must pass before task 7

### Task 7: Implement the workspace edit applier

**Files:**
- Create: `plugins/dotrush/tools/DotRushCli/WorkspaceEditApplier.cs`
- Create: `tests/DotRushCli.Tests/WorkspaceEditApplierTests.cs`

- [x] write failing tests for text application: several edits on one line; edits on different lines applied last-to-first; CRLF and LF files keep their endings; U+2028 counts as a line break; a position after a surrogate pair lands correctly; overlapping edits rejected
- [x] write failing tests for file handling: UTF-8 BOM preserved; invalid UTF-8 refused without writing; executable bit preserved; a symlinked file writes its target and keeps the link; a failure writing one temp file leaves every file untouched; a failing move reports changed and unchanged files
- [x] write failing tests for plans: save/load round trip; apply refuses when any file's sha256 changed and writes nothing; invalid plan id (`../x`) and unknown plan id → "preview again"; plan and diff deleted after a successful apply
- [x] write failing tests for paths and the guard: `file://` URI with a space and non-ASCII chars; a workspace reached through a symlink compares equal to its real path; files outside the root or under `obj`/`bin` refused without `--outside-workspace`, accepted with it
- [x] write failing tests for the diff: hunks with three context lines, merged overlapping hunks, relative paths in headers
- [x] implement `WorkspaceEditApplier` (decoding, position mapping, plan model, hash check, temp-then-move writes with mode and symlink handling, guard, diff)
- [x] run tests - C# and Python suites must pass before task 8

### Task 8: Implement `rename preview`

**Files:**
- Create: `plugins/dotrush/tools/DotRushCli/RenameCommand.cs`
- Modify: `plugins/dotrush/tools/DotRushCli/Program.cs`
- Create: `tests/DotRushCli.Tests/RenameCommandTests.cs`

- [ ] write failing tests for identifier validation: `Welcomer`, unicode letters, `@class`, `var`, `record` accepted; `class`, `1abc`, `a-b`, empty rejected with exit 2 and no request sent
- [ ] write failing tests with `FakeProxy`: 1-based line/column become a 0-based LSP position; a position not on an identifier → exit 1 before any request
- [ ] write failing tests: a response with edits saves the plan and `.diff`, prints the summary, per-file lines, at most 200 diff lines, `diff:` and `plan:`; null or empty response → exit 1 "no symbol at this position; check it with documentSymbol"; a range whose disk text is not the old name → exit 1 "differs from disk"; outside-workspace files marked
- [ ] implement `rename preview <file> <line> <column> <NewName>` using `LspChannel` and `WorkspaceEditApplier`
- [ ] run tests - C# and Python suites must pass before task 9

### Task 9: Implement `rename apply`

**Files:**
- Modify: `plugins/dotrush/tools/DotRushCli/RenameCommand.cs`
- Modify: `plugins/dotrush/tools/DotRushCli/Program.cs`
- Modify: `tests/DotRushCli.Tests/RenameCommandTests.cs`

- [ ] write failing tests: apply prints the changed files, exits 0 and sends one `textDocument/didOpen` per changed file to the FIFO; a file changed since preview → exit 1 "changed since preview; run rename preview again", no writes and no `didOpen`; `--outside-workspace` required for marked files
- [ ] implement `rename apply <plan-id> [--outside-workspace]`
- [ ] run tests - C# and Python suites must pass before task 10

### Task 10: Add the e2e rename checks

**Files:**
- Create: `tests/DotRushCli.Tests/E2E/RenameE2ETests.cs`
- Modify: `tests/DotRushCli.Tests/E2E/DotRushServerFixture.cs`

- [ ] write e2e test: preview renaming `Greeter` → `Welcomer` lists edits in both files; apply rewrites both on disk
- [ ] write e2e test: after apply, `request textDocument/hover` at the class shows the new name — once for a longer name (`Welcomer`) and once for a same-length name (`Welcome`), which DotRush's size-based watcher alone would miss
- [ ] write e2e test: modify one file between preview and apply → apply refuses and both files are unchanged
- [ ] run the e2e and unit suites - must pass before task 11

### Task 11: Add the `dotrush-rename` skill

**Files:**
- Create: `plugins/dotrush/skills/dotrush-rename/SKILL.md`
- Create: `tests/DotRushCli.Tests/SkillCommandTests.cs`

- [ ] write failing `SkillCommandTests`: extract every `dotrush-cli.sh <subcommand> …` line from all `plugins/dotrush/skills/*/SKILL.md` files and assert each subcommand and flag exists in `Program`'s usage table
- [ ] write the skill: when to use (rename a C# symbol across the solution); locate the position with LSP `documentSymbol`/`workspaceSymbol`; run `rename preview`; show the user the summary and diff (point to the `.diff` file when truncated); run `rename apply` only after confirmation; re-read changed files before further edits
- [ ] document each error and what to tell the user: no session, older proxy, load not completed, position not on an identifier, no symbol, differs from disk, changed since preview, outside workspace, timeout
- [ ] document limits: no overloads, strings, comments or file renames (report when a class lives in a same-named file); only documents in loaded projects
- [ ] run tests - C# and Python suites must pass before task 12

### Task 12: Verify acceptance criteria
- [ ] verify all requirements from Overview are implemented (channel, CLI commands, rename skill, shared lookup)
- [ ] verify edge cases are handled (0.6.x proxy for diagnostics, older proxy for the channel, load not completed, timeout + cancel, differs from disk, hash mismatch, outside workspace, BOM/CRLF/UTF-16/file mode, foreign `global.json`, missing `CLAUDE_PLUGIN_DATA`)
- [ ] run full test suite: `python3 -m unittest tests.test_profile_reports` and `dotnet test tests/DotRushCli.Tests`
- [ ] run e2e tests: `DOTRUSH_E2E=1 dotnet test tests/DotRushCli.Tests --filter Category=E2E`
- [ ] verify every new C# class and every CLI error path has at least one test

### Task 13: [Final] Update documentation
- [ ] update `plugins/dotrush/README.md`: Contents table (CLI, wrapper, rename skill); a "Semantic rename" section; the request channel under "Injecting custom LSP messages", replacing the warning that injected requests' responses go to Claude Code; Requirements note that the SDK also builds the CLI on first use
- [ ] update root `README.md`: a rename usage example, the file tree, and the Requirements note
- [ ] add a 0.7.0 entry to `plugins/dotrush/CHANGELOG.md` (including the narrowed session-lookup fallback) and set `version` to 0.7.0 in `plugins/dotrush/.claude-plugin/plugin.json`; mention rename in the `plugin.json` and `.claude-plugin/marketplace.json` descriptions and keywords
- [ ] update CLAUDE.md if new patterns discovered (the repo has none today; create only if there is something non-obvious to record)
- [ ] move this plan to `docs/plans/completed/`

## Post-Completion
*Items requiring manual intervention or external systems - no checkboxes, informational only*

**Manual verification**:
- after `claude plugin marketplace update dotrush-cc` and `claude plugin update dotrush@dotrush-cc`, restart Claude Code and rename a symbol in a real solution through the skill; confirm the first use builds the CLI and later uses do not
- confirm Claude Code's own view stays consistent after an apply: its `new-diagnostics` and LSP results reflect the new name, and editing a renamed file prompts a re-read rather than overwriting it
- run `dotrush-diagnostics` in a repo whose `global.json` pins an older SDK, to confirm the CLI build is unaffected

**External system updates**:
- none; the plugin is distributed through this repository's marketplace
