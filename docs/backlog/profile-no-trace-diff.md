---
worth: yes
where: plugins/dotrush/scripts/dotrush-profile.sh:331
added: 2026-09-17
---
# No `trace-diff` to compare two CPU captures

`heap-diff` compares two gcdumps; traces have nothing like it. The dxvcs #46984 session was a before/after fix
and compared two `.top30.txt` reports by eye (usage-scenario FINDINGS, feature requests).

Proposed: `trace-diff <baseline> <current> [count] [thread-id]` ranking functions by change in exclusive and
inclusive time, each as a share of its own capture's total, since two captures differ in length and thread
count. It should use the same mode on both sides: if either capture has no managed-tagged sample, compare
on-stack time and say so.
