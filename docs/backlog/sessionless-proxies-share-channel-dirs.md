---
worth: later
where: plugins/dotrush/bin/lsp-proxy.py:61
added: 2026-09-17
---
# Sessions without a session id share one runtime dir

Without `DOTRUSH_SESSION_ID`/`AGTERM_SESSION_ID`, the runtime dir is keyed on the workspace path, so two Claude
sessions in the same folder share it: the second proxy clears the first one's `responses/` and `edits/`, and both
proxies read the same `inject.fifo`. The README documents the shared-dir fallback; the request channel makes the
collision more harmful. Unknown that settles it: whether session-less setups (headless/CI, other terminals) ever
run two sessions on one folder at once.
