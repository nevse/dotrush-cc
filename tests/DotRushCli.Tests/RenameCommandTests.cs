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

    // The real DotRush sends Roslyn's minimal text changes: Greeter -> Welcomer keeps the shared "er" and arrives as
    // Greet -> Welcom, so an edit's own text is only part of the old name.
    [Fact]
    public void Edits_trimmed_to_the_changed_part_of_the_old_name_are_accepted()
    {
        var greeter = WriteFile("Greeter.cs", GreeterText);
        var app = WriteFile("App.cs", AppText);
        AnswerRequestsWith(ChangesJson(
            (greeter, [EditJson(GreeterText, 2, "Greet", "Welcom")]),
            (app, [EditJson(AppText, 4, "Greet", "Welcom"), EditJson(AppText, 5, "Greet", "Welcom")])));

        var result = proxy.Run("rename", "preview", greeter, "3", "14", "Welcomer");

        Assert.Equal((0, ""), (result.Exit, result.Stderr));
        Assert.StartsWith("3 edits in 2 files\n", result.Stdout);
        Assert.Contains("+public class Welcomer\n", result.Stdout);
        Assert.Contains("+    public static string Run() => new Welcomer().Greet();\n", result.Stdout);
        Assert.Contains("+    static Welcomer Make() => new();\n", result.Stdout);
    }

    [Fact]
    public void An_insertion_at_the_end_of_the_old_name_is_accepted()
    {
        var greeter = WriteFile("Greeter.cs", GreeterText);
        var character = GreeterText.Split('\n')[2].IndexOf("Greeter", StringComparison.Ordinal) + "Greeter".Length;
        AnswerRequestsWith(ChangesJson((greeter,
            [$$$"""{"range":{"start":{"line":2,"character":{{{character}}}},"end":{"line":2,"character":{{{character}}}}},"newText":"X"}"""])));

        var result = proxy.Run("rename", "preview", greeter, "3", "14", "GreeterX");

        Assert.Equal((0, ""), (result.Exit, result.Stderr));
        Assert.Contains("+public class GreeterX\n", result.Stdout);
    }

    [Fact]
    public void A_trimmed_edit_inside_a_different_identifier_differs_from_disk()
    {
        var greeter = WriteFile("Greeter.cs", GreeterText);
        // Line 4's "Greet" is the whole method name Greet, not part of Greeter.
        AnswerRequestsWith(ChangesJson((greeter,
            [EditJson(GreeterText, 2, "Greet", "Welcom"), EditJson(GreeterText, 4, "Greet", "Welcom")])));

        var result = proxy.Run("rename", "preview", greeter, "3", "14", "Welcomer");

        Assert.Equal((1, "", $"dotrush-cli: DotRush's view of {Posix.RealPath(greeter)} {DiffersFromDisk}\n"), result);
        Assert.Empty(SavedPlans());
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

    // --- apply ---

    const string ApplySynopsis = "rename apply <plan-id> [--outside-workspace]";

    static string Renamed(string text) => text.Replace("Greeter", "Welcomer", StringComparison.Ordinal);

    // Previews renaming Greeter to Welcomer in the given files (the class in the first) and returns the plan id.
    string PreviewGreeterRename(params (string Path, string Text)[] files)
    {
        AnswerRequestsWith(ChangesJson([.. files.Select(file => (file.Path, GreeterEdits(file.Text)))]));
        var result = proxy.Run("rename", "preview", files[0].Path, "3", "14", "Welcomer");
        Assert.Equal((0, ""), (result.Exit, result.Stderr));
        return Regex.Match(result.Stdout, "^plan: ([0-9a-f]{12})$", RegexOptions.Multiline).Groups[1].Value;
    }

    // An edit for every Greeter in text.
    static string[] GreeterEdits(string text)
    {
        var edits = new List<string>();
        var lines = text.Split('\n');
        for (var line = 0; line < lines.Length; line++)
        {
            var count = Regex.Matches(lines[line], "Greeter").Count;
            for (var occurrence = 0; occurrence < count; occurrence++)
            {
                edits.Add(EditJson(text, line, "Greeter", "Welcomer", occurrence));
            }
        }
        return [.. edits];
    }

    IReadOnlyList<JsonObject> DidOpens() =>
        [.. proxy.Lines
            .Select(line => { try { return JsonNode.Parse(line) as JsonObject; } catch (System.Text.Json.JsonException) { return null; } })
            .OfType<JsonObject>()
            .Where(message => message["method"]?.GetValue<string>() == "textDocument/didOpen")];

    // The didOpen notifications once count of them reached the proxy (or whatever arrived within 5 s).
    IReadOnlyList<JsonObject> WaitForDidOpens(int count)
    {
        var deadline = DateTime.UtcNow.AddSeconds(5);
        while (DidOpens().Count < count && DateTime.UtcNow < deadline)
        {
            Thread.Sleep(20);
        }
        Thread.Sleep(100);
        return DidOpens();
    }

    void AssertNoDidOpen()
    {
        Thread.Sleep(200);
        Assert.Empty(DidOpens());
    }

    static void AssertDidOpen(string uri, JsonObject message) =>
        Assert.True(JsonNode.DeepEquals(new JsonObject
        {
            ["jsonrpc"] = "2.0",
            ["method"] = "textDocument/didOpen",
            ["params"] = new JsonObject
            {
                ["textDocument"] = new JsonObject { ["uri"] = uri, ["languageId"] = "csharp", ["version"] = 0, ["text"] = "" },
            },
        }, message), message.ToJsonString());

    bool PlanExists(string planId) => File.Exists(Path.Combine(EditsDir, planId + ".json"));

    [Fact]
    public void Apply_writes_the_files_prints_them_and_sends_one_didOpen_per_changed_file()
    {
        var greeter = WriteFile("Greeter.cs", GreeterText);
        var app = WriteFile("App.cs", AppText);
        var planId = PreviewGreeterRename((greeter, GreeterText), (app, AppText));

        var result = proxy.Run("rename", "apply", planId);

        Assert.Equal((0, ""), (result.Exit, result.Stderr));
        Assert.Equal($"renamed Greeter to Welcomer: 3 edits in 2 files\n{app}\n{greeter}\n", result.Stdout);
        Assert.Equal(Renamed(GreeterText), File.ReadAllText(greeter));
        Assert.Equal(Renamed(AppText), File.ReadAllText(app));
        var didOpens = WaitForDidOpens(2);
        Assert.Equal(2, didOpens.Count);
        AssertDidOpen(UriOf(app), didOpens[0]);
        AssertDidOpen(UriOf(greeter), didOpens[1]);
        Assert.Empty(SavedPlans());
    }

    [Fact]
    public void Apply_opens_each_file_under_the_uri_DotRush_returned_for_a_workspace_reached_through_a_symlink()
    {
        WriteFile("Greeter.cs", GreeterText);
        WriteFile("App.cs", AppText);
        var link = Path.Combine(Path.GetDirectoryName(proxy.Workspace)!, "project-link");
        Directory.CreateSymbolicLink(link, proxy.Workspace);
        var linkedGreeter = Path.Combine(link, "Greeter.cs");
        var linkedApp = Path.Combine(link, "App.cs");
        var planId = PreviewGreeterRename((linkedGreeter, GreeterText), (linkedApp, AppText));

        var result = proxy.Run("rename", "apply", planId);

        Assert.Equal((0, ""), (result.Exit, result.Stderr));
        Assert.Equal($"renamed Greeter to Welcomer: 3 edits in 2 files\n{linkedApp}\n{linkedGreeter}\n", result.Stdout);
        var didOpens = WaitForDidOpens(2);
        Assert.Equal(2, didOpens.Count);
        AssertDidOpen(UriOf(linkedApp), didOpens[0]);
        AssertDidOpen(UriOf(linkedGreeter), didOpens[1]);
        Assert.NotEqual(UriOf(linkedGreeter), UriOf(Posix.RealPath(linkedGreeter)!));
        Assert.Equal(Renamed(GreeterText), File.ReadAllText(Path.Combine(proxy.Workspace, "Greeter.cs")));
        Assert.True(new FileInfo(link).LinkTarget is not null);
    }

    [Fact]
    public void Apply_of_a_plan_without_uris_opens_the_files_by_their_real_paths()
    {
        var greeter = WriteFile("Greeter.cs", GreeterText);
        var planId = PreviewGreeterRename((greeter, GreeterText));
        var planPath = Path.Combine(EditsDir, planId + ".json");
        var plan = JsonNode.Parse(File.ReadAllText(planPath))!.AsObject();
        Assert.True(plan.Remove("uris"));
        File.WriteAllText(planPath, plan.ToJsonString());

        var result = proxy.Run("rename", "apply", planId);

        Assert.Equal((0, ""), (result.Exit, result.Stderr));
        AssertDidOpen(UriOf(Posix.RealPath(greeter)!), Assert.Single(WaitForDidOpens(1)));
    }

    [Fact]
    public void Apply_refuses_a_file_changed_since_preview_writes_nothing_and_sends_no_didOpen()
    {
        var greeter = WriteFile("Greeter.cs", GreeterText);
        var app = WriteFile("App.cs", AppText);
        var planId = PreviewGreeterRename((greeter, GreeterText), (app, AppText));
        File.WriteAllText(app, AppText + "// edited\n");

        var result = proxy.Run("rename", "apply", planId);

        Assert.Equal(
            (1, "", $"dotrush-cli: {Posix.RealPath(app)} changed since preview; run rename preview again\n"), result);
        Assert.Equal(GreeterText, File.ReadAllText(greeter));
        Assert.Equal(AppText + "// edited\n", File.ReadAllText(app));
        AssertNoDidOpen();
        Assert.True(PlanExists(planId));
    }

    [Fact]
    public void Apply_requires_outside_workspace_for_marked_files()
    {
        var greeter = WriteFile("Greeter.cs", GreeterText);
        var generated = WriteFile("obj/Debug/Greeter.g.cs", GreeterText);
        var planId = PreviewGreeterRename((greeter, GreeterText), (generated, GreeterText));

        var refused = proxy.Run("rename", "apply", planId);

        Assert.Equal((1, ""), (refused.Exit, refused.Stdout));
        Assert.Contains($"{Posix.RealPath(generated)} is outside the workspace", refused.Stderr);
        Assert.Contains("apply with --outside-workspace", refused.Stderr);
        Assert.Equal(GreeterText, File.ReadAllText(greeter));
        Assert.Equal(GreeterText, File.ReadAllText(generated));
        AssertNoDidOpen();

        var applied = proxy.Run("rename", "apply", "--outside-workspace", planId);

        Assert.Equal((0, ""), (applied.Exit, applied.Stderr));
        Assert.Equal(Renamed(GreeterText), File.ReadAllText(greeter));
        Assert.Equal(Renamed(GreeterText), File.ReadAllText(generated));
        var didOpens = WaitForDidOpens(2);
        Assert.Equal(2, didOpens.Count);
        AssertDidOpen(UriOf(greeter), didOpens[0]);
        AssertDidOpen(UriOf(generated), didOpens[1]);
    }

    [Theory]
    [InlineData("pid")]
    [InlineData("responses")]
    [InlineData("load-completed")]
    public void Apply_checks_channel_readiness_before_writing_any_file(string broken)
    {
        var greeter = WriteFile("Greeter.cs", GreeterText);
        var planId = PreviewGreeterRename((greeter, GreeterText));
        switch (broken)
        {
            case "pid":
                // Past the largest pid macOS and Linux hand out.
                File.WriteAllText(Path.Combine(proxy.Dir, "pid"), "2147483000\n");
                break;
            case "responses":
                Directory.Delete(proxy.ResponsesDir);
                break;
            default:
                File.Delete(Path.Combine(proxy.Dir, "load-completed"));
                break;
        }

        var result = proxy.Run("rename", "apply", planId);

        Assert.Equal((1, ""), (result.Exit, result.Stdout));
        Assert.StartsWith("dotrush-cli: ", result.Stderr);
        Assert.Equal(GreeterText, File.ReadAllText(greeter));
        AssertNoDidOpen();
        Assert.True(PlanExists(planId));
    }

    [Theory]
    [InlineData("../x")]
    [InlineData("0123456789ab")]
    public void Apply_of_an_unknown_plan_says_to_preview_again(string planId)
    {
        var result = proxy.Run("rename", "apply", planId);

        Assert.Equal((1, "", $"dotrush-cli: unknown plan '{planId}'; run rename preview again\n"), result);
        AssertNothingSent();
    }

    [Theory]
    [InlineData("apply")]
    [InlineData("apply|0123456789ab|extra")]
    [InlineData("apply|0123456789ab|--force")]
    [InlineData("apply|--outside-workspace")]
    public void Malformed_apply_arguments_are_a_usage_error(string joinedArgs)
    {
        var result = proxy.Run(["rename", .. joinedArgs.Split('|')]);

        Assert.Equal((2, ""), (result.Exit, result.Stdout));
        Assert.Contains($"usage: dotrush-cli.sh {ApplySynopsis}", result.Stderr);
        AssertNothingSent();
    }

    [Fact]
    public void Both_rename_subcommands_are_in_the_usage_table()
    {
        var stdout = new StringWriter();

        Program.Run(["help"], proxy.Env(), proxy.Workspace, stdout, new StringWriter());

        Assert.Contains("dotrush-cli.sh rename preview <file> <line> <column> <NewName> [--timeout N]", stdout.ToString());
        Assert.Contains($"dotrush-cli.sh {ApplySynopsis}", stdout.ToString());
    }

    [Fact]
    public void A_failing_move_reports_changed_and_unchanged_files_and_opens_only_the_changed_ones()
    {
        var greeter = WriteFile("Greeter.cs", GreeterText);
        var app = WriteFile("App.cs", AppText);
        var planId = PreviewGreeterRename((greeter, GreeterText), (app, AppText));
        var realGreeter = Posix.RealPath(greeter)!;
        var applier = new WorkspaceEditApplier(moveFile: (from, to) =>
        {
            if (to == realGreeter)
            {
                throw new IOException("permission denied");
            }
            File.Move(from, to, overwrite: true);
        });
        var stdout = new StringWriter();
        var stderr = new StringWriter();

        var exit = RenameCommand.Run(new CommandContext(proxy.Env(), proxy.Workspace, stdout, stderr), ["apply", planId], applier);

        Assert.Equal((1, ""), (exit, stdout.ToString()));
        Assert.Contains("permission denied", stderr.ToString());
        Assert.Contains($"changed: {Posix.RealPath(app)}; unchanged: {realGreeter}", stderr.ToString());
        Assert.Equal(Renamed(AppText), File.ReadAllText(app));
        Assert.Equal(GreeterText, File.ReadAllText(greeter));
        AssertDidOpen(UriOf(app), Assert.Single(WaitForDidOpens(1)));
        Assert.True(PlanExists(planId));
    }

    [Fact]
    public void A_didOpen_that_cannot_be_written_after_apply_is_reported_with_the_changed_files()
    {
        using var deaf = new FakeProxy(withReader: false);
        var greeter = Path.Combine(deaf.Workspace, "Greeter.cs");
        File.WriteAllText(greeter, GreeterText);
        var preview = WorkspaceEditApplier.Preview(deaf.Workspace, "Greeter", "Welcomer",
            new Dictionary<string, IReadOnlyList<LspTextEdit>>
            {
                [UriOf(greeter)] = [new(new(new(2, 13), new(2, 20)), "Welcomer")],
            });
        var planId = WorkspaceEditApplier.SavePlan(deaf.Dir, preview);

        var result = deaf.Run("rename", "apply", planId);

        Assert.Equal(1, result.Exit);
        Assert.Equal($"renamed Greeter to Welcomer: 1 edit in 1 file\n{greeter}\n", result.Stdout);
        Assert.Contains("cannot write to the proxy's FIFO", result.Stderr);
        Assert.Contains("the files were changed, but DotRush was not told to re-read them", result.Stderr);
        Assert.Equal(Renamed(GreeterText), File.ReadAllText(greeter));
    }
}
