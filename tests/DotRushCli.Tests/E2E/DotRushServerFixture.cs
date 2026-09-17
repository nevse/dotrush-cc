using System.Diagnostics;
using System.Runtime.Versioning;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;

namespace DotRushCli.Tests.E2E;

// Starts the checkout's lsp-proxy.py in front of the installed DotRush server, in a temp demo project, and plays
// Claude Code's part: a minimal LSP client that initializes, answers every server request with null, records every
// frame the proxy forwards, and didOpens the demo's files. It is ready once the proxy has written load-completed.
// Nothing starts unless DOTRUSH_E2E=1; the tests skip themselves then.
[UnsupportedOSPlatform("windows")]
public sealed class DotRushServerFixture : IAsyncLifetime
{
    public const string SkipReason = "set DOTRUSH_E2E=1 to run the tests against the real DotRush server";
    static readonly TimeSpan InitializeTimeout = TimeSpan.FromSeconds(120);
    static readonly TimeSpan LoadTimeout = TimeSpan.FromSeconds(180);

    readonly List<JsonObject> frames = [];
    readonly Dictionary<int, TaskCompletionSource<JsonObject>> pending = [];
    readonly StringBuilder proxyStderr = new();
    readonly object writeLock = new();
    DirectoryInfo? root;
    Process? proxy;
    Thread? reader;
    int nextId;

    public static bool Enabled => Environment.GetEnvironmentVariable("DOTRUSH_E2E") == "1";

    // The installed DotRush server; DOTRUSH_E2E_SERVER overrides the plugin's usual install location.
    public static string ServerPath =>
        Environment.GetEnvironmentVariable("DOTRUSH_E2E_SERVER") is { Length: > 0 } explicitPath
            ? explicitPath
            : Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
                ".claude/plugins/data/dotrush-dotrush-cc/server/DotRush.dll");

    public string SessionId { get; } = "e2e-" + Guid.NewGuid().ToString("N");
    public string DataDir { get; private set; } = "";
    public string Workspace { get; private set; } = "";
    public string SessionDir { get; private set; } = "";
    public string GreeterPath { get; private set; } = "";
    public string AppPath { get; private set; } = "";

    // Every frame the client received from the proxy, in order.
    public IReadOnlyList<JsonObject> Frames
    {
        get { lock (frames) return [.. frames]; }
    }

    public async ValueTask InitializeAsync()
    {
        if (!Enabled)
        {
            return;
        }
        if (!File.Exists(ServerPath))
        {
            throw new InvalidOperationException(
                $"no DotRush server at {ServerPath}; install the plugin's server or set DOTRUSH_E2E_SERVER");
        }
        // Real paths throughout, so macOS's /var -> /private/var link never makes two spellings of one file.
        root = new DirectoryInfo(Posix.RealPath(Directory.CreateTempSubdirectory("dotrush-cli-e2e-").FullName)!);
        DataDir = Directory.CreateDirectory(Path.Combine(root.FullName, "data")).FullName;
        Workspace = Directory.CreateDirectory(Path.Combine(root.FullName, "Demo")).FullName;
        WriteDemoProject();
        SessionDir = Path.Combine(DataDir, "ws", "sess-" + Sha1Hex(SessionId)[..12]);
        Directory.CreateDirectory(SessionDir);
        // The proxy replays the target at startup, so DotRush loads the project during initialize.
        var target = new JsonObject
        {
            ["projectOrSolutionFiles"] = new JsonArray(Path.Combine(Workspace, "Demo.csproj")),
            ["restoreProjectsBeforeLoading"] = true,
        };
        File.WriteAllText(Path.Combine(SessionDir, "target.json"), target.ToJsonString());

        StartProxy();
        var initialize = SendRequest("initialize", new JsonObject
        {
            ["processId"] = Environment.ProcessId,
            ["rootUri"] = Uri(Workspace),
            ["workspaceFolders"] = new JsonArray(new JsonObject { ["uri"] = Uri(Workspace), ["name"] = "Demo" }),
            ["capabilities"] = new JsonObject(),
        });
        if (await Task.WhenAny(initialize, Task.Delay(InitializeTimeout)) != initialize)
        {
            throw new TimeoutException($"no initialize response in {InitializeTimeout}{Diagnostics()}");
        }
        SendNotification("initialized", new JsonObject());
        DidOpen(GreeterPath);
        DidOpen(AppPath);

        var deadline = DateTime.UtcNow + LoadTimeout;
        while (!File.Exists(Path.Combine(SessionDir, "load-completed")))
        {
            if (proxy!.HasExited)
            {
                throw new InvalidOperationException($"the proxy exited with {proxy.ExitCode}{Diagnostics()}");
            }
            if (DateTime.UtcNow > deadline)
            {
                throw new TimeoutException($"DotRush did not finish loading in {LoadTimeout}{Diagnostics()}");
            }
            await Task.Delay(200);
        }
    }

    public async ValueTask DisposeAsync()
    {
        if (proxy is not null)
        {
            // DotRush exits when its stdin closes, and the proxy closes it on its own stdin's EOF.
            try { proxy.StandardInput.Close(); } catch (IOException) { } catch (InvalidOperationException) { }
            using var exited = new CancellationTokenSource(TimeSpan.FromSeconds(5));
            try
            {
                await proxy.WaitForExitAsync(exited.Token);
            }
            catch (OperationCanceledException)
            {
            }
            if (!proxy.HasExited)
            {
                try { proxy.Kill(entireProcessTree: true); } catch (InvalidOperationException) { }
                proxy.WaitForExit(5000);
            }
            reader?.Join(TimeSpan.FromSeconds(2));
            proxy.Dispose();
        }
        try { root?.Delete(recursive: true); } catch (IOException) { } catch (UnauthorizedAccessException) { }
    }

    public static string Uri(string path) => new Uri(path).AbsoluteUri;

    // Runs the CLI in-process against this session, as the wrapper would with DOTRUSH_DATA_DIR exported.
    public CliResult RunCli(params string[] args) => Cli.Run(new Dictionary<string, string?>
    {
        ["DOTRUSH_DATA_DIR"] = DataDir,
        ["DOTRUSH_SESSION_ID"] = SessionId,
    }, Workspace, args);

    // Runs the CLI until accept takes its result or the time is up, and returns the last result. Right after
    // load-completed the semantic model can still be settling, and a request may briefly find no symbol.
    public async Task<CliResult> RunCliUntilAsync(
        Func<CliResult, bool> accept, TimeSpan within, params string[] args)
    {
        var deadline = DateTime.UtcNow + within;
        while (true)
        {
            var result = RunCli(args);
            if (accept(result) || DateTime.UtcNow >= deadline)
            {
                return result;
            }
            await Task.Delay(1000, TestContext.Current.CancellationToken);
        }
    }

    // textDocument/hover through the CLI's `request` at a 0-based position, retried until accept takes the result
    // JSON (for up to 60 s).
    public Task<CliResult> HoverAsync(
        string path, int line, int character, Func<string, bool> accept)
    {
        var hoverParams = new JsonObject
        {
            ["textDocument"] = new JsonObject { ["uri"] = Uri(path) },
            ["position"] = new JsonObject { ["line"] = line, ["character"] = character },
        }.ToJsonString();
        return RunCliUntilAsync(result => result.Exit == 0 && accept(result.Stdout), TimeSpan.FromSeconds(60),
            "request", "textDocument/hover", hoverParams, "--timeout", "30");
    }

    // The 0-based LSP position of needle[offset] in text (the demo files are ASCII, so chars are UTF-16 units).
    public static (int Line, int Character) PositionOf(string text, string needle, int offset)
    {
        var index = text.IndexOf(needle, StringComparison.Ordinal);
        Assert.True(index >= 0, $"'{needle}' not found in:\n{text}");
        index += offset;
        var lineStart = text.LastIndexOf('\n', index - 1) + 1;
        return (text[..lineStart].Count(c => c == '\n'), index - lineStart);
    }

    // The proxy's log and stderr, appended to a failure message.
    public string Diagnostics()
    {
        var log = Path.Combine(SessionDir, "proxy.log");
        var tail = File.Exists(log) ? string.Join('\n', File.ReadAllLines(log).TakeLast(40)) : "(no proxy.log)";
        string stderr;
        lock (proxyStderr) stderr = proxyStderr.ToString();
        return $"\n--- proxy.log tail ---\n{tail}\n--- proxy stderr ---\n{stderr}";
    }

    void WriteDemoProject()
    {
        File.WriteAllText(Path.Combine(Workspace, "Demo.csproj"), """
            <Project Sdk="Microsoft.NET.Sdk">
              <PropertyGroup>
                <TargetFramework>net10.0</TargetFramework>
                <Nullable>enable</Nullable>
              </PropertyGroup>
            </Project>
            """);
        GreeterPath = Path.Combine(Workspace, "Greeter.cs");
        File.WriteAllText(GreeterPath, """
            namespace Demo;

            public class Greeter
            {
                public string Greet(string name) => "Hello, " + name;
            }
            """);
        AppPath = Path.Combine(Workspace, "App.cs");
        File.WriteAllText(AppPath, """
            namespace Demo;

            public static class App
            {
                public static string Run() => new Greeter().Greet("world");
            }
            """);
    }

    void StartProxy()
    {
        var start = new ProcessStartInfo("python3")
        {
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            WorkingDirectory = Workspace,
        };
        start.ArgumentList.Add(Path.Combine(TestProcesses.Checkout, "plugins/dotrush/bin/lsp-proxy.py"));
        start.Environment.Clear();
        // Without the MSBuild variables `dotnet test` leaves behind, which would steer DotRush's own MSBuild.
        foreach (var (key, value) in TestProcesses.BaseEnvironment())
        {
            if (!key.StartsWith("DOTRUSH_", StringComparison.Ordinal))
            {
                start.Environment[key] = value;
            }
        }
        start.Environment["DOTRUSH_REAL_BIN"] = ServerPath;
        start.Environment["DOTRUSH_DATA_DIR"] = DataDir;
        start.Environment["DOTRUSH_SESSION_ID"] = SessionId;
        start.Environment["DOTRUSH_WORKSPACE"] = Workspace;

        proxy = Process.Start(start)!;
        proxy.ErrorDataReceived += (_, e) =>
        {
            if (e.Data is not null)
            {
                lock (proxyStderr) proxyStderr.AppendLine(e.Data);
            }
        };
        proxy.BeginErrorReadLine();
        reader = new Thread(ReadLoop) { IsBackground = true, Name = "E2E LSP client reader" };
        reader.Start();
    }

    Task<JsonObject> SendRequest(string method, JsonNode parameters)
    {
        var id = Interlocked.Increment(ref nextId);
        var reply = new TaskCompletionSource<JsonObject>(TaskCreationOptions.RunContinuationsAsynchronously);
        lock (pending) pending[id] = reply;
        Send(new JsonObject { ["jsonrpc"] = "2.0", ["id"] = id, ["method"] = method, ["params"] = parameters });
        return reply.Task;
    }

    void SendNotification(string method, JsonNode parameters) =>
        Send(new JsonObject { ["jsonrpc"] = "2.0", ["method"] = method, ["params"] = parameters });

    void DidOpen(string path) =>
        SendNotification("textDocument/didOpen", new JsonObject
        {
            ["textDocument"] = new JsonObject
            {
                ["uri"] = Uri(path),
                ["languageId"] = "csharp",
                ["version"] = 1,
                ["text"] = File.ReadAllText(path),
            },
        });

    void Send(JsonObject message)
    {
        var body = Encoding.UTF8.GetBytes(message.ToJsonString());
        var header = Encoding.ASCII.GetBytes($"Content-Length: {body.Length}\r\n\r\n");
        lock (writeLock)
        {
            var stdin = proxy!.StandardInput.BaseStream;
            stdin.Write(header);
            stdin.Write(body);
            stdin.Flush();
        }
    }

    void ReadLoop()
    {
        var stdout = proxy!.StandardOutput.BaseStream;
        try
        {
            while (ReadFrame(stdout) is { } body)
            {
                if (JsonNode.Parse(body) is not JsonObject message)
                {
                    continue;
                }
                lock (frames) frames.Add(message);
                Dispatch(message);
            }
        }
        catch (Exception exception)
        {
            // The stream closes when the fixture stops the proxy; anything else surfaces in Diagnostics() and as a
            // missing reply, never as a crash of the test host from this thread.
            lock (proxyStderr) proxyStderr.AppendLine($"[client reader stopped: {exception.GetType().Name}: {exception.Message}]");
        }
        finally
        {
            lock (pending)
            {
                foreach (var reply in pending.Values)
                {
                    reply.TrySetException(new IOException($"the proxy closed its stdout{Diagnostics()}"));
                }
                pending.Clear();
            }
        }
    }

    void Dispatch(JsonObject message)
    {
        var hasMethod = message.ContainsKey("method");
        var id = message["id"];
        if (hasMethod && id is not null)
        {
            // A server request: DotRush may stall waiting for an answer, and Claude Code's answers are not needed.
            try
            {
                Send(new JsonObject { ["jsonrpc"] = "2.0", ["id"] = id.DeepClone(), ["result"] = null });
            }
            catch (IOException)
            {
            }
        }
        else if (!hasMethod && id is JsonValue value && value.TryGetValue<int>(out var number))
        {
            TaskCompletionSource<JsonObject>? reply;
            lock (pending) pending.Remove(number, out reply);
            reply?.TrySetResult(message);
        }
    }

    static string? ReadFrame(Stream stream)
    {
        var length = -1;
        var line = new StringBuilder();
        while (true)
        {
            var next = stream.ReadByte();
            if (next < 0)
            {
                return null;
            }
            if (next == '\n')
            {
                var text = line.ToString().TrimEnd('\r');
                line.Clear();
                if (text.Length == 0)
                {
                    break;
                }
                if (text.StartsWith("Content-Length:", StringComparison.OrdinalIgnoreCase))
                {
                    length = int.Parse(text["Content-Length:".Length..].Trim());
                }
                continue;
            }
            line.Append((char)next);
        }
        if (length < 0)
        {
            throw new InvalidDataException("an LSP frame without Content-Length");
        }
        var body = new byte[length];
        stream.ReadExactly(body);
        return Encoding.UTF8.GetString(body);
    }

    static string Sha1Hex(string text) =>
        Convert.ToHexStringLower(SHA1.HashData(Encoding.UTF8.GetBytes(text)));
}
