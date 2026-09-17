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
bytes and object counts by type and by allocating stack. Needs a provider choice in `trace` first
([[profile-trace-providers-fixed]]). Also check what DotRush's bundled `dotnet-trace` and TraceEvent convert
`AllocationTick` into.
