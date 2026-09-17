---
worth: yes
where: plugins/dotrush/bin/lsp-proxy.py:61
added: 2026-09-17
---
# Claude Code background jobs in one agterm tab share a session dir

Background jobs (`claude bg-spare`) inherit `AGTERM_SESSION_ID` from the agterm tab they were started from, so
every Claude Code process in that tab keys its runtime dir on the same id. Seen live on 0.7.0 while verifying
rename: two jobs' proxies (pids 86069 and 94663) ran in one `ws/sess-e09ca7ad7a13`. The later proxy overwrote
`pid` and cleared the other's `responses/` and `edits/`, and both read `inject.fifo`: this job's
`textDocument/rename` was answered by the other job's DotRush, while the `didOpen`s after `rename apply` went to
this job's server. It came out right only because both sessions had the same project loaded.

Unlike `sessionless-proxies-share-channel-dirs`, the id is present and still not unique, so it hits every agterm
user who runs background jobs, and it breaks the invariants the request channel relies on (a `responses/` never
recreated, saved rename plans that survive, one reader per FIFO). A likely fix is to add the launching Claude
process (the proxy's parent pid) or a Claude Code session id, if one is exposed, to the key.
