---
name: dotrush-profile-memory
description: Profile managed-memory usage and suspected leaks in a running .NET application with DotRush's dotnet-gcdump, report exact per-type sizes and what retains the most memory, and compare snapshots. Use for growing GC heaps, retained object types, surviving-object counts, retention paths, or when the user asks to create a heap dump. Do not use for CPU hot paths, allocation-rate or allocation-count questions (a gcdump forces a full GC and sees only survivors), or native-memory-only problems.
---

# Profile a .NET managed heap

Collect one or two managed-heap snapshots from the exact .NET process, report exact per-type sizes and the objects that retain the most memory, and compare snapshots.

Use the plugin helper at `${CLAUDE_PLUGIN_ROOT}/scripts/dotrush-profile.sh`. It runs DotRush's own build of `dotnet-gcdump` at the DotRush ref the plugin pins, installed exactly as the plugin's DotRush language server is: downloaded from that DotRush release when it ships the bundles, otherwise built once from DotRush source, which needs `git` and a .NET SDK and takes a few minutes. It keeps the tools in `${CLAUDE_PLUGIN_DATA}` (or the user cache) and runs them as `dotnet dotnet-gcdump.dll`, so a .NET runtime must be on `PATH` or in `DOTNET_ROOT`. Each snapshot is collected with `--format Json`, which writes the heap graph beside the `.gcdump` as `.gcdump.json`; the reports read that graph.

Run `"${CLAUDE_PLUGIN_ROOT}/scripts/dotrush-profile.sh" tools` first; it installs nothing. Its `pinned:` line says whether the tools come from a release or a build from source, and its `diagnostics:` line whether they are installed. If they are not installed, or not at the pin, tell the user what the first profiling command will do (download the release bundle, or build the tools from source for a few minutes) and honor any runtime approval prompt; give that command a timeout long enough for a build. If the `diagnostics:` line shows `gcdump-json=no`, the heap commands refuse to run: report that the pinned DotRush lacks `dotnet-gcdump --format Json`. If a build fails, report the log tail it prints and the log path. Do not substitute another `dotnet-gcdump`.

## Safety first

`dotnet-gcdump` deliberately triggers a full generation 2 GC and walks the managed heap. It can pause the target for a noticeable time and can add memory pressure on a large heap. Before collecting from production, a latency-sensitive service, or a target whose role is unclear, explain this impact and obtain confirmation. Run as the same user as the target; on Linux and macOS, also preserve the target's `TMPDIR`.

Do not upload, commit, or casually share `.gcdump`, `.gcdump.json` and report artifacts: type names, static field names and object sizes describe the application. With no `OUTPUT_DIR` the helper already writes outside the repository (`$DOTRUSH_PROFILE_OUTPUT_DIR`, else `${CLAUDE_PLUGIN_DATA}/profiles`, else the user cache); if you do pass one, keep it out of version control.

## Workflow

1. If no PID was supplied, discover attachable processes:

   ```bash
   "${CLAUDE_PLUGIN_ROOT}/scripts/dotrush-profile.sh" ps gcdump [--filter <TEXT>]
   ```

   Rows show `PID`, `ELAPSED`, `NAME`, `ASSEMBLY` (the first `.dll`/`.exe` argument) and `COMMAND` (its tail when long); `--filter` keeps rows containing the text, ignoring case, and exits 1 when none match. Select only an unambiguous process. Otherwise ask the user, showing those columns.

2. For a one-time heap composition question, collect one snapshot:

   ```bash
   "${CLAUDE_PLUGIN_ROOT}/scripts/dotrush-profile.sh" heap <PID> [OUTPUT_DIR]
   ```

   The helper prints `GCDUMP=`, `GCDUMP_JSON=` and `REPORT=` lines with absolute paths. Read the report. For a different row count, run `heap-report <SNAPSHOT.gcdump> 50`; it prints the report, converting a `.gcdump` that has no `.gcdump.json` yet.

3. For suspected growth or a leak, prefer two snapshots of the same process:

   - Warm the application and collect a baseline.
   - Run the same representative workload for a defined number of iterations or time.
   - Let transient work settle when appropriate, then collect the current snapshot.
   - Compare them:

   ```bash
   "${CLAUDE_PLUGIN_ROOT}/scripts/dotrush-profile.sh" heap-diff <BASELINE.gcdump> <CURRENT.gcdump> 30
   ```

4. Read the report and the diff:

   - `Bytes` per type is exact: the sizes of all its objects summed. Size-bucket rows such as `System.Byte[] (Bytes > 10K)` are merged into their type. In the diff, rank by `DeltaBytes` and use `DeltaCount` to tell many small objects from a few large ones. The heap-wide `HeapBytes` and `HeapObjects` deltas give overall growth.
   - `RetainedBytes` is what collecting that object would free: itself plus everything reachable only through it. `RetainedBy` is its dominator chain up to a root category, nearest owner first. When one static field alone holds the object, the chain names it, as in `[static var App.Cache.s_items] <- [.NET Roots]`. A chain ending directly in `[.NET Roots]` means more than one root reaches the object, so no single root owns it. A dominator is the nearest object that every reference path passes through, not the only reference; the `edges` array in the `.gcdump.json` holds every reference when that distinction matters.
   - Retained rows nest: a collection and its backing array both appear, the array naming the collection in its chain. Never add retained bytes across rows. An object whose immediate dominator has the same type is left out, so a linked list is listed once, at its head.
   - For a type that grows in the diff, find what retains it in the current snapshot's report (run `heap-report` with a larger count when it is not in the top rows) and follow the chain to the application-owned field or collection. A positive delta is a lead, not proof of a leak. Repeated growth across equivalent workload windows is stronger evidence than a single pair.

5. Report the PID, workload and interval between snapshots, artifact paths, top deltas, the retention chains that explain them, and limitations. Because each snapshot forces a full GC, unrelated transient objects can disappear; total object count may fall even while retained bytes and selected types grow. Working-set or RSS growth can come from native memory, JIT, thread stacks, mapped files, or allocator behavior and therefore may not appear in a managed-heap snapshot.

## Boundaries

- If collection fails because heap events were dropped or memory is constrained, stop and report that limitation rather than retrying repeatedly against a sensitive process.
- The `.gcdump.json` is larger than the `.gcdump`. The report reads it as a stream but keeps about 8 bytes per object in memory, so a heap of tens of millions of objects needs hundreds of megabytes to report on.
- For a full process dump, native-memory investigation, or deadlock analysis, use `dotnet-dump` or platform tools; this skill intentionally collects only GC dumps.
- Use `dotrush-profile-cpu` for high CPU, hot paths, or latency traces.
