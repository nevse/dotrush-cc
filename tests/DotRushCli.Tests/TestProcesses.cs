using System.Collections;
using System.Diagnostics;

namespace DotRushCli.Tests;

// The checkout under test, and child processes started from it with a clean environment.
public static class TestProcesses
{
    public static readonly string Checkout = FindCheckout();

    // The test host's environment without anything that would point the wrapper at a data dir or a
    // build, and without the MSBuild variables `dotnet test` leaves behind for its child processes.
    public static Dictionary<string, string?> BaseEnvironment()
    {
        string[] dropped = ["CLAUDE_PLUGIN_DATA", "CLAUDE_PLUGIN_ROOT", "DOTRUSH_CLI_DIR", "DOTRUSH_DATA_DIR", "DOTRUSH_DOTNET"];
        var env = new Dictionary<string, string?>();
        foreach (DictionaryEntry entry in Environment.GetEnvironmentVariables())
        {
            var key = (string)entry.Key;
            if (dropped.Contains(key) || key.StartsWith("MSBUILD", StringComparison.OrdinalIgnoreCase)
                || key == "DOTNET_HOST_PATH")
            {
                continue;
            }
            env[key] = (string?)entry.Value;
        }
        return env;
    }

    // Runs a bash script with exactly env as its environment, in cwd.
    public static CliResult RunScript(string path, IEnumerable<string> args, IReadOnlyDictionary<string, string?> env,
        string cwd, TimeSpan timeout)
    {
        var start = new ProcessStartInfo("bash")
        {
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            RedirectStandardInput = true,
            UseShellExecute = false,
            WorkingDirectory = cwd,
        };
        start.ArgumentList.Add(path);
        foreach (var arg in args)
        {
            start.ArgumentList.Add(arg);
        }
        start.Environment.Clear();
        foreach (var (key, value) in env)
        {
            start.Environment[key] = value;
        }
        return RunProcess(start, timeout);
    }

    public static CliResult RunProcess(ProcessStartInfo start, TimeSpan timeout)
    {
        using var process = Process.Start(start)!;
        process.StandardInput.Close();
        var stdout = process.StandardOutput.ReadToEndAsync();
        var stderr = process.StandardError.ReadToEndAsync();
        if (!process.WaitForExit(timeout))
        {
            process.Kill(entireProcessTree: true);
            throw new TimeoutException($"{start.FileName} {string.Join(' ', start.ArgumentList)} did not exit in {timeout}");
        }
        return (process.ExitCode, stdout.Result, stderr.Result);
    }

    static string FindCheckout()
    {
        for (var dir = new DirectoryInfo(AppContext.BaseDirectory); dir is not null; dir = dir.Parent)
        {
            if (File.Exists(Path.Combine(dir.FullName, "plugins/dotrush/scripts/dotrush-install.sh")))
            {
                return dir.FullName;
            }
        }
        throw new InvalidOperationException($"no dotrush-cc checkout above {AppContext.BaseDirectory}");
    }
}
