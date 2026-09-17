using System.Runtime.Versioning;

namespace DotRushCli.Tests;

[UnsupportedOSPlatform("windows")]
public class ProgramTests
{
    static CliResult Run(params string[] args) => Cli.Run(new Dictionary<string, string?>(), Path.GetTempPath(), args);

    [Fact]
    public void No_arguments_print_usage_to_stderr_and_return_2()
    {
        var (exit, stdout, stderr) = Run();

        Assert.Equal(2, exit);
        Assert.Equal("", stdout);
        Assert.StartsWith("Usage:", stderr);
    }

    [Fact]
    public void An_unknown_command_prints_usage_to_stderr_and_returns_2()
    {
        var (exit, stdout, stderr) = Run("frobnicate", "--now");

        Assert.Equal(2, exit);
        Assert.Equal("", stdout);
        Assert.Contains("unknown command 'frobnicate'", stderr);
        Assert.Contains("Usage:", stderr);
    }

    [Theory]
    [InlineData("help")]
    [InlineData("-h")]
    [InlineData("--help")]
    public void Help_prints_usage_to_stdout_and_returns_0(string argument)
    {
        var (exit, stdout, stderr) = Run(argument);

        Assert.Equal(0, exit);
        Assert.StartsWith("Usage:", stdout);
        Assert.Equal("", stderr);
    }
}
