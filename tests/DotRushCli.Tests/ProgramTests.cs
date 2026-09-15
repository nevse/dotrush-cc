namespace DotRushCli.Tests;

public class ProgramTests
{
    static (int Exit, string Stdout, string Stderr) Run(params string[] args)
    {
        var stdout = new StringWriter();
        var stderr = new StringWriter();
        var exit = Program.Run(args, new Dictionary<string, string?>(), Path.GetTempPath(), stdout, stderr);
        return (exit, stdout.ToString(), stderr.ToString());
    }

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

    [Fact]
    public void Help_prints_usage_to_stdout_and_returns_0()
    {
        var (exit, stdout, stderr) = Run("help");

        Assert.Equal(0, exit);
        Assert.StartsWith("Usage:", stdout);
        Assert.Equal("", stderr);
    }
}
