using System.Runtime.Versioning;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace DotRushCli.Tests.E2E;

// The request channel against the real DotRush server behind the checkout's proxy.
[Trait("Category", "E2E")]
[UnsupportedOSPlatform("windows")]
public sealed partial class RequestChannelE2ETests(DotRushServerFixture server) : IClassFixture<DotRushServerFixture>
{
    [Fact]
    public async Task Request_hover_returns_the_servers_result_and_the_client_never_sees_the_response()
    {
        Assert.SkipUnless(DotRushServerFixture.Enabled, DotRushServerFixture.SkipReason);
        // `public class Greeter` in Greeter.cs: line 2, the G at character 13 (0-based). load-completed can come a
        // moment before the semantic model answers, so the fixture gives hover a few tries.
        var result = await server.HoverAsync(server.GreeterPath, 2, 13, stdout => stdout.Contains("Greeter"));

        Assert.True(result.Exit == 0 && result.Stdout.Contains("Greeter"),
            $"exit {result.Exit}\nstdout: {result.Stdout}\nstderr: {result.Stderr}{server.Diagnostics()}");
        Assert.Equal("", result.Stderr);
        Assert.IsType<JsonObject>(JsonNode.Parse(result.Stdout));
        Assert.Empty(Directory.GetFileSystemEntries(Path.Combine(server.SessionDir, "responses")));
        Assert.DoesNotContain(server.Frames, frame =>
            frame["id"] is JsonValue id && id.TryGetValue<string>(out var text)
            && text.StartsWith("dotrush-cc:", StringComparison.Ordinal));
    }

    [Fact]
    public void Session_through_the_real_wrapper_reports_a_running_loaded_proxy_with_the_channel()
    {
        Assert.SkipUnless(DotRushServerFixture.Enabled, DotRushServerFixture.SkipReason);
        // The wrapper always exports DOTRUSH_DATA_DIR from dotrush_data_dir, which honours CLAUDE_PLUGIN_DATA, so
        // this builds the CLI for real into the proxy's temp data dir and looks the session up there.
        var env = WrapperTests.BaseEnvironment();
        env["CLAUDE_PLUGIN_DATA"] = server.DataDir;
        env["DOTRUSH_SESSION_ID"] = server.SessionId;
        var start = new System.Diagnostics.ProcessStartInfo("bash")
        {
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            RedirectStandardInput = true,
            UseShellExecute = false,
            WorkingDirectory = server.Workspace,
        };
        start.ArgumentList.Add(Path.Combine(WrapperTests.Checkout, "plugins/dotrush/scripts/dotrush-cli.sh"));
        start.ArgumentList.Add("session");
        start.Environment.Clear();
        foreach (var (key, value) in env)
        {
            start.Environment[key] = value;
        }

        var result = WrapperTests.RunProcess(start, TimeSpan.FromMinutes(5));

        Assert.True(result.Exit == 0, $"exit {result.Exit}\nstdout: {result.Stdout}\nstderr: {result.Stderr}");
        var lines = result.Stdout.Split('\n', StringSplitOptions.RemoveEmptyEntries);
        Assert.Contains($"dir: {server.SessionDir}", lines);
        Assert.Contains(lines, line => RunningProxy().IsMatch(line));
        Assert.Contains("load: completed", lines);
        Assert.Contains("channel: available", lines);
        var built = Directory.GetDirectories(Path.Combine(server.DataDir, "cli"));
        Assert.Contains(built, dir => File.Exists(Path.Combine(dir, "DotRushCli.dll")));
    }

    [GeneratedRegex(@"^proxy: running \(pid \d+\)$")]
    private static partial Regex RunningProxy();
}
