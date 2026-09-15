using System.Diagnostics;
using System.Runtime.Versioning;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace DotRushCli.Tests;

// The request channel and the `request` command, against a FakeProxy reading the session's inject.fifo.
[UnsupportedOSPlatform("windows")]
public sealed class LspChannelTests : IDisposable
{
    const string HoverParams = """{"textDocument":{"uri":"file:///repo/Greeter.cs"},"position":{"line":2,"character":13}}""";

    readonly FakeProxy proxy = new();

    public void Dispose() => proxy.Dispose();

    static JsonObject Parse(string line) => (JsonObject)JsonNode.Parse(line)!;

    static string IdOf(JsonObject message) => message["id"]!.GetValue<string>();

    void AnswerRequestsWith(string fields) =>
        proxy.OnMessage = (fake, message) =>
        {
            if (message["id"] is not null)
            {
                fake.Respond(IdOf(message), fields);
            }
        };

    [Fact]
    public void Request_prints_the_result_and_deletes_the_response_file()
    {
        AnswerRequestsWith("""
            "result":{"contents":{"kind":"markdown","value":"class Greeter"}}
            """);

        var result = proxy.Run("request", "textDocument/hover", HoverParams);

        Assert.Equal((0, """{"contents":{"kind":"markdown","value":"class Greeter"}}""" + "\n", ""), result);
        var line = Assert.Single(proxy.Lines);
        var message = Parse(line);
        Assert.Equal("2.0", message["jsonrpc"]!.GetValue<string>());
        Assert.Matches(new Regex("^dotrush-cc:[0-9a-f-]{36}$"), IdOf(message));
        Assert.Equal("textDocument/hover", message["method"]!.GetValue<string>());
        Assert.True(JsonNode.DeepEquals(JsonNode.Parse(HoverParams), message["params"]));
        Assert.Equal(1, proxy.Responses);
        Assert.Empty(Directory.GetFileSystemEntries(proxy.ResponsesDir));
    }

    [Fact]
    public void Each_request_gets_its_own_id()
    {
        AnswerRequestsWith("\"result\":null");

        Assert.Equal((0, "null\n", ""), proxy.Run("request", "workspace/symbol", """{"query":"Greeter"}"""));
        Assert.Equal((0, "null\n", ""), proxy.Run("request", "workspace/symbol", """{"query":"Greeter"}"""));

        var ids = proxy.Lines.Select(line => IdOf(Parse(line))).ToArray();
        Assert.Equal(2, ids.Length);
        Assert.NotEqual(ids[0], ids[1]);
    }

    [Fact]
    public void A_json_rpc_error_is_printed_as_code_and_message()
    {
        AnswerRequestsWith("""
            "error":{"code":-32601,"message":"Method not found"}
            """);

        var result = proxy.Run("request", "textDocument/frobnicate", "{}");

        Assert.Equal((1, "", "dotrush-cli: -32601: Method not found\n"), result);
        Assert.Empty(Directory.GetFileSystemEntries(proxy.ResponsesDir));
    }

    [Fact]
    public void Without_a_response_in_time_the_request_is_cancelled_and_a_late_response_deleted()
    {
        proxy.OnMessage = (fake, message) =>
        {
            // The server stays silent on the request and answers only once it is cancelled.
            if (message["method"]?.GetValue<string>() == "$/cancelRequest")
            {
                fake.Respond(message["params"]!["id"]!.GetValue<string>(), """
                    "error":{"code":-32800,"message":"Request cancelled"}
                    """);
            }
        };

        var result = proxy.Run("request", "textDocument/hover", HoverParams, "--timeout", "0.3");

        Assert.Equal(3, result.Exit);
        Assert.Equal("", result.Stdout);
        Assert.Contains("no response to textDocument/hover within 0.3 s", result.Stderr);
        var lines = proxy.Lines;
        Assert.Equal(2, lines.Count);
        var request = Parse(lines[0]);
        var cancel = Parse(lines[1]);
        Assert.Equal("$/cancelRequest", cancel["method"]!.GetValue<string>());
        Assert.Null(cancel["id"]);
        Assert.Equal(IdOf(request), cancel["params"]!["id"]!.GetValue<string>());
        Assert.Equal(1, proxy.Responses);
        Assert.Empty(Directory.GetFileSystemEntries(proxy.ResponsesDir));
    }

    [Theory]
    [InlineData("{not json")]
    [InlineData("42")]
    [InlineData("\"text\"")]
    public void Invalid_params_json_is_a_usage_error_and_writes_nothing(string paramsJson)
    {
        AnswerRequestsWith("\"result\":null");

        var result = proxy.Run("request", "textDocument/hover", paramsJson);

        Assert.Equal(2, result.Exit);
        Assert.Equal("", result.Stdout);
        Assert.Contains("request <method> <params-json> [--timeout N]", result.Stderr);
        Thread.Sleep(200);
        Assert.Empty(proxy.Lines);
    }

    [Theory]
    [InlineData("")]
    [InlineData("textDocument/hover")]
    [InlineData("textDocument/hover|{}|extra")]
    [InlineData("textDocument/hover|{}|--timeout")]
    [InlineData("textDocument/hover|{}|--timeout|0")]
    [InlineData("textDocument/hover|{}|--timeout|soon")]
    [InlineData("|{}")]
    public void Malformed_request_arguments_are_a_usage_error(string joinedArgs)
    {
        // Arguments separated by '|'; an empty string is no arguments at all.
        string[] args = joinedArgs.Length == 0 ? [] : joinedArgs.Split('|');

        var result = proxy.Run(["request", .. args]);

        Assert.Equal(2, result.Exit);
        Assert.Equal("", result.Stdout);
        Assert.Contains("request <method> <params-json> [--timeout N]", result.Stderr);
        Thread.Sleep(100);
        Assert.Empty(proxy.Lines);
    }

    [Fact]
    public void Params_may_be_an_array_and_the_timeout_may_come_first()
    {
        AnswerRequestsWith("\"result\":[1,2]");

        var result = proxy.Run("request", "--timeout", "5", "custom/method", "[1,\"two\"]");

        Assert.Equal((0, "[1,2]\n", ""), result);
        Assert.True(JsonNode.DeepEquals(JsonNode.Parse("[1,\"two\"]"), Parse(Assert.Single(proxy.Lines))["params"]));
    }

    [Fact]
    public void Stale_response_files_are_removed_on_start_and_fresh_ones_kept()
    {
        AnswerRequestsWith("\"result\":null");
        var stale = Path.Combine(proxy.ResponsesDir, "00000000-0000-0000-0000-000000000001.json");
        var fresh = Path.Combine(proxy.ResponsesDir, "00000000-0000-0000-0000-000000000002.json");
        File.WriteAllText(stale, "{}");
        File.SetLastWriteTimeUtc(stale, DateTime.UtcNow.AddMinutes(-11));
        File.WriteAllText(fresh, "{}");
        File.SetLastWriteTimeUtc(fresh, DateTime.UtcNow.AddMinutes(-9));

        Assert.Equal(0, proxy.Run("request", "textDocument/hover", HoverParams).Exit);

        Assert.False(File.Exists(stale));
        Assert.True(File.Exists(fresh));
    }

    [Fact]
    public void Request_checks_channel_readiness_before_writing()
    {
        File.Delete(Path.Combine(proxy.Dir, "load-completed"));

        var result = proxy.Run("request", "textDocument/hover", HoverParams);

        Assert.Equal((1, "",
            "dotrush-cli: DotRush has not finished loading a project in this session; choose one with dotrush-pick-project, or wait for the load to finish and retry\n"),
            result);
        Thread.Sleep(100);
        Assert.Empty(proxy.Lines);
    }

    [Fact]
    public void A_fifo_without_a_reader_fails_within_the_open_timeout_instead_of_hanging()
    {
        using var silent = new FakeProxy(withReader: false);
        var clock = Stopwatch.StartNew();

        var result = silent.Run("request", "textDocument/hover", HoverParams, "--timeout", "30");

        clock.Stop();
        Assert.Equal(1, result.Exit);
        Assert.Equal("", result.Stdout);
        Assert.Contains("cannot write to the proxy's FIFO", result.Stderr);
        Assert.InRange(clock.Elapsed, TimeSpan.FromSeconds(1.5), TimeSpan.FromSeconds(10));
    }
}
