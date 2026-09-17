---
worth: yes
where: plugins/dotrush/skills/dotrush-pick-project/SKILL.md:43
added: 2026-09-17
---
# pick-project hardcodes `restoreProjectsBeforeLoading: true`

Both the `target.json` template and the FIFO payload set it to `true`. On a built worktree, or when a private
NuGet feed is unreachable (dozens of slow restores failing with `NU1900` as error), `false` is the right call;
dxvcs documents that tradeoff and the session used `false`, but the skill never mentions the choice (FINDINGS F7).

Proposed: make it a parameter of steps 5-6 with one sentence on when to use `false`.
