---
worth: yes
added: 2026-09-17
---
# No skill answers "what allocates"

`dotrush-profile-memory` sees only survivors (a gcdump forces a full GC) and `dotrush-profile-cpu` measures stack
time, so allocation rate and count by type and call stack, the most common .NET perf question, has no home. In
the dxvcs #46984 session the headline result (1.53 GB -> 732 KB allocated per loop) came from
`GC.GetAllocatedBytesForCurrentThread()` in hand-written probe code (FINDINGS F4).

Proposed: `dotrush-profile-allocations` on `dotnet-trace --profile gc-verbose` (`AllocationTick`), reporting
bytes and object counts by type and by allocating stack. Capturing is done since 0.7.4:
`trace <pid> --profile gc-verbose` records the events with the sampler kept on; only the report is missing.

Blocked on DotRush: at `2026.09` (`JaneySprings/diagnostics@89a1406`) `dotnet-trace convert` and `report` build
only thread-time stacks (`SampleProfilerThreadTimeComputer`, `IncludeEventSourceEvents = false`), so every
`AllocationTick` is dropped. Requested an allocation output format upstream:
https://github.com/JaneySprings/DotRush/issues/206. Before starting, check whether it is closed and which ref
ships it, then bump `dotrush-version.json`. If it stalls, the fallback is reading the nettrace in `DotRushCli`
through the `Microsoft.Diagnostics.Tracing.TraceEvent` package.
