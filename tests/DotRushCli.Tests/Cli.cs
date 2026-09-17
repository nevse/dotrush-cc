global using CliResult = (int Exit, string Stdout, string Stderr);

using System.Runtime.Versioning;

namespace DotRushCli.Tests;

// Runs the CLI in-process through Program.Run and captures what it writes, as the wrapper would run it.
[UnsupportedOSPlatform("windows")]
public static class Cli
{
    public static CliResult Run(IReadOnlyDictionary<string, string?> env, string cwd, params string[] args)
    {
        var stdout = new StringWriter();
        var stderr = new StringWriter();
        var exit = Program.Run(args, env, cwd, stdout, stderr);
        return (exit, stdout.ToString(), stderr.ToString());
    }
}
