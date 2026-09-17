using System.Runtime.Versioning;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace DotRushCli.Tests.E2E;

// rename preview and rename apply against the real DotRush server behind the checkout's proxy. A rename changes the
// demo project on disk, so every test starts its own server on a fresh project instead of sharing a class fixture.
[Trait("Category", "E2E")]
[UnsupportedOSPlatform("windows")]
public sealed partial class RenameE2ETests : IAsyncLifetime
{
    readonly DotRushServerFixture server = new();

    public ValueTask InitializeAsync() => server.InitializeAsync();

    public ValueTask DisposeAsync() => server.DisposeAsync();

    [Fact]
    public async Task Preview_lists_edits_in_both_files_and_apply_rewrites_both_on_disk()
    {
        Assert.SkipUnless(DotRushServerFixture.Enabled, DotRushServerFixture.SkipReason);
        var greeterBefore = File.ReadAllText(server.GreeterPath);
        var appBefore = File.ReadAllText(server.AppPath);

        var preview = await PreviewAsync("Welcomer");

        var lines = Lines(preview.Stdout);
        Assert.Equal("2 edits in 2 files", lines[0]);
        Assert.Contains("Greeter.cs: 1 edit", lines);
        Assert.Contains("App.cs: 1 edit", lines);
        Assert.Contains("-public class Greeter", lines);
        Assert.Contains("+public class Welcomer", lines);
        Assert.Contains("-    public static string Run() => new Greeter().Greet(\"world\");", lines);
        Assert.Contains("+    public static string Run() => new Welcomer().Greet(\"world\");", lines);
        // Preview writes nothing to the sources.
        Assert.Equal(greeterBefore, File.ReadAllText(server.GreeterPath));
        Assert.Equal(appBefore, File.ReadAllText(server.AppPath));

        var apply = server.RunCli("rename", "apply", PlanId(preview.Stdout));

        Assert.True(apply.Exit == 0, Describe("apply", apply));
        Assert.Equal("", apply.Stderr);
        var applied = Lines(apply.Stdout);
        Assert.Equal("renamed Greeter to Welcomer: 2 edits in 2 files", applied[0]);
        Assert.Equal([server.AppPath, server.GreeterPath], applied[1..].Order());
        Assert.Equal(greeterBefore.Replace("class Greeter", "class Welcomer"), File.ReadAllText(server.GreeterPath));
        Assert.Equal(appBefore.Replace("new Greeter()", "new Welcomer()"), File.ReadAllText(server.AppPath));
    }

    // Welcome is as long as Greeter, so the renamed files keep their size. DotRush's watcher asks for FileName,
    // DirectoryName and Size only, and apply sends didOpen so DotRush re-reads such an edit. On macOS the watcher
    // reports same-size writes too (a probe showed an in-place write picked up within 5 s), so hover alone cannot
    // tell which re-read happened here: the test checks the outcome, and the proxy log for apply's didOpens.
    [Theory]
    [InlineData("Welcomer")]
    [InlineData("Welcome")]
    public async Task After_apply_hover_at_the_class_shows_the_new_name(string newName)
    {
        Assert.SkipUnless(DotRushServerFixture.Enabled, DotRushServerFixture.SkipReason);
        var sizeBefore = new FileInfo(server.GreeterPath).Length;
        var preview = await PreviewAsync(newName);
        var apply = server.RunCli("rename", "apply", PlanId(preview.Stdout));
        Assert.True(apply.Exit == 0, Describe("apply", apply));
        Assert.Equal(newName.Length == "Greeter".Length, new FileInfo(server.GreeterPath).Length == sizeBefore);
        Assert.True(await InjectedDidOpensAsync(2), $"expected 2 injected didOpen notifications{server.Diagnostics()}");

        foreach (var (path, needle) in new[] { (server.GreeterPath, "class " + newName), (server.AppPath, "new " + newName) })
        {
            // The position of the new name in the file as it is on disk now.
            var (line, character) = DotRushServerFixture.PositionOf(File.ReadAllText(path), needle, needle.IndexOf(' ') + 1);
            var hover = await server.HoverAsync(path, line, character,
                stdout => stdout.Contains(newName, StringComparison.Ordinal) && !stdout.Contains("Greeter", StringComparison.Ordinal));

            Assert.True(hover.Exit == 0 && hover.Stdout.Contains(newName, StringComparison.Ordinal)
                && !hover.Stdout.Contains("Greeter", StringComparison.Ordinal),
                Describe($"hover in {Path.GetFileName(path)} at {line}:{character}", hover));
        }
    }

    [Fact]
    public async Task Apply_refuses_when_a_file_changed_after_preview_and_leaves_both_files_as_they_are()
    {
        Assert.SkipUnless(DotRushServerFixture.Enabled, DotRushServerFixture.SkipReason);
        var greeterBefore = File.ReadAllText(server.GreeterPath);
        var preview = await PreviewAsync("Welcomer");
        var appEdited = File.ReadAllText(server.AppPath) + "// edited after preview\n";
        File.WriteAllText(server.AppPath, appEdited);

        var apply = server.RunCli("rename", "apply", PlanId(preview.Stdout));

        Assert.True(apply.Exit == 1, Describe("apply", apply));
        Assert.Contains("changed since preview; run rename preview again", apply.Stderr);
        Assert.Equal("", apply.Stdout);
        Assert.Equal(greeterBefore, File.ReadAllText(server.GreeterPath));
        Assert.Equal(appEdited, File.ReadAllText(server.AppPath));
        Assert.Empty(Directory.GetFiles(server.Workspace, "*" + WorkspaceEditApplier.TempSuffix, SearchOption.AllDirectories));
    }

    // rename preview on the Greeter class name in Greeter.cs, retried while DotRush's semantic model settles.
    async Task<CliResult> PreviewAsync(string newName)
    {
        var (line, character) = DotRushServerFixture.PositionOf(File.ReadAllText(server.GreeterPath), "class Greeter", "class ".Length);
        var preview = await server.RunCliUntilAsync(result => result.Exit == 0, TimeSpan.FromSeconds(60),
            "rename", "preview", server.GreeterPath, (line + 1).ToString(), (character + 1).ToString(), newName);
        if (preview.Exit != 0)
        {
            // What DotRush itself answers, to tell a server-side difference from a CLI one.
            var raw = server.RunCli("request", "textDocument/rename", new JsonObject
            {
                ["textDocument"] = new JsonObject { ["uri"] = DotRushServerFixture.Uri(server.GreeterPath) },
                ["position"] = new JsonObject { ["line"] = line, ["character"] = character },
                ["newName"] = newName,
            }.ToJsonString());
            Assert.Fail($"{Describe("preview", preview)}\n--- raw textDocument/rename ---\n{raw.Stdout}{raw.Stderr}"
                + $"\n--- App.cs ---\n{File.ReadAllText(server.AppPath)}");
        }
        return preview;
    }

    // Waits up to 5 s for the proxy to log count textDocument/didOpen notifications injected through its FIFO. The
    // fixture's own didOpens go through stdin, so every injected one came from the CLI.
    async Task<bool> InjectedDidOpensAsync(int count)
    {
        var log = Path.Combine(server.SessionDir, "proxy.log");
        var deadline = DateTime.UtcNow.AddSeconds(5);
        while (true)
        {
            var injected = File.Exists(log)
                ? File.ReadAllLines(log).Count(line => InjectedDidOpen().IsMatch(line))
                : 0;
            if (injected == count || DateTime.UtcNow >= deadline)
            {
                return injected == count;
            }
            await Task.Delay(100, TestContext.Current.CancellationToken);
        }
    }

    string PlanId(string previewStdout)
    {
        var match = PlanLine().Match(previewStdout);
        Assert.True(match.Success, $"no plan line in:\n{previewStdout}");
        return match.Groups[1].Value;
    }

    static string[] Lines(string text) => text.Split('\n', StringSplitOptions.RemoveEmptyEntries);

    string Describe(string step, CliResult result) =>
        $"{step}: exit {result.Exit}\nstdout: {result.Stdout}\nstderr: {result.Stderr}{server.Diagnostics()}";

    [GeneratedRegex(@"^plan: ([0-9a-f]{12})$", RegexOptions.Multiline)]
    private static partial Regex PlanLine();

    // The proxy's log line for a notification injected through its FIFO, whatever padding it uses.
    [GeneratedRegex(@"INJECT -> notif\s+textDocument/didOpen\b")]
    private static partial Regex InjectedDidOpen();
}
