---
worth: yes
where: tests/DotRushCli.Tests/SkillCommandTests.cs:9
added: 2026-09-17
---
# SKILL.md error strings are not checked against the CLI

`skills/dotrush-rename/SKILL.md` quotes about twenty CLI error messages verbatim so Claude can match them, and the
diagnostics and pick-project skills quote a few more. `SkillCommandTests` checks only subcommands and flags, so a
reworded message in `Session.cs`, `RenameCommand.cs`, `LspChannel.cs` or `WorkspaceEditApplier.cs` drifts silently
and the skill stops recognising the error. Add a string-presence test: every backticked `dotrush-cli: …` message
in a skill must appear in the CLI sources (placeholders like `<file>` as wildcards). Deferred by the 0.7.0 phase 1
fixer as test-only churn.
