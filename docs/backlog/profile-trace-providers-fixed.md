---
worth: yes
where: plugins/dotrush/scripts/dotrush-profile.sh:257
added: 2026-09-17
---
# `trace` cannot choose the profile, providers or buffer size

`collect` runs with dotnet-trace's defaults (`dotnet-common` + `dotnet-sampled-thread-time`). There is no way to
ask for `gc-verbose`, `cpu-sampling`, a custom `--providers` string or `--buffersize`, which would have answered
the allocation question in the dxvcs session with the same tool (FINDINGS F5).

Proposed: a validated `--profile`/`--providers` passthrough. Note `summarize-speedscope.py` does not handle the
`AWAIT_TIME`/`UNKNOWN_ASYNC` pseudo-frames a TPL-enabled profile produces (0.7.0 review), so enabling other
profiles needs that too.
