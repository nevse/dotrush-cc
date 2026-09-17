---
worth: yes
added: 2026-09-17
---
# Verify semantic rename in a live Claude Code session

0.7.0 was verified by unit tests and E2E tests against the installed DotRush server, never through Claude Code
itself. After `claude plugin marketplace update dotrush-cc`, `claude plugin update dotrush@dotrush-cc` and a
restart, rename a symbol in a real solution through `dotrush-rename`: confirm the first use builds the CLI and
later uses do not, that Claude Code's LSP results and `new-diagnostics` show the new name, and that editing a
renamed file prompts a re-read. Also run `dotrush-diagnostics` in a repo whose `global.json` pins an older SDK.
From the plan's Post-Completion list.
