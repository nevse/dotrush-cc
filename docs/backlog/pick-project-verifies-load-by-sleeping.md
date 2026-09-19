---
worth: later
where: plugins/dotrush/skills/dotrush-pick-project/SKILL.md:64
added: 2026-09-17
---
# pick-project verifies the load by sleeping

Step 6 says "wait a few seconds (large solutions take longer), then run documentSymbol". The dxvcs session got
symbols on the first try by luck (FINDINGS F8). The report suggested polling `load-completed`, but that marker
is written on the first load only, so after a project switch it already exists and proves nothing.

Unknown that settles it: whether DotRush sends a per-load signal (`projectLoaded` shows up in `proxy.log`) the
proxy could turn into a marker or a CLI `status` to wait on. Without one, the sleep stays.
