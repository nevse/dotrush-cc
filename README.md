# dotrush-cc

A Claude Code **marketplace** containing the `dotrush` plugin: the [DotRush](https://github.com/JaneySprings/DotRush)
Roslyn language server wired into Claude Code's `LSP` tool for C#/.NET, plus a stdio **proxy** that can
inject custom LSP messages into the running server, and skills for CPU and managed-memory profiling.

The DotRush server is **not** committed here (it's ~118 MB and platform-specific). Instead the plugin
**auto-downloads the official release bundle** for your OS/arch on first use.

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
- A .NET 10 SDK on PATH (DotRush loads/analyzes MSBuild projects, runs on .NET 10+, and builds from source with it)

## Quick start after install

1. Restart Claude Code; the C# LSP server loads on first use (installing the pinned DotRush server once).
2. Point DotRush at your project — ask Claude to **"set up the DotRush project"** (the `dotrush-pick-project`
   skill). It finds your `.sln/.slnx/.csproj`, asks which to use, applies it live, and remembers the
   choice — no `dotrush.config.json` needed, nothing written into your repo.
3. Verify via **`/plugin` → Installed → `dotrush`** (and the **Errors** tab). There is no `/lsp` command.
4. See [`plugins/dotrush/README.md`](plugins/dotrush/README.md) for capabilities, the injection FIFO,
   on-demand diagnostics, **live reconfigure/reload without a restart**, and .NET profiling.

## What's in here

```
dotrush-cc/
├── .claude-plugin/marketplace.json      # marketplace manifest
└── plugins/dotrush/
    ├── .claude-plugin/plugin.json        # plugin manifest (declares the LSP server)
    ├── .lsp.json                         # csharp LSP -> bin/lsp-proxy.py, portable ${CLAUDE_PLUGIN_*} paths
    ├── bin/lsp-proxy.py                  # stdio MITM proxy + injector + auto-install-on-first-run
    ├── dotrush-version.json              # pins the DotRush server release and diagnostics tag or commit
    ├── scripts/install-dotrush.sh        # installs the DotRush server or profiling tools at the pinned ref
    ├── scripts/dotrush-install.sh        # shared install logic: one ref, release bundles or a build from source
    ├── scripts/dotrush-profile.sh        # collects/reports bounded CPU traces and GC dumps
    ├── scripts/summarize-speedscope.py   # produces agent-readable managed-CPU rankings
    ├── scripts/analyze-gcdump.py         # streams gcdump JSON: per-type bytes, retention chains, snapshot diffs
    ├── skills/dotrush-pick-project/      # picks the .sln/.slnx/.csproj DotRush loads, applied live
    ├── skills/dotrush-profile-cpu/       # CPU, hot-path, and latency profiling workflow
    ├── skills/dotrush-profile-memory/    # managed-heap snapshot and comparison workflow
    └── README.md                         # plugin usage
tests/test_profile_reports.py             # unit tests for the report tools, installer and proxy (python3 -m unittest)
```
