---
name: dotrush-profile-cpu
description: Profile CPU usage, hot paths, throughput, or latency in a running .NET application, or in a short-lived .NET program or test it launches, with dotnet-trace, then interpret the trace report. Use for high CPU, slow requests, regressions, flame graphs, profiling a benchmark or a single test, or when the user asks to attach a trace profiler. Do not use for managed-memory growth or leak analysis.
---

# Profile .NET CPU usage

Collect a bounded sampling trace from the exact .NET process while a representative workload runs, then explain the evidence rather than guessing from source alone.

Use the plugin helper at `${CLAUDE_PLUGIN_ROOT}/scripts/dotrush-profile.sh`. It runs DotRush's own build of `dotnet-trace` at the DotRush ref the plugin pins, installed exactly as the plugin's DotRush language server is: downloaded from that DotRush release when it ships the bundles, otherwise built once from DotRush source, which needs `git` and a .NET SDK and takes a few minutes. It keeps the tools in `${CLAUDE_PLUGIN_DATA}` (or the user cache) and runs them as `dotnet dotnet-trace.dll`, so a .NET runtime must be on `PATH` or in `DOTNET_ROOT`. `DOTRUSH_DIAGNOSTICS_DIR` points it at a ready directory of the tools instead.

Run `"${CLAUDE_PLUGIN_ROOT}/scripts/dotrush-profile.sh" tools` first; it installs nothing. Its `pinned:` line says whether the tools come from a release or a build from source, and its `diagnostics:` line whether they are installed. If they are not installed, or not at the pin, tell the user what the first profiling command will do (download the release bundle, or build the tools from source for a few minutes) and honor any runtime approval prompt; give that command a timeout long enough for a build. If a build fails, report the log tail it prints and the log path.

## Workflow

1. **Launch instead of attaching when the workload is short-lived or can be started on demand** — a microbenchmark, a console tool, a single test. The helper starts the command suspended, traces it from startup, and stops when it exits or the duration ends (at the duration it kills the command):

   ```bash
   "${CLAUDE_PLUGIN_ROOT}/scripts/dotrush-profile.sh" trace --launch 00:02:00 [OUTPUT_DIR] -- <COMMAND> [ARGS...]
   ```

   The command must itself be the .NET process doing the work: `dotnet bin/Release/net10.0/App.dll`, `dotnet exec …`, or the app's own executable. The helper refuses `dotnet test`, `dotnet run` and other SDK commands, because every .NET process they start inherits the suspended diagnostic port and hangs. For a single test, run a test project that builds to an executable (Microsoft.Testing.Platform: `EnableNUnitRunner`, `EnableMSTestRunner`, xUnit v3) directly with its filter, for example `-- tests/App.Tests/bin/Release/net10.0/App.Tests --filter "Name=Spin"` for NUnit; build it in Release first. A VSTest-only test project has no such entry point: attach to its `testhost` instead (step 2), or ask the user. Choose a duration that covers the whole run, since hitting it kills the command mid-way; the command's output goes to stderr, and `EXIT=` on stdout is its exit code (a failing test still leaves a usable trace; `0` also follows a kill at the duration). Then continue at step 4 with the printed artifacts. The capture includes runtime startup and JIT, so make the measured loop long enough to dominate them.

2. Otherwise, if no PID was supplied, discover attachable processes:

   ```bash
   "${CLAUDE_PLUGIN_ROOT}/scripts/dotrush-profile.sh" ps trace [--filter <TEXT>]
   ```

   Each row has `PID`, `ELAPSED` (time since the process started), `NAME`, `ASSEMBLY` (the first `.dll`/`.exe` argument, so processes started through the `dotnet` host differ) and `COMMAND` (the command line, its tail when long). `--filter` keeps rows whose name, path or command line contains the text, ignoring case — for example `--filter testhost` or the project name; with no match it exits 1 and says how many processes were listed. Tell processes apart by assembly and arguments, and by `ELAPSED` when one was just started. Select only an unambiguous process. Otherwise ask the user, showing those columns.

3. Prefer a warmed-up Release build for meaningful measurements. Note when the target is a Debug build, still warming up, idle, or sharing the machine with noisy workloads.

   Collect a 30-second trace by default. Keep collection at two minutes or less unless the user explicitly asks for longer. Always write the duration as `hh:mm:ss` with `hh` 00-23 — never abbreviate to `mm:ss` (`dotnet-trace` reads `00:30` as `hh:mm`, 30 minutes) and never carry hours past 23 (it reads `24:00:00` as `dd:hh:mm`, 24 days). The helper rejects both. For a day or more, use `dd:hh:mm:ss`. For a production, remote, containerized, or otherwise sensitive target, state the expected interval and confirm the target before attaching.

   ```bash
   "${CLAUDE_PLUGIN_ROOT}/scripts/dotrush-profile.sh" trace <PID> 00:00:30 [OUTPUT_DIR]
   ```

   Reproduce the slow operation during that interval when it is within scope. Start collection just before the workload and keep idle time outside the capture where practical. The helper writes `.nettrace`, `.speedscope.json`, and `.top30.txt` artifacts and prints their absolute paths. The text report opens with `Runtime` (the target's .NET version, `unknown` without the `.nettrace`), `Threads`, `WallClockDuration` (the capture window) and the thread-summed `SampledThreadTime`, `ManagedSampledTime` and `UnmanagedOrBlockedTime`, then gives a thread table (stack changes, weight and top function per thread) and exclusive and inclusive managed-CPU rankings.

4. Read the text report. If a different list size is useful, or one thread should be ranked alone, run the command below; `THREAD_ID` is the number in a `Thread (<id>)` row of the thread table, and without it every thread is ranked:

   ```bash
   "${CLAUDE_PLUGIN_ROOT}/scripts/dotrush-profile.sh" trace-report <TRACE.nettrace> 50 [THREAD_ID]
   ```

   **If the header has a `Warning` line and `ManagedOnStackTime`, no sample was tagged as running managed code.** Either the target was blocked or in native code for the whole capture, or its runtime tags every sample as unmanaged even in a managed loop — .NET 9 and 10.0.0–10.0.3 do on macOS arm64 (8.0 and 10.0.4+ do not). The rankings then measure time on stack, and a thread parked in a wait counts as fully as a working one, so on a service most of the total is idle threads. In that case:

   - Find the working threads in the thread table: many stack changes and a top function that is not a wait (`Monitor.Wait`, `WaitHandle.Wait*`, `LowLevelLifoSemaphore.WaitForSignal`, `ManualResetEventSlim.Wait`, `SocketPal.Poll`, `Thread.Sleep`, `?!?`). Rank each with `trace-report <TRACE.nettrace> 50 <THREAD_ID>`, and use a count large enough to get past the framework frames that sit at 100% inclusive.
   - Report the numbers as on-stack time of that thread, not CPU, and name the `Runtime` line. For CPU-only numbers, suggest running the target on a runtime that tags samples, when that is possible.
   - If every thread sits in a wait, the capture was idle: say so and re-capture while the workload runs rather than diagnosing the waits.

   **To see who calls a hot function and where its time goes, focus on it** instead of piecing a chain together from the inclusive list:

   ```bash
   "${CLAUDE_PLUGIN_ROOT}/scripts/dotrush-profile.sh" trace-report <TRACE.nettrace> 20 [THREAD_ID] --focus <FUNCTION> [--depth 8]
   ```

   `FUNCTION` is a name as a ranking row prints it, or its end (`GetReference`, `Sheet.GetReference`), or any part of it. A name that matches several functions is refused with the candidates and their weights; pass one of them as printed. The report keeps its header, then gives `FocusInclusive` and `FocusExclusive` and two indented trees, one level per two spaces: callers of the function going up, and what it calls going down, with `(self)` as its own time at each level. `Percent` is a share of the whole measure (as in the rankings), `OfFocus` a share of the focus function's time; the count caps rows per level, and rows under 1% of the function are folded into `(N more)`. Follow the heaviest callee path down to the frame that holds the time, and name the caller that matters when a function is hot only under one of them.

   **Add events to a capture only when the user asks for them** or another tool will read the `.nettrace` (PerfView, Visual Studio). `trace` and `trace --launch` take, anywhere before `--`:

   ```bash
   "${CLAUDE_PLUGIN_ROOT}/scripts/dotrush-profile.sh" trace <PID> 00:00:30 --profile gc-verbose [--buffersize 512]
   ```

   `--profile` takes comma-separated `dotnet-trace` profiles (`gc-verbose` for GC and allocation-sampling events, `gc-collect`, `database`; `dotnet-common,dotnet-sampled-thread-time` is the default pair), `--providers` extra EventPipe providers in `dotnet-trace`'s syntax without spaces, and `--buffersize` the buffer in MB (256 by default; raise it if `dotnet-trace` reports dropped events). The helper always keeps `dotnet-sampled-thread-time`, since the report is built from it. `cpu-sampling` and `thread-time` are Linux kernel profiles for `collect-linux` and are refused. The text report still ranks CPU only: it does not count allocations. Extra events add stacks between samples, so a thread with few stack changes but a large weight (often the finalizer under `gc-verbose`) is gap time, not work. Do not enable `System.Threading.Tasks.TplEventSource` for a CPU question: the report then prints a `Warning` because task stacks are stitched across threads.

5. **Compare two captures for a before/after question** — a fix, a regression, a configuration change. Capture both the same way (same workload, duration and build configuration; `trace --launch` makes that easy), then:

   ```bash
   "${CLAUDE_PLUGIN_ROOT}/scripts/dotrush-profile.sh" trace-diff <BASELINE.nettrace> <CURRENT.nettrace> 30 [BASELINE_THREAD_ID CURRENT_THREAD_ID]
   ```

   It ranks functions by `Change`: the current share minus the baseline share of each capture's own total, in percentage points, since two captures differ in length and thread count; `BaselineWeight` and `CurrentWeight` are the raw times. The header gives both totals (`ManagedSampledTime 1200.00 -> 300.00`) and both `Runtime`s. A negative change means the function takes a smaller part of the work, not necessarily less time: when the whole run got faster, read the totals and weights too, and report the drop in the total as the headline. If either capture has no managed-tagged sample, the header has a `Warning` and both are compared by time on stack (see step 4); then compare one working thread from each capture by passing both thread ids, since thread ids differ between processes.

6. Report the PID or launched command, workload, duration, build/configuration caveats, artifact paths, and the strongest findings. Distinguish exclusive hot methods from inclusive callers. Prioritize application frames; runtime initialization, EventSource setup, terminal I/O, and waiting frames can be measurement noise. If they dominate, verify that the workload actually overlapped the capture and repeat once after warm-up rather than diagnosing the noise. Sampling shows where sampled CPU stacks spend time; it does not by itself prove wall-clock latency, allocation volume, or causality. Async state-machine frames such as `MoveNext` should be mapped back to their owning method when possible.

   **Expect inlining, and say so rather than reporting the caller as the hot method.** Step 2 asks for a Release build, and the JIT inlines small methods into their callers there, so their frames do not exist in the trace at all — their time is attributed to the caller. The signature is a method with high *exclusive* time and few or no callees beneath it, often something as coarse as `Program.Main()` or a request handler. Reporting "`Main` is hot" is true and useless. When you see it:

   - Say in the report that the hot frame is an inlining root and name the callees it likely absorbed, read from the source rather than the trace.
   - To get the real attribution, re-run the target with `DOTNET_TieredCompilation=0` and `DOTNET_JitNoInline=1` set in its environment, and say that this itself changes performance — it is a diagnostic run for attribution, not a measurement of production behaviour.
   - Never conclude that a large method needs optimizing just because it absorbed its callees' samples.

   `WallClockDuration` is the capture window. `SampledThreadTime`, `ManagedSampledTime` and `UnmanagedOrBlockedTime` are summed across the `Threads` count, so on a multi-threaded target they exceed the wall clock and must not be reported as elapsed time. Percentages are shares of `ManagedSampledTime`, or of `ManagedOnStackTime` when that line is present. Method names print without their IL parameter lists unless two rows would otherwise read identically.

## Boundaries

- On Linux and macOS the profiler must normally run as the same user and with the same `TMPDIR` as the target process.
- If the target is absent from `ps` or attach fails despite the correct user, check sandbox, container, PID-namespace, and diagnostic-socket boundaries. Run the target and profiler in the same boundary; do not substitute an unrelated host PID.
- Do not upload a trace to speedscope.app or any other external service without explicit permission. Traces can expose assembly, namespace, type, and method names.
- Use `dotrush-profile-memory` for GC heap growth or suspected managed-memory leaks.
