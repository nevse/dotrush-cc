---
worth: yes
added: 2026-10-04
---
# DotRush: document symbols drop type parameters

`textDocument/documentSymbol` names `class Result<T>` as `Result`, so a file with `Result` and `Result<T>` lists two
identical `Result` entries, and `List<T> First<T>(IEnumerable<T>)` as `First(IEnumerable<T>)`. roslyn-language-server
gives `Result<T>` and `First<T>(IEnumerable<T>)`. Seen at the `6cbf7c0` pin on ExpenseTracker's `Result.cs`.

Cause upstream: `DocumentSymbolHandler.cs` takes `Identifier.Text` for types and methods, and `GetFormattedName`
appends only parameter types, never `TypeParameterList` (type, method, delegate). A few-line fix there plus a case
in `DocumentSymbolHandlerTests`; not yet reported to DotRush.
