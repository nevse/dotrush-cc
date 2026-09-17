---
worth: yes
where: plugins/dotrush/scripts/dotrush-profile.sh:14
added: 2026-09-17
---
# `trace` can only attach, not launch the target

`dotrush-profile.sh trace <pid> ...` attaches only, though `dotnet-trace collect` can start a child process
(`-- <command>`). Anything short-lived (a microbenchmark, a single test) cannot be profiled. In the dxvcs #46984
session (usage-scenario archive, FINDINGS F2) profiling one NUnit test took five steps: a 75 s `[Explicit]` loop,
`nohup dotnet test &`, `sleep 25`, `ps | grep testhost.dll`, then a 30 s attach timed to land inside the loop.

Proposed: `trace --launch [duration] [output-dir] -- <command...>`. Makes the `ps` problem
([[profile-ps-rows-indistinguishable]]) moot for this case. `heap` would want the same mode.
