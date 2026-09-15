using System.Diagnostics;
using System.Globalization;
using System.Text.Encodings.Web;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace DotRushCli;

public enum LspReplyKind
{
    // The server answered with a result (possibly null).
    Result,
    // The server answered with a JSON-RPC error; Message is "code: message".
    Error,
    // No answer in time; the request was cancelled.
    Timeout,
    // The request could not be sent or its response could not be read; Message says why.
    Failed,
}

public sealed record LspReply(LspReplyKind Kind, JsonElement Result, string Message);

// The request channel of a live session: request lines go into the proxy's inject.fifo with a dotrush-cc:<uuid>
// id, and the proxy writes the server's response to responses/<uuid>.json instead of forwarding it to Claude Code.
// Callers check Session.RequireChannel first.
public sealed class LspChannel(SessionState session)
{
    public const string IdPrefix = "dotrush-cc:";

    // .NET cannot open a FIFO with O_NONBLOCK and an open for writing blocks until a reader exists, so the open and
    // the write run on their own thread and are given up on after this long.
    public static readonly TimeSpan OpenTimeout = TimeSpan.FromSeconds(2);
    static readonly TimeSpan LateResponseWait = TimeSpan.FromSeconds(1);
    static readonly TimeSpan StaleResponseAge = TimeSpan.FromMinutes(10);
    static readonly TimeSpan PollInterval = TimeSpan.FromMilliseconds(20);
    static readonly JsonWriterOptions LineOptions = new() { Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping };
    static readonly JsonElement Null = JsonDocument.Parse("null").RootElement.Clone();

    public string FifoPath => Path.Combine(session.Dir, "inject.fifo");

    public string ResponsesDir => Path.Combine(session.Dir, "responses");

    // A lowercase GUID after the prefix: the only suffix the proxy turns into a file name.
    public static string NewId() => IdPrefix + Guid.NewGuid().ToString("D");

    // Responses nobody collected (a CLI killed while waiting) would otherwise pile up.
    public void DeleteStaleResponses()
    {
        var cutoff = DateTime.UtcNow - StaleResponseAge;
        string[] files;
        try
        {
            files = Directory.GetFiles(ResponsesDir, "*.json");
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException)
        {
            return;
        }
        foreach (var file in files)
        {
            try
            {
                if (File.GetLastWriteTimeUtc(file) < cutoff)
                {
                    File.Delete(file);
                }
            }
            catch (Exception e) when (e is IOException or UnauthorizedAccessException)
            {
            }
        }
    }

    // Sends a request and waits for its response. On timeout it sends $/cancelRequest and deletes a response that
    // still arrives within a second.
    public LspReply Request(string method, JsonNode? parameters, TimeSpan timeout)
    {
        var id = NewId();
        if (Write(Line(id, method, parameters)) is { } failure)
        {
            return new(LspReplyKind.Failed, Null, failure);
        }
        var body = WaitForResponse(id, timeout);
        if (body is null)
        {
            Write(Line(null, "$/cancelRequest", new JsonObject { ["id"] = id }));
            WaitForResponse(id, LateResponseWait);
            return new(LspReplyKind.Timeout, Null,
                $"no response to {method} within {Seconds(timeout)} s; the request was cancelled");
        }
        return Read(body);
    }

    // Sends a notification; null on success, else why it could not be written.
    public string? Notify(string method, JsonNode? parameters) => Write(Line(null, method, parameters));

    // One JSON-RPC message on one line, newline included, so it goes into the FIFO with a single write.
    static byte[] Line(string? id, string method, JsonNode? parameters)
    {
        using var buffer = new MemoryStream();
        using (var writer = new Utf8JsonWriter(buffer, LineOptions))
        {
            writer.WriteStartObject();
            writer.WriteString("jsonrpc", "2.0");
            if (id is not null)
            {
                writer.WriteString("id", id);
            }
            writer.WriteString("method", method);
            if (parameters is not null)
            {
                writer.WritePropertyName("params");
                parameters.WriteTo(writer);
            }
            writer.WriteEndObject();
        }
        buffer.WriteByte((byte)'\n');
        return buffer.ToArray();
    }

    string? Write(byte[] line)
    {
        var path = FifoPath;
        var write = Task.Factory.StartNew(() =>
        {
            using var fifo = new FileStream(path, FileMode.Open, FileAccess.Write, FileShare.ReadWrite, bufferSize: 0);
            fifo.Write(line);
        }, CancellationToken.None, TaskCreationOptions.LongRunning, TaskScheduler.Default);
        try
        {
            if (write.Wait(OpenTimeout))
            {
                return null;
            }
        }
        catch (AggregateException e)
        {
            return $"cannot write to the proxy's FIFO {path}: {e.InnerException?.Message}";
        }
        // The thread stays blocked in open until a reader appears or the process exits; observe its outcome so a
        // late failure is not reported as an unobserved task exception.
        write.ContinueWith(task => _ = task.Exception, TaskContinuationOptions.OnlyOnFaulted);
        return $"cannot write to the proxy's FIFO {path}: the proxy did not open it for reading within {Seconds(OpenTimeout)} s; restart Claude Code if this persists";
    }

    // The response body for id, deleted once read, or null when it did not arrive in time.
    byte[]? WaitForResponse(string id, TimeSpan timeout)
    {
        var path = Path.Combine(ResponsesDir, id[IdPrefix.Length..] + ".json");
        var clock = Stopwatch.StartNew();
        while (true)
        {
            try
            {
                // The proxy writes a temp file and renames it, so a file that exists is complete.
                var body = File.ReadAllBytes(path);
                try
                {
                    File.Delete(path);
                }
                catch (Exception e) when (e is IOException or UnauthorizedAccessException)
                {
                }
                return body;
            }
            catch (Exception e) when (e is IOException or UnauthorizedAccessException)
            {
            }
            if (clock.Elapsed >= timeout)
            {
                return null;
            }
            Thread.Sleep(PollInterval);
        }
    }

    static LspReply Read(byte[] body)
    {
        try
        {
            using var document = JsonDocument.Parse(body);
            var root = document.RootElement;
            if (root.ValueKind != JsonValueKind.Object)
            {
                return new(LspReplyKind.Failed, Null, "the response is not a JSON-RPC message");
            }
            if (root.TryGetProperty("error", out var error) && error.ValueKind != JsonValueKind.Null)
            {
                return new(LspReplyKind.Error, Null, DescribeError(error));
            }
            return new(LspReplyKind.Result,
                root.TryGetProperty("result", out var result) ? result.Clone() : Null, "");
        }
        catch (JsonException e)
        {
            return new(LspReplyKind.Failed, Null, $"the response is not valid JSON: {e.Message}");
        }
    }

    static string DescribeError(JsonElement error)
    {
        if (error.ValueKind != JsonValueKind.Object)
        {
            return error.GetRawText();
        }
        var code = error.TryGetProperty("code", out var c) ? c.GetRawText() : "unknown";
        var message = error.TryGetProperty("message", out var m)
            ? m.ValueKind == JsonValueKind.String ? m.GetString() : m.GetRawText()
            : "";
        return $"{code}: {message}";
    }

    internal static string Seconds(TimeSpan span) => span.TotalSeconds.ToString("0.###", CultureInfo.InvariantCulture);
}

public static class RequestCommand
{
    public const string Synopsis = "request <method> <params-json> [--timeout N]";
    internal static readonly TimeSpan DefaultTimeout = TimeSpan.FromSeconds(60);
    const double MaxTimeoutSeconds = 24 * 60 * 60;

    public static int Run(CommandContext context, string[] args)
    {
        var parsed = Parse(args);
        if (parsed is not { Method: { } method, Parameters: { } parameters, Timeout: { } timeout })
        {
            if (parsed.Problem is not null)
            {
                context.Stderr.WriteLine($"dotrush-cli: {parsed.Problem}");
            }
            context.Stderr.WriteLine($"dotrush-cli: usage: dotrush-cli.sh {Synopsis}");
            return ExitCode.Usage;
        }
        var ready = Session.RequireChannel(context, out var session);
        if (ready != ExitCode.Success)
        {
            return ready;
        }
        var channel = new LspChannel(session!);
        channel.DeleteStaleResponses();
        var reply = channel.Request(method, parameters, timeout);
        switch (reply.Kind)
        {
            case LspReplyKind.Result:
                context.Stdout.WriteLine(reply.Result.GetRawText());
                return ExitCode.Success;
            case LspReplyKind.Timeout:
                context.Stderr.WriteLine($"dotrush-cli: {reply.Message}");
                return ExitCode.Timeout;
            default:
                context.Stderr.WriteLine($"dotrush-cli: {reply.Message}");
                return ExitCode.Error;
        }
    }

    sealed record Arguments(string? Method, JsonNode? Parameters, TimeSpan? Timeout, string? Problem);

    // Moves every argument except `--timeout N` into positional and sets timeout from N; returns the problem with
    // a malformed --timeout, or null.
    internal static string? TakeTimeout(string[] args, List<string> positional, ref TimeSpan timeout)
    {
        for (var i = 0; i < args.Length; i++)
        {
            if (args[i] != "--timeout")
            {
                positional.Add(args[i]);
                continue;
            }
            if (i + 1 >= args.Length
                || !double.TryParse(args[i + 1], NumberStyles.Float, CultureInfo.InvariantCulture, out var seconds)
                || !(seconds > 0 && seconds <= MaxTimeoutSeconds))
            {
                return "--timeout needs a number of seconds greater than 0";
            }
            timeout = TimeSpan.FromSeconds(seconds);
            i++;
        }
        return null;
    }

    static Arguments Parse(string[] args)
    {
        var positional = new List<string>();
        var timeout = DefaultTimeout;
        if (TakeTimeout(args, positional, ref timeout) is { } problem)
        {
            return new(null, null, null, problem);
        }
        if (positional.Count != 2)
        {
            return new(null, null, null, null);
        }
        if (positional[0].Length == 0)
        {
            return new(null, null, null, "<method> must not be empty");
        }
        JsonNode? parameters;
        try
        {
            parameters = JsonNode.Parse(positional[1]);
        }
        catch (JsonException e)
        {
            return new(null, null, null, $"<params-json> is not valid JSON: {e.Message}");
        }
        if (parameters is not (JsonObject or JsonArray))
        {
            return new(null, null, null, "<params-json> must be a JSON object or array");
        }
        return new(positional[0], parameters, timeout, null);
    }
}
