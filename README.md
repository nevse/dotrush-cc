# dotrush-cc

A Claude Code **marketplace** containing the `dotrush` plugin: the [DotRush](https://github.com/JaneySprings/DotRush)
Roslyn language server wired into Claude Code's `LSP` tool for C#/.NET, plus a stdio **proxy** that can
inject custom LSP messages into the running server and read the responses, and skills for semantic renames,
solution diagnostics, and CPU, allocation and managed-memory profiling.

The DotRush server is **not** committed here. The DotRush version is pinned in
[`dotrush-version.json`](plugins/dotrush/dotrush-version.json), and on first use the plugin **downloads that
release's platform-neutral bundles** (or builds them from source when the pin is a commit).

## Install

```bash
# 1. add this marketplace (GitHub shorthand, or any git URL)
claude plugin marketplace add nevse/dotrush-cc
#   e.g. claude plugin marketplace add https://github.com/nevse/dotrush-cc.git

# 2. install the plugin
claude plugin install dotrush@dotrush-cc

# 3. restart Claude Code. On first C# LSP use, the pinned DotRush server installs into the
#    plugin's data dir (${CLAUDE_PLUGIN_DATA}/server): downloaded from its release, or built
#    from source when the pin is not a release with bundles (git + .NET SDK, a few minutes).
```

Or enable it declaratively in `.claude/settings.json`:

```json
{
  "extraKnownMarketplaces": {
    "dotrush-cc": { "source": { "source": "github", "repo": "nevse/dotrush-cc" } }
  },
  "enabledPlugins": { "dotrush@dotrush-cc": true }
}
```

## Requirements

- `curl` and `unzip` to download DotRush release bundles, or `git` to build DotRush when the pin is not such a release
- `python3` (the proxy is a stdlib-only Python 3 script)
- A .NET 10 SDK on PATH (DotRush loads/analyzes MSBuild projects, runs on .NET 10+, and builds from source with it;
  the plugin also builds its small C# CLI with it on first use, used by the rename, diagnostics and project-picking
  skills)
- `shasum` or `sha256sum` (the CLI wrapper hashes its sources to pick the build directory)

## Quick start after install

1. Restart Claude Code; the C# LSP server loads on first use (installing the pinned DotRush server once).
2. Point DotRush at your project — ask Claude to **"set up the DotRush project"** (the `dotrush-pick-project`
   skill). It finds your `.sln/.slnx/.csproj`, asks which to use, applies it live, and remembers the
   choice — no `dotrush.config.json` needed, nothing written into your repo.
3. Verify via **`/plugin` → Installed → `dotrush`** (and the **Errors** tab). There is no `/lsp` command.
4. See [`plugins/dotrush/README.md`](plugins/dotrush/README.md) for capabilities, the injection FIFO,
   on-demand diagnostics, semantic rename, the request channel, **live reconfigure/reload without a restart**,
   and .NET profiling.

## Usage examples

You talk to Claude in plain language; the plugin supplies the LSP server and the skills behind it.

**Pick the project** (once per session, or when queries return "No symbols found"):

> set up the DotRush project

Claude lists the `.sln/.slnx/.csproj` files it finds, asks which one to load, and applies the choice without a restart.

**Navigate and understand code** — answered by DotRush rather than by grepping text:

> where is `OrderService.PlaceOrder` called from?
>
> find all implementations of `IPaymentGateway`
>
> what type does `CreateClient()` return in `Startup.cs`, and where is it defined?
>
> rename-safe check: list every reference to `LegacyMapper` before I delete it

Behind these are `findReferences`, `goToImplementation`, `hover`, `goToDefinition`, `documentSymbol` and
`workspaceSymbol`. Call hierarchy isn't available, so "who calls X" is answered from references.

**Rename a symbol** across the solution:

> rename `OrderService.PlaceOrder` to `SubmitOrder`

Claude locates the method, asks DotRush for the Roslyn rename, and shows you the edit count per file and the diff.
Nothing is written until you confirm, and a file edited since the preview makes it stop and preview again. Every
file is prepared before any is replaced, so a refusal changes nothing; should replacing them fail part way, it names
exactly which files changed and which did not. Same-named members of other types stay as they are. Overloads, strings, comments
and file names are not renamed (the `dotrush-rename` skill).

**Profile CPU** of a running app:

> my API is at 100% CPU under load — profile it for 30 seconds while I hit `/orders`

Claude finds the process (or asks which one), captures a `dotnet-trace`, and reports the hottest methods
with their exclusive and inclusive time, pointing out where JIT inlining moved samples into a caller.

**Find what allocates**:

> this import loop triggers a gen0 GC every few milliseconds — what is allocating?

Claude captures a trace with allocation sampling (`--profile gc-verbose`) and ranks the allocated types, the
functions that allocated them and the application callers above them, then follows the biggest row down to its
call path with `alloc-report --focus`. Every figure is an estimate from one sample per ~100 KB allocated.

**Chase a memory leak**:

> memory keeps growing in `MyApp.Worker` — take a baseline heap snapshot, I'll run the import job, then take another and compare

Claude takes two `dotnet-gcdump` snapshots, ranks the types by byte growth, and shows the retention
chain that keeps each one alive (for example `[static var App.Cache.s_items] <- [.NET Roots]`). It
asks first before attaching to a production or latency-sensitive process, because a gcdump forces a full GC.

**Run the profiling helper yourself** — the same script the skills use:

```bash
P=$(ls -d ~/.claude/plugins/cache/dotrush-cc/dotrush/*/ | sort -V | tail -1)   # installed plugin version
"$P/scripts/dotrush-profile.sh" tools                # pin, and whether the tools are installed
"$P/scripts/dotrush-profile.sh" ps trace --filter testhost  # attachable .NET processes, with command lines
"$P/scripts/dotrush-profile.sh" trace 12345 00:00:30 # always hh:mm:ss — 00:30 would mean 30 minutes
"$P/scripts/dotrush-profile.sh" trace --launch 00:02:00 -- dotnet bin/Release/net10.0/Bench.dll  # trace from startup
"$P/scripts/dotrush-profile.sh" trace 12345 00:00:30 --profile gc-verbose   # add GC and allocation events to the capture
"$P/scripts/dotrush-profile.sh" alloc-report trace.nettrace 20                    # what that capture allocated, by type and function
"$P/scripts/dotrush-profile.sh" alloc-report trace.nettrace --focus "System.String (Small)"  # who allocates one type
"$P/scripts/dotrush-profile.sh" trace-report trace.nettrace 20 --focus Sheet.GetReference  # callers and callees of one function
"$P/scripts/dotrush-profile.sh" trace-diff before.nettrace after.nettrace 30  # which functions gained or lost CPU share
"$P/scripts/dotrush-profile.sh" heap 12345           # snapshot + per-type/retention report
"$P/scripts/dotrush-profile.sh" heap-diff base.gcdump current.gcdump 30
```

**Check the whole solution for compiler errors** without building:

> what compiler errors and warnings does the solution have?

Claude runs DotRush's solution analysis in the language server and reports counts by severity, the most
frequent codes, and each error with its location (the `dotrush-diagnostics` skill).

## What's in here

```
dotrush-cc/
├── .claude-plugin/marketplace.json      # marketplace manifest
└── plugins/dotrush/
    ├── .claude-plugin/plugin.json        # plugin manifest (declares the LSP server)
    ├── .lsp.json                         # csharp LSP -> bin/lsp-proxy.py, portable ${CLAUDE_PLUGIN_*} paths
    ├── bin/lsp-proxy.py                  # stdio MITM proxy + injector + request channel + auto-install-on-first-run
    ├── dotrush-version.json              # pins the DotRush server release and diagnostics tag or commit
    ├── tools/DotRushCli/                 # C# CLI: session lookup, LSP requests, rename preview/apply
    ├── scripts/dotrush-cli.sh            # builds the CLI on first use into the plugin data dir and runs it
    ├── scripts/install-dotrush.sh        # installs the DotRush server or profiling tools at the pinned ref
    ├── scripts/dotrush-install.sh        # shared install logic: one ref, release bundles or a build from source
    ├── scripts/dotrush-profile.sh        # collects/reports bounded CPU and allocation traces and GC dumps
    ├── scripts/summarize-speedscope.py   # produces agent-readable managed-CPU and allocation rankings
    ├── scripts/analyze-gcdump.py         # streams gcdump JSON: per-type bytes, retention chains, snapshot diffs
    ├── scripts/dotrush-diagnostics.sh    # runs solution analysis in the session's server and reports the results
    ├── scripts/summarize-diagnostics.py  # summarizes the diagnostics the proxy captures
    ├── scripts/list-dotnet-processes.py  # lists attachable .NET processes (backs dotrush-profile.sh ps)
    ├── scripts/dotrush-pick-project.sh   # saves the session's project choice and applies it to a running server
    ├── skills/dotrush-pick-project/      # picks the .sln/.slnx/.csproj DotRush loads, applied live
    ├── skills/dotrush-diagnostics/       # whole-solution compiler errors and warnings
    ├── skills/dotrush-rename/            # semantic rename: preview, confirm, apply (checks every file first)
    ├── skills/dotrush-profile-cpu/       # CPU, hot-path, and latency profiling workflow
    ├── skills/dotrush-profile-allocations/ # allocation sampling: what allocates, by type and call path
    ├── skills/dotrush-profile-memory/    # managed-heap snapshot and comparison workflow
    ├── README.md                         # plugin usage
    └── CHANGELOG.md                      # release notes
tests/test_profile_reports.py             # unit tests for the report tools, installer and proxy (python3 -m unittest)
tests/DotRushCli.Tests/                   # xUnit tests for the CLI and its wrapper (dotnet test); E2E/ runs against a real DotRush
```
