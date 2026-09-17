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
        AssertNothingSent();
    }

    [Theory]
    [InlineData("")]
    [InlineData("textDocument/hover")]
    [InlineData("textDocument/hover|{}|extra")]
    [InlineData("textDocument/hover|{}|--timeout")]
    [InlineData("textDocument/hover|{}|--timeout|0")]
    [InlineData("textDocument/hover|{}|--timeout|soon")]
    [InlineData("textDocument/hover|{}|--timeout|100000")]
    [InlineData("textDocument/hover|{}|--timeout|86401")]
    [InlineData("textDocument/hover|{}|--timeout|5|--timeout|5")]
    [InlineData("|{}")]
    public void Malformed_request_arguments_are_a_usage_error(string joinedArgs)
    {
        // Arguments separated by '|'; an empty string is no arguments at all.
        string[] args = joinedArgs.Length == 0 ? [] : joinedArgs.Split('|');

        var result = proxy.Run(["request", .. args]);

        Assert.Equal(2, result.Exit);
        Assert.Equal("", result.Stdout);
        Assert.Contains("request <method> <params-json> [--timeout N]", result.Stderr);
        AssertNothingSent();
    }

    [Fact]
    public void The_largest_accepted_timeout_is_twenty_four_hours()
    {
        AnswerRequestsWith("\"result\":null");

        // 86401 is the usage error above; the cap itself is accepted and the answer arrives at once.
        Assert.Equal((0, "null\n", ""), proxy.Run("request", "textDocument/hover", HoverParams, "--timeout", "86400"));
    }

    // A ping through the same FIFO: once the proxy has recorded it, anything the CLI sent is recorded too.
    void AssertNothingSent()
    {
        proxy.Ping();
        Assert.Equal([FakeProxy.PingLine], proxy.Lines);
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
        // What a proxy killed between writing a response and renaming it leaves behind.
        var staleTemp = Path.Combine(proxy.ResponsesDir, "00000000-0000-0000-0000-000000000003.json.999.tmp");
        File.WriteAllText(stale, "{}");
        File.SetLastWriteTimeUtc(stale, DateTime.UtcNow.AddMinutes(-11));
        File.WriteAllText(staleTemp, "{}");
        File.SetLastWriteTimeUtc(staleTemp, DateTime.UtcNow.AddMinutes(-11));
        File.WriteAllText(fresh, "{}");
        File.SetLastWriteTimeUtc(fresh, DateTime.UtcNow.AddMinutes(-9));

        Assert.Equal(0, proxy.Run("request", "textDocument/hover", HoverParams).Exit);

        Assert.False(File.Exists(stale));
        Assert.False(File.Exists(staleTemp));
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
        AssertNothingSent();
    }

    [Theory]
    [InlineData("not json", "dotrush-cli: the response is not valid JSON: ")]
    [InlineData("[1]", "dotrush-cli: the response is not a JSON-RPC message\n")]
    [InlineData("""{"jsonrpc":"2.0","error":"boom"}""", "dotrush-cli: \"boom\"\n")]
    [InlineData("""{"jsonrpc":"2.0","error":{}}""", "dotrush-cli: unknown: \n")]
    public void A_response_that_is_not_a_json_rpc_result_is_an_error(string body, string expectedStderr)
    {
        proxy.OnMessage = (fake, message) =>
        {
            if (message["id"] is not null)
            {
                fake.RespondWithBody(IdOf(message), body);
            }
        };

        var result = proxy.Run("request", "textDocument/hover", HoverParams);

        Assert.Equal((1, ""), (result.Exit, result.Stdout));
        Assert.StartsWith(expectedStderr, result.Stderr);
        Assert.Empty(Directory.GetFileSystemEntries(proxy.ResponsesDir));
    }

    [Fact]
    public void A_missing_fifo_fails_with_the_reason()
    {
        using var gone = new FakeProxy(withReader: false);
        File.Delete(gone.Fifo);

        var result = gone.Run("request", "textDocument/hover", HoverParams);

        Assert.Equal((1, ""), (result.Exit, result.Stdout));
        Assert.StartsWith($"dotrush-cli: cannot write to the proxy's FIFO {gone.Fifo}: ", result.Stderr);
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
        // The requirement is that it gives up quickly, not that it waits any particular time.
        Assert.True(clock.Elapsed < TimeSpan.FromSeconds(10), $"gave up only after {clock.Elapsed}");
    }

    // A stream standing in for one FIFO open: it records what was written through it and when it was disposed, and
    // fails the write numbered FailAt (0-based) with an IOException, as a write into a pipe without a reader does.
    sealed class OpenRecord(int index, List<string> events, int failAt) : MemoryStream
    {
        int writes;
        public List<string> Written { get; } = [];

        public override void Write(ReadOnlySpan<byte> buffer)
        {
            if (writes++ == failAt)
            {
                events.Add($"open {index}: broken pipe");
                throw new IOException("Broken pipe");
            }
            Written.Add(System.Text.Encoding.UTF8.GetString(buffer));
        }

        protected override void Dispose(bool disposing)
        {
            if (disposing)
            {
                events.Add($"close {index}");
            }
            base.Dispose(disposing);
        }
    }

    static readonly byte[][] Batch = [.. new[] { "a\n", "b\n", "c\n" }.Select(System.Text.Encoding.UTF8.GetBytes)];

    static (List<OpenRecord> Opens, List<string> Events, Exception? Failure) WriteBatch(params int[] failAt)
    {
        var opens = new List<OpenRecord>();
        var events = new List<string>();
        Func<Stream> open = () =>
        {
            var index = opens.Count;
            events.Add($"open {index}");
            var stream = new OpenRecord(index, events, index < failAt.Length ? failAt[index] : -1);
            opens.Add(stream);
            return stream;
        };
        var failure = Record.Exception(() => LspChannel.WriteLines(open, Batch));
        return (opens, events, failure);
    }

    [Fact]
    public void A_batch_goes_through_one_open_with_one_write_per_line()
    {
        var (opens, events, failure) = WriteBatch();

        Assert.Null(failure);
        Assert.Equal(["a\n", "b\n", "c\n"], Assert.Single(opens).Written);
        Assert.Equal(["open 0", "close 0"], events);
    }

    // Lines written before a broken pipe went to a reader that was going away, so none of them counts as read: the
    // retry sends the whole batch again, and the failed open is closed only after the new open returned, since a pipe
    // left with neither reader nor writer loses what it holds.
    [Theory]
    [InlineData(0)]
    [InlineData(1)]
    [InlineData(2)]
    public void A_broken_pipe_resends_the_whole_batch_through_a_new_open(int failAt)
    {
        var (opens, events, failure) = WriteBatch(failAt);

        Assert.Null(failure);
        Assert.Equal(2, opens.Count);
        Assert.Equal(Batch.Take(failAt).Select(System.Text.Encoding.UTF8.GetString), opens[0].Written);
        Assert.Equal(["a\n", "b\n", "c\n"], opens[1].Written);
        Assert.Equal(["open 0", "open 0: broken pipe", "open 1", "close 0", "close 1"], events);
    }

    [Fact]
    public void A_second_broken_pipe_is_reported_and_closes_both_opens()
    {
        var (opens, events, failure) = WriteBatch(1, 0);

        Assert.IsType<IOException>(failure);
        Assert.Equal(2, opens.Count);
        Assert.Equal(["open 0", "open 0: broken pipe", "open 1", "close 0", "open 1: broken pipe", "close 1"], events);
    }

    [Fact]
    public void A_failed_reopen_still_closes_the_first_open()
    {
        var events = new List<string>();
        var opens = 0;
        Func<Stream> open = () =>
        {
            if (opens++ == 1)
            {
                throw new IOException("No such file or directory");
            }
            return new OpenRecord(0, events, failAt: 0);
        };

        Assert.IsType<IOException>(Record.Exception(() => LspChannel.WriteLines(open, Batch)));
        Assert.Equal(["open 0: broken pipe", "close 0"], events);
    }
}
