using System.Runtime.InteropServices;
using System.Text.Json;

namespace DotRushCli;

// A per-session runtime dir as lsp-proxy.py lays it out: workspace.txt, session.txt, pid, target.json,
// diagnostics.json, load-completed and responses/. Every property reads the disk when asked.
public sealed record SessionState(string Dir)
{
    public string? Workspace => ReadTrimmed("workspace.txt");

    public int? Pid => int.TryParse(ReadTrimmed("pid"), out var pid) && pid > 0 ? pid : null;

    public bool IsProxyRunning => Pid is { } pid && Posix.IsAlive(pid);

    public bool LoadCompleted => File.Exists(Path.Combine(Dir, "load-completed"));

    public string? Target => ReadTrimmed("target.json");

    public bool CapturesDiagnostics => File.Exists(Path.Combine(Dir, "diagnostics.json"));

    public bool HasChannel => Directory.Exists(Path.Combine(Dir, "responses"));

    // Like `$(cat file)`: the content without trailing newlines, or null when the file cannot be read.
    string? ReadTrimmed(string name)
    {
        try
        {
            return File.ReadAllText(Path.Combine(Dir, name)).TrimEnd('\n');
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException)
        {
            return null;
        }
    }

    // What diagnostics.json counts as published, as `dotrush-diagnostics.sh where` printed it.
    public string Publishes()
    {
        try
        {
            using var document = JsonDocument.Parse(File.ReadAllBytes(Path.Combine(Dir, "diagnostics.json")));
            if (document.RootElement.ValueKind != JsonValueKind.Object
                || !document.RootElement.TryGetProperty("publishes", out var publishes))
            {
                return "0";
            }
            return publishes.ValueKind == JsonValueKind.String ? publishes.GetString()! : publishes.GetRawText();
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException or JsonException)
        {
            return "unknown";
        }
    }
}

public static class SessionCommand
{
    public const string Synopsis = "session [--dir]";

    // `session` prints the dir and its state; `session --dir` only the dir. Both succeed whenever a dir
    // is found, however unready the session is: readiness is each caller's own check.
    public static int Run(CommandContext context, string[] args)
    {
        var dirOnly = args is ["--dir"];
        if (args.Length > 0 && !dirOnly)
        {
            return CliErrors.Usage(context, null, Synopsis);
        }
        var session = Session.Locate(context);
        if (session is null)
        {
            return ExitCode.Error;
        }
        var stdout = context.Stdout;
        if (dirOnly)
        {
            stdout.WriteLine(session.Dir);
            return ExitCode.Success;
        }
        stdout.WriteLine($"dir: {session.Dir}");
        stdout.WriteLine($"workspace: {session.Workspace ?? "unknown"}");
        stdout.WriteLine(session.IsProxyRunning ? $"proxy: running (pid {session.Pid})" : "proxy: not running");
        stdout.WriteLine(session.LoadCompleted
            ? "load: completed" : "load: not completed (no project loaded yet, or still loading)");
        stdout.WriteLine($"target: {session.Target ?? "none chosen"}");
        stdout.WriteLine(session.CapturesDiagnostics
            ? $"publishes: {session.Publishes()}" : "publishes: no capture (older proxy)");
        stdout.WriteLine(session.HasChannel ? "channel: available" : "channel: unavailable (older proxy)");
        return ExitCode.Success;
    }
}

// Finds this session's runtime dir and checks it is ready for the request channel.
public static class Session
{
    // The checks `request` and `rename` need before they write to the proxy's FIFO, in order.
    public static int RequireChannel(CommandContext context, out SessionState? session)
    {
        session = null;
        var found = Locate(context);
        if (found is null)
        {
            return ExitCode.Error;
        }
        var problem =
            !found.IsProxyRunning
                ? "the DotRush language server for this session is not running; run any C# LSP operation to start it"
            : !found.HasChannel
                ? "the running DotRush proxy predates the request channel; restart Claude Code"
            : !found.LoadCompleted
                ? "DotRush has not finished loading a project in this session; choose one with dotrush-pick-project, or wait for the load to finish and retry"
            : null;
        if (problem is not null)
        {
            return CliErrors.Fail(context, problem);
        }
        session = found;
        return ExitCode.Success;
    }

    // This session's runtime dir, or null after printing why there is none.
    public static SessionState? Locate(CommandContext context)
    {
        var dataDir = Get(context.Env, "DOTRUSH_DATA_DIR");
        if (dataDir is null)
        {
            CliErrors.Write(context.Stderr, "DOTRUSH_DATA_DIR is not set; run the CLI through scripts/dotrush-cli.sh");
            return null;
        }
        var ws = Path.Combine(dataDir, "ws");
        var dir = Find(ws, context.Env, context.Cwd);
        if (dir is null)
        {
            CliErrors.Write(context.Stderr,
                $"no DotRush language server has started in this session (looked in {ws}); run any C# LSP operation first");
            return null;
        }
        return new SessionState(dir);
    }

    // 1. with a session id, a sess-* dir whose session.txt records it;
    // 2. otherwise, or when none does, a dir without the sess- prefix whose workspace.txt is the project
    //    dir (CLAUDE_PROJECT_DIR, else the cwd), so another session's dir is never picked;
    // 3. among several, a live pid first, then the newest pid file.
    static string? Find(string ws, IReadOnlyDictionary<string, string?> env, string cwd)
    {
        string[] dirs;
        try
        {
            dirs = Directory.GetDirectories(ws);
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException)
        {
            return null;
        }
        Array.Sort(dirs, StringComparer.Ordinal);

        var sessionId = Get(env, "DOTRUSH_SESSION_ID") ?? Get(env, "AGTERM_SESSION_ID");
        var matches = sessionId is null
            ? []
            : dirs.Where(dir => IsSessionDir(dir) && HasLine(Path.Combine(dir, "session.txt"), line => line == sessionId))
                .ToArray();
        if (matches.Length == 0)
        {
            var workspace = Get(env, "CLAUDE_PROJECT_DIR") ?? cwd;
            var realWorkspace = Posix.RealPath(workspace);
            matches = dirs
                .Where(dir => !IsSessionDir(dir) && HasLine(Path.Combine(dir, "workspace.txt"),
                    line => line == workspace || (realWorkspace is not null && Posix.RealPath(line) == realWorkspace)))
                .ToArray();
        }
        return matches
            .Select(dir => new SessionState(dir))
            .OrderByDescending(session => session.IsProxyRunning)
            .ThenByDescending(session => PidWritten(session.Dir))
            .Select(session => session.Dir)
            .FirstOrDefault();
    }

    static bool IsSessionDir(string dir) => Path.GetFileName(dir).StartsWith("sess-", StringComparison.Ordinal);

    static DateTime PidWritten(string dir)
    {
        var pid = Path.Combine(dir, "pid");
        return File.Exists(pid) ? File.GetLastWriteTimeUtc(pid) : DateTime.MinValue;
    }

    static bool HasLine(string path, Func<string, bool> predicate)
    {
        try
        {
            return File.ReadLines(path).Any(line => line.Length > 0 && predicate(line));
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException)
        {
            return false;
        }
    }

    // Like `${NAME:-}`: an empty value counts as unset.
    static string? Get(IReadOnlyDictionary<string, string?> env, string name) =>
        env.TryGetValue(name, out var value) && !string.IsNullOrEmpty(value) ? value : null;
}

public static class Posix
{
    [DllImport("libc", SetLastError = true)]
    static extern int kill(int pid, int sig);

    [DllImport("libc", SetLastError = true)]
    static extern IntPtr realpath([MarshalAs(UnmanagedType.LPUTF8Str)] string path, IntPtr resolved);

    [DllImport("libc")]
    static extern void free(IntPtr pointer);

    // Like `kill -0 pid`: true only when the signal could be sent.
    public static bool IsAlive(int pid) => pid > 0 && kill(pid, 0) == 0;

    // The path with every symlink resolved, or null when it does not exist.
    public static string? RealPath(string path)
    {
        var resolved = realpath(path, IntPtr.Zero);
        if (resolved == IntPtr.Zero)
        {
            return null;
        }
        try
        {
            return Marshal.PtrToStringUTF8(resolved);
        }
        finally
        {
            free(resolved);
        }
    }
}
