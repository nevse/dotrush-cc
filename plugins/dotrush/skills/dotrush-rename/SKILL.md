---
name: dotrush-rename
description: Rename a C# symbol (type, method, property, field, event, local or parameter) everywhere it is used across the loaded solution, through the Roslyn rename in this session's DotRush language server, with a diff preview and, after the user confirms, an apply that checks every file before replacing any. Use when the user asks to rename a C# class, member or variable across the codebase, or to "rename X to Y" in C# code. Do not use for renaming files, folders or projects, text inside strings or comments, non-C# files, or a plain search-and-replace of text that is not one symbol.
---

# Rename a C# symbol with DotRush

DotRush resolves the symbol with Roslyn, so only real references to that one symbol change: a same-named local, member or type elsewhere is left alone, which a text search-and-replace cannot promise. The rename runs in two steps through the plugin's CLI: `rename preview` asks DotRush for the edits, checks them against the files on disk and saves them as a plan; `rename apply` prepares every file before replacing any, so anything it refuses changes nothing, and a failure while replacing them names exactly which files changed.

Run both steps with the Bash tool. The first call of the CLI builds it once per plugin version, which takes a few seconds and prints `building the DotRush CLI` on stderr.

## Workflow

1. **Find the symbol's position.** Use the LSP tool: `workspaceSymbol` with the name, or `documentSymbol` on the file that declares it. Then read that line of the file and take the 1-based column of the identifier itself (its first character is simplest), not of a modifier or the keyword before it. Line and column are 1-based, as in the LSP tool; a tab counts as one column. The position can be the declaration or any reference to the symbol.

2. **Preview the rename:**

   ```bash
   "${CLAUDE_PLUGIN_ROOT}/scripts/dotrush-cli.sh" rename preview <file> <line> <column> <NewName>
   ```

   `<file>` may be absolute or relative to the working directory. `<NewName>` must be a valid C# identifier; a reserved keyword needs `@` (`@class`), while contextual keywords such as `var` or `record` work as they are. DotRush gets 60 seconds by default; on a large solution add `--timeout <seconds>` and give the Bash call a longer timeout than that:

   ```bash
   "${CLAUDE_PLUGIN_ROOT}/scripts/dotrush-cli.sh" rename preview <file> <line> <column> <NewName> --timeout 180
   ```

   On success (exit 0) it prints:

   ```
   3 edits in 2 files
   App.cs: 2 edits
   Greeter.cs: 1 edit
   --- a/App.cs
   +++ b/App.cs
   @@ -2,6 +2,6 @@
   ...
   diff: <session dir>/edits/<plan-id>.diff
   plan: <plan-id>
   ```

   - The first line counts every edit; then one line per file, relative to the workspace, marked `(outside workspace)` when the file is outside the workspace root or under a `bin`/`obj` directory (generated code, for example).
   - Then the unified diff, at most 200 lines. A longer diff ends with `(diff truncated: K more lines)`; the full diff is in the file named by `diff:`.
   - DotRush sends minimal edits (renaming `Greeter` to `Welcomer` edits `Greet` into `Welcom` and keeps `er`), so edit counts are occurrences of the symbol, and the diff shows whole changed lines.

3. **Show the user the preview and ask for confirmation.** Show the summary line, the per-file lines and the diff. When the diff was truncated, say so and give the `diff:` path (read that file if the user wants to see more of it). Point out any `(outside workspace)` files. Then call **AskUserQuestion**: apply the rename, or cancel. When files are marked `(outside workspace)`, ask explicitly whether those files may be edited too. Never apply without the user's confirmation, and never pass `--outside-workspace` unless the user agreed to edit those files.

4. **Apply the confirmed plan**, with the id from the `plan:` line:

   ```bash
   "${CLAUDE_PLUGIN_ROOT}/scripts/dotrush-cli.sh" rename apply <plan-id>
   ```

   Only when the preview marked files `(outside workspace)` and the user agreed to edit them:

   ```bash
   "${CLAUDE_PLUGIN_ROOT}/scripts/dotrush-cli.sh" rename apply <plan-id> --outside-workspace
   ```

   Before writing anything it checks that every file still has the bytes the preview saw; then it writes all files (keeping each file's BOM, line endings and file mode), deletes the plan and has DotRush re-read every changed file from disk. On success (exit 0) it prints `renamed <Old> to <New>: N edits in M files`, then the absolute path of each changed file, one per line.

5. **After applying:**
   - **Read every changed file again before editing it.** The files changed on disk behind Claude Code's back, so an earlier read of them is stale.
   - Report the rename and the changed files.
   - If the renamed type lived in a file with its old name (`Greeter.cs` declaring `Greeter`), say that the file still has the old name; rename the file only if the user asks.
   - The rename does not touch strings, comments or non-C# files. If the old name may appear in them (`nameof` is covered, a `"Greeter"` string is not), search for it with Grep and list what you find; change those only if the user asks.

## Errors

Every error goes to stderr prefixed with `dotrush-cli: `. Exit status: 1 error, 2 usage, 3 timeout.

**Session readiness** (both steps check these first):

- `no DotRush language server has started in this session (looked in …); run any C# LSP operation first` — run an LSP operation (for example `documentSymbol` on a `.cs` file in the project), then retry.
- `the DotRush language server for this session is not running; run any C# LSP operation to start it` — same: start it with an LSP operation, then retry.
- `the running DotRush proxy predates the request channel; restart Claude Code` — the language server started before the plugin was updated. Tell the user to restart Claude Code. Do not fall back to a text search-and-replace unless the user asks for one.
- `DotRush has not finished loading a project in this session; choose one with dotrush-pick-project, or wait for the load to finish and retry` — if no project was chosen, run the `dotrush-pick-project` skill; if one was just chosen, wait a little and retry.
- `cannot write to the proxy's FIFO …` — the proxy is not taking requests. Retry once; if it persists, tell the user to restart Claude Code.

**Preview:**

- `'<name>' is not a valid C# identifier` (exit 2), with `('<name>' is a reserved C# keyword; use @<name>)` for a keyword — ask the user for a valid name, or offer the `@` form.
- `<line> and <column> must be whole numbers starting at 1` (exit 2) — fix the arguments.
- `--timeout needs a number of seconds greater than 0` or `--timeout may be given only once` (exit 2) — the value must be a number above 0 and at most 86400 (24 h), passed once. Fix it and retry.
- `<file> does not exist` — check the path.
- `the session dir <dir> does not record its workspace; restart Claude Code` — the session's runtime dir is incomplete; tell the user to restart Claude Code.
- `line <line>, column <column> of <file> is not on an identifier` — the column points at whitespace, punctuation or a number. Read the line again, recount the column so it lands on the identifier, and retry.
- `the symbol is already named <Name>` — the new name equals the current one; nothing to do.
- `no symbol at this position; check it with documentSymbol` — DotRush found nothing to rename there: the position is on a keyword or another word that is not a renameable symbol, or the file is not part of a loaded project. Check the position with `documentSymbol`. Right after a project load DotRush can briefly find no symbol, so retry once after a few seconds before telling the user.
- `DotRush's view of <file> differs from disk; preview again after the file is saved` — an edit lands past the end of that file, so DotRush has not seen its latest version (it was just edited, or edited outside Claude Code). Wait a few seconds and preview again; if it repeats, run an LSP operation such as `documentSymbol` on that file and preview again. Nothing was saved.
- `DotRush's rename edits '<other>' in <file>, which is not <Old>: it needs edits outside the old name (Roslyn conflict resolution), or its view of that file differs from disk; not supported` — Roslyn wanted to change an identifier other than the old name, usually to resolve a conflict the new name creates (qualifying an unrelated name, for example). Do not keep waiting and re-previewing: tell the user this rename needs conflict-resolution edits the skill does not apply, and offer a different new name. One retry is worth it only if that file was edited moments ago; re-read it with `documentSymbol` first. Nothing was saved.
- `DotRush's rename would turn '<Old>' in <file> into '<Other>', not <New>; not supported` — the edit would not produce the requested name. Report it as it is; nothing was saved.
- `DotRush returned the rename as documentChanges, which this command cannot apply; restart Claude Code, and report this if it persists` — the server answered in the other WorkspaceEdit shape. Nothing was saved; this is a plugin limitation, so report it rather than retrying.
- `no response to textDocument/rename within <N> s; the request was cancelled` (exit 3) — DotRush is still busy (a large solution, or a project still loading). Retry with a larger `--timeout`, and a Bash timeout above it.
- `<code>: <message>` — DotRush answered with an error. Report it as it is.

**Apply:**

- `unknown plan '<plan-id>'; run rename preview again` or `plan '<plan-id>' cannot be read; run rename preview again` — the plan is gone: it was already applied, or the language server restarted (a restart clears saved plans). Preview again and ask for confirmation again.
- `<path> changed since preview; run rename preview again` — a file changed after the preview; nothing was written. Preview again, show the new diff and ask again.
- `<path> is outside the workspace <root> (or under bin/obj); apply with --outside-workspace to edit it` (or `<paths> are outside … to edit them`) — nothing was written. Ask the user whether those files may be edited; add `--outside-workspace` only if they agree, otherwise cancel.
- `cannot write <file>.dotrush-cc.tmp: <reason>; no file was changed` — a temporary file could not be written (permissions, full disk). Report the reason; no file changed.
- `cannot replace <path>: <reason>; changed: <paths>; unchanged: <paths>` — a write failed part way, so the rename is incomplete. Tell the user exactly which files changed and which did not, read the changed ones again, and let the user decide how to proceed (for example restoring the changed files with git). DotRush was still told to re-read the changed files. The plan is dropped, because the changed files no longer match it: finishing the rename means previewing again.
- `the files were changed, but DotRush may not have been told to re-read them (<reason>); restart Claude Code so DotRush loads them from disk` — the rename was written (the summary and changed files are still printed on stdout) but DotRush may still see the old text in some of them. Report the rename, read the changed files again, and tell the user to restart Claude Code before relying on C# LSP results for those files.
- Any other `dotrush-cli:` message (a file that is not valid UTF-8, a file that cannot be read, overlapping edits) — report it as it is; nothing was written.

## Limits

- **What DotRush renames:** the symbol's declaration and its references in C# code, including `nameof`. It does not rename overloads of a method (each overload is its own symbol), text in strings or comments, or files: a class in a file of the same name keeps that file name.
- **Only loaded projects:** only documents in the projects DotRush has loaded change. Projects outside the chosen solution, and non-C# files (`.razor`, `.xaml`, `.json`, scripts) are not edited; search those for the old name after applying.
- Generated files under `bin`/`obj` can be part of the edit; they are marked `(outside workspace)` and are usually regenerated by the next build, so leaving them out is normally right.
- Avoid editing C# files between preview and apply: an edited file makes apply refuse, and the preview must be run again.
