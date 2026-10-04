---
worth: yes
added: 2026-10-04
---
# DotRush: definition into metadata opens the decompiled file at 1:1

Upstream [DotRush#212](https://github.com/JaneySprings/DotRush/issues/212), open. Still there at the `6cbf7c0` pin:
`textDocument/definition` on `new[] { 1, 2 }.FirstOrDefault()` returns
`server/_decompiled_/<Project>/System.Linq/System.Linq.Enumerable.cs:1:1`; roslyn-language-server 5.12 returns
`Enumerable.cs:9242:28`, on the method. Claude lands at the top of a file of thousands of lines and has to search
it. Recheck on every pin move; when fixed, add a line to the plugin README's pin note.
