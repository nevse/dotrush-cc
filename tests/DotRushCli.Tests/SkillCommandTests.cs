using System.Runtime.Versioning;
using System.Text.RegularExpressions;

namespace DotRushCli.Tests;

// Every `dotrush-cli.sh <command> …` line a skill documents must name a command, and only flags, that the CLI's
// usage table (its `help` output) lists.
[UnsupportedOSPlatform("windows")]
public partial class SkillCommandTests
{
    static readonly string SkillsDir = Path.Combine(WrapperTests.Checkout, "plugins/dotrush/skills");

    // The rest of the line after the script name, as in `"${CLAUDE_PLUGIN_ROOT}/scripts/dotrush-cli.sh" session --dir`.
    [GeneratedRegex(@"dotrush-cli\.sh""?[ \t]+([^\r\n]*)")]
    private static partial Regex Invocation();

    // A usage line is "  dotrush-cli.sh <synopsis>  <description>"; words inside a synopsis are one space apart.
    [GeneratedRegex(@"^  dotrush-cli\.sh (\S+(?: \S+)*)  ", RegexOptions.Multiline)]
    private static partial Regex UsageLine();

    [GeneratedRegex(@"(?<![\w-])--[a-z][a-z-]*")]
    private static partial Regex Flag();

    // A command line's subcommand words (`rename preview`) and the flags it uses.
    sealed record CommandLine(string Path, HashSet<string> Flags);

    [GeneratedRegex(@"<[^<>\s]*>")]
    private static partial Regex Placeholder();

    static CommandLine Parse(string text)
    {
        // Placeholders such as <plan-id> go first, so their > is not read as a redirection; then a shell operator
        // ends the command: `$(… session --dir)`, pipes, redirections.
        text = Placeholder().Replace(text, " ARG ");
        var end = text.IndexOfAny([')', '|', ';', '&', '<', '>', '`']);
        var command = end < 0 ? text : text[..end];
        var words = command.Split((char[]?)null, StringSplitOptions.RemoveEmptyEntries)
            .TakeWhile(word => word.All(char.IsAsciiLetterLower));
        return new(string.Join(' ', words), [.. Flag().Matches(command).Select(match => match.Value)]);
    }

    static List<CommandLine> UsageTable()
    {
        var stdout = new StringWriter();
        Assert.Equal(0, Program.Run(["help"], new Dictionary<string, string?>(), Path.GetTempPath(), stdout, new StringWriter()));
        var usage = UsageLine().Matches(stdout.ToString()).Select(match => Parse(match.Groups[1].Value)).ToList();
        Assert.NotEmpty(usage);
        return usage;
    }

    static List<(string Skill, string Text)> SkillCommandLines() =>
        [.. Directory.GetFiles(SkillsDir, "SKILL.md", SearchOption.AllDirectories)
            .Order(StringComparer.Ordinal)
            .SelectMany(file => Invocation().Matches(File.ReadAllText(file))
                .Select(match => (Path.GetRelativePath(SkillsDir, file), match.Groups[1].Value)))];

    [Fact]
    public void Every_cli_command_line_in_a_skill_is_in_the_usage_table()
    {
        var usage = UsageTable();
        var lines = SkillCommandLines();
        Assert.NotEmpty(lines);

        var problems = new List<string>();
        foreach (var (skill, text) in lines)
        {
            var line = Parse(text);
            var known = usage.Where(entry => entry.Path == line.Path).ToList();
            if (line.Path.Length == 0 || known.Count == 0)
            {
                problems.Add($"{skill}: dotrush-cli.sh {text}: no command '{line.Path}' in the usage table");
                continue;
            }
            var unknown = line.Flags.Where(flag => !known.Any(entry => entry.Flags.Contains(flag))).ToList();
            if (unknown.Count > 0)
            {
                problems.Add($"{skill}: dotrush-cli.sh {text}: '{line.Path}' has no {string.Join(", ", unknown)}");
            }
        }
        Assert.True(problems.Count == 0, string.Join("\n", problems));
    }

    [Fact]
    public void The_rename_skill_runs_preview_and_apply()
    {
        var paths = SkillCommandLines()
            .Where(line => line.Skill.StartsWith("dotrush-rename/", StringComparison.Ordinal))
            .Select(line => Parse(line.Text).Path)
            .ToHashSet();

        Assert.Contains("rename preview", paths);
        Assert.Contains("rename apply", paths);
    }
}
