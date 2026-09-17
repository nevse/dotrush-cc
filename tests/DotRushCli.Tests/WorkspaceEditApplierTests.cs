using System.Runtime.Versioning;
using System.Text;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace DotRushCli.Tests;

// Turning a rename's WorkspaceEdit into a saved plan and a diff, and applying that plan to the disk.
[UnsupportedOSPlatform("windows")]
public sealed class WorkspaceEditApplierTests : IDisposable
{
    static readonly byte[] Bom = [0xEF, 0xBB, 0xBF];

    readonly string root;
    readonly string workspace;
    readonly string session;

    public WorkspaceEditApplierTests()
    {
        root = Posix.RealPath(Directory.CreateTempSubdirectory("dotrush-cli-edit-").FullName)!;
        workspace = Directory.CreateDirectory(Path.Combine(root, "ws")).FullName;
        session = Directory.CreateDirectory(Path.Combine(root, "session")).FullName;
    }

    public void Dispose()
    {
        try { Directory.Delete(root, recursive: true); } catch (IOException) { } catch (UnauthorizedAccessException) { }
    }

    static LspTextEdit Edit(int startLine, int startCharacter, int endLine, int endCharacter, string newText) =>
        new(new(new(startLine, startCharacter), new(endLine, endCharacter)), newText);

    static string ApplyToText(string text, params LspTextEdit[] edits) =>
        SourceDocument.FromBytes("Test.cs", Encoding.UTF8.GetBytes(text)).Apply(edits);

    string WriteFile(string relative, string text) => WriteBytes(relative, Encoding.UTF8.GetBytes(text));

    string WriteBytes(string relative, byte[] bytes)
    {
        var path = Path.Combine(workspace, relative);
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        File.WriteAllBytes(path, bytes);
        return path;
    }

    static string UriOf(string path) => new Uri(path).AbsoluteUri;

    static Dictionary<string, IReadOnlyList<LspTextEdit>> Changes(params (string Path, LspTextEdit[] Edits)[] files) =>
        files.ToDictionary(file => UriOf(file.Path), file => (IReadOnlyList<LspTextEdit>)file.Edits);

    EditPreview Preview(params (string Path, LspTextEdit[] Edits)[] files) =>
        WorkspaceEditApplier.Preview(workspace, "Greeter", "Welcomer", Changes(files));

    string Save(EditPreview preview) => WorkspaceEditApplier.SavePlan(session, preview);

    string[] Temps() => Directory.GetFiles(root, "*" + WorkspaceEditApplier.TempSuffix, SearchOption.AllDirectories);

    // "Greeter" at the start of a line becomes "Welcomer".
    static LspTextEdit RenameAt(int line, int character = 0) => Edit(line, character, line, character + 7, "Welcomer");

    // --- text application ---

    [Fact]
    public void Several_edits_on_one_line_are_all_applied()
    {
        Assert.Equal("Welcomer a = new Welcomer();",
            ApplyToText("Greeter a = new Greeter();", RenameAt(0), RenameAt(0, 16)));
    }

    [Fact]
    public void Edits_on_different_lines_use_the_original_positions_whatever_their_order()
    {
        const string text = "class Greeter\n{\n    Greeter() {}\n}\n";
        LspTextEdit[] edits = [Edit(0, 0, 0, 5, "sealed\nclass"), RenameAt(2, 4)];

        Assert.Equal("sealed\nclass Greeter\n{\n    Welcomer() {}\n}\n", ApplyToText(text, edits));
        Assert.Equal("sealed\nclass Greeter\n{\n    Welcomer() {}\n}\n", ApplyToText(text, [.. edits.Reverse()]));
    }

    [Theory]
    [InlineData("\r\n")]
    [InlineData("\n")]
    [InlineData("\r")]
    public void A_file_keeps_its_line_endings(string newline)
    {
        var path = WriteFile("Greeter.cs", $"class Greeter{newline}{{{newline}    Greeter() {{}}{newline}}}{newline}");
        var id = Save(Preview((path, [RenameAt(0, 6), RenameAt(2, 4)])));

        new WorkspaceEditApplier().Apply(session, id, allowOutsideWorkspace: false);

        Assert.Equal($"class Welcomer{newline}{{{newline}    Welcomer() {{}}{newline}}}{newline}", File.ReadAllText(path));
    }

    [Theory]
    [InlineData("\u2028")]
    [InlineData("\u2029")]
    [InlineData("\u0085")]
    public void Unicode_line_separators_count_as_line_breaks(string separator)
    {
        Assert.Equal($"// a{separator}Welcomer g;", ApplyToText($"// a{separator}Greeter g;", RenameAt(1)));
    }

    [Fact]
    public void A_position_after_a_surrogate_pair_counts_utf16_code_units()
    {
        Assert.Equal("var s = \"\U0001F600\"; Welcomer g;", ApplyToText("var s = \"\U0001F600\"; Greeter g;", RenameAt(0, 14)));
    }

    [Fact]
    public void Overlapping_edits_are_rejected()
    {
        var path = WriteFile("Greeter.cs", "class Greeter {}\n");

        var error = Assert.Throws<WorkspaceEditException>(() =>
            Preview((path, [Edit(0, 6, 0, 10, "A"), Edit(0, 8, 0, 13, "B")])));

        Assert.Contains("overlap", error.Message);
        Assert.Throws<WorkspaceEditException>(() => ApplyToText("Greeter", Edit(0, 0, 0, 0, "A"), Edit(0, 0, 0, 0, "B")));
    }

    [Fact]
    public void A_position_outside_the_file_is_rejected()
    {
        const string message = "DotRush's view of Test.cs differs from disk; preview again after the file is saved";

        Assert.Equal(message, Assert.Throws<WorkspaceEditException>(() => ApplyToText("Greeter\n", RenameAt(3))).Message);
        Assert.Equal(message,
            Assert.Throws<WorkspaceEditException>(() => ApplyToText("Greeter\n", Edit(0, 0, 0, 9, "x"))).Message);
    }

    [Fact]
    public void A_range_that_ends_before_it_starts_is_rejected()
    {
        var error = Assert.Throws<WorkspaceEditException>(() => ApplyToText("Greeter\n", Edit(0, 5, 0, 2, "x")));

        Assert.Equal("the range 0:5-0:2 in Test.cs ends before it starts", error.Message);
    }

    [Fact]
    public void New_text_that_is_not_valid_unicode_is_refused()
    {
        var document = SourceDocument.FromBytes("Test.cs", "Greeter\n"u8.ToArray());

        var error = Assert.Throws<WorkspaceEditException>(() => document.Encode("\ud800 lone surrogate"));

        Assert.Equal("the new text for Test.cs is not valid Unicode", error.Message);
    }

    // --- file handling ---

    [Fact]
    public void A_utf8_bom_is_preserved()
    {
        var path = WriteBytes("Greeter.cs", [.. Bom, .. "class Greeter {}\n"u8]);
        var id = Save(Preview((path, [RenameAt(0, 6)])));

        new WorkspaceEditApplier().Apply(session, id, allowOutsideWorkspace: false);

        Assert.Equal([.. Bom, .. "class Welcomer {}\n"u8], File.ReadAllBytes(path));
    }

    [Fact]
    public void A_file_that_is_not_valid_utf8_is_refused_and_left_alone()
    {
        byte[] bytes = [.. "class Greeter {} // "u8, 0xFF, 0x0A];
        var path = WriteBytes("Greeter.cs", bytes);

        var error = Assert.Throws<WorkspaceEditException>(() => Preview((path, [RenameAt(0, 6)])));

        Assert.Contains("not valid UTF-8", error.Message);
        Assert.Equal(bytes, File.ReadAllBytes(path));
        Assert.False(Directory.Exists(Path.Combine(session, "edits")));
    }

    [Fact]
    public void The_file_mode_is_preserved()
    {
        var path = WriteFile("greet.csx", "Greeter.Hi();\n");
        const UnixFileMode mode = UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute
            | UnixFileMode.GroupRead | UnixFileMode.GroupExecute | UnixFileMode.OtherRead | UnixFileMode.OtherExecute;
        File.SetUnixFileMode(path, mode);
        var id = Save(Preview((path, [RenameAt(0)])));

        new WorkspaceEditApplier().Apply(session, id, allowOutsideWorkspace: false);

        Assert.Equal("Welcomer.Hi();\n", File.ReadAllText(path));
        Assert.Equal(mode, File.GetUnixFileMode(path));
    }

    [Fact]
    public void A_symlinked_file_writes_its_target_and_keeps_the_link()
    {
        var target = WriteFile("real/Greeter.cs", "class Greeter {}\n");
        var link = Path.Combine(workspace, "Greeter.cs");
        File.CreateSymbolicLink(link, target);
        var preview = Preview((link, [RenameAt(0, 6)]));
        Assert.Equal([target], preview.Plan.Files.Keys);

        new WorkspaceEditApplier().Apply(session, Save(preview), allowOutsideWorkspace: false);

        Assert.Equal(target, new FileInfo(link).LinkTarget);
        Assert.Equal("class Welcomer {}\n", File.ReadAllText(target));
    }

    [Fact]
    public void A_failing_temp_write_leaves_every_file_untouched()
    {
        var a = WriteFile("A.cs", "class Greeter {}\n");
        var b = WriteFile("B.cs", "Greeter g;\n");
        var c = WriteFile("C.cs", "Greeter h;\n");
        var id = Save(Preview((a, [RenameAt(0, 6)]), (b, [RenameAt(0)]), (c, [RenameAt(0)])));
        var applier = new WorkspaceEditApplier(writeFile: (path, bytes, mode) =>
        {
            if (path == b + WorkspaceEditApplier.TempSuffix)
            {
                File.WriteAllBytes(path, bytes[..2]);
                throw new IOException("disk full");
            }
            File.WriteAllBytes(path, bytes);
            File.SetUnixFileMode(path, mode);
        });

        var error = Assert.Throws<WorkspaceEditException>(() => applier.Apply(session, id, allowOutsideWorkspace: false));

        Assert.Contains("disk full", error.Message);
        Assert.Contains("no file was changed", error.Message);
        Assert.Empty(error.Changed);
        Assert.Equal([a, b, c], error.Unchanged);
        Assert.Equal("class Greeter {}\n", File.ReadAllText(a));
        Assert.Equal("Greeter g;\n", File.ReadAllText(b));
        Assert.Equal("Greeter h;\n", File.ReadAllText(c));
        Assert.Empty(Temps());
    }

    [Fact]
    public void A_failing_move_reports_the_changed_and_unchanged_files()
    {
        var a = WriteFile("A.cs", "class Greeter {}\n");
        var b = WriteFile("B.cs", "Greeter g;\n");
        var c = WriteFile("C.cs", "Greeter h;\n");
        var id = Save(Preview((a, [RenameAt(0, 6)]), (b, [RenameAt(0)]), (c, [RenameAt(0)])));
        var applier = new WorkspaceEditApplier(moveFile: (from, to) =>
        {
            if (to == b)
            {
                throw new IOException("permission denied");
            }
            File.Move(from, to, overwrite: true);
        });

        var error = Assert.Throws<WorkspaceEditException>(() => applier.Apply(session, id, allowOutsideWorkspace: false));

        Assert.Equal([a], error.Changed);
        Assert.Equal([b, c], error.Unchanged);
        Assert.Contains("permission denied", error.Message);
        Assert.Contains($"changed: {a}", error.Message);
        Assert.Contains($"unchanged: {b}, {c}", error.Message);
        Assert.Equal("class Welcomer {}\n", File.ReadAllText(a));
        Assert.Equal("Greeter g;\n", File.ReadAllText(b));
        Assert.Equal("Greeter h;\n", File.ReadAllText(c));
        Assert.Empty(Temps());
        // A partly applied plan can never apply again: the files it wrote no longer match its hashes.
        Assert.Empty(Directory.GetFileSystemEntries(Path.Combine(session, "edits")));
    }

    // --- plans ---

    [Fact]
    public void A_saved_plan_loads_back_unchanged()
    {
        var a = WriteFile("A.cs", "class Greeter {}\n");
        var b = WriteFile("src/B.cs", "Greeter g = new Greeter();\n");
        var preview = Preview((a, [RenameAt(0, 6)]), (b, [RenameAt(0), RenameAt(0, 16)]));

        var id = Save(preview);
        var plan = WorkspaceEditApplier.LoadPlan(session, id);

        Assert.Matches(new Regex("^[0-9a-f]{12}$"), id);
        Assert.Equal(workspace, plan.Workspace);
        Assert.Equal("Greeter", plan.OldName);
        Assert.Equal("Welcomer", plan.NewName);
        Assert.Equal([a, b], plan.Changes.Keys.Order(StringComparer.Ordinal));
        Assert.Equal(preview.Plan.Changes[b], plan.Changes[b]);
        Assert.Equal(preview.Plan.Files.OrderBy(file => file.Key, StringComparer.Ordinal),
            plan.Files.OrderBy(file => file.Key, StringComparer.Ordinal));
        Assert.Equal(Convert.ToHexStringLower(System.Security.Cryptography.SHA256.HashData(File.ReadAllBytes(a))),
            plan.Files[a]);
        Assert.Equal(preview.Diff, File.ReadAllText(Path.Combine(session, "edits", id + ".diff")));

        var json = JsonNode.Parse(File.ReadAllText(Path.Combine(session, "edits", id + ".json")))!;
        Assert.Equal(workspace, json["workspace"]!.GetValue<string>());
        Assert.Equal("Welcomer", json["newName"]!.GetValue<string>());
        Assert.Equal(16, json["changes"]![b]![1]!["range"]!["start"]!["character"]!.GetValue<int>());
        Assert.Equal("Welcomer", json["changes"]![b]![1]!["newText"]!.GetValue<string>());
        Assert.Equal(plan.Files[b], json["files"]![b]!.GetValue<string>());
    }

    [Fact]
    public void Apply_refuses_when_any_file_changed_since_preview_and_writes_nothing()
    {
        var a = WriteFile("A.cs", "class Greeter {}\n");
        var b = WriteFile("B.cs", "Greeter g;\n");
        var id = Save(Preview((a, [RenameAt(0, 6)]), (b, [RenameAt(0)])));
        File.WriteAllText(b, "Greeter g; // edited\n");

        var error = Assert.Throws<WorkspaceEditException>(() =>
            new WorkspaceEditApplier().Apply(session, id, allowOutsideWorkspace: false));

        Assert.Equal($"{b} changed since preview; run rename preview again", error.Message);
        Assert.Equal("class Greeter {}\n", File.ReadAllText(a));
        Assert.Equal("Greeter g; // edited\n", File.ReadAllText(b));
        Assert.Empty(Temps());
        Assert.True(File.Exists(Path.Combine(session, "edits", id + ".json")));
    }

    [Theory]
    [InlineData("../x")]
    [InlineData("0123456789AB")]
    [InlineData("0123456789a")]
    [InlineData("0123456789abc")]
    [InlineData("0123456789ab")]
    public void An_invalid_or_unknown_plan_id_asks_to_preview_again(string id)
    {
        var path = WriteFile("A.cs", "class Greeter {}\n");
        var saved = Save(Preview((path, [RenameAt(0, 6)])));
        // A plan reachable only by escaping edits/ must not be found.
        File.Copy(Path.Combine(session, "edits", saved + ".json"), Path.Combine(session, "x.json"));

        var error = Assert.Throws<WorkspaceEditException>(() =>
            new WorkspaceEditApplier().Apply(session, id, allowOutsideWorkspace: false));

        Assert.Equal($"unknown plan '{id}'; run rename preview again", error.Message);
        Assert.Equal("class Greeter {}\n", File.ReadAllText(path));
    }

    [Fact]
    public void Apply_refuses_when_a_file_was_deleted_since_preview_and_writes_nothing()
    {
        var a = WriteFile("A.cs", "class Greeter {}\n");
        var b = WriteFile("B.cs", "Greeter g;\n");
        var id = Save(Preview((a, [RenameAt(0, 6)]), (b, [RenameAt(0)])));
        File.Delete(b);

        var error = Assert.Throws<WorkspaceEditException>(() =>
            new WorkspaceEditApplier().Apply(session, id, allowOutsideWorkspace: false));

        Assert.Equal($"{b} changed since preview; run rename preview again", error.Message);
        Assert.Equal("class Greeter {}\n", File.ReadAllText(a));
        Assert.Empty(Temps());
    }

    [Theory]
    [InlineData("{not json")]
    [InlineData("null")]
    [InlineData("""{"workspace":"/w","oldName":"a","newName":"b","changes":{"/w/A.cs":[]},"files":{}}""")]
    [InlineData("""{"workspace":"/w","oldName":"a","changes":{},"files":{}}""")]
    [InlineData("""{"workspace":"/w","oldName":"a","newName":"b","changes":{"/w/A.cs":null},"files":{"/w/A.cs":"h"}}""")]
    [InlineData("""{"workspace":"/w","oldName":"a","newName":"b","changes":{"/w/A.cs":[null]},"files":{"/w/A.cs":"h"}}""")]
    public void A_plan_file_that_cannot_be_read_asks_to_preview_again(string content)
    {
        const string id = "0123456789ab";
        Directory.CreateDirectory(Path.Combine(session, "edits"));
        File.WriteAllText(Path.Combine(session, "edits", id + ".json"), content);

        var error = Assert.Throws<WorkspaceEditException>(() =>
            new WorkspaceEditApplier().Apply(session, id, allowOutsideWorkspace: false));

        Assert.Equal($"plan '{id}' cannot be read; run rename preview again", error.Message);
    }

    [Fact]
    public void A_successful_apply_deletes_the_plan_and_its_diff_and_returns_the_changed_files()
    {
        var a = WriteFile("A.cs", "class Greeter {}\n");
        var b = WriteFile("B.cs", "Greeter g;\n");
        var id = Save(Preview((b, [RenameAt(0)]), (a, [RenameAt(0, 6)])));

        var changed = new WorkspaceEditApplier().Apply(session, id, allowOutsideWorkspace: false);

        Assert.Equal([a, b], changed);
        Assert.Equal("class Welcomer {}\n", File.ReadAllText(a));
        Assert.Equal("Welcomer g;\n", File.ReadAllText(b));
        Assert.Empty(Directory.GetFileSystemEntries(Path.Combine(session, "edits")));
    }

    // --- paths and the workspace guard ---

    [Fact]
    public void A_file_uri_with_a_space_and_non_ascii_characters_resolves_to_the_file()
    {
        var path = WriteFile("My Dir/Grüße.cs", "class Greeter {}\n");
        var uri = "file://" + string.Join("/", path.Split('/').Select(Uri.EscapeDataString));
        Assert.Contains("My%20Dir/Gr%C3%BC%C3%9Fe.cs", uri);

        var preview = WorkspaceEditApplier.Preview(workspace, "Greeter", "Welcomer",
            new Dictionary<string, IReadOnlyList<LspTextEdit>> { [uri] = [RenameAt(0, 6)] });
        new WorkspaceEditApplier().Apply(session, Save(preview), allowOutsideWorkspace: false);

        var file = Assert.Single(preview.Files);
        Assert.Equal(path, file.Path);
        Assert.Equal("My Dir/Grüße.cs", file.RelativePath);
        Assert.Equal("class Greeter {}\n", file.Document.Text);
        Assert.False(file.OutsideRoot);
        Assert.Equal("class Welcomer {}\n", File.ReadAllText(path));
    }

    [Theory]
    [InlineData("https://example.com/Greeter.cs")]
    [InlineData("Greeter.cs")]
    public void A_uri_that_is_not_a_file_uri_is_refused(string uri)
    {
        var error = Assert.Throws<WorkspaceEditException>(() => WorkspaceEditApplier.ResolveUri(uri));

        Assert.Equal($"{uri} is not a file URI", error.Message);
    }

    [Fact]
    public void A_file_uri_for_a_missing_file_is_refused()
    {
        var missing = Path.Combine(workspace, "Missing.cs");

        var error = Assert.Throws<WorkspaceEditException>(() => WorkspaceEditApplier.ResolveUri(UriOf(missing)));

        Assert.Equal($"{missing} does not exist", error.Message);
    }

    [Fact]
    public void A_preview_for_a_missing_workspace_is_refused()
    {
        var path = WriteFile("Greeter.cs", "class Greeter {}\n");
        var gone = Path.Combine(root, "gone");

        var error = Assert.Throws<WorkspaceEditException>(() =>
            WorkspaceEditApplier.Preview(gone, "Greeter", "Welcomer", Changes((path, [RenameAt(0, 6)]))));

        Assert.Equal($"the workspace {gone} does not exist", error.Message);
    }

    [Fact]
    public void A_workspace_reached_through_a_symlink_compares_equal_to_its_real_path()
    {
        WriteFile("Greeter.cs", "class Greeter {}\n");
        var linkedWorkspace = Path.Combine(root, "ws-link");
        Directory.CreateSymbolicLink(linkedWorkspace, workspace);
        var linkedFile = Path.Combine(linkedWorkspace, "Greeter.cs");

        var preview = WorkspaceEditApplier.Preview(linkedWorkspace, "Greeter", "Welcomer",
            Changes((linkedFile, [RenameAt(0, 6)])));
        var id = Save(preview);
        new WorkspaceEditApplier().Apply(session, id, allowOutsideWorkspace: false);

        var file = Assert.Single(preview.Files);
        Assert.False(file.OutsideWorkspace);
        Assert.Equal("Greeter.cs", file.RelativePath);
        Assert.Equal(workspace, preview.Plan.Workspace);
        Assert.StartsWith("--- a/Greeter.cs\n+++ b/Greeter.cs\n", preview.Diff);
        Assert.Equal("class Welcomer {}\n", File.ReadAllText(linkedFile));
    }

    [Theory]
    [InlineData("../other/Greeter.cs")]
    [InlineData("obj/Generated.cs")]
    [InlineData("bin/Generated.cs")]
    [InlineData("src/Demo/obj/Debug/Generated.cs")]
    public void Files_outside_the_root_or_under_bin_or_obj_need_outside_workspace(string relative)
    {
        var path = Path.GetFullPath(WriteFile(relative, "class Greeter {}\n"));
        var inside = WriteFile("src/robin/Greeter.cs", "Greeter g;\n");
        var preview = Preview((path, [RenameAt(0, 6)]), (inside, [RenameAt(0)]));
        var id = Save(preview);

        Assert.True(preview.Files.Single(file => file.Path == path).OutsideWorkspace);
        Assert.False(preview.Files.Single(file => file.Path == inside).OutsideWorkspace);

        var error = Assert.Throws<WorkspaceEditException>(() =>
            new WorkspaceEditApplier().Apply(session, id, allowOutsideWorkspace: false));
        Assert.Contains(path, error.Message);
        Assert.Contains("outside the workspace", error.Message);
        Assert.Equal("class Greeter {}\n", File.ReadAllText(path));
        Assert.Equal("Greeter g;\n", File.ReadAllText(inside));

        new WorkspaceEditApplier().Apply(session, id, allowOutsideWorkspace: true);
        Assert.Equal("class Welcomer {}\n", File.ReadAllText(path));
        Assert.Equal("Welcomer g;\n", File.ReadAllText(inside));
    }

    // --- the diff ---

    static string Lines(int count, Dictionary<int, string> replaced) =>
        string.Concat(Enumerable.Range(1, count).Select(n => (replaced.TryGetValue(n, out var line) ? line : $"line {n}") + "\n"));

    [Fact]
    public void A_hunk_has_three_lines_of_context_and_a_relative_header()
    {
        var path = WriteFile("src/Lines.cs", Lines(20, new() { [10] = "Greeter g;" }));

        var preview = Preview((path, [RenameAt(9)]));

        Assert.Equal("""
            --- a/src/Lines.cs
            +++ b/src/Lines.cs
            @@ -7,7 +7,7 @@
             line 7
             line 8
             line 9
            -Greeter g;
            +Welcomer g;
             line 11
             line 12
             line 13

            """.ReplaceLineEndings("\n"), preview.Diff);
    }

    [Fact]
    public void Hunks_whose_context_overlaps_are_merged_and_later_hunks_count_added_lines()
    {
        var path = WriteFile("Lines.cs", Lines(30, new() { [5] = "Greeter a;", [10] = "Greeter b;", [20] = "Greeter c;" }));

        var preview = Preview((path, [RenameAt(4), Edit(9, 0, 9, 7, "Welcomer\n// split"), RenameAt(19)]));

        Assert.Equal("""
            --- a/Lines.cs
            +++ b/Lines.cs
            @@ -2,12 +2,13 @@
             line 2
             line 3
             line 4
            -Greeter a;
            +Welcomer a;
             line 6
             line 7
             line 8
             line 9
            -Greeter b;
            +Welcomer
            +// split b;
             line 11
             line 12
             line 13
            @@ -17,7 +18,7 @@
             line 17
             line 18
             line 19
            -Greeter c;
            +Welcomer c;
             line 21
             line 22
             line 23

            """.ReplaceLineEndings("\n"), preview.Diff);
    }

    [Fact]
    public void The_diff_lists_files_in_path_order_with_paths_relative_to_the_workspace()
    {
        var a = WriteFile("src/A.cs", "class Greeter {}\r\n");
        var b = WriteFile("B.cs", "Greeter g;");

        var preview = Preview((a, [RenameAt(0, 6)]), (b, [RenameAt(0)]));

        Assert.Equal("""
            --- a/B.cs
            +++ b/B.cs
            @@ -1 +1 @@
            -Greeter g;
            +Welcomer g;
            --- a/src/A.cs
            +++ b/src/A.cs
            @@ -1 +1 @@
            -class Greeter {}
            +class Welcomer {}

            """.ReplaceLineEndings("\n"), preview.Diff);
        Assert.Equal([b, a], preview.Files.Select(file => file.Path));
        Assert.Equal([1, 1], preview.Files.Select(file => file.EditCount));
    }
}
