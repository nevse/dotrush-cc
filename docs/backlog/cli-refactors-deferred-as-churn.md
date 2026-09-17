---
worth: no
where: plugins/dotrush/tools/DotRushCli/WorkspaceEditApplier.cs
added: 2026-09-17
---
# CLI refactors deferred as churn without behaviour change

The 0.7.0 reviews suggested these, and the fixers deliberately left them: making `WorkspaceEditApplier`,
`RenameCommand.Run` and `LspChannel.WriteLines` internal with `InternalsVisibleTo` (they are public only for
tests); folding the CRLF/line-splitting code paths in `SourceDocument` into one helper; replacing the diff
builder's double hunk merge; restructuring the `request` argument parser; replacing `SkillCommandTests`'
shell-line parser. Not done: each is covered by passing tests and changes no behaviour, so the churn outweighs the
value. Revisit only when that code is being changed anyway.
