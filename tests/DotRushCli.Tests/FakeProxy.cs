using Microsoft.Win32.SafeHandles;
using System.Runtime.InteropServices;
using System.Runtime.Versioning;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace DotRushCli.Tests;

// A ready session dir (live pid, responses/, load-completed) whose inject.fifo is read by a thread standing in
// for lsp-proxy.py's injector: it records every line and hands each JSON object to OnMessage, which can answer
// a request with Respond the way the proxy routes a dotrush-cc: response into responses/<uuid>.json.
[UnsupportedOSPlatform("windows")]
public sealed partial class FakeProxy : IDisposable
{
    public const string SessionId = "fake-proxy-session";
    const int O_RDWR = 2;

    readonly DirectoryInfo root = Directory.CreateTempSubdirectory("dotrush-cli-channel-");
    readonly List<string> lines = [];
    readonly Thread? reader;
    volatile bool stopping;
    int responses;
    Exception? handlerFailure;

    public string DataDir { get; }
    public string Workspace { get; }
    public string Dir { get; }
    public string Fifo { get; }
    public string ResponsesDir { get; }

    // Called on the reader thread for every line that parses as a JSON object.
    public Action<FakeProxy, JsonObject> OnMessage { get; set; } = (_, _) => { };

    public FakeProxy(bool withReader = true)
    {
        DataDir = Path.Combine(root.FullName, "data");
        Workspace = Directory.CreateDirectory(Path.Combine(root.FullName, "project")).FullName;
        Dir = Directory.CreateDirectory(Path.Combine(DataDir, "ws", "sess-0123456789ab")).FullName;
        File.WriteAllText(Path.Combine(Dir, "workspace.txt"), Workspace + "\n");
        File.WriteAllText(Path.Combine(Dir, "session.txt"), SessionId + "\n");
        File.WriteAllText(Path.Combine(Dir, "pid"), Environment.ProcessId + "\n");
        File.WriteAllText(Path.Combine(Dir, "load-completed"), "");
        ResponsesDir = Directory.CreateDirectory(Path.Combine(Dir, "responses")).FullName;
        Fifo = Path.Combine(Dir, "inject.fifo");
        if (mkfifo(Fifo, 0b110_000_000) != 0)
        {
            throw new IOException($"mkfifo {Fifo} failed: errno {Marshal.GetLastPInvokeError()}");
        }
        if (withReader)
        {
            reader = new Thread(ReadLoop) { IsBackground = true, Name = "FakeProxy reader" };
            reader.Start();
        }
    }

    public IReadOnlyList<string> Lines
    {
        get { lock (lines) return [.. lines]; }
    }

    // How many responses Respond has written.
    public int Responses => Volatile.Read(ref responses);

    // A line the test itself sends through the FIFO. Once the reader has recorded it, everything the CLI wrote
    // before it has been recorded too, so a test can assert on what did — or did not — arrive without sleeping.
    public const string PingLine = """{"method":"dotrush-cc/ping"}""";

    public void Ping()
    {
        var write = Task.Run(() =>
        {
            using var fifo = new FileStream(Fifo, FileMode.Open, FileAccess.Write, FileShare.ReadWrite, bufferSize: 0);
            fifo.Write(Encoding.UTF8.GetBytes(PingLine + "\n"));
        });
        Assert.True(write.Wait(TimeSpan.FromSeconds(10)), $"nothing read the ping from {Fifo}");
        var deadline = DateTime.UtcNow.AddSeconds(10);
        while (!Lines.Contains(PingLine) && DateTime.UtcNow < deadline)
        {
            Thread.Sleep(10);
        }
        Assert.Contains(PingLine, Lines);
    }

    public Dictionary<string, string?> Env() => new()
    {
        ["DOTRUSH_DATA_DIR"] = DataDir,
        ["DOTRUSH_SESSION_ID"] = SessionId,
    };

    public CliResult Run(params string[] args) => Cli.Run(Env(), Workspace, args);

    // Writes {"jsonrpc":"2.0","id":<id>,<fields>} to responses/<uuid>.json, atomically as the proxy does.
    public void Respond(string id, string fields) =>
        RespondWithBody(id, $$"""{"jsonrpc":"2.0","id":"{{id}}",{{fields}}}""");

    // Writes body verbatim to responses/<uuid>.json, as the proxy does with whatever the server sent.
    public void RespondWithBody(string id, string body)
    {
        if (!id.StartsWith(LspChannel.IdPrefix, StringComparison.Ordinal))
        {
            throw new ArgumentException($"not a request-channel id: {id}", nameof(id));
        }
        var target = Path.Combine(ResponsesDir, id[LspChannel.IdPrefix.Length..] + ".json");
        var temp = target + "." + Environment.ProcessId + ".tmp";
        File.WriteAllText(temp, body);
        File.Move(temp, target, overwrite: true);
        Interlocked.Increment(ref responses);
    }

    public void Dispose()
    {
        stopping = true;
        // A read-write open of a FIFO never blocks and counts as both a reader and a writer, so it releases any CLI
        // writer blocked in open for want of a reader (a FakeProxy without one). The newline wakes the reader
        // blocked in read so it sees `stopping`.
        var fd = open(Fifo, O_RDWR);
        if (fd >= 0)
        {
            try
            {
                write(fd, "\n"u8.ToArray(), 1);
                reader?.Join(TimeSpan.FromSeconds(2));
                Thread.Sleep(100);
            }
            finally
            {
                close(fd);
            }
        }
        try { root.Delete(recursive: true); } catch (IOException) { } catch (UnauthorizedAccessException) { }
        // An assertion a test put in its handler runs on the reader thread, where throwing would only kill that
        // thread; it is reported here instead, so such a test fails rather than silently timing out or passing.
        if (Volatile.Read(ref handlerFailure) is { } failure)
        {
            throw new InvalidOperationException($"the FakeProxy's OnMessage handler failed: {failure.Message}", failure);
        }
    }

    void ReadLoop()
    {
        while (!stopping)
        {
            try
            {
                // Like the proxy's injector, one open for the reader's whole life that also holds a write end (a
                // read-write open of a FIFO counts as both), so the reader never sees end of file when a writer
                // leaves and nothing a later writer sends can fall into a reopen.
                var fd = open(Fifo, O_RDWR);
                if (fd < 0)
                {
                    throw new IOException($"open {Fifo} failed: errno {Marshal.GetLastPInvokeError()}");
                }
                using var stream = new FileStream(new SafeFileHandle(fd, ownsHandle: true), FileAccess.Read, bufferSize: 0);
                while (ReadLine(stream) is { } line)
                {
                    if (stopping)
                    {
                        return;
                    }
                    Handle(line);
                }
            }
            catch (Exception) when (stopping)
            {
                return;
            }
            catch (IOException)
            {
                Thread.Sleep(50);
            }
        }
    }

    // One line without its newline, or null at end of file. Read byte by byte, so the reader holds nothing past the
    // line it hands on.
    static string? ReadLine(Stream stream)
    {
        var line = new MemoryStream();
        while (true)
        {
            var next = stream.ReadByte();
            if (next < 0)
            {
                return line.Length == 0 ? null : Encoding.UTF8.GetString(line.ToArray());
            }
            if (next == '\n')
            {
                return Encoding.UTF8.GetString(line.ToArray());
            }
            line.WriteByte((byte)next);
        }
    }

    void Handle(string line)
    {
        lock (lines)
        {
            lines.Add(line);
        }
        JsonObject? message;
        try
        {
            // A line that is not a JSON object (the newline Dispose writes) is only recorded.
            message = JsonNode.Parse(line) as JsonObject;
        }
        catch (JsonException)
        {
            return;
        }
        if (message is null)
        {
            return;
        }
        try
        {
            OnMessage(this, message);
        }
        catch (Exception e)
        {
            // Kept for Dispose to report: throwing here would only end the reader thread.
            Interlocked.CompareExchange(ref handlerFailure, e, null);
        }
    }

    [DllImport("libc", SetLastError = true)]
    static extern int mkfifo([MarshalAs(UnmanagedType.LPUTF8Str)] string path, uint mode);

    [DllImport("libc", SetLastError = true)]
    static extern int open([MarshalAs(UnmanagedType.LPUTF8Str)] string path, int flags);

    [DllImport("libc", SetLastError = true)]
    static extern nint write(int fd, byte[] buffer, nint count);

    [DllImport("libc")]
    static extern int close(int fd);
}
