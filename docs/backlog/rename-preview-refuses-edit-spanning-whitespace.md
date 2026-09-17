---
worth: later
where: plugins/dotrush/tools/DotRushCli/RenameCommand.cs:285
added: 2026-09-17
---
# rename preview refuses an edit that spans two identifiers

`UnsupportedEdit` widens each edit to its enclosing identifier. If Roslyn's minimal diff merges changes in two
renamed tokens separated only by whitespace into one edit, the span contains a non-identifier character and the
preview refuses with the "not supported" message. Nothing is written wrongly. Unknown that settles it: whether
DotRush ever emits such a merged edit for a rename — worth fixing only if a real rename hits it.
