using System.Collections;

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

public static class Program
{
    sealed record Command(string Name, string Synopsis, string Description, Func<CommandContext, string[], int> Handler);

    // The usage table: every command the CLI accepts, in the order the usage text lists them.
    static readonly Command[] Commands =
    [
        new("session", Session.Synopsis, "print this session's DotRush runtime dir and its state (--dir: the dir only)",
            Session.Run),
        new("request", RequestCommand.Synopsis,
            "send an LSP request to this session's DotRush server and print its result as JSON (default timeout 60 s)",
            RequestCommand.Run),
        new("help", "help", "print this help", (context, _) => PrintUsage(context.Stdout, ExitCode.Success)),
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
        if (args.Length == 0)
        {
            return PrintUsage(stderr, ExitCode.Usage);
        }
        var command = Commands.FirstOrDefault(candidate => candidate.Name == args[0]);
        if (command is null)
        {
            stderr.WriteLine($"dotrush-cli: unknown command '{args[0]}'");
            return PrintUsage(stderr, ExitCode.Usage);
        }
        return command.Handler(new CommandContext(env, cwd, stdout, stderr), args[1..]);
    }

    static int PrintUsage(TextWriter writer, int exitCode)
    {
        var width = Commands.Max(command => command.Synopsis.Length);
        writer.WriteLine("Usage:");
        foreach (var command in Commands)
        {
            writer.WriteLine($"  dotrush-cli.sh {command.Synopsis.PadRight(width)}  {command.Description}");
        }
        writer.WriteLine();
        writer.WriteLine("Exit status: 0 success, 1 error, 2 usage, 3 timeout.");
        return exitCode;
    }
}
