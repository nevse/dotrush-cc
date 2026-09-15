using System.Diagnostics;
using System.Runtime.Versioning;
using System.Text.RegularExpressions;

namespace DotRushCli.Tests;

// Runs scripts/dotrush-cli.sh from a copy of the plugin laid out as Claude Code installs it, so the
// wrapper has to derive the data dir from its own path: CLAUDE_PLUGIN_DATA is never set here.
[UnsupportedOSPlatform("windows")]
public sealed partial class WrapperTests : IDisposable
{
    internal static readonly string Checkout = FindCheckout();

    readonly DirectoryInfo root = Directory.CreateTempSubdirectory("dotrush-cli-wrapper-");
    readonly string plugin;
    readonly string data;
    readonly string calls;
    readonly string stub;

    public WrapperTests()
    {
        plugin = Path.Combine(root.FullName, ".claude/plugins/cache/dotrush-cc/dotrush/0.7.0");
        data = Path.Combine(root.FullName, ".claude/plugins/data/dotrush-dotrush-cc");
        CopyTree(Path.Combine(Checkout, "plugins/dotrush/scripts"), Path.Combine(plugin, "scripts"));
        CopyTree(Path.Combine(Checkout, "plugins/dotrush/tools"), Path.Combine(plugin, "tools"));
        File.Copy(Path.Combine(Checkout, "plugins/dotrush/dotrush-version.json"), Path.Combine(plugin, "dotrush-version.json"));

        calls = Path.Combine(root.FullName, "dotnet.calls");
        stub = Path.Combine(root.FullName, "dotnet");
        File.WriteAllText(stub, """
            #!/bin/sh
            printf '%s|%s\n' "$(pwd -P)" "$*" >> "$STUB_CALLS"
            if [ "$1" = build ]; then
              if [ -n "${STUB_BUILD_FAIL:-}" ]; then
                echo "restoring"
                echo "error CS1002: stub build failed"
                exit 1
              fi
              out=""
              while [ $# -gt 0 ]; do
                if [ "$1" = -o ]; then out="$2"; fi
                shift
              done
              mkdir -p "$out" && : > "$out/DotRushCli.dll"
              exit 0
            fi
            echo "ran $*"
            echo "data $DOTRUSH_DATA_DIR"
            """.Replace("\r\n", "\n"));
        File.SetUnixFileMode(stub, UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute);
    }

    public void Dispose()
    {
        try { root.Delete(recursive: true); } catch (IOException) { } catch (UnauthorizedAccessException) { }
    }

    string Tools => Path.Combine(plugin, "tools");
    string CliDir => Path.Combine(data, "cli");

    string[] HashDirs() => Directory.Exists(CliDir)
        ? Directory.GetDirectories(CliDir).Where(dir => HashName().IsMatch(Path.GetFileName(dir))).ToArray()
        : [];

    string[] Calls() => File.Exists(calls) ? File.ReadAllLines(calls) : [];

    string[] BuildCalls() => Calls().Where(line => line.Split('|', 2)[1].StartsWith("build ")).ToArray();

    [GeneratedRegex("^[0-9a-f]{64}$")]
    private static partial Regex HashName();

    [Fact]
    public void The_first_run_builds_into_the_derived_data_dir_without_claude_plugin_data()
    {
        var result = RunWrapper(["session", "--dir"], Stubbed());

        Assert.Equal(0, result.Exit);
        var hashDir = Assert.Single(HashDirs());
        Assert.True(File.Exists(Path.Combine(hashDir, "DotRushCli.dll")));
        Assert.Single(BuildCalls());
        Assert.Equal($"ran {Path.Combine(hashDir, "DotRushCli.dll")} session --dir", result.Stdout.Split('\n')[0]);
        Assert.Contains($"data {data}\n", result.Stdout);
        Assert.False(Directory.Exists(Path.Combine(data, "cli.lock")));
        Assert.DoesNotContain(Directory.GetDirectories(CliDir), dir => dir.Contains(".new."));
    }

    [Fact]
    public void A_second_run_does_not_build()
    {
        Assert.Equal(0, RunWrapper(["help"], Stubbed()).Exit);
        var second = RunWrapper(["help"], Stubbed());

        Assert.Equal(0, second.Exit);
        Assert.Single(BuildCalls());
        Assert.Equal(3, Calls().Length);
        Assert.Single(HashDirs());
    }

    [Fact]
    public void Changed_sources_build_a_new_hash_dir_and_prune_stale_ones()
    {
        Assert.Equal(0, RunWrapper(["help"], Stubbed()).Exit);
        var first = Assert.Single(HashDirs());
        Directory.SetLastWriteTimeUtc(first, DateTime.UtcNow.AddDays(-2));

        File.AppendAllText(Path.Combine(Tools, "DotRushCli/Program.cs"), "\n// changed\n");
        var result = RunWrapper(["help"], Stubbed());

        Assert.Equal(0, result.Exit);
        Assert.Equal(2, BuildCalls().Length);
        var second = Assert.Single(HashDirs());
        Assert.NotEqual(Path.GetFileName(first), Path.GetFileName(second));
    }

    [Fact]
    public void Build_output_under_bin_or_obj_does_not_change_the_hash()
    {
        Assert.Equal(0, RunWrapper(["help"], Stubbed()).Exit);
        Directory.CreateDirectory(Path.Combine(Tools, "DotRushCli/obj"));
        File.WriteAllText(Path.Combine(Tools, "DotRushCli/obj/project.assets.json"), "{}");
        Directory.CreateDirectory(Path.Combine(Tools, "DotRushCli/bin/Release"));
        File.WriteAllText(Path.Combine(Tools, "DotRushCli/bin/Release/DotRushCli.dll"), "");

        Assert.Equal(0, RunWrapper(["help"], Stubbed()).Exit);
        Assert.Single(BuildCalls());
    }

    [Fact]
    public void A_failing_build_prints_the_log_tail_exits_1_and_leaves_no_hash_dir()
    {
        var env = Stubbed();
        env["STUB_BUILD_FAIL"] = "1";

        var result = RunWrapper(["help"], env);

        Assert.Equal(1, result.Exit);
        Assert.Contains("error CS1002: stub build failed", result.Stderr);
        Assert.Contains(Path.Combine(data, "cli-build.log"), result.Stderr);
        Assert.DoesNotContain("ran ", result.Stdout);
        Assert.Empty(HashDirs());
        Assert.Empty(Directory.GetDirectories(CliDir));
        Assert.False(Directory.Exists(Path.Combine(data, "cli.lock")));
    }

    [Fact]
    public void Dotrush_cli_dir_skips_building()
    {
        var prebuilt = Path.Combine(root.FullName, "prebuilt");
        var env = Stubbed();
        env["DOTRUSH_CLI_DIR"] = prebuilt;

        var result = RunWrapper(["help"], env);

        Assert.Equal(0, result.Exit);
        Assert.Empty(BuildCalls());
        Assert.Equal($"ran {Path.Combine(prebuilt, "DotRushCli.dll")} help", result.Stdout.Split('\n')[0]);
        Assert.Contains($"data {data}\n", result.Stdout);
        Assert.False(Directory.Exists(CliDir));
    }

    [Fact]
    public void The_build_runs_with_tools_as_its_working_directory()
    {
        var elsewhere = Directory.CreateDirectory(Path.Combine(root.FullName, "repo")).FullName;

        Assert.Equal(0, RunWrapper(["help"], Stubbed(), elsewhere).Exit);

        var build = Assert.Single(BuildCalls()).Split('|', 2);
        Assert.Equal(RealPath(Tools), build[0]);
        Assert.StartsWith("build DotRushCli/DotRushCli.csproj -c Release --nologo --artifacts-path ", build[1]);
    }

    [Fact]
    public void A_real_build_ignores_a_foreign_global_json_and_leaves_the_tools_tree_clean()
    {
        var repo = Directory.CreateDirectory(Path.Combine(root.FullName, "repo")).FullName;
        File.WriteAllText(Path.Combine(repo, "global.json"),
            """{ "sdk": { "version": "1.0.100", "rollForward": "disable" } }""");

        var result = RunWrapper(["frobnicate"], BaseEnvironment(), repo, TimeSpan.FromMinutes(5));

        Assert.True(result.Exit == 2, $"exit {result.Exit}\nstdout: {result.Stdout}\nstderr: {result.Stderr}");
        Assert.Contains("unknown command 'frobnicate'", result.Stderr);
        var hashDir = Assert.Single(HashDirs());
        Assert.True(File.Exists(Path.Combine(hashDir, "DotRushCli.dll")));
        Assert.Empty(Directory.GetDirectories(Tools, "bin", SearchOption.AllDirectories));
        Assert.Empty(Directory.GetDirectories(Tools, "obj", SearchOption.AllDirectories));
    }

    Dictionary<string, string?> Stubbed()
    {
        var env = BaseEnvironment();
        env["DOTRUSH_DOTNET"] = stub;
        env["STUB_CALLS"] = calls;
        return env;
    }

    // The test host's environment without anything that would point the wrapper at a data dir or a
    // build, and without the MSBuild variables `dotnet test` leaves behind for its child processes.
    internal static Dictionary<string, string?> BaseEnvironment()
    {
        string[] dropped = ["CLAUDE_PLUGIN_DATA", "CLAUDE_PLUGIN_ROOT", "DOTRUSH_CLI_DIR", "DOTRUSH_DATA_DIR", "DOTRUSH_DOTNET"];
        var env = new Dictionary<string, string?>();
        foreach (System.Collections.DictionaryEntry entry in Environment.GetEnvironmentVariables())
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

    (int Exit, string Stdout, string Stderr) RunWrapper(string[] args, Dictionary<string, string?> env,
        string? cwd = null, TimeSpan? timeout = null)
    {
        var start = new ProcessStartInfo("bash")
        {
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            RedirectStandardInput = true,
            UseShellExecute = false,
            WorkingDirectory = cwd ?? root.FullName,
        };
        start.ArgumentList.Add(Path.Combine(plugin, "scripts/dotrush-cli.sh"));
        foreach (var arg in args)
        {
            start.ArgumentList.Add(arg);
        }
        start.Environment.Clear();
        foreach (var (key, value) in env)
        {
            start.Environment[key] = value;
        }
        return RunProcess(start, timeout ?? TimeSpan.FromSeconds(30));
    }

    internal static (int Exit, string Stdout, string Stderr) RunProcess(ProcessStartInfo start, TimeSpan timeout)
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

    static string RealPath(string path)
    {
        var start = new ProcessStartInfo("bash")
        {
            RedirectStandardOutput = true, RedirectStandardError = true, RedirectStandardInput = true,
        };
        start.ArgumentList.Add("-c");
        start.ArgumentList.Add("cd \"$1\" && pwd -P");
        start.ArgumentList.Add("_");
        start.ArgumentList.Add(path);
        var (exit, stdout, stderr) = RunProcess(start, TimeSpan.FromSeconds(10));
        Assert.True(exit == 0, stderr);
        return stdout.Trim();
    }

    static void CopyTree(string from, string to)
    {
        Directory.CreateDirectory(to);
        foreach (var file in Directory.GetFiles(from))
        {
            File.Copy(file, Path.Combine(to, Path.GetFileName(file)));
        }
        foreach (var dir in Directory.GetDirectories(from))
        {
            var name = Path.GetFileName(dir);
            if (name is not ("bin" or "obj"))
            {
                CopyTree(dir, Path.Combine(to, name));
            }
        }
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
