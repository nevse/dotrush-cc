---
worth: yes
where: plugins/dotrush/bin/lsp-proxy.py:308
added: 2026-09-17
---
# open_fifo_for_reading leaks the held write fd on error

After the write end is opened, a failure in `os.set_blocking` or `os.fdopen` closes only `read_fd` in the
`except`, so `write_fd` leaks; each injector retry after such an error leaks one more. Neither call fails in
practice, so there is no symptom today. Close `write_fd` too in that handler. Noted in the 0.7.0 iteration 5 review.
