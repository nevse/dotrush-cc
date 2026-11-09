---
name: dotrush-profile-gc
description: Measure how much a running .NET application, or a short-lived .NET program or test it launches, pays for garbage collection, with a low-overhead dotnet-trace GC capture, reporting GC mode (workstation or server, heaps, concurrent), collection counts per generation, pause totals and percentiles, the share of the capture spent paused, and the longest pauses with their reasons. Use for "is GC the problem", GC pauses, time in GC, latency spikes that may be GC, gen2 or induced collections, or comparing workstation and server GC. Do not use for what allocates (use dotrush-profile-allocations), what the heap retains or leaks (use dotrush-profile-memory) or CPU hot paths (use dotrush-profile-cpu).
---

# Profile .NET garbage collections

Capture the GC events of the exact .NET process while a representative workload runs, then report what the collections cost the application: how often each generation ran, how long the runtime held every managed thread suspended, and which collections were the longest. This is the measurement to take before touching allocations or GC settings: a high allocation rate that costs 1% of the time in pauses is not the problem to solve.

Use the plugin helper at `${CLAUDE_PLUGIN_ROOT}/scripts/dotrush-profile.sh`. It runs DotRush's own build of `dotnet-trace` at the DotRush ref the plugin pins, installed exactly as the plugin's DotRush language server is: downloaded from that DotRush release when it ships the bundles, otherwise built once from DotRush source, which needs `git` and a .NET SDK and takes a few minutes. It keeps the tools in `${CLAUDE_PLUGIN_DATA}` (or the user cache) and runs them as `dotnet dotnet-trace.dll`, so a .NET runtime must be on `PATH`, in `DOTNET_ROOT` or in a standard install dir such as `~/.dotnet` or `/usr/local/share/dotnet`.

Run `"${CLAUDE_PLUGIN_ROOT}/scripts/dotrush-profile.sh" tools` first; it installs nothing. Its `pinned:` line says whether the tools come from a release or a build from source, and its `diagnostics:` line whether they are installed. If they are not installed, or not at the pin, tell the user what the first profiling command will do (download the release bundle, or build the tools from source for a few minutes) and honor any runtime approval prompt; give that command a timeout long enough for a build. If the `diagnostics:` line shows `trace-json=no`, `gc-report` refuses to run: report that the pinned DotRush's `dotnet-trace` lacks `--format Json`. If a build fails, report the log tail it prints and the log path. Do not substitute another `dotnet-trace`.

## How the numbers are made

The `gc-collect` profile records only the runtime's GC events — start and end of each collection, heap statistics, suspension and restart — so it costs little even on a busy service. The converter runs TraceEvent's GC analysis over them, the one PerfView's GCStats view shows. Unlike allocation sampling, these are exact per collection:

- **Pause** is the time from the start of the runtime suspension to the end of the restart: every managed thread waited that long. gen0 and gen1 collections are always blocking. A background gen2 (`BackgroundGC`) runs mostly beside the application, so its `Duration` far exceeds its `Pause`; only the pause stopped the application.
- **`PauseTotal` and its share of the capture** is the headline. Many short pauses add up: ten thousand 0.05 ms gen0 pauses cost as much as one 500 ms gen2.
- **`CaptureDuration`** spans the process's first to last event, so a launch includes startup.
- A collection the capture cut off is counted in `Collections` but left out of the pause and heap figures.
- **`GCMode`** comes from the events too. TraceEvent only ever detects a server GC, so a process with no such event is reported as workstation. `concurrent` and `HardLimit` come from the GC settings event that .NET 7 and later raise at the end of a capture; without it the line has neither.

## Workflow

1. **Launch instead of attaching when the workload is short-lived or can be started on demand** — a microbenchmark, a console tool, a single test:

   ```bash
   "${CLAUDE_PLUGIN_ROOT}/scripts/dotrush-profile.sh" trace --launch 00:02:00 [OUTPUT_DIR] --profile gc-collect -- <COMMAND> [ARGS...]
   ```

   The command must itself be the .NET process doing the work: `dotnet bin/Release/net10.0/App.dll`, `dotnet exec …`, or the app's own executable. The helper refuses `dotnet test`, `dotnet run` and other SDK commands, because every .NET process they start inherits the suspended diagnostic port and hangs. Choose a duration that covers the whole run, since hitting it kills the command. `EXIT=` on stdout is its exit code. GC settings such as `DOTNET_gcServer=1` or `DOTNET_GCDynamicAdaptationMode=0` go in the command's environment; the launched process inherits it.

2. Otherwise, if no PID was supplied, discover attachable processes:

   ```bash
   "${CLAUDE_PLUGIN_ROOT}/scripts/dotrush-profile.sh" ps trace [--filter <TEXT>]
   ```

   Select only an unambiguous process; otherwise ask the user, showing the `PID`, `ELAPSED`, `NAME`, `ASSEMBLY` and `COMMAND` columns. Then capture while the workload runs, 30 seconds by default and two minutes at most unless the user asks for longer:

   ```bash
   "${CLAUDE_PLUGIN_ROOT}/scripts/dotrush-profile.sh" trace <PID> 00:00:30 [OUTPUT_DIR] --profile gc-collect
   ```

   Always write the duration as `hh:mm:ss` with `hh` 00-23 (`00:30` would be 30 minutes); the helper rejects anything else. Prefer a warmed-up Release build under its real load: an idle service collects rarely and shows nothing. If allocations are part of the question, capture once with `--profile gc-verbose` instead: the same `.nettrace` then feeds both `gc-report` and `alloc-report` (see `dotrush-profile-allocations`), at the cost of one event per ~100 KB allocated.

   Both forms print `TRACE=` with the `.nettrace` path. They also write the CPU report of the same capture, which `dotrush-profile-cpu` explains; ignore it unless CPU is part of the question.

3. Report the collections:

   ```bash
   "${CLAUDE_PLUGIN_ROOT}/scripts/dotrush-profile.sh" gc-report <TRACE.nettrace> [COUNT]
   ```

   The first run converts the trace into a `.nettrace.json` beside it, the file `alloc-report` reads too. The header gives `Runtime`, `Process`, `GCMode`, `HardLimit` when one is set, `CaptureDuration`, `Collections` (per generation, induced, background, cut off), `PauseTotal` with its share of the capture, pause percentiles, `AllocatedBetweenGCs` with its rate, and the heap size after collections. Three tables follow: by generation, by reason, and the `COUNT` longest pauses with when they happened, their type, reason and heap sizes.

   If it says the file `does not export GC events`, the `.nettrace.json` beside the trace was converted by an older `dotnet-trace`: delete that file and run `gc-report` again. If it says `no garbage collections`, the capture lacks GC events: re-capture with `--profile gc-collect`.

4. Read it in this order:

   - **Is GC a cost at all?** A pause share of a few percent with short percentiles means the time goes elsewhere: say so and turn to `dotrush-profile-cpu`. Do not recommend GC tuning for a cost the capture does not show.
   - **Which generation pays.** Many gen0 collections with tiny pauses point at the allocation rate: follow with `dotrush-profile-allocations`. Frequent gen1 or gen2 collections, or gen2 with large `Promoted MB`, mean objects survive long enough to be promoted: find what holds them with `dotrush-profile-memory`.
   - **Which reasons.** `Induced` is code calling `GC.Collect()` (or `dotnet-gcdump`, which forces one): find the call. `AllocLarge` is large-object-heap allocations (85 000 bytes or more), which can trigger gen2. `LowMemory` and the `OutOfSpace*` reasons mean memory pressure; with a `HardLimit`, a container limit is close.
   - **The longest pauses**, with `At ms`, line up with latency spikes the user saw. A long pause in a `BackgroundGC` is its blocking phases, not its whole `Duration`.
   - **Server GC** collects less often with longer pauses per collection and more memory; workstation the reverse. .NET 9 and later adapt the server GC's heap count to the load (DATAS) unless `DOTNET_GCDynamicAdaptationMode=0` or a fixed `GCHeapCount` turns it off, so its `heaps` figure is the count the trace saw, not the configuration.

5. **Compare before and after a change** — a fix that cuts allocations, server versus workstation, DATAS on or off — by capturing both the same way (same workload, duration and build; `trace --launch` makes that easy) and running `gc-report` on each. Compare `PauseTotal` as a share of the capture and the pause percentiles, not raw collection counts: the server GC runs far fewer collections, each longer, so a smaller count alone is no improvement. Lead with the change in pause share, then the percentiles and the generation that moved.

6. Report the PID or launched command, workload, duration, artifact paths, `GCMode`, `PauseTotal` and its share, the percentiles, and the generations and reasons that account for most of the pause time, then the next step: allocations, retention, or nothing to do about GC. GC settings are the last resort, only after the measurement shows a GC cost that reducing allocations and lifetimes cannot remove.

## Boundaries

- It measures collections, not what was allocated (`dotrush-profile-allocations`) or what is retained (`dotrush-profile-memory`).
- It sees the managed heap only; native memory and a container's OOM kill, which ends the process without an `OutOfMemoryException`, are invisible to it.
- On Linux and macOS the profiler must normally run as the same user and with the same `TMPDIR` as the target process.
- Do not upload a trace to any external service without explicit permission. Traces expose assembly, namespace, type and method names.
