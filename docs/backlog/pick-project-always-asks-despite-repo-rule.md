---
worth: yes
where: plugins/dotrush/skills/dotrush-pick-project/SKILL.md:37
added: 2026-09-17
---
# pick-project must ask even when a repo rule already picks the solution

Step 4 says "Ask the user (required) ... Never guess — always ask". dxvcs's `CLAUDE.md` maps work areas to
solutions, so the session followed the repo rule and knowingly broke the skill (FINDINGS F6).

Proposed: if a project or user instruction already determines the target, apply it and say which rule chose it;
ask only otherwise.
