---
name: dotrush-pick-project
description: Pick which C# project/solution (.sln/.slnx/.csproj) DotRush should load for LSP in THIS workspace, and apply it live — no dotrush.config.json needed. Use when the user wants to set or change the DotRush project, or when C# LSP returns "No symbols found" because no project is loaded.
---

# Pick the DotRush project (per workspace)

Choose the C# project/solution DotRush loads for the current workspace, and apply it **without** a
hand-written `dotrush.config.json`. The choice is persisted per workspace and replayed at startup, so
it's asked only once — and each project/session keeps its own choice.

## When to run
- The user asks to set / pick / change the DotRush (C#) project or solution.
- C# LSP operations return "No symbols found" (server up, but no project loaded).

## Steps

1. **Find this workspace's DotRush runtime dir** (the proxy records the project path in `workspace.txt`;
   each workspace has its own dir, so concurrent sessions don't collide):
   ```bash
   ROOT="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/plugins/data"
   HIT=$(grep -lFx "$PWD" "$ROOT"/*/ws/*/workspace.txt 2>/dev/null | head -1)
   WSDIR=$(dirname "$HIT" 2>/dev/null)
   ```
   - If `$HIT` is empty, the C# LSP server hasn't started for this workspace yet. Ask the user to trigger
     it (open any `.cs` file, or run any C# LSP action) and re-run this skill.
   - `FIFO="$WSDIR/inject.fifo"`; persisted choice = `"$WSDIR/target.json"`.

2. **Don't re-ask if already configured**: if `"$WSDIR/target.json"` exists, read it and report the current
   target. Only continue if the user explicitly wants to change it.

3. **Discover candidates** with Glob, relative to the user's working directory, solutions first:
   `**/*.slnx`, then `**/*.sln`, then `**/*.csproj`. Dedupe, keep absolute paths, cap ~10.

4. **Ask the user (required)** — call **AskUserQuestion**: "Which project/solution should DotRush load for
   C# in this workspace?" Options = the discovered candidates (basename + parent folder). The user can pick
   "Other" to type an absolute path. Never guess — always ask (unless step 2 applied).

5. **Persist the choice** to `"$WSDIR/target.json"`, using the chosen absolute path:
   ```bash
   printf '%s\n' '{"projectOrSolutionFiles":["<ABS_PATH>"],"restoreProjectsBeforeLoading":true}' > "$WSDIR/target.json"
   ```

6. **Apply it live now** (no restart) via this workspace's FIFO:
   ```bash
   printf '%s\n' '{"method":"workspace/didChangeConfiguration","params":{"settings":{"dotrush":{"roslyn":{"projectOrSolutionFiles":["<ABS_PATH>"],"restoreProjectsBeforeLoading":true}}}}}' > "$FIFO"
   printf '%s\n' '{"method":"dotrush/reloadWorkspace","params":{"workspaceFolders":[{"uri":"file://<WORKDIR>","name":"ws"}]}}' > "$FIFO"
   ```

7. **Verify** — wait a few seconds (large solutions take longer), then run an LSP `documentSymbol` on a
   `.cs` file from the chosen project. Symbols back → success. Still empty → `tail -n 30 "$WSDIR/proxy.log"`
   and look for `projectLoaded` / errors.

## Notes
- Always use **absolute** paths.
- The choice is stored **per workspace** in the plugin data dir (`ws/<hash>/target.json`) and auto-loaded
  on future sessions. Nothing is written into the user's repo (no `dotrush.config.json`).
- Two Claude sessions on **different** projects each get their own project + FIFO (no collision). Running
  two sessions on the **same** project dir shares one FIFO — LSP navigation still works per session, but
  live injection into that shared workspace is ambiguous; prefer one session per project dir for injection.
