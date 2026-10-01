---
worth: yes
where: plugins/dotrush/bin/lsp-proxy.py:501
added: 2026-10-01
---
# dotnet is found only on PATH or in DOTNET_ROOT

`server_command` looks for `dotnet` with `shutil.which` and then `$DOTNET_ROOT`; without either the proxy exits
127 and Claude Code gives up after `maxRestarts`, with only "crashed with exit code 127" to show for it. A Claude
Code started with a GUI environment has neither: on 2026-10-01 a session launched through agterm's
`session new --command` failed this way, though `dotnet` was installed in `~/.dotnet` and
`/usr/local/share/dotnet`. The same holds for the desktop app or an IDE launched from the Dock.

Fix: after PATH and `DOTNET_ROOT`, try the standard install dirs (`/usr/local/share/dotnet`, `~/.dotnet`, and
`/usr/share/dotnet`, `/usr/lib/dotnet` on Linux), and log which one was used. `dotrush_dotnet` in
`scripts/dotrush-install.sh:72` has the same lookup and should get the same fallback, so the installer, the proxy
and the CLI wrapper agree on one `dotnet`.
