---
name: dotrush-profile-memory
description: Profile managed-memory usage and suspected leaks in a running .NET application with dotnet-gcdump, produce heap statistics, and compare snapshots. Use for growing GC heaps, retained object types, surviving-object counts, or when the user asks to create a heap dump. Do not use for CPU hot paths, allocation-rate or allocation-count questions (a gcdump forces a full GC and sees only survivors), or native-memory-only problems.
---

# Profile a .NET managed heap

Collect one or two managed-heap snapshots from the exact .NET process, generate heap-stat reports, and interpret per-type object-count and heap-wide byte evidence carefully.

Use the plugin helper at `${CLAUDE_PLUGIN_ROOT}/scripts/dotrush-profile.sh`. It prefers globally installed tools and otherwise installs the `dotnet-gcdump` NuGet tool lazily under `${CLAUDE_PLUGIN_DATA}` (or the user cache when that variable is unavailable). The install runs from the tool directory, so a repository's own `NuGet.Config` cannot redirect it; the user's and machine's NuGet configuration still decides the feed.

Before the first lazy installation, tell the user that the helper will download a NuGet tool package from their configured feed and honor any runtime approval prompt.

## Safety first

`dotnet-gcdump` deliberately triggers a full generation 2 GC and walks the managed heap. It can pause the target for a noticeable time and can add memory pressure on a large heap. Before collecting from production, a latency-sensitive service, or a target whose role is unclear, explain this impact and obtain confirmation. Run as the same user as the target; on Linux and macOS, also preserve the target's `TMPDIR`.

Do not upload, commit, or casually share `.gcdump` and report artifacts. With no `OUTPUT_DIR` the helper already writes outside the repository (`$DOTRUSH_PROFILE_OUTPUT_DIR`, else `${CLAUDE_PLUGIN_DATA}/profiles`, else the user cache); if you do pass one, keep it out of version control.

## Workflow

1. If no PID was supplied, discover attachable processes:

   ```bash
   "${CLAUDE_PLUGIN_ROOT}/scripts/dotrush-profile.sh" ps gcdump
   ```

   Select only an unambiguous process. Otherwise ask the user.

2. For a one-time heap composition question, collect one snapshot:

   ```bash
   "${CLAUDE_PLUGIN_ROOT}/scripts/dotrush-profile.sh" heap <PID> [OUTPUT_DIR]
   ```

   The helper retains the `.gcdump`, generates `.heapstat.txt`, and prints absolute paths.

3. For suspected growth or a leak, prefer two snapshots of the same process:

   - Warm the application and collect a baseline.
   - Run the same representative workload for a defined number of iterations or time.
   - Let transient work settle when appropriate, then collect the current snapshot.
   - Compare them:

   ```bash
   "${CLAUDE_PLUGIN_ROOT}/scripts/dotrush-profile.sh" heap-diff <BASELINE.gcdump> <CURRENT.gcdump> 30
   ```

4. Rank positive **object-count** deltas, then relate suspicious types to the application's ownership and lifetime model. `heap-diff` reports no per-type byte deltas, and you must not compute one: `dotnet-gcdump report` prints a single size per type that PerfView takes from the first object it saw, so for strings and for arrays under 1 KB it is one arbitrary object's size. `SampleObjectBytes` is that sample — useful for telling a buffer type from a small one, worthless as a total. The heap-wide `HeapBytes` delta is sound; use it for overall growth. For arrays or buffers, inspect nearby application-owned wrapper/container types even when their own count delta is small; the large payload and its owner often appear as separate rows. A positive delta is a lead, not proof of a leak. Repeated growth across equivalent workload windows is stronger evidence than a single pair.

5. Report the PID, workload and interval between snapshots, artifact paths, top deltas, likely ownership paths to inspect, and limitations. Because each snapshot forces a full GC, unrelated transient objects can disappear; total object count may fall even while retained bytes and selected types grow. Working-set or RSS growth can come from native memory, JIT, thread stacks, mapped files, or allocator behavior and therefore may not appear in a managed-heap snapshot. The text heapstat report does not establish a retaining root; use the `.gcdump` in a compatible memory viewer when root or dominator analysis is required.

## Boundaries

- If collection fails because heap events were dropped or memory is constrained, stop and report that limitation rather than retrying repeatedly against a sensitive process.
- For a full process dump, native-memory investigation, or deadlock analysis, use `dotnet-dump` or platform tools; this skill intentionally collects only GC dumps.
- Use `dotrush-profile-cpu` for high CPU, hot paths, or latency traces.
