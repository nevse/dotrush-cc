---
paths:
  - "plugins/dotrush/scripts/dotrush-install.sh"
  - "plugins/dotrush/scripts/install-dotrush.sh"
  - "plugins/dotrush/dotrush-version.json"
---
# Installing DotRush and its tools

- `dotrush-version.json` holds one ref for both components. Both are downloaded when that ref's release ships every
  bundle, otherwise both are built from source: a downloaded server never sits beside diagnostics built from source.
- A ref that is a commit builds from source on every first use (minutes). Move the pin to a release tag once one
  ships, and record why the pin is where it is in the plugin README.
- An install prepares the component beside its target and swaps it in whole under `<target>.lock`; a failure leaves
  the previous install running. Keep new steps inside the staged dir.
- A server named by `DOTRUSH_REAL_BIN` is never installed over.
- `CLAUDE_PLUGIN_DATA` reaches the language server but not a skill's Bash commands, so every script goes through
  `dotrush_data_dir`, which derives the same dir from the plugin's install path. Never read the variable directly.
- `dotrush_lock` reclaims a dead holder's lock non-atomically; that is known and accepted
  (`docs/backlog/lock-reclaim-is-not-atomic.md`), not a review finding.
- Tests: `InstallTests` in `tests/test_profile_reports.py`.
