---
worth: yes
where: plugins/dotrush/scripts/dotrush-profile.sh:234
added: 2026-09-17
---
# `ps trace` rows cannot be told apart

`ps` passes through `dotnet-trace ps`, which prints pid, name and path only. Under `dotnet test` the dxvcs session
got 18 identical `dotnet  /usr/local/share/dotnet/dotnet` rows, so the CPU skill's "select only an unambiguous
process, otherwise ask the user" has no useful branch: the user cannot tell them apart either. The session fell
back to `ps -Ao pid,etime,command | grep testhost.dll` (FINDINGS F3).

Proposed: add the command line (at least its tail), the main assembly and elapsed time to each row, and a
`--filter <substring>`.
