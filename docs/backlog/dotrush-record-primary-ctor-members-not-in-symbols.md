---
worth: later
added: 2026-10-04
---
# DotRush: record primary-constructor properties are not document symbols

`record BudgetDay(DateOnly Date, decimal Spent)` lists no children: `DocumentSymbolHandler` never walks a record's
`ParameterList`. roslyn-language-server does not list them either, so it is not a regression against the reference
server. Unknown that settles it: whether Claude ever needs those positions from documentSymbol rather than from
hover or workspace symbols; so far only the smoke test noticed.
