using System.Runtime.Versioning;
using System.Text;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace DotRushCli.Tests;

// `rename preview` against a FakeProxy that answers textDocument/rename the way DotRush does.
[UnsupportedOSPlatform("windows")]
public sealed class RenameCommandTests : IDisposable
{
    const string GreeterText = """
        namespace Demo;

        public class Greeter
        {
            public string Greet() => "hi";
        }

        """;

    const string AppText = """
        namespace Demo;

        public static class App
        {
            public static string Run() => new Greeter().Greet();
            static Greeter Make() => new();
        }

        """;

    const string DiffersFromDisk = "differs from disk; preview again after the file is saved";

    readonly FakeProxy proxy = new();

    public void Dispose() => proxy.Dispose();

    string WriteFile(string relative, string text)
    {
        var path = Path.Combine(proxy.Workspace, relative);
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        File.WriteAllText(path, text);
        return path;
    }

    static string UriOf(string path) => new Uri(path).AbsoluteUri;

    static string IdOf(JsonObject message) => message["id"]!.GetValue<string>();

    // A TextEdit JSON object replacing the occurrence-th `word` on the 0-based line of text.
    static string EditJson(string text, int line, string word, string newText, int occurrence = 0)
    {
        var lineText = text.Split('\n')[line];
        var character = -1;
        for (var i = 0; i <= occurrence; i++)
        {
            character = lineText.IndexOf(word, character + 1, StringComparison.Ordinal);
        }
        Assert.True(character >= 0, $"'{word}' not found on line {line}");
        var end = character + word.Length;
        return $$$"""{"range":{"start":{"line":{{{line}}},"character":{{{character}}}},"end":{"line":{{{line}}},"character":{{{end}}}}},"newText":"{{{newText}}}"}""";
    }

    static string ChangesJson(params (string Path, string[] Edits)[] files) =>
        "\"result\":{\"changes\":{" + string.Join(",", files.Select(file => $"\"{UriOf(file.Path)}\":[{string.Join(",", file.Edits)}]")) + "}}";

    // Answers every request with fields and records the requests.
    List<JsonObject> AnswerRequestsWith(string fields)
    {
        var requests = new List<JsonObject>();
        proxy.OnMessage = (fake, message) =>
        {
            if (message["id"] is not null)
            {
                lock (requests) requests.Add(message);
                fake.Respond(IdOf(message), fields);
            }
        };
        return requests;
    }

    string EditsDir => Path.Combine(proxy.Dir, "edits");

    string[] SavedPlans() => Directory.Exists(EditsDir) ? Directory.GetFiles(EditsDir) : [];

    void AssertNothingSent()
    {
        Thread.Sleep(200);
        Assert.Empty(proxy.Lines);
    }

    // --- identifier validation ---

    [Theory]
    [InlineData("Welcomer")]
    [InlineData("Приветствие")]
    [InlineData("_greeter2")]
    [InlineData("@class")]
    [InlineData("var")]
    [InlineData("record")]
    [InlineData("async")]
    public void A_valid_identifier_is_sent_as_the_new_name(string newName)
    {
        var greeter = WriteFile("Greeter.cs", GreeterText);
        var requests = AnswerRequestsWith("\"result\":null");

        var result = proxy.Run("rename", "preview", greeter, "3", "14", newName);

        Assert.Equal(1, result.Exit);
        var request = Assert.Single(requests);
        Assert.Equal("textDocument/rename", request["method"]!.GetValue<string>());
        Assert.Equal(newName, request["params"]!["newName"]!.GetValue<string>());
    }

    [Theory]
    [InlineData("class")]
    [InlineData("1abc")]
    [InlineData("a-b")]
    [InlineData("")]
    [InlineData("@")]
    [InlineData("a b")]
    [InlineData("Greeter\n")]
    public void An_invalid_identifier_is_a_usage_error_and_sends_nothing(string newName)
    {
        var greeter = WriteFile("Greeter.cs", GreeterText);
        AnswerRequestsWith("\"result\":null");

        var result = proxy.Run("rename", "preview", greeter, "3", "14", newName);

        Assert.Equal(2, result.Exit);
        Assert.Equal("", result.Stdout);
        Assert.Contains("is not a valid C# identifier", result.Stderr);
        AssertNothingSent();
    }

    [Fact]
    public void A_reserved_keyword_error_suggests_the_escaped_form()
    {
        var greeter = WriteFile("Greeter.cs", GreeterText);

        var result = proxy.Run("rename", "preview", greeter, "3", "14", "class");

        Assert.Contains("'class' is a reserved C# keyword; use @class", result.Stderr);
    }

    [Theory]
    [InlineData("")]
    [InlineData("preview")]
    [InlineData("preview|Greeter.cs|3|14")]
    [InlineData("preview|Greeter.cs|3|14|Welcomer|extra")]
    [InlineData("preview|Greeter.cs|zero|14|Welcomer")]
    [InlineData("preview|Greeter.cs|0|14|Welcomer")]
    [InlineData("preview|Greeter.cs|3|-1|Welcomer")]
    [InlineData("preview|Greeter.cs|3|14|Welcomer|--timeout|0")]
    [InlineData("frobnicate|Greeter.cs|3|14|Welcomer")]
    public void Malformed_rename_arguments_are_a_usage_error(string joinedArgs)
    {
        WriteFile("Greeter.cs", GreeterText);
        string[] args = joinedArgs.Length == 0 ? [] : joinedArgs.Split('|');

        var result = proxy.Run(["rename", .. args]);

        Assert.Equal(2, result.Exit);
        Assert.Equal("", result.Stdout);
        Assert.Contains("rename preview <file> <line> <column> <NewName>", result.Stderr);
        AssertNothingSent();
    }

    // --- position ---

    [Fact]
    public void The_one_based_line_and_column_become_a_zero_based_lsp_position()
    {
        var greeter = WriteFile("Greeter.cs", GreeterText);
        var requests = AnswerRequestsWith("\"result\":null");

        // Relative to the working directory, which FakeProxy.Run sets to the workspace.
        proxy.Run("rename", "preview", "Greeter.cs", "3", "14", "Welcomer");

        var parameters = Assert.Single(requests)["params"]!;
        Assert.Equal(Posix.RealPath(greeter), WorkspaceEditApplier.ResolveUri(parameters["textDocument"]!["uri"]!.GetValue<string>()));
        Assert.True(JsonNode.DeepEquals(JsonNode.Parse("""{"line":2,"character":13}"""), parameters["position"]));
    }

    [Theory]
    [InlineData("3", "7")]   // the space between `public` and `class`
    [InlineData("1", "15")]  // the `;` after the namespace
    [InlineData("3", "21")]  // just past the end of `public class Greeter`
    [InlineData("3", "40")]  // beyond the line
    [InlineData("40", "1")]  // beyond the file
    public void A_position_not_on_an_identifier_fails_before_any_request(string line, string column)
    {
        var greeter = WriteFile("Greeter.cs", GreeterText);
        AnswerRequestsWith("\"result\":null");

        var result = proxy.Run("rename", "preview", greeter, line, column, "Welcomer");

        Assert.Equal(1, result.Exit);
        Assert.Equal("", result.Stdout);
        Assert.Contains($"line {line}, column {column} of {greeter} is not on an identifier", result.Stderr);
        AssertNothingSent();
    }

    [Fact]
    public void A_number_literal_is_not_an_identifier()
    {
        var file = WriteFile("Numbers.cs", "class Numbers { int x = 1234; }\n");
        AnswerRequestsWith("\"result\":null");

        var result = proxy.Run("rename", "preview", file, "1", "26", "Welcomer");

        Assert.Equal(1, result.Exit);
        Assert.Contains("is not on an identifier", result.Stderr);
        AssertNothingSent();
    }

    [Fact]
    public void A_missing_file_fails_before_any_request()
    {
        AnswerRequestsWith("\"result\":null");

        var result = proxy.Run("rename", "preview", "Missing.cs", "1", "1", "Welcomer");

        Assert.Equal(1, result.Exit);
        Assert.Contains("Missing.cs does not exist", result.Stderr);
        AssertNothingSent();
    }

    [Fact]
    public void Renaming_to_the_current_name_fails_before_any_request()
    {
        var greeter = WriteFile("Greeter.cs", GreeterText);
        AnswerRequestsWith("\"result\":null");

        var result = proxy.Run("rename", "preview", greeter, "3", "14", "Greeter");

        Assert.Equal(1, result.Exit);
        Assert.Contains("the symbol is already named Greeter", result.Stderr);
        AssertNothingSent();
    }

    [Fact]
    public void Preview_checks_channel_readiness_before_writing()
    {
        var greeter = WriteFile("Greeter.cs", GreeterText);
        Directory.Delete(proxy.ResponsesDir);

        var result = proxy.Run("rename", "preview", greeter, "3", "14", "Welcomer");

        Assert.Equal((1, "", "dotrush-cli: the running DotRush proxy predates the request channel; restart Claude Code\n"), result);
        AssertNothingSent();
    }

    // --- responses ---

    [Fact]
    public void Edits_are_saved_as_a_plan_and_summarised()
    {
        var greeter = WriteFile("Greeter.cs", GreeterText);
        var app = WriteFile("App.cs", AppText);
        AnswerRequestsWith(ChangesJson(
            (greeter, [EditJson(GreeterText, 2, "Greeter", "Welcomer")]),
            (app, [EditJson(AppText, 4, "Greeter", "Welcomer"), EditJson(AppText, 5, "Greeter", "Welcomer")])));

        var result = proxy.Run("rename", "preview", greeter, "3", "14", "Welcomer");

        Assert.Equal((0, ""), (result.Exit, result.Stderr));
        var planId = Regex.Match(result.Stdout, "^plan: ([0-9a-f]{12})$", RegexOptions.Multiline).Groups[1].Value;
        Assert.NotEmpty(planId);
        var diffPath = Path.Combine(proxy.Dir, "edits", planId + ".diff");
        // Built line by line: a blank context line is a single space, which a raw string literal would drop.
        string[] expectedDiff =
        [
            "--- a/App.cs",
            "+++ b/App.cs",
            "@@ -2,6 +2,6 @@",
            " ",
            " public static class App",
            " {",
            "-    public static string Run() => new Greeter().Greet();",
            "+    public static string Run() => new Welcomer().Greet();",
            "-    static Greeter Make() => new();",
            "+    static Welcomer Make() => new();",
            " }",
            "--- a/Greeter.cs",
            "+++ b/Greeter.cs",
            "@@ -1,6 +1,6 @@",
            " namespace Demo;",
            " ",
            "-public class Greeter",
            "+public class Welcomer",
            " {",
            "     public string Greet() => \"hi\";",
            " }",
        ];
        Assert.Equal(string.Join("", expectedDiff.Select(line => line + "\n")), File.ReadAllText(diffPath));
        Assert.Equal(
            "3 edits in 2 files\n" +
            "App.cs: 2 edits\n" +
            "Greeter.cs: 1 edit\n" +
            File.ReadAllText(diffPath) +
            $"diff: {diffPath}\n" +
            $"plan: {planId}\n",
            result.Stdout);

        var plan = WorkspaceEditApplier.LoadPlan(proxy.Dir, planId);
        Assert.Equal(("Greeter", "Welcomer", Posix.RealPath(proxy.Workspace)), (plan.OldName, plan.NewName, plan.Workspace));
        Assert.Equal([Posix.RealPath(app)!, Posix.RealPath(greeter)!], plan.Files.Keys.Order(StringComparer.Ordinal));
        Assert.Equal(2, plan.Changes[Posix.RealPath(app)!].Count);
        Assert.Equal(GreeterText, File.ReadAllText(greeter));
        Assert.Equal(AppText, File.ReadAllText(app));
    }

    [Fact]
    public void Only_the_first_200_diff_lines_are_printed()
    {
        var text = new StringBuilder("namespace Demo;\n");
        for (var i = 0; i < 150; i++)
        {
            text.Append("static Greeter G").Append(i).Append(" = null;\n");
        }
        var source = text.ToString();
        var file = WriteFile("Many.cs", source);
        AnswerRequestsWith(ChangesJson((file, [.. Enumerable.Range(1, 150).Select(line => EditJson(source, line, "Greeter", "Welcomer"))])));

        var result = proxy.Run("rename", "preview", file, "2", "8", "Welcomer");

        Assert.Equal(0, result.Exit);
        var lines = result.Stdout.TrimEnd('\n').Split('\n');
        var planId = lines[^1]["plan: ".Length..];
        var diffPath = Path.Combine(proxy.Dir, "edits", planId + ".diff");
        var diffLines = File.ReadAllText(diffPath).TrimEnd('\n').Split('\n');
        Assert.Equal(304, diffLines.Length);
        Assert.Equal("150 edits in 1 file", lines[0]);
        Assert.Equal("Many.cs: 150 edits", lines[1]);
        Assert.Equal(diffLines[..200], lines[2..202]);
        Assert.Equal("(diff truncated: 104 more lines)", lines[202]);
        Assert.Equal($"diff: {diffPath}", lines[203]);
        Assert.Equal(205, lines.Length);
    }

    [Theory]
    [InlineData("\"result\":null")]
    [InlineData("\"result\":{}")]
    [InlineData("\"result\":{\"changes\":{}}")]
    [InlineData("\"result\":{\"changes\":{\"file:///nowhere/Greeter.cs\":[]}}")]
    public void No_edits_means_no_symbol_at_the_position(string fields)
    {
        var greeter = WriteFile("Greeter.cs", GreeterText);
        AnswerRequestsWith(fields);

        var result = proxy.Run("rename", "preview", greeter, "3", "14", "Welcomer");

        Assert.Equal((1, "", "dotrush-cli: no symbol at this position; check it with documentSymbol\n"), result);
        Assert.Empty(SavedPlans());
    }

    [Fact]
    public void A_range_whose_disk_text_is_not_the_old_name_differs_from_disk()
    {
        var greeter = WriteFile("Greeter.cs", GreeterText);
        var app = WriteFile("App.cs", AppText);
        AnswerRequestsWith(ChangesJson(
            (greeter, [EditJson(GreeterText, 2, "Greeter", "Welcomer")]),
            (app, [EditJson(AppText, 4, "string", "Welcomer")])));

        var result = proxy.Run("rename", "preview", greeter, "3", "14", "Welcomer");

        Assert.Equal((1, "", $"dotrush-cli: DotRush's view of {Posix.RealPath(app)} {DiffersFromDisk}\n"), result);
        Assert.Empty(SavedPlans());
    }

    [Fact]
    public void A_range_outside_the_file_on_disk_differs_from_disk()
    {
        var greeter = WriteFile("Greeter.cs", GreeterText);
        AnswerRequestsWith(ChangesJson((greeter,
            ["""{"range":{"start":{"line":40,"character":0},"end":{"line":40,"character":7}},"newText":"Welcomer"}"""])));

        var result = proxy.Run("rename", "preview", greeter, "3", "14", "Welcomer");

        Assert.Equal(1, result.Exit);
        Assert.Contains($"DotRush's view of {Posix.RealPath(greeter)} {DiffersFromDisk}", result.Stderr);
        Assert.Empty(SavedPlans());
    }

    [Fact]
    public void Escaped_and_attribute_forms_of_the_old_name_are_accepted()
    {
        const string text = """
            class MarkerAttribute : System.Attribute { }
            [Marker] class A { }
            [MarkerAttribute] class B { }
            class C { MarkerAttribute @MarkerAttribute; }

            """;
        var file = WriteFile("Marker.cs", text);
        AnswerRequestsWith(ChangesJson((file,
        [
            EditJson(text, 0, "MarkerAttribute", "TagAttribute"),
            EditJson(text, 1, "Marker", "Tag"),
            EditJson(text, 2, "MarkerAttribute", "TagAttribute"),
            EditJson(text, 3, "MarkerAttribute", "TagAttribute"),
            EditJson(text, 3, "@MarkerAttribute", "@TagAttribute"),
        ])));

        var result = proxy.Run("rename", "preview", file, "1", "7", "TagAttribute");

        Assert.Equal((0, ""), (result.Exit, result.Stderr));
        Assert.StartsWith("5 edits in 1 file\n", result.Stdout);
    }

    [Fact]
    public void The_old_name_with_an_attribute_suffix_is_accepted()
    {
        const string text = "class Marker : System.Attribute { }\n[MarkerAttribute] class A { }\n";
        var file = WriteFile("Marker.cs", text);
        AnswerRequestsWith(ChangesJson((file,
            [EditJson(text, 0, "Marker", "Tag"), EditJson(text, 1, "MarkerAttribute", "TagAttribute")])));

        var result = proxy.Run("rename", "preview", file, "1", "7", "Tag");

        Assert.Equal((0, ""), (result.Exit, result.Stderr));
    }

    [Fact]
    public void A_position_on_the_at_sign_of_an_escaped_identifier_uses_the_name_after_it()
    {
        const string text = "class C { int @event; }\n";
        var file = WriteFile("C.cs", text);
        AnswerRequestsWith(ChangesJson((file, [EditJson(text, 0, "@event", "@signal")])));

        var result = proxy.Run("rename", "preview", file, "1", "15", "@signal");

        Assert.Equal((0, ""), (result.Exit, result.Stderr));
        var planId = Regex.Match(result.Stdout, "^plan: ([0-9a-f]{12})$", RegexOptions.Multiline).Groups[1].Value;
        Assert.Equal("event", WorkspaceEditApplier.LoadPlan(proxy.Dir, planId).OldName);
    }

    [Fact]
    public void Files_outside_the_workspace_or_under_obj_are_marked()
    {
        var greeter = WriteFile("Greeter.cs", GreeterText);
        var generated = WriteFile("obj/Debug/Greeter.g.cs", GreeterText);
        var shared = Path.Combine(Path.GetDirectoryName(proxy.Workspace)!, "Shared.cs");
        File.WriteAllText(shared, GreeterText);
        AnswerRequestsWith(ChangesJson(
            (greeter, [EditJson(GreeterText, 2, "Greeter", "Welcomer")]),
            (generated, [EditJson(GreeterText, 2, "Greeter", "Welcomer")]),
            (shared, [EditJson(GreeterText, 2, "Greeter", "Welcomer")])));

        var result = proxy.Run("rename", "preview", greeter, "3", "14", "Welcomer");

        Assert.Equal((0, ""), (result.Exit, result.Stderr));
        var lines = result.Stdout.Split('\n');
        Assert.Equal("3 edits in 3 files", lines[0]);
        // In real-path order: <root>/Shared.cs sorts before <root>/project/…
        Assert.Equal(
            [$"{Posix.RealPath(shared)}: 1 edit (outside workspace)", "Greeter.cs: 1 edit", "obj/Debug/Greeter.g.cs: 1 edit (outside workspace)"],
            lines[1..4]);
    }

    [Fact]
    public void A_json_rpc_error_is_reported()
    {
        var greeter = WriteFile("Greeter.cs", GreeterText);
        AnswerRequestsWith("""
            "error":{"code":-32603,"message":"rename failed"}
            """);

        var result = proxy.Run("rename", "preview", greeter, "3", "14", "Welcomer");

        Assert.Equal((1, "", "dotrush-cli: -32603: rename failed\n"), result);
    }

    [Fact]
    public void A_rename_without_a_response_in_time_times_out()
    {
        var greeter = WriteFile("Greeter.cs", GreeterText);

        var result = proxy.Run("rename", "preview", greeter, "3", "14", "Welcomer", "--timeout", "0.3");

        Assert.Equal(3, result.Exit);
        Assert.Contains("no response to textDocument/rename within 0.3 s", result.Stderr);
        Assert.Empty(SavedPlans());
    }
}
