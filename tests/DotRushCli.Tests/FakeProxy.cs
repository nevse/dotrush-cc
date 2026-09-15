using System.Runtime.InteropServices;
using System.Runtime.Versioning;
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

    public Dictionary<string, string?> Env() => new()
    {
        ["DOTRUSH_DATA_DIR"] = DataDir,
        ["DOTRUSH_SESSION_ID"] = SessionId,
    };

    public (int Exit, string Stdout, string Stderr) Run(params string[] args)
    {
        var stdout = new StringWriter();
        var stderr = new StringWriter();
        var exit = Program.Run(args, Env(), Workspace, stdout, stderr);
        return (exit, stdout.ToString(), stderr.ToString());
    }

    // Writes {"jsonrpc":"2.0","id":<id>,<fields>} to responses/<uuid>.json, atomically as the proxy does.
    public void Respond(string id, string fields) =>
        RespondWithBody(id, $$"""{"jsonrpc":"2.0","id":"{{id}}",{{fields}}}""");

    // Writes body verbatim to responses/<uuid>.json, as the proxy does with whatever the server sent.
    public void RespondWithBody(string id, string body)
    {
        const string prefix = "dotrush-cc:";
        if (!id.StartsWith(prefix, StringComparison.Ordinal))
        {
            throw new ArgumentException($"not a request-channel id: {id}", nameof(id));
        }
        var target = Path.Combine(ResponsesDir, id[prefix.Length..] + ".json");
        var temp = target + "." + Environment.ProcessId + ".tmp";
        File.WriteAllText(temp, body);
        File.Move(temp, target, overwrite: true);
        Interlocked.Increment(ref responses);
    }

    public void Dispose()
    {
        stopping = true;
        // A read-write open of a FIFO never blocks and counts as both a reader and a writer, so it releases the
        // reader thread blocked in open and any CLI writer blocked in open for want of a reader. The newline wakes
        // a reader blocked in read so it sees `stopping`.
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
    }

    void ReadLoop()
    {
        while (!stopping)
        {
            try
            {
                // Reopened after every writer closes, as the proxy's injector does.
                using var stream = new FileStream(Fifo, FileMode.Open, FileAccess.Read, FileShare.ReadWrite, bufferSize: 0);
                using var text = new StreamReader(stream);
                while (text.ReadLine() is { } line)
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

    void Handle(string line)
    {
        lock (lines) lines.Add(line);
        try
        {
            if (JsonNode.Parse(line) is JsonObject message)
            {
                OnMessage(this, message);
            }
        }
        catch (Exception)
        {
            // A test's handler or a malformed line must not crash the test host from this thread; the test's own
            // assertions on Lines and the CLI's output report what went wrong.
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
