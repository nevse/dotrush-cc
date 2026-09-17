---
worth: later
where: tests/DotRushCli.Tests/E2E/RenameE2ETests.cs
added: 2026-09-17
---
# E2E tests start several DotRush servers in parallel

`RenameE2ETests` starts a fresh server per test while `RequestChannelE2ETests` runs in a parallel collection, so up
to four DotRush servers plus MSBuild run at once against the fixture's 180 s load budget. No flake has been seen
(14 consecutive clean runs). Unknown that settles it: a timeout flake on a slower machine — then serialize the E2E
collections.
