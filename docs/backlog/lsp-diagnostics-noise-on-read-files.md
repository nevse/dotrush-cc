---
worth: later
added: 2026-09-17
---
# Info-level diagnostics flood every LSP call on read-only files

In the dxvcs session every LSP call returned a `<new-diagnostics>` block for the file it touched, before any
edit: IDE0058, IDE0130, CA1863, CA1305, IDE0040. None actionable in a large repo with its own conventions, and
repeated per file (FINDINGS F9).

Unknown that settles it: whether Claude Code filters by severity itself or offers a setting, and whether the
proxy should drop hints/info from what it forwards. Filtering in the proxy would also change what
`diagnostics.json` holds for `dotrush-diagnostics`, so it may need to filter only the forwarded copy.
