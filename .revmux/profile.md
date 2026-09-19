# Project profile — dotrush-cc

## What this is

A **Claude Code plugin marketplace**, not an application. It ships one plugin, `dotrush`, which wires the
DotRush Roslyn language server into Claude Code's `LSP` tool and adds .NET profiling workflows. Everything
here is one of five things (plus the tests under `tests/`):

- **manifests** — `.claude-plugin/marketplace.json`, `plugins/dotrush/.claude-plugin/plugin.json`,
  `plugins/dotrush/.lsp.json`. Machine-read by Claude Code at plugin load. A malformed or wrong-shaped
  manifest breaks install for every user, silently, with the failure surfacing only in `/plugin → Errors`.
- **runtime code** — `bin/lsp-proxy.py` (stdio MITM proxy, stdlib-only Python 3) and `scripts/*.sh`,
  `scripts/*.py`. This runs on the user's machine, unattended, spawned by Claude Code.
- **the CLI** — `tools/DotRushCli` (C#, `net10.0`, no packages), built on first use by `scripts/dotrush-cli.sh`
  into the plugin data dir. It runs the request channel and the rename, which writes the user's source files.
- **skills** — `skills/*/SKILL.md`. These are *prompts an agent executes with a shell*. Prose here is
  executable: an ambiguous instruction, a stale flag, or a command that contradicts the script it calls
  makes an agent do the wrong thing on someone else's process.
- **docs** — the two `README.md` files and `plugins/dotrush/CHANGELOG.md`.
  Users copy the commands out of them verbatim.

## What a real failure looks like here

Ranked by how much it actually costs the user:

1. **A command in a `SKILL.md` or README that does not match the script it invokes** — wrong flag name,
   wrong argument order, an env var the script never reads, an output path the script does not write.
   The agent runs it, it fails or silently produces nothing, and the user blames the plugin. Check every
   documented invocation against the actual argument parsing in `scripts/`.
2. **Attaching to or perturbing a process the user did not consent to.** `dotnet-gcdump` forces a full
   gen-2 GC and can pause the target; `dotnet-trace` attaches to a live PID. Any path that attaches,
   pauses, kills, or writes into a target process without an explicit confirmation gate is a major
   finding, as is a gate a skill's own wording lets the agent skip.
3. **Writing into the user's repo or a fixed absolute path.** The plugin's stated contract is that
   *nothing is written into the user's repo* — runtime state goes under `${CLAUDE_PLUGIN_DATA}`, profiling
   artifacts under an output dir the caller chooses, defaulting outside the repository
   (`$DOTRUSH_PROFILE_OUTPUT_DIR` → `${CLAUDE_PLUGIN_DATA}/profiles` → the user cache). A hardcoded
   `/tmp` path, a write next to the script, a default under `$PWD`, or state outside the per-session
   dir violates it.
4. **Breaking per-session isolation.** Runtime state is scoped to `${CLAUDE_PLUGIN_DATA}/ws/sess-<hash>/`
   precisely so parallel sessions (the git-worktree workflow) do not mesh. Anything that reintroduces a
   shared path, a shared FIFO, or a fixed filename across sessions is major.
5. **Unquoted shell expansion, missing `set -euo pipefail`, unchecked `cd`, or a pipeline whose failure is
   swallowed** in `scripts/*.sh`. Paths here routinely contain spaces (macOS `~/Library/...`).
6. **Non-stdlib Python imports.** `python3` with no pip install is the only interpreter guarantee the
   README makes. A `requests`/`numpy`/`pandas` import is a hard break on a clean machine. So is syntax newer
   than 3.9, macOS's `/usr/bin/python3`: `match`, or an `X | None` annotation without
   `from __future__ import annotations`.
7. **Version/changelog drift** — `plugin.json` `version` bumped without a changelog entry, or a changelog
   entry describing behavior the code does not have. The changelog is `plugins/dotrush/CHANGELOG.md`;
   both READMEs' file listings must include any new script or skill.

## Blast radius

Wide and unattended. This is distributed through a marketplace and auto-updates. There is no CI: the Python
and .NET test suites run only on the author's machine, nothing gates installs, and nothing reports runtime
errors back to the author. A broken manifest or a wrong documented command reaches every installed user on their next plugin load, and the only feedback channel
is a GitHub issue. Weight findings accordingly: correctness of what *ships* outranks internal tidiness by
a wide margin.

External tools are downloaded at runtime (`curl` + `unzip` of DotRush release bundles, or a `git` checkout of
DotRush at the pinned ref built with `dotnet publish`).
Anything touching a download URL, a release tag, an extraction path, or a checksum is security-relevant.

## Reporting bar

- **major or above**: anything in the seven categories above; anything that makes an agent act on a user's
  live process incorrectly; anything that breaks install or plugin load.
- **minor**: real defects that degrade output quality but do not misdirect — a report that mis-ranks,
  a summary that rounds wrong, an error message that does not say what to do next.
- **do not report**: prose style in the READMEs, heading capitalization, table alignment, the em-dash and
  bolding style (deliberate and consistent throughout), the absence of type annotations in the Python
  scripts, or the absence of a CI config. There is deliberately no linter config, no
  `CONTRIBUTING.md`, and no build system or package manifest beyond the CLI's `DotRushCli.csproj` and the
  wrapper that builds it — do not propose adding them.
- Prefer one grounded finding with the exact conflicting line quoted over three speculative ones.

## Deliberate conventions — do not flag as defects

- **Shell**: `#!/usr/bin/env bash`, `set -euo pipefail`, a header comment block giving Usage and every
  env var it reads. Args parsed with a `for`/`case` loop, not `getopts`.
- **Python**: `#!/usr/bin/env python3`, module docstring describing the role and every env var, stdlib
  only, no external dependencies, no packaging.
- **Skills**: YAML front matter with exactly `name` and `description`, where `description` states both
  what it does and *when to use it* — that text is what routes the skill, so it is intentionally long.
- **The DotRush server binary is never committed** ; it is installed on first use at the ref pinned
  in `dotrush-version.json`, downloaded or built from source. `.gitignore` guards against local copies. Absence of the binary is not a finding.
- Documentation is written for two audiences at once — a human reading the README and an agent executing
  the skill. Redundancy between the plugin README and a `SKILL.md` is intentional; *contradiction*
  between them is a finding.
