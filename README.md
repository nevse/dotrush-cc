# dotrush-cc

A Claude Code **marketplace** containing the `dotrush` plugin: the [DotRush](https://github.com/JaneySprings/DotRush)
Roslyn language server wired into Claude Code's `LSP` tool for C#/.NET, plus a stdio **proxy** that can
inject custom LSP messages into the running server (on-demand diagnostics, live solution reconfigure/reload).

The DotRush server is **not** committed here (it's ~118 MB and platform-specific). Instead the plugin
**auto-downloads the official release bundle** for your OS/arch on first use.

## Install

```bash
# 1. add this marketplace (GitHub shorthand, or any git URL)
claude plugin marketplace add nevse/dotrush-cc
#   e.g. claude plugin marketplace add https://github.com/nevse/dotrush-cc.git

# 2. install the plugin
claude plugin install dotrush@dotrush-cc

# 3. restart Claude Code. On first C# LSP use, the DotRush server auto-downloads
#    into the plugin's data dir (${CLAUDE_PLUGIN_DATA}/server). One-time, ~48 MB.
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

- `curl` and `unzip` (for the one-time server download)
- `python3` (the proxy is a stdlib-only Python 3 script)
- A .NET SDK on PATH (DotRush loads/analyzes MSBuild projects)

## Quick start after install

1. Point DotRush at your project — create `dotrush.config.json` in your Claude working directory:
   ```json
   { "dotrush": { "roslyn": { "projectOrSolutionFiles": ["/abs/path/to/YourSolution.sln"] } } }
   ```
   (Required in a multi-project/monorepo root, where auto-discovery can't pick a single target.)
2. Restart Claude Code; the C# LSP loads on first use.
3. See [`plugins/dotrush/README.md`](plugins/dotrush/README.md) for capabilities, the injection FIFO,
   on-demand diagnostics, and **live reconfigure/reload without a restart**.

## What's in here

```
dotrush-cc/
├── .claude-plugin/marketplace.json      # marketplace manifest
└── plugins/dotrush/
    ├── .claude-plugin/plugin.json        # plugin manifest (declares the LSP server)
    ├── .lsp.json                         # csharp LSP -> bin/lsp-proxy.py, portable ${CLAUDE_PLUGIN_*} paths
    ├── bin/lsp-proxy.py                  # stdio MITM proxy + injector + auto-install-on-first-run
    ├── scripts/install-dotrush.sh        # downloads the DotRush server bundle for this OS/arch
    └── README.md                         # plugin usage
```
