---
paths:
  - "plugins/dotrush/scripts/dotrush-profile.sh"
  - "plugins/dotrush/scripts/summarize-speedscope.py"
  - "plugins/dotrush/scripts/analyze-gcdump.py"
  - "plugins/dotrush/scripts/list-dotnet-processes.py"
  - "plugins/dotrush/skills/dotrush-profile-*/**"
---
# Profiling

- Profiling does not touch the proxy or the session dir; it runs `dotnet-trace`/`dotnet-gcdump` from the
  `diagnostics` component (`install.md`) as `dotnet <tool>.dll`.
- Durations are `hh:mm:ss` or `dd:hh:mm:ss` with every field in range: `dotnet-trace` parses `--duration` with
  `TimeSpan.Parse`, which reads `00:30` as 30 minutes and `24:00:00` as 24 days.
- Artifacts never default into the user's repository: explicit dir, else `$DOTRUSH_PROFILE_OUTPUT_DIR`, else
  `$(dotrush_data_dir)/profiles` (never `CLAUDE_PLUGIN_DATA` directly, see `install.md`).
- `dotnet-gcdump` forces a full gen-2 GC. The memory skill keeps an explicit impact check before attaching to a
  production or latency-sensitive process, and no skill uploads artifacts anywhere.
- A report that needs `--format Json` (`heap-report`, `heap-diff`, `alloc-report`) refuses a pinned build without
  it; `tools` shows `gcdump-json=` and `trace-json=`.
- Allocation figures are `GCAllocationTick` samples, one per ~100 KB; reports and skills call them estimates.
- The CPU report is built from the thread-time sampler, so the script adds `dotnet-sampled-thread-time` to any
  `--profile` that lacks it; keep that when changing trace options.
- Tests: the `Speedscope`, `TraceDiff`, `Focus`, `Allocation`, `GcdumpGraph`, `ProcessList` and `TraceLaunch`
  classes in `tests/test_profile_reports.py`.
