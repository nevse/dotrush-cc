---
name: dotrush-diagnostics
description: Run DotRush's whole-solution compiler analysis in this session's C# language server and report the errors and warnings it finds, grouped by code and listed errors first. Use when the user asks for solution or project diagnostics, compiler errors or warnings across the codebase, "does it compile", or a health check of C# code without running a build. Do not use for analyzer-only rules (a solution run reports compiler diagnostics), runtime failures, or test results.
---

# Report DotRush solution diagnostics

Ask the running DotRush server to analyze every project it has loaded, then report what it publishes. This answers "what does the compiler say about this solution" from the language server's in-memory workspace, without `dotnet build` and without touching `bin/` or `obj/`.

Use the plugin helper at `${CLAUDE_PLUGIN_ROOT}/scripts/dotrush-diagnostics.sh`. It finds this session's DotRush runtime directory, injects `dotrush/solutionDiagnostics` into the server through the proxy, waits for the results, and prints a summary. The proxy mirrors every diagnostics publish into `diagnostics.json` in that directory, which is where the results are read from.

## Workflow

1. Check the session is ready:

   ```bash
   "${CLAUDE_PLUGIN_ROOT}/scripts/dotrush-diagnostics.sh" where
   ```

   - `no DotRush language server has started` — run any C# LSP operation first (for example `documentSymbol` on a `.cs` file in the project), then retry.
   - `load: not completed` — DotRush has not finished its first project load, and it starts code analysis only after that; `solution` refuses to run. If a project was just chosen, wait for the load and re-check. If the project was switched with a `dotrush/reloadWorkspace` before any load completed, analysis will not start in this server: tell the user to restart Claude Code.
   - `load: completed with no project` — no project was chosen, and DotRush found no single solution or project in the workspace to load on its own, so `solution` refuses to run. Run the `dotrush-pick-project` skill, then re-check.
   - `load: completed (N projects)` — ready; N is how many projects DotRush loaded. A proxy from before 0.8.7 prints `load: completed` without the count.
   - `target: none chosen` — normal when the workspace holds a single solution (or a single project) or a `dotrush.config.json`: DotRush loads that on its own. The `load:` line says whether it did.
   - `publishes: no capture (older proxy)` — the language server started before the plugin was updated. Tell the user to restart Claude Code; do not try to work around it.
   - `channel: unavailable (older proxy)` — the proxy predates the request channel that plugin tools such as rename use. Diagnostics do not need it, so `solution` and `report` still work; mention a Claude Code restart only if the user also wants those tools.
   - The first `where` (or any first use of the plugin's CLI) builds that CLI once per plugin version, which takes a few seconds and prints `building the DotRush CLI` on stderr. `where`, `solution` and `report` all find the session through that CLI, so `building the DotRush CLI failed; full log: …` (or a missing `shasum`/`sha256sum`) stops all three, even `report`, which otherwise needs nothing running. That is a .NET 10 SDK problem, not a language-server one: report the tail of the log it prints.
   - `no DotRush language server has started` while the server is plainly running usually means the session id is missing from this Bash environment: the lookup matches `DOTRUSH_SESSION_ID` or `AGTERM_SESSION_ID`, and falls back to a per-workspace directory only. Tell the user to restart Claude Code from a terminal that sets one, rather than retrying.

2. Run the analysis:

   ```bash
   "${CLAUDE_PLUGIN_ROOT}/scripts/dotrush-diagnostics.sh" solution [count]
   ```

   It waits up to `DOTRUSH_DIAGNOSTICS_TIMEOUT` seconds (default 300) for the first result, so give the Bash call a timeout above that on a large solution. `count` (default 50) limits the listed diagnostics; the counts and the per-code table always cover everything.

3. Read the output:

   - Claude Code may also show a `new-diagnostics` block after the run. It holds only errors and warnings it has not shown before (so files already reported are missing from it), at most 30, and a multi-targeted project's error appears there once per framework. Report from this script's output, which is the complete current set.
   - A diagnostic followed by a bracketed list was reported by several sources and is listed and counted once: `[net10.0, net11.0]` means one project built for several target frameworks, `[App.Core.Tests(net10.0, net11.0), App.Tests]` means a file several projects compile, such as a package's `Program.cs`. Projects appear under their full names, sorted, each with its frameworks when it has several. That is normal, not a duplicate.
   - If every diagnostic appears exactly twice as two identical rows, DotRush loaded the project twice (a project switch that raced its first load). Report the findings once and tell the user a Claude Code restart clears it.
   - The first line counts the files that have any diagnostic (hints included) and the diagnostics by severity. `Publishes:` is the server's running total of `publishDiagnostics` since it started, not a per-run figure; it only shows that results arrived. `By code` ranks codes by occurrence. The list is sorted errors first, then by path. Paths are relative to the workspace, except a file outside it (one from a NuGet package, say), which keeps its absolute path and sorts first; `line:column` positions are 1-based.
   - Hints (mostly `CS8019` unnecessary usings, many in generated `obj/` files) are counted but not listed. Pass `--hints` to the summarizer only when the user asks for them (see step 4).
   - **Exit status 0** means the run finished and reported, errors or not; it is a report, not a pass/fail check. Read `Errors:` for that.
   - **Exit status 3** means nothing was published before the timeout. DotRush publishes only files that have diagnostics or that just lost them, so this is what a clean solution looks like on a first run — but it is also what a still-running or cancelled analysis looks like. Say both, and suggest a larger timeout for a big solution before calling it clean.

4. To re-read the last results without analyzing again, or to list more rows or the hints:

   ```bash
   "${CLAUDE_PLUGIN_ROOT}/scripts/dotrush-diagnostics.sh" report 200
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/summarize-diagnostics.py" <DIR>/diagnostics.json --hints --root <WORKSPACE> --count 500
   ```

   `DIR` and `WORKSPACE` are the `dir:` and `workspace:` lines of `where`. `--count` (default 50) is how many rows the summarizer lists, and `report N` and `solution N` pass N as it; past it the list ends with `... K more; pass a larger count`. The counts above the list always cover everything.

5. Report the totals, the most frequent codes, and the errors with their locations. Group repeated codes rather than listing each occurrence. For each error you explain, read the source at that location rather than guessing from the message.

## What the results do and do not cover

- A solution run reports **compiler** diagnostics (with any diagnostic suppressors applied) for every loaded project. Analyzer rules from NuGet analyzer packages are not part of it.
- Results reflect DotRush's in-memory workspace, including unsaved edits Claude Code has sent it, and only the projects it loaded. A project DotRush failed to load contributes nothing; check `proxy.log` in the runtime directory for `dotrush/projectLoaded`.
- Every later analysis replaces the set. When Claude Code opens or edits a file afterwards, DotRush re-analyzes just that document and clears the other files' entries, so `report` then shows only that file. Run `solution` again for a whole-solution view.
- A request that arrives while the analysis runs (an edit, another analysis) cancels it. Avoid editing C# files while `solution` waits.
- Nothing here builds, restores, or runs tests. For build-only failures (MSBuild targets, source generators that need a build, missing restore), use `dotnet build`.
