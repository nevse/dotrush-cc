using System.Globalization;
using System.Runtime.Versioning;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace DotRushCli;

// `rename preview`: asks DotRush for the WorkspaceEdit of a rename, checks it against the disk, saves it as a plan
// and prints a summary with the start of the diff.
[UnsupportedOSPlatform("windows")]
public static partial class RenameCommand
{
    public const string PreviewSynopsis = "rename preview <file> <line> <column> <NewName> [--timeout N]";
    const int PrintedDiffLines = 200;
    const string AttributeSuffix = "Attribute";

    // C# reserved keywords (and Roslyn's __arglist family): valid names only when escaped with @. Contextual
    // keywords such as var, record or async are ordinary identifiers.
    static readonly HashSet<string> ReservedKeywords = new(StringComparer.Ordinal)
    {
        "abstract", "as", "base", "bool", "break", "byte", "case", "catch", "char", "checked", "class", "const",
        "continue", "decimal", "default", "delegate", "do", "double", "else", "enum", "event", "explicit", "extern",
        "false", "finally", "fixed", "float", "for", "foreach", "goto", "if", "implicit", "in", "int", "interface",
        "internal", "is", "lock", "long", "namespace", "new", "null", "object", "operator", "out", "override",
        "params", "private", "protected", "public", "readonly", "ref", "return", "sbyte", "sealed", "short", "sizeof",
        "stackalloc", "static", "string", "struct", "switch", "this", "throw", "true", "try", "typeof", "uint", "ulong",
        "unchecked", "unsafe", "ushort", "using", "virtual", "void", "volatile", "while",
        "__arglist", "__makeref", "__reftype", "__refvalue",
    };

    // \z, not $: $ also matches before a trailing newline.
    [GeneratedRegex(@"^@?[\p{L}\p{Nl}_][\p{L}\p{Nl}\p{Nd}\p{Mn}\p{Mc}\p{Pc}\p{Cf}]*\z")]
    private static partial Regex IdentifierPattern();

    public static int Run(CommandContext context, string[] args) => args switch
    {
        ["preview", .. var rest] => Preview(context, rest),
        _ => Usage(context, null),
    };

    // Why name cannot be a new C# name, or null when it can.
    public static string? IdentifierProblem(string name)
    {
        if (!IdentifierPattern().IsMatch(name))
        {
            return $"'{name}' is not a valid C# identifier";
        }
        return ReservedKeywords.Contains(name)
            ? $"'{name}' is not a valid C# identifier ('{name}' is a reserved C# keyword; use @{name})"
            : null;
    }

    static int Preview(CommandContext context, string[] args)
    {
        var positional = new List<string>();
        var timeout = RequestCommand.DefaultTimeout;
        if (RequestCommand.TakeTimeout(args, positional, ref timeout) is { } timeoutProblem)
        {
            return Usage(context, timeoutProblem);
        }
        if (positional is not [var file, var lineText, var columnText, var newName] || file.Length == 0)
        {
            return Usage(context, null);
        }
        if (!TryParsePositive(lineText, out var line) || !TryParsePositive(columnText, out var column))
        {
            return Usage(context, "<line> and <column> must be whole numbers starting at 1");
        }
        if (IdentifierProblem(newName) is { } nameProblem)
        {
            return Usage(context, nameProblem);
        }

        var ready = Session.RequireChannel(context, out var session);
        if (ready != ExitCode.Success)
        {
            return ready;
        }
        if (session!.Workspace is not { Length: > 0 } workspace)
        {
            return Fail(context, $"the session dir {session.Dir} does not record its workspace; restart Claude Code");
        }
        var path = Path.GetFullPath(file, context.Cwd);
        if (!File.Exists(path))
        {
            return Fail(context, $"{file} does not exist");
        }

        try
        {
            var position = new LspPosition(line - 1, column - 1);
            if (IdentifierAt(SourceDocument.Load(path), position) is not { } oldName)
            {
                return Fail(context, $"line {line}, column {column} of {file} is not on an identifier");
            }
            if ((newName.StartsWith('@') ? newName[1..] : newName) == oldName)
            {
                return Fail(context, $"the symbol is already named {oldName}");
            }

            var channel = new LspChannel(session);
            channel.DeleteStaleResponses();
            var reply = channel.Request("textDocument/rename", new JsonObject
            {
                ["textDocument"] = new JsonObject { ["uri"] = new Uri(path).AbsoluteUri },
                ["position"] = new JsonObject { ["line"] = position.Line, ["character"] = position.Character },
                ["newName"] = newName,
            }, timeout);
            switch (reply.Kind)
            {
                case LspReplyKind.Timeout:
                    context.Stderr.WriteLine($"dotrush-cli: {reply.Message}");
                    return ExitCode.Timeout;
                case LspReplyKind.Error or LspReplyKind.Failed:
                    return Fail(context, reply.Message);
            }
            if (ReadChanges(reply.Result) is not { } changes)
            {
                return Fail(context, "no symbol at this position; check it with documentSymbol");
            }

            var preview = WorkspaceEditApplier.Preview(workspace, oldName, newName, changes);
            var accepted = AcceptedRangeTexts(oldName);
            if (preview.Files.FirstOrDefault(entry => !entry.RangeTexts.All(accepted.Contains)) is { } stale)
            {
                return Fail(context, $"DotRush's view of {stale.Path} differs from disk; preview again after the file is saved");
            }
            var planId = WorkspaceEditApplier.SavePlan(session.Dir, preview);
            Print(context.Stdout, preview, WorkspaceEditApplier.DiffPath(session.Dir, planId), planId);
            return ExitCode.Success;
        }
        catch (WorkspaceEditException e)
        {
            return Fail(context, e.Message);
        }
    }

    // The identifier the position lands on, without a leading @; null when it is not on one. A position on the @
    // of an escaped identifier counts as on the identifier.
    static string? IdentifierAt(SourceDocument document, LspPosition position)
    {
        var text = document.Text;
        if (!document.TryGetOffset(position, out var offset) || offset >= text.Length)
        {
            return null;
        }
        if (text[offset] == '@' && offset + 1 < text.Length && IsIdentifierPart(text, offset + 1))
        {
            offset++;
        }
        if (!IsIdentifierPart(text, offset))
        {
            return null;
        }
        var start = offset;
        while (start > 0 && IsIdentifierPart(text, start - 1))
        {
            start--;
        }
        var end = offset + 1;
        while (end < text.Length && IsIdentifierPart(text, end))
        {
            end++;
        }
        // A run starting with a digit is a number literal, not a name.
        return IsIdentifierStart(text, start) ? text[start..end] : null;
    }

    static UnicodeCategory CategoryAt(string text, int index)
    {
        if (char.IsLowSurrogate(text[index]) && index > 0 && char.IsHighSurrogate(text[index - 1]))
        {
            index--;
        }
        return CharUnicodeInfo.GetUnicodeCategory(text, index);
    }

    static bool IsLetter(UnicodeCategory category) => category is UnicodeCategory.UppercaseLetter
        or UnicodeCategory.LowercaseLetter or UnicodeCategory.TitlecaseLetter or UnicodeCategory.ModifierLetter
        or UnicodeCategory.OtherLetter or UnicodeCategory.LetterNumber;

    static bool IsIdentifierStart(string text, int index) => text[index] == '_' || IsLetter(CategoryAt(text, index));

    static bool IsIdentifierPart(string text, int index)
    {
        var category = CategoryAt(text, index);
        return IsLetter(category) || category is UnicodeCategory.DecimalDigitNumber or UnicodeCategory.NonSpacingMark
            or UnicodeCategory.SpacingCombiningMark or UnicodeCategory.ConnectorPunctuation or UnicodeCategory.Format;
    }

    // What a range of the rename may cover on disk: the old name, escaped with @, and for attributes the name with
    // or without its Attribute suffix.
    static HashSet<string> AcceptedRangeTexts(string oldName)
    {
        List<string> names = [oldName, oldName + AttributeSuffix];
        if (oldName.Length > AttributeSuffix.Length && oldName.EndsWith(AttributeSuffix, StringComparison.Ordinal))
        {
            names.Add(oldName[..^AttributeSuffix.Length]);
        }
        return new HashSet<string>([.. names, .. names.Select(name => "@" + name)], StringComparer.Ordinal);
    }

    // The WorkspaceEdit's changes, uri → edits, or null when it has no edits at all.
    static Dictionary<string, IReadOnlyList<LspTextEdit>>? ReadChanges(JsonElement result)
    {
        if (result.ValueKind != JsonValueKind.Object || !result.TryGetProperty("changes", out var changes)
            || changes.ValueKind != JsonValueKind.Object)
        {
            return null;
        }
        var byUri = new Dictionary<string, IReadOnlyList<LspTextEdit>>(StringComparer.Ordinal);
        try
        {
            foreach (var entry in changes.EnumerateObject())
            {
                if (entry.Value.ValueKind == JsonValueKind.Null)
                {
                    continue;
                }
                var edits = entry.Value.Deserialize<List<LspTextEdit?>>(WorkspaceEditApplier.JsonOptions);
                if (edits is null || edits.Contains(null))
                {
                    throw new JsonException("an edit is null");
                }
                if (edits.Count > 0)
                {
                    byUri[entry.Name] = [.. edits.OfType<LspTextEdit>()];
                }
            }
        }
        catch (JsonException e)
        {
            throw new WorkspaceEditException($"DotRush returned a rename result that cannot be read: {e.Message}");
        }
        return byUri.Count == 0 ? null : byUri;
    }

    static void Print(TextWriter stdout, EditPreview preview, string diffPath, string planId)
    {
        stdout.WriteLine($"{Count(preview.Files.Sum(file => file.EditCount), "edit")} in {Count(preview.Files.Count, "file")}");
        foreach (var file in preview.Files)
        {
            var outsideRoot = file.RelativePath == ".." || file.RelativePath.StartsWith("../", StringComparison.Ordinal);
            stdout.WriteLine(
                $"{(outsideRoot ? file.Path : file.RelativePath)}: {Count(file.EditCount, "edit")}{(file.OutsideWorkspace ? " (outside workspace)" : "")}");
        }
        var diffLines = preview.Diff.Split('\n');
        var lineCount = preview.Diff.EndsWith('\n') ? diffLines.Length - 1 : diffLines.Length;
        foreach (var diffLine in diffLines.Take(Math.Min(lineCount, PrintedDiffLines)))
        {
            stdout.WriteLine(diffLine);
        }
        if (lineCount > PrintedDiffLines)
        {
            stdout.WriteLine($"(diff truncated: {lineCount - PrintedDiffLines} more lines)");
        }
        stdout.WriteLine($"diff: {diffPath}");
        stdout.WriteLine($"plan: {planId}");
    }

    static string Count(int count, string noun) => $"{count} {noun}{(count == 1 ? "" : "s")}";

    static bool TryParsePositive(string text, out int value) =>
        int.TryParse(text, NumberStyles.None, CultureInfo.InvariantCulture, out value) && value >= 1;

    static int Usage(CommandContext context, string? problem)
    {
        if (problem is not null)
        {
            context.Stderr.WriteLine($"dotrush-cli: {problem}");
        }
        context.Stderr.WriteLine($"dotrush-cli: usage: dotrush-cli.sh {PreviewSynopsis}");
        return ExitCode.Usage;
    }

    static int Fail(CommandContext context, string message)
    {
        context.Stderr.WriteLine($"dotrush-cli: {message}");
        return ExitCode.Error;
    }
}
