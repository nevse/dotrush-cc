---
worth: yes
where: plugins/dotrush/README.md:286
added: 2026-09-17
---
# README says every request and rename prunes stale responses

The request-channel section says response files older than 10 minutes are removed when a `request` or `rename`
starts. Only `request` and `rename preview` call `StaleFiles.Delete` on `responses/`; `rename apply` sends only
`didOpen` notifications and never prunes. Reword to "a `request` or `rename preview`". Surfaced in the 0.7.0
phase 4 review.
