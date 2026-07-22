---
name: dotrush-pick-project
description: Pick which C# project/solution (.sln/.slnx/.csproj) DotRush should load for LSP in THIS workspace, and apply it live — no dotrush.config.json needed. Use when the user wants to set or change the DotRush project, or when C# LSP returns "No symbols found" because no project is loaded.
---

# Pick the DotRush project (per session)

Choose the C# project/solution DotRush loads for the current session, and apply it **without** a
hand-written `dotrush.config.json`. The choice is scoped **per Claude session** (so parallel sessions —
e.g. one per git worktree under the same folder — never clobber each other) and replayed on the session's
LSP restarts, so within a session it's asked only once.

## When to run
- The user asks to set / pick / change the DotRush (C#) project or solution.
- C# LSP operations return "No symbols found" (server up, but no project loaded).

## Steps

1. **Find this session's DotRush runtime dir.** The proxy records the session id in `session.txt` and the
   project path in `workspace.txt`. Match on the **session id** first — parallel sessions in different
   worktrees share `$PWD`, so a `$PWD` match alone can hit the wrong session's FIFO:
   ```bash
   ROOT="${CLAUDE_CONFIG_DIR:-$HOME/.claude}/plugins/data"
   SID="${DOTRUSH_SESSION_ID:-$AGTERM_SESSION_ID}"
   HIT=""
   [ -n "$SID" ] && HIT=$(grep -lFx "$SID" "$ROOT"/*/ws/sess-*/session.txt 2>/dev/null | head -1)
   # Fallback (headless / no session id): match the recorded workspace path.
   [ -z "$HIT" ] && HIT=$(grep -lFx "$PWD" "$ROOT"/*/ws/*/workspace.txt 2>/dev/null | head -1)
   WSDIR=$(dirname "$HIT" 2>/dev/null)
   ```
   - If `$HIT` is empty, the C# LSP server hasn't started for this session yet. Ask the user to trigger
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
- The choice is stored **per Claude session** in the plugin data dir (`ws/sess-<hash>/target.json`) and
  replayed on that session's LSP restarts. Nothing is written into the user's repo (no `dotrush.config.json`).
- **Every session gets its own dir + FIFO + target**, keyed by session id — so parallel sessions (e.g. one
  per git worktree under the same folder) never collide or mesh, and each can target a different solution.
- Session ids are per session, so a **fresh** session (new Claude launch) re-asks — the previous session's
  choice isn't inherited. Stale dirs from ended sessions are pruned automatically by the proxy.
- On terminals with no session id (headless/CI), it falls back to per-workspace scoping keyed on the
  project path, which *does* persist across restarts.
