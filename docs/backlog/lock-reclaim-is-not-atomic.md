---
worth: no
where: plugins/dotrush/scripts/dotrush-install.sh:212
added: 2026-09-17
---
# Stale lock reclaim in dotrush_lock is not atomic

`dotrush_lock` reclaims a dead-pid or pid-less lock with `rm -rf` then `mkdir`, so two waiters whose polls align
within about a millisecond could both take the lock and build or install concurrently. Not fixed: the window is
negligible, the pattern predates 0.7.0, and an atomic claim needs a redesign of the lock for both the installer and
the CLI build. Kept so reviews stop re-raising it.
