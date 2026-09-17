using System.Collections;
using System.Runtime.Versioning;

namespace DotRushCli;

public static class ExitCode
{
    public const int Success = 0;
    public const int Error = 1;
    public const int Usage = 2;
    public const int Timeout = 3;
}

// What a command runs with. Commands never read the process environment, working directory or Console
// directly, so tests can drive them in-process and in parallel.
public sealed record CommandContext(
    IReadOnlyDictionary<string, string?> Env, string Cwd, TextWriter Stdout, TextWriter Stderr);

// How every command reports a problem: one stderr line per message, each with the CLI's prefix.
public static class CliErrors
{
    const string Prefix = "dotrush-cli: ";

    public static void Write(TextWriter stderr, string message) => stderr.WriteLine(Prefix + message);

    public static int Fail(CommandContext context, string message)
    {
        Write(context.Stderr, message);
        return ExitCode.Error;
    }

    // The problem, when there is one, then a usage line per synopsis.
    public static int Usage(CommandContext context, string? problem, params string[] synopses)
    {
        if (problem is not null)
        {
            Write(context.Stderr, problem);
        }
        foreach (var synopsis in synopses)
        {
            Write(context.Stderr, $"usage: dotrush-cli.sh {synopsis}");
        }
        return ExitCode.Usage;
    }

    // A request that got no result: a timeout exits 3, every other failure 1.
    public static int ReportFailure(CommandContext context, LspReply reply)
    {
        Write(context.Stderr, reply.Message);
        return reply.Kind == LspReplyKind.Timeout ? ExitCode.Timeout : ExitCode.Error;
    }
}

// Deletes what a killed or abandoned run left behind in a dir: files with one of the extensions not written for
// longer than age. A dir or file that cannot be read or deleted is skipped.
public static class StaleFiles
{
    public static void Delete(string dir, TimeSpan age, params string[] extensions)
    {
        var cutoff = DateTime.UtcNow - age;
        string[] files;
        try
        {
            files = Directory.GetFiles(dir);
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException)
        {
            return;
        }
        foreach (var file in files)
        {
            try
            {
                if (extensions.Any(extension => file.EndsWith(extension, StringComparison.Ordinal))
                    && File.GetLastWriteTimeUtc(file) < cutoff)
                {
                    File.Delete(file);
                }
            }
            catch (Exception e) when (e is IOException or UnauthorizedAccessException)
            {
            }
        }
    }
}

// The CLI drives a Unix FIFO and libc; it does not run on Windows.
[UnsupportedOSPlatform("windows")]
public static class Program
{
    // One command, with a usage row per form it takes.
    sealed record Command(
        string Name, Func<CommandContext, string[], int> Handler, params (string Synopsis, string Description)[] Usage);

    // The usage table: every command the CLI accepts, in the order the usage text lists them.
    static readonly Command[] Commands =
    [
        new("session", SessionCommand.Run,
            (SessionCommand.Synopsis, "print this session's DotRush runtime dir and its state (--dir: the dir only)")),
        new("request", RequestCommand.Run,
            (RequestCommand.Synopsis,
                "send an LSP request to this session's DotRush server and print its result as JSON (default timeout 60 s)")),
        new("rename", RenameCommand.Run,
            (RenameCommand.PreviewSynopsis,
                "ask DotRush to rename the symbol at a 1-based position; save the edits as a plan and print them with a diff"),
            (RenameCommand.ApplySynopsis,
                "write a previewed plan's edits to every file (checking every file before replacing any) and have DotRush re-read the changed files")),
        new("help", (context, _) => PrintUsage(context.Stdout, ExitCode.Success),
            ("help", "print this help; -h and --help do the same")),
    ];

    public static int Main(string[] args)
    {
        var env = new Dictionary<string, string?>();
        foreach (DictionaryEntry entry in Environment.GetEnvironmentVariables())
        {
            env[(string)entry.Key] = (string?)entry.Value;
        }
        return Run(args, env, Environment.CurrentDirectory, Console.Out, Console.Error);
    }

    public static int Run(string[] args, IReadOnlyDictionary<string, string?> env, string cwd,
        TextWriter stdout, TextWriter stderr)
    {
        try
        {
            return Dispatch(args, env, cwd, stdout, stderr);
        }
        // Writing a plan, a temp file or a response: a full disk or a permission problem is an ordinary error, not a
        // stack trace on stderr and an exit code outside the documented 0/1/2/3.
        catch (Exception e) when (e is IOException or UnauthorizedAccessException)
        {
            CliErrors.Write(stderr, e.Message);
            return ExitCode.Error;
        }
    }

    static int Dispatch(string[] args, IReadOnlyDictionary<string, string?> env, string cwd,
        TextWriter stdout, TextWriter stderr)
    {
        if (args.Length == 0)
        {
            return PrintUsage(stderr, ExitCode.Usage);
        }
        var name = args[0] is "-h" or "--help" ? "help" : args[0];
        var command = Commands.FirstOrDefault(candidate => candidate.Name == name);
        if (command is null)
        {
            CliErrors.Write(stderr, $"unknown command '{args[0]}'");
            return PrintUsage(stderr, ExitCode.Usage);
        }
        return command.Handler(new CommandContext(env, cwd, stdout, stderr), args[1..]);
    }

    static int PrintUsage(TextWriter writer, int exitCode)
    {
        var rows = Commands.SelectMany(command => command.Usage).ToList();
        var width = rows.Max(row => row.Synopsis.Length);
        writer.WriteLine("Usage:");
        foreach (var (synopsis, description) in rows)
        {
            writer.WriteLine($"  dotrush-cli.sh {synopsis.PadRight(width)}  {description}");
        }
        writer.WriteLine();
        writer.WriteLine("Exit status: 0 success, 1 error, 2 usage, 3 timeout.");
        return exitCode;
    }
}
