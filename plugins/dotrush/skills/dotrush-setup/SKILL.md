---
name: dotrush-setup
description: Choose which C# project/solution (.sln/.slnx/.csproj) DotRush should load for LSP and apply it live — no dotrush.config.json needed. Use when the user wants to set up or change the DotRush project, or when C# LSP returns "No symbols found" because no project is loaded.
---

# DotRush project setup

Pick the C# project/solution DotRush loads, and apply it **without** a hand-written
`dotrush.config.json`. The choice is persisted in the plugin data dir and replayed at
startup by the proxy, so it is asked only once.

## When to run
- The user asks to set up / pick / change the DotRush (C#) project or solution.
- C# LSP operations return "No symbols found" (server up, but no project loaded).

## Steps

1. **Locate the plugin data dir** (the proxy creates its FIFO there once the C# LSP server has started):
   ```bash
   FIFO=$(find "$HOME/.claude/plugins" -name inject.fifo -path '*dotrush*' 2>/dev/null | head -1)
   ```
   - If `$FIFO` is empty, the C# LSP server hasn't started yet. Ask the user to trigger it
     (open any `.cs` file, or run any C# LSP action) and then re-run this skill.
   - `DATA="$(dirname "$FIFO")"`; the persisted target is `"$DATA/target.json"`.

2. **Don't re-ask if already configured** ("if he didn't give it before"): if `"$DATA/target.json"`
   exists, read it and tell the user the current target. Only continue to step 3 if the user
   explicitly wants to change it.

3. **Discover candidates** with Glob, relative to the user's working directory, solutions first:
   `**/*.slnx`, then `**/*.sln`, then `**/*.csproj`. Dedupe, keep absolute paths, cap ~10.

4. **Ask the user (required)** — call **AskUserQuestion**: "Which project/solution should DotRush
   load for C#?" Options = the discovered candidates (show basename + parent folder). The user can
   pick "Other" to type an absolute path. Never guess the target — always ask (unless step 2 applied).

5. **Persist the choice** to `"$DATA/target.json"` (the roslyn config section), using the chosen absolute path:
   ```bash
   printf '%s\n' '{"projectOrSolutionFiles":["<ABS_PATH>"],"restoreProjectsBeforeLoading":true}' > "$DATA/target.json"
   ```

6. **Apply it live now** (no restart) via the FIFO:
   ```bash
   printf '%s\n' '{"method":"workspace/didChangeConfiguration","params":{"settings":{"dotrush":{"roslyn":{"projectOrSolutionFiles":["<ABS_PATH>"],"restoreProjectsBeforeLoading":true}}}}}' > "$FIFO"
   printf '%s\n' '{"method":"dotrush/reloadWorkspace","params":{"workspaceFolders":[{"uri":"file://<WORKDIR>","name":"ws"}]}}' > "$FIFO"
   ```

7. **Verify** — wait a few seconds (large solutions take longer), then run an LSP `documentSymbol`
   on a `.cs` file from the chosen project. Symbols back → report success. Still empty →
   `tail -n 30 "$DATA/proxy.log"` and look for `projectLoaded` / errors.

## Notes
- Always use **absolute** paths.
- The choice persists in the plugin data dir (`target.json`); future sessions auto-load it — nothing
  is written into the user's repo (no `dotrush.config.json`).
- To change the target later, re-run this skill and pick a different one.
