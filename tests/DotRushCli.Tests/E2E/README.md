# End-to-end tests

The tests in this folder run the CLI against the real DotRush language server, with the checkout's
`plugins/dotrush/bin/lsp-proxy.py` between the two. They are tagged `[Trait("Category", "E2E")]` and skip unless
`DOTRUSH_E2E=1`, so a plain `dotnet test tests/DotRushCli.Tests` stays fast.

## Run

```bash
DOTRUSH_E2E=1 dotnet test tests/DotRushCli.Tests --filter Category=E2E
```

## Prerequisites

- macOS or Linux (the proxy's FIFO and the wrapper need a POSIX system).
- .NET 10 SDK on `PATH`. It runs the DotRush server and builds the CLI for the wrapper test.
- `python3` on `PATH`, for the proxy.
- An installed DotRush server. The tests look for
  `~/.claude/plugins/data/dotrush-dotrush-cc/server/DotRush.dll`, where the plugin installs it on first use. Set
  `DOTRUSH_E2E_SERVER` to use a `DotRush.dll` somewhere else. With `DOTRUSH_E2E=1` and no server, the tests fail
  and name the path they checked.

## What the fixture does

`DotRushServerFixture` creates a temp dir holding a `net10.0` class library (`Greeter.cs`, used from `App.cs`) and a
data dir. It then:

1. writes the session's `target.json` pointing at the demo project, so the proxy replays it at startup and DotRush
   loads the project during `initialize`;
2. starts `lsp-proxy.py` with `DOTRUSH_REAL_BIN`, `DOTRUSH_DATA_DIR`, `DOTRUSH_SESSION_ID` and `DOTRUSH_WORKSPACE`,
   leaving out the MSBuild variables that `dotnet test` passes to child processes;
3. acts as Claude Code would: sends `initialize` and `initialized`, answers every server request with `null`,
   records every frame, and sends `didOpen` for both source files;
4. waits up to 180 s for the proxy to write `load-completed`.

On teardown it closes the proxy's stdin, which stops DotRush, waits a few seconds, kills the process tree if
anything is still running, and deletes the temp dir. Each test class using the fixture gets its own server;
`RenameE2ETests` changes the demo files on disk, so it starts a fresh server and project for every test.

The wrapper test runs the real `plugins/dotrush/scripts/dotrush-cli.sh` with `CLAUDE_PLUGIN_DATA` set to the
fixture's data dir, so the wrapper builds the CLI into that dir with the real SDK before running it.

When the fixture fails to start, the error includes the end of `proxy.log` and the proxy's stderr.
