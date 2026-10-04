---
worth: later
added: 2026-10-04
---
# DotRush: document symbols carry no detail

DotRush leaves `DocumentSymbol.detail` empty; roslyn-language-server fills it with the type or signature
(`Changed : EventHandler?`, `First<T>(IEnumerable<T>) : List<T>`, `Value : T?`). Unknown that settles it: whether
Claude Code's LSP tool shows `detail` in its documentSymbol output at all. If it does, the types would spare a
hover per member; if not, there is nothing to gain.
