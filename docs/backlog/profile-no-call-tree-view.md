---
worth: later
where: plugins/dotrush/scripts/dotrush-profile.sh:273
added: 2026-09-17
---
# CPU report has flat rankings only, no call tree

`trace-report` prints flat exclusive and inclusive rankings. It cannot show who calls a hot frame or what sits
under it. In the dxvcs session, `ToValue` 85% -> `GetReference` 81% -> `WriteSheetName` -> `IsSheetNameIdent`
30% had to be pieced together from the flat inclusive list (usage-scenario FINDINGS, feature requests). The
flat per-frame view `agg.py` gave is covered since 0.7.1 by `trace-report <trace> <count> <thread-id>`.

Unknown that settles it: whether a text call tree helps an agent more than the flat list plus the source code.
A useful shape would be a tree under one function (`--focus <name>`) or a caller list for it, pruned by share
and depth to stay readable.
