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

1. **Find this session's DotRush runtime dir** with the plugin's CLI. It matches the dir recording this
   session's id first, and falls back to the project path only among dirs of proxies started without a session
   id — so parallel sessions in different worktrees never pick each other's FIFO:
   ```bash
   WSDIR=$("${CLAUDE_PLUGIN_ROOT}/scripts/dotrush-cli.sh" session --dir)
   ```
   - The first call builds the CLI (a few seconds, once per plugin version).
   - If it exits 1 (`no DotRush language server has started in this session`), the C# LSP server hasn't
     started for this session yet. Ask the user to trigger it (open any `.cs` file, or run any C# LSP action)
     and re-run this skill.
   - The persisted choice is `"$WSDIR/target.json"`.

2. **Don't re-ask if already configured**: run `"${CLAUDE_PLUGIN_ROOT}/scripts/dotrush-pick-project.sh" show`;
   if it prints a target, report it. Only continue if the user explicitly wants to change it.

3. **Discover candidates** with Glob, relative to the user's working directory, solutions first:
   `**/*.slnx`, then `**/*.sln`, then `**/*.csproj`. Dedupe, keep absolute paths, cap ~10.

4. **Choose the target.** If a project or user instruction (a `CLAUDE.md` mapping work areas to solutions, or
   the user naming one) already determines it, use that and tell the user which rule chose it. Otherwise call
   **AskUserQuestion**: "Which project/solution should DotRush load for C# in this workspace?" Options = the
   discovered candidates (basename + parent folder). The user can pick "Other" to type an absolute path. Never
   guess between candidates.

   Also decide whether to restore: yes by default; no when the projects are already restored (a built
   checkout) or a NuGet feed is unreachable (restores fail slowly with `NU1900` and similar), or when an
   instruction says so.

5. **Save and apply it** with the script, passing the chosen absolute path as one single-quoted argument (write
   an apostrophe inside it as `'\''`), plus `--no-restore` when step 4 decided against restoring:
   ```bash
   "${CLAUDE_PLUGIN_ROOT}/scripts/dotrush-pick-project.sh" apply '<ABS_PATH>' [--no-restore]
   ```
   Never write `target.json` or the FIFO by hand: the script builds the JSON from its arguments, so no path is
   pasted into shell or JSON source, and it writes the FIFO without blocking and without creating a file there.
   What it prints decides the next step:
   - It prints `not applied live`: the language server for this session is not running yet (it may still be installing).
     The choice is saved and loads when the server starts; tell the user that and skip step 6.
   - It prints `applied: configuration sent`: the running server got the configuration. The script also sends
     `dotrush/reloadWorkspace` (`workspace reload sent`), but only when `"$WSDIR/load-completed"` exists.
     Without that file DotRush has not loaded a project yet: its initialization is waiting for the configuration
     and loads the project itself as soon as it arrives, and a reload then would race that load (code analysis
     never starts, or every diagnostic appears twice). With the file, the server needs the reload to switch.
   - It exits 1 with an error: report it; the path was not absolute or does not exist, or the FIFO write failed.

6. **Verify** — wait a few seconds (large solutions take longer), then run an LSP `documentSymbol` on a
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
