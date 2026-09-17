---
worth: yes
where: plugins/dotrush/bin/lsp-proxy.py:360
added: 2026-09-17
---
# Proxy injector thread dies on a deeply nested JSON line

`inject_lines` catches only `json.JSONDecodeError`. A pathologically nested line written to `inject.fifo` raises
`RecursionError` and ends the injector thread, so every later injection (diagnostics, pick-project, the request
channel) is silently ignored until the proxy restarts. Catch `(ValueError, RecursionError)` and log it like a bad
line. Pre-existing before 0.7.0, but the injector now lives for the whole session, which makes the loss permanent.
