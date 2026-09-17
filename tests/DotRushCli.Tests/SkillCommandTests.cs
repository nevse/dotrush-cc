using System.Runtime.Versioning;
using System.Text.RegularExpressions;

namespace DotRushCli.Tests;

// Every `dotrush-cli.sh <command> …` line a skill documents must name a command, and only flags, that the CLI's
// usage table (its `help` output) lists.
[UnsupportedOSPlatform("windows")]
public partial class SkillCommandTests
{
    static readonly string SkillsDir = Path.Combine(TestProcesses.Checkout, "plugins/dotrush/skills");

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

    // A list item that opens with quoted CLI output: "- `message` — what to do", or "- `a` or `b` (…) — …".
    [GeneratedRegex(@"^[ \t]*- (`.*?) — ", RegexOptions.Multiline)]
    private static partial Regex QuotedMessageItem();

    [GeneratedRegex(@"`([^`]+)`")]
    private static partial Regex Backticked();

    // A skill's stand-ins for variable text: `<file>`, `…`.
    [GeneratedRegex(@"<[^<>\s]*>|…")]
    private static partial Regex MessageHole();

    const char Hole = '\u0001';

    // Every C# string literal in the CLI sources, literals joined by `+` as one, with each interpolation hole
    // replaced by Hole. Escapes are left as written; the messages the skills quote have none.
    static List<string> CliStringLiterals()
    {
        var dir = Path.Combine(TestProcesses.Checkout, "plugins/dotrush/tools/DotRushCli");
        var literals = new List<string>();
        foreach (var file in Directory.GetFiles(dir, "*.cs"))
        {
            var text = File.ReadAllText(file);
            var joinable = false;
            for (var i = 0; i < text.Length; i++)
            {
                if (text[i] == '/' && i + 1 < text.Length && text[i + 1] == '/')
                {
                    i = text.IndexOf('\n', i) is var end and >= 0 ? end : text.Length;
                    continue;
                }
                if (text[i] == '\'')
                {
                    i = SkipCharLiteral(text, i);
                    joinable = false;
                    continue;
                }
                if (text[i] != '"')
                {
                    if (text[i] == '+' && joinable)
                    {
                        continue;
                    }
                    joinable = joinable && char.IsWhiteSpace(text[i]);
                    continue;
                }
                var verbatim = i > 0 && text[i - 1] == '@' || i > 1 && text[i - 1] == '$' && text[i - 2] == '@';
                var interpolated = i > 0 && text[i - 1] == '$' || i > 1 && text[i - 1] == '@' && text[i - 2] == '$';
                var (literal, next) = ReadLiteral(text, i + 1, verbatim, interpolated);
                if (joinable)
                {
                    literals[^1] += literal;
                }
                else
                {
                    literals.Add(literal);
                }
                i = next;
                joinable = true;
            }
        }
        return literals;
    }

    static int SkipCharLiteral(string text, int i)
    {
        var j = i + 1;
        while (j < text.Length && text[j] != '\'')
        {
            j += text[j] == '\\' ? 2 : 1;
        }
        return j;
    }

    // Reads the literal body from start (just past its opening quote); returns it and its closing quote's index.
    static (string Literal, int End) ReadLiteral(string text, int start, bool verbatim, bool interpolated)
    {
        var body = new System.Text.StringBuilder();
        var i = start;
        while (i < text.Length)
        {
            var c = text[i];
            if (c == '"')
            {
                if (verbatim && i + 1 < text.Length && text[i + 1] == '"')
                {
                    body.Append('"');
                    i += 2;
                    continue;
                }
                return (body.ToString(), i);
            }
            if (c == '\\' && !verbatim)
            {
                body.Append(text, i, 2);
                i += 2;
                continue;
            }
            if (interpolated && (c == '{' || c == '}'))
            {
                if (i + 1 < text.Length && text[i + 1] == c)
                {
                    body.Append(c);
                    i += 2;
                    continue;
                }
                if (c == '{')
                {
                    i = SkipHole(text, i + 1);
                    body.Append(Hole);
                    continue;
                }
            }
            body.Append(c);
            i++;
        }
        return (body.ToString(), i);
    }

    // Skips an interpolation hole's expression, nested literals and braces included; returns the index past its `}`.
    static int SkipHole(string text, int i)
    {
        var depth = 1;
        while (i < text.Length)
        {
            var c = text[i];
            if (c == '"')
            {
                var interpolated = text[i - 1] == '$';
                i = ReadLiteral(text, i + 1, verbatim: false, interpolated).End + 1;
                continue;
            }
            if (c == '\'')
            {
                i = SkipCharLiteral(text, i) + 1;
                continue;
            }
            depth += c == '{' ? 1 : c == '}' ? -1 : 0;
            i++;
            if (depth == 0)
            {
                return i;
            }
        }
        return i;
    }

    static List<(string Skill, string Message)> QuotedMessages() =>
        [.. Directory.GetFiles(SkillsDir, "SKILL.md", SearchOption.AllDirectories)
            .Order(StringComparer.Ordinal)
            .SelectMany(file => QuotedMessageItem().Matches(File.ReadAllText(file))
                .SelectMany(item => Backticked().Matches(item.Groups[1].Value))
                .Select(quote => (Path.GetRelativePath(SkillsDir, file), quote.Groups[1].Value)))];

    // A quoted message is found when it is part of a literal (its stand-ins matching anything), or when a literal
    // with its holes matching anything is the whole message (a hole covering text the skill spells out). A literal
    // with almost no text of its own, such as `$"{a}: {b}"`, would match any message, so it takes part only in the
    // first check.
    static bool IsCliMessage(string message, List<string> literals)
    {
        var quoted = new Regex(string.Join(".*", MessageHole().Split(message).Select(Regex.Escape)), RegexOptions.Singleline);
        var sample = MessageHole().Replace(message, "X");
        return literals.Any(literal => quoted.IsMatch(literal)
            || literal.Count(char.IsLetter) >= 5 && Regex.IsMatch(sample, "^" + string.Join(".*", literal.Split(Hole).Select(Regex.Escape)) + "$", RegexOptions.Singleline));
    }

    [Fact]
    public void Every_error_message_a_skill_quotes_is_in_the_cli_sources()
    {
        var literals = CliStringLiterals();
        var messages = QuotedMessages();
        Assert.True(messages.Count >= 20, $"found only {messages.Count} quoted messages");

        var missing = messages.Where(entry => !IsCliMessage(entry.Message, literals))
            .Select(entry => $"{entry.Skill}: `{entry.Message}`").ToList();
        Assert.True(missing.Count == 0, "not in the CLI sources:\n" + string.Join("\n", missing));
    }

    [Fact]
    public void A_reworded_message_is_not_found()
    {
        var literals = CliStringLiterals();

        Assert.True(IsCliMessage("line <line>, column <column> of <file> is not on an identifier", literals));
        Assert.True(IsCliMessage("target: none chosen", literals));
        Assert.False(IsCliMessage("line <line>, column <column> of <file> is not at an identifier", literals));
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
