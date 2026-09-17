---
worth: later
where: plugins/dotrush/scripts/dotrush-cli.sh:65
added: 2026-09-17
---
# CLI build prune can remove another version's build just before it runs

A build prunes `cli/<hash>` dirs untouched for 24 h. A session on another plugin version that has not used its
CLI for a day can check its build exists and have it pruned before its `touch` and `exec`, so that one call fails;
the next call rebuilds. Unknown that settles it: whether two plugin versions ever run side by side long enough for
this to happen outside tests.
