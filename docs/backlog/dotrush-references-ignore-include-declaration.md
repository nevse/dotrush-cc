---
worth: yes
added: 2026-10-04
---
# DotRush: references ignore includeDeclaration

`textDocument/references` with `context.includeDeclaration: true` leaves the declaration out: an event with two
uses gives 2 locations where roslyn-language-server gives 3; `IsBusy` in ExpenseTracker gives 81 against 82.
Claude Code's findReferences then never shows where the symbol is declared. Seen at the `6cbf7c0` pin; not yet
reported to DotRush.
