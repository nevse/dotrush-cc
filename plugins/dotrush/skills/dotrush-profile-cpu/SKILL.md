---
name: dotrush-profile-cpu
description: Profile CPU usage, hot paths, throughput, or latency in a running .NET application with dotnet-trace, then interpret the trace report. Use for high CPU, slow requests, regressions, flame graphs, or when the user asks to attach a trace profiler. Do not use for managed-memory growth or leak analysis.
---

# Profile .NET CPU usage

Collect a bounded sampling trace from the exact .NET process while a representative workload runs, then explain the evidence rather than guessing from source alone.

Use the plugin helper at `${CLAUDE_PLUGIN_ROOT}/scripts/dotrush-profile.sh`. It prefers globally installed tools and otherwise installs the `dotnet-trace` NuGet tool lazily under `${CLAUDE_PLUGIN_DATA}` (or the user cache when that variable is unavailable). The install runs from the tool directory, so a repository's own `NuGet.Config` cannot redirect it; the user's and machine's NuGet configuration still decides the feed.

Before the first lazy installation, tell the user that the helper will download a NuGet tool package from their configured feed and honor any runtime approval prompt.

## Workflow

1. If no PID was supplied, discover attachable processes:

   ```bash
   "${CLAUDE_PLUGIN_ROOT}/scripts/dotrush-profile.sh" ps trace
   ```

   Select only an unambiguous process. Otherwise ask the user.

2. Prefer a warmed-up Release build for meaningful measurements. Note when the target is a Debug build, still warming up, idle, or sharing the machine with noisy workloads.

3. Collect a 30-second trace by default. Keep collection at two minutes or less unless the user explicitly asks for longer. Always write the duration as `hh:mm:ss` with `hh` 00-23 — never abbreviate to `mm:ss` (`dotnet-trace` reads `00:30` as `hh:mm`, 30 minutes) and never carry hours past 23 (it reads `24:00:00` as `dd:hh:mm`, 24 days). The helper rejects both. For a day or more, use `dd:hh:mm:ss`. For a production, remote, containerized, or otherwise sensitive target, state the expected interval and confirm the target before attaching.

   ```bash
   "${CLAUDE_PLUGIN_ROOT}/scripts/dotrush-profile.sh" trace <PID> 00:00:30 [OUTPUT_DIR]
   ```

   Reproduce the slow operation during that interval when it is within scope. Start collection just before the workload and keep idle time outside the capture where practical. The helper writes `.nettrace`, `.speedscope.json`, and `.top30.txt` artifacts and prints their absolute paths. The text report contains both exclusive and inclusive managed-CPU rankings plus the unmanaged-or-blocked interval.

4. Read the text report. If a different list size is useful, run:

   ```bash
   "${CLAUDE_PLUGIN_ROOT}/scripts/dotrush-profile.sh" trace-report <TRACE.nettrace> 50
   ```

5. Report the PID, command/workload, duration, build/configuration caveats, artifact paths, and the strongest findings. Distinguish exclusive hot methods from inclusive callers. Prioritize application frames; runtime initialization, EventSource setup, terminal I/O, and waiting frames can be measurement noise. If they dominate, verify that the workload actually overlapped the capture and repeat once after warm-up rather than diagnosing the noise. Sampling shows where sampled CPU stacks spend time; it does not by itself prove wall-clock latency, allocation volume, or causality. Async state-machine frames such as `MoveNext` should be mapped back to their owning method when possible.

## Boundaries

- On Linux and macOS the profiler must normally run as the same user and with the same `TMPDIR` as the target process.
- If the target is absent from `ps` or attach fails despite the correct user, check sandbox, container, PID-namespace, and diagnostic-socket boundaries. Run the target and profiler in the same boundary; do not substitute an unrelated host PID.
- Do not upload a trace to speedscope.app or any other external service without explicit permission. Traces can expose assembly, namespace, type, and method names.
- Use `dotrush-profile-memory` for GC heap growth or suspected managed-memory leaks.
