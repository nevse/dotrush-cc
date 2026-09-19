---
name: dotrush-profile-allocations
description: Find what a running .NET application, or a short-lived .NET program or test it launches, allocates and where from, with dotnet-trace's allocation sampling (GCAllocationTick), reporting allocated bytes by type, by allocating function and by call path. Use for allocation rate, GC pressure, "what allocates", "who allocates this type", frequent gen0 collections, or reducing allocations in a hot loop. Do not use for what the heap retains or leaks (use dotrush-profile-memory) or for CPU hot paths (use dotrush-profile-cpu).
---

# Profile .NET allocations

Capture a bounded trace with allocation sampling from the exact .NET process while a representative workload runs, then report which types were allocated, which functions allocated them and through which callers — the evidence behind a claim about allocations, rather than a guess from source.

Use the plugin helper at `${CLAUDE_PLUGIN_ROOT}/scripts/dotrush-profile.sh`. It runs DotRush's own build of `dotnet-trace` at the DotRush ref the plugin pins, installed exactly as the plugin's DotRush language server is: downloaded from that DotRush release when it ships the bundles, otherwise built once from DotRush source, which needs `git` and a .NET SDK and takes a few minutes. It keeps the tools in `${CLAUDE_PLUGIN_DATA}` (or the user cache) and runs them as `dotnet dotnet-trace.dll`, so a .NET runtime must be on `PATH` or in `DOTNET_ROOT`.

Run `"${CLAUDE_PLUGIN_ROOT}/scripts/dotrush-profile.sh" tools` first; it installs nothing. Its `pinned:` line says whether the tools come from a release or a build from source, and its `diagnostics:` line whether they are installed. If they are not installed, or not at the pin, tell the user what the first profiling command will do (download the release bundle, or build the tools from source for a few minutes) and honor any runtime approval prompt; give that command a timeout long enough for a build. If the `diagnostics:` line shows `trace-json=no`, `alloc-report` refuses to run: report that the pinned DotRush's `dotnet-trace` lacks `--format Json`. If a build fails, report the log tail it prints and the log path. Do not substitute another `dotnet-trace`.

## How the numbers are made

The `gc-verbose` profile makes the runtime raise a `GCAllocationTick` event about every 100 KB a thread allocates, carrying the type of the object that crossed the threshold, its kind (`Small`, `Large` for the large object heap, `Pinned`) and the stack. The whole 100 KB is charged to that one object. Every figure is therefore a sampled estimate: types and functions allocating a lot are measured well, a row resting on a handful of samples is noise, and object counts are not known at all. Say so when you report, and quote the `Samples` column for any row you lean on.

## Workflow

1. **Launch instead of attaching when the workload is short-lived or can be started on demand** — a microbenchmark, a console tool, a single test:

   ```bash
   "${CLAUDE_PLUGIN_ROOT}/scripts/dotrush-profile.sh" trace --launch 00:02:00 [OUTPUT_DIR] --profile gc-verbose -- <COMMAND> [ARGS...]
   ```

   The command must itself be the .NET process doing the work: `dotnet bin/Release/net10.0/App.dll`, `dotnet exec …`, or the app's own executable. The helper refuses `dotnet test`, `dotnet run` and other SDK commands, because every .NET process they start inherits the suspended diagnostic port and hangs. For a single test, run a test project that builds to an executable (Microsoft.Testing.Platform) directly with its filter, built in Release. Choose a duration that covers the whole run, since hitting it kills the command. `EXIT=` on stdout is its exit code. The capture includes startup, whose allocations (EventSource setup, reflection, JIT) show up as their own rows; make the measured loop long enough to dominate them.

2. Otherwise, if no PID was supplied, discover attachable processes:

   ```bash
   "${CLAUDE_PLUGIN_ROOT}/scripts/dotrush-profile.sh" ps trace [--filter <TEXT>]
   ```

   Rows show `PID`, `ELAPSED`, `NAME`, `ASSEMBLY` and `COMMAND`; `--filter` keeps rows containing the text, ignoring case. Select only an unambiguous process. Otherwise ask the user, showing those columns. Then capture while the workload runs, 30 seconds by default and two minutes at most unless the user asks for longer:

   ```bash
   "${CLAUDE_PLUGIN_ROOT}/scripts/dotrush-profile.sh" trace <PID> 00:00:30 [OUTPUT_DIR] --profile gc-verbose
   ```

   Always write the duration as `hh:mm:ss` with `hh` 00-23 (`00:30` would be 30 minutes); the helper rejects anything else. `gc-verbose` adds an event per ~100 KB allocated and per GC, so on a process allocating gigabytes a second the capture grows by tens of MB per second and the target slows down measurably; for a production or latency-sensitive target, state that and confirm before attaching. Raise `--buffersize` (MB, 256 by default) if `dotnet-trace` reports dropped events.

   Both forms print `TRACE=` with the `.nettrace` path. They also write the CPU report of the same capture, which `dotrush-profile-cpu` explains; ignore it unless CPU is part of the question.

3. Report the allocations:

   ```bash
   "${CLAUDE_PLUGIN_ROOT}/scripts/dotrush-profile.sh" alloc-report <TRACE.nettrace> [COUNT]
   ```

   The first run converts the trace into a `.nettrace.json` beside it; later runs read that file. The header gives `Runtime`, `AllocationSamples`, `AllocatedMB`, and, when the capture has thread timelines, `WallClockDuration` and `AllocationRate` (MB per second of the capture window, startup included for a launch). Three rankings follow, each with `Percent` of `AllocatedMB`, `MB` and `Samples`:

   - **Types** — what was allocated. A `Large` row is the large object heap: objects of 85 000 bytes or more, collected only with gen2.
   - **Functions by exclusive MB** — the frame that allocated: the method whose own code made the object, often a framework method such as `StringBuilder.ToString()` or `List.Grow`. `[no managed frame]` collects ticks with no managed stack.
   - **Functions by inclusive MB** — everything allocated under a function, callees included. This is where the application's own methods appear when the allocating frame is in the framework.

4. **Follow one row down to its cause with `--focus`**:

   ```bash
   "${CLAUDE_PLUGIN_ROOT}/scripts/dotrush-profile.sh" alloc-report <TRACE.nettrace> 20 --focus <FUNCTION_OR_TYPE> [--depth 8]
   ```

   - A **type** (`System.String (Small)`, or `System.String` when only one kind was allocated) prints who allocates it: its callers, one level per two spaces, nearest first. Use it for "who allocates all these strings".
   - A **function** prints its callers and, below it, its callees down to the types they allocate, which are the leaves of that tree.

   Names resolve as in `trace-report --focus`: exactly as a row prints it, or its end (`Method`, `Type.Method`), or any part of it. A name matching several functions or types is refused with the candidates; pass one of them as printed. `OfFocus` is a share of the focus row, and rows under 1% of it are folded into `(N more)`.

5. **Compare before and after a fix** by capturing both the same way (same workload, duration and build; `trace --launch` makes that easy) and running `alloc-report` on each. Allocation volume depends on how much work ran, so compare per unit of work when the workload is fixed (a launch of the same benchmark: `AllocatedMB`), or `AllocationRate` over the same kind of window, and name which you used. Lead with the change in the total, then the types and functions that account for it.

6. Report the PID or launched command, workload, duration, artifact paths, `AllocatedMB` and `AllocationRate`, and the rows that explain most of the volume, each with the call path that leads to it from application code. Map framework frames back to the application method that calls them. Name the concrete allocation — a `StringBuilder` per iteration, a boxed struct, a closure, an array resized in a loop, a LINQ enumerator — and read the source to confirm it before proposing a change.

   **Expect inlining.** In a Release build the JIT inlines small methods, so an allocation made by an inlined callee is charged to its caller: a `Char[]` charged to your method may really come from an inlined `StringBuilder.Append`. When the allocating frame cannot allocate that type by itself, say so and name the likely inlined callee from the source. For exact attribution, re-run with `DOTNET_TieredCompilation=0` and `DOTNET_JitNoInline=1` in the target's environment, noting that this changes performance.

## Boundaries

- Allocation sampling says what was allocated, not what survives. For heap growth, retained objects or leaks, use `dotrush-profile-memory`. A high allocation rate with a flat heap is GC pressure, not a leak.
- It measures managed allocations only; native memory is invisible to it.
- On Linux and macOS the profiler must normally run as the same user and with the same `TMPDIR` as the target process.
- Do not upload a trace to speedscope.app or any other external service without explicit permission. Traces expose assembly, namespace, type and method names. The `.nettrace.json` opens in speedscope with an `Allocations` profile, should the user want a flame graph locally.
