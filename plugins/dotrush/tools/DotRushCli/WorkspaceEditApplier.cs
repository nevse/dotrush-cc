using System.Runtime.Versioning;
using System.Security.Cryptography;
using System.Text;
using System.Text.Encodings.Web;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace DotRushCli;

// LSP shapes as DotRush sends them: 0-based lines, UTF-16 characters.
public sealed record LspPosition(int Line, int Character);

public sealed record LspRange(LspPosition Start, LspPosition End);

public sealed record LspTextEdit(LspRange Range, string NewText);

// What `rename preview` saves to edits/<plan-id>.json and `rename apply` executes. Every path is a fully resolved
// real path; Files holds the sha256 of each file's bytes as the preview read them. Uris holds, per real path, the
// URI DotRush used for the file in its rename result: DotRush knows a document by that form (which may run through
// a symlink, or /var rather than /private/var), so `rename apply` opens the file again under it.
public sealed record EditPlan(
    string Workspace,
    string OldName,
    string NewName,
    IReadOnlyDictionary<string, IReadOnlyList<LspTextEdit>> Changes,
    IReadOnlyDictionary<string, string> Files,
    IReadOnlyDictionary<string, string>? Uris = null)
{
    // The URI to tell DotRush about path by: the one it returned, else the real path's.
    public string UriOf(string path) =>
        Uris is not null && Uris.TryGetValue(path, out var uri) ? uri : new Uri(path).AbsoluteUri;
}

// One file of a preview: how many edits it takes, where it sits relative to the workspace, and the text the preview
// read from it, so a caller can check the edits against the disk without loading the file a second time.
public sealed record FilePreview(
    string Path, string RelativePath, int EditCount, bool OutsideWorkspace, bool OutsideRoot, SourceDocument Document);

public sealed record EditPreview(EditPlan Plan, string Diff, IReadOnlyList<FilePreview> Files);

public sealed class WorkspaceEditException(
    string message, IReadOnlyList<string>? changed = null, IReadOnlyList<string>? unchanged = null) : Exception(message)
{
    // An edit that lands past the end of its line or of the file: DotRush has not seen the file's latest version.
    public static string StaleView(string path) =>
        $"DotRush's view of {path} differs from disk; preview again after the file is saved";

    // Files already rewritten when the apply stopped, and files still as they were.
    public IReadOnlyList<string> Changed { get; } = changed ?? [];
    public IReadOnlyList<string> Unchanged { get; } = unchanged ?? [];
}

// A file's text as DotRush sees it: strict UTF-8 with an optional BOM, lines split where Roslyn's SourceText splits
// them, positions counted in UTF-16 code units.
public sealed class SourceDocument
{
    static readonly byte[] Bom = [0xEF, 0xBB, 0xBF];
    static readonly UTF8Encoding StrictUtf8 = new(encoderShouldEmitUTF8Identifier: false, throwOnInvalidBytes: true);
    const int DiffContext = 3;

    readonly int[] lineStarts;

    SourceDocument(string path, byte[] bytes, bool hasBom, string text)
    {
        Path = path;
        Bytes = bytes;
        HasBom = hasBom;
        Text = text;
        lineStarts = LineStarts(text);
    }

    public string Path { get; }

    public byte[] Bytes { get; }

    public bool HasBom { get; }

    public string Text { get; }

    public static SourceDocument Load(string path)
    {
        try
        {
            return FromBytes(path, File.ReadAllBytes(path));
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException)
        {
            throw new WorkspaceEditException($"cannot read {path}: {e.Message}");
        }
    }

    public static SourceDocument FromBytes(string path, byte[] bytes)
    {
        var hasBom = bytes.AsSpan().StartsWith(Bom);
        var offset = hasBom ? Bom.Length : 0;
        try
        {
            return new(path, bytes, hasBom, StrictUtf8.GetString(bytes, offset, bytes.Length - offset));
        }
        catch (DecoderFallbackException)
        {
            throw new WorkspaceEditException($"{path} is not valid UTF-8; refusing to edit it");
        }
    }

    // The bytes to write for text: the BOM again when the file had one.
    public byte[] Encode(string text)
    {
        try
        {
            var body = StrictUtf8.GetBytes(text);
            return HasBom ? [.. Bom, .. body] : body;
        }
        catch (EncoderFallbackException)
        {
            throw new WorkspaceEditException($"the new text for {Path} is not valid Unicode");
        }
    }

    // The text with every edit applied. Positions refer to the original text, so edits go in last to first.
    public string Apply(IEnumerable<LspTextEdit> edits)
    {
        var result = new StringBuilder(Text);
        foreach (var (edit, start, end) in Enumerable.Reverse(Sorted(edits)))
        {
            result.Remove(start, end - start).Insert(start, edit.NewText);
        }
        return result.ToString();
    }

    // A unified diff of the edits: each changed run of lines with three lines of context, hunks whose context
    // overlaps merged. Empty when the edits change nothing.
    public string Diff(string relativePath, IEnumerable<LspTextEdit> edits)
    {
        var oldLines = SplitLines(Text);
        var blocks = new List<Block>();
        foreach (var edit in Sorted(edits))
        {
            if (blocks.Count > 0 && edit.Edit.Range.Start.Line <= blocks[^1].LastLine)
            {
                blocks[^1].Edits.Add(edit);
                blocks[^1].LastLine = Math.Max(blocks[^1].LastLine, edit.Edit.Range.End.Line);
            }
            else
            {
                blocks.Add(new(edit.Edit.Range.Start.Line, edit.Edit.Range.End.Line, [edit]));
            }
        }
        var changes = blocks.Select(ToChange).Where(change => !change.Old.SequenceEqual(change.New)).ToList();
        if (changes.Count == 0)
        {
            return "";
        }

        var diff = new StringBuilder();
        diff.Append("--- a/").Append(relativePath).Append('\n');
        diff.Append("+++ b/").Append(relativePath).Append('\n');
        var delta = 0;
        for (var first = 0; first < changes.Count;)
        {
            var last = first;
            while (last + 1 < changes.Count && changes[last + 1].FirstLine - changes[last].EndLine <= 2 * DiffContext)
            {
                last++;
            }
            var from = Math.Max(0, changes[first].FirstLine - DiffContext);
            var to = Math.Min(oldLines.Count, changes[last].EndLine + DiffContext);
            var body = new StringBuilder();
            var hunkDelta = 0;
            var line = from;
            for (var i = first; i <= last; i++)
            {
                var change = changes[i];
                for (; line < change.FirstLine; line++)
                {
                    body.Append(' ').Append(oldLines[line]).Append('\n');
                }
                foreach (var removed in change.Old)
                {
                    body.Append('-').Append(removed).Append('\n');
                }
                foreach (var added in change.New)
                {
                    body.Append('+').Append(added).Append('\n');
                }
                line = change.EndLine;
                hunkDelta += change.New.Count - change.Old.Count;
            }
            for (; line < to; line++)
            {
                body.Append(' ').Append(oldLines[line]).Append('\n');
            }
            var oldCount = to - from;
            diff.Append($"@@ -{HunkRange(from, oldCount)} +{HunkRange(from + delta, oldCount + hunkDelta)} @@\n");
            diff.Append(body);
            delta += hunkDelta;
            first = last + 1;
        }
        return diff.ToString();
    }

    sealed class Block(int firstLine, int lastLine, List<(LspTextEdit Edit, int Start, int End)> edits)
    {
        public int FirstLine { get; } = firstLine;
        public int LastLine { get; set; } = lastLine;
        public List<(LspTextEdit Edit, int Start, int End)> Edits { get; } = edits;
    }

    sealed record Change(int FirstLine, List<string> Old, List<string> New)
    {
        public int EndLine => FirstLine + Old.Count;
    }

    // The block's whole lines before and after its edits.
    Change ToChange(Block block)
    {
        var from = lineStarts[block.FirstLine];
        var to = LineEnd(block.LastLine);
        var text = new StringBuilder(Text, from, to - from, to - from);
        foreach (var (edit, start, end) in Enumerable.Reverse(block.Edits))
        {
            text.Remove(start - from, end - start).Insert(start - from, edit.NewText);
        }
        return new(block.FirstLine, SplitLines(Text[from..to]), SplitLines(text.ToString()));
    }

    // "l,s" as unified diffs write it: ",1" omitted, and the line before the hunk when it has no lines.
    static string HunkRange(int start, int count) =>
        count == 1 ? $"{start + 1}" : $"{(count == 0 ? start : start + 1)},{count}";

    // The edits with their offsets, in text order; overlapping edits (or two at the same position, whose order
    // would be ambiguous) are refused.
    List<(LspTextEdit Edit, int Start, int End)> Sorted(IEnumerable<LspTextEdit> edits)
    {
        var sorted = edits
            .Select(edit =>
            {
                var (start, end) = Span(edit.Range);
                return (Edit: edit, Start: start, End: end);
            })
            .OrderBy(edit => edit.Start)
            .ThenBy(edit => edit.End)
            .ToList();
        for (var i = 1; i < sorted.Count; i++)
        {
            if (sorted[i].Start < sorted[i - 1].End || sorted[i].Start == sorted[i - 1].Start)
            {
                throw new WorkspaceEditException(
                    $"edits overlap in {Path} at {Describe(sorted[i - 1].Edit.Range)} and {Describe(sorted[i].Edit.Range)}");
            }
        }
        return sorted;
    }

    (int Start, int End) Span(LspRange range)
    {
        var start = Offset(range.Start);
        var end = Offset(range.End);
        if (end < start)
        {
            throw new WorkspaceEditException($"the range {Describe(range)} in {Path} ends before it starts");
        }
        return (start, end);
    }

    // A position past the end of its line or of the text means the server saw a different version of the file.
    int Offset(LspPosition position) => TryGetOffset(position, out var offset)
        ? offset
        : throw new WorkspaceEditException(WorkspaceEditException.StaleView(Path));

    // The UTF-16 offset of a 0-based position; false when the position is past the end of its line or of the text.
    public bool TryGetOffset(LspPosition position, out int offset)
    {
        offset = 0;
        if (position.Line < 0 || position.Line >= lineStarts.Length
            || position.Character < 0 || position.Character > LineLength(position.Line))
        {
            return false;
        }
        offset = lineStarts[position.Line] + position.Character;
        return true;
    }

    // Where the line ends, its line break included.
    int LineEnd(int line) => line + 1 < lineStarts.Length ? lineStarts[line + 1] : Text.Length;

    // The line's length without its line break.
    int LineLength(int line)
    {
        var end = LineEnd(line);
        if (line + 1 < lineStarts.Length)
        {
            end -= end >= 2 && Text[end - 2] == '\r' && Text[end - 1] == '\n' ? 2 : 1;
        }
        return end - lineStarts[line];
    }

    static string Describe(LspRange range) =>
        $"{range.Start.Line}:{range.Start.Character}-{range.End.Line}:{range.End.Character}";

    static bool IsLineBreak(char c) => c is '\n' or '\r' or '\u0085' or '\u2028' or '\u2029';

    // Where each line starts. As in SourceText, text ending in a line break has a last, empty line after it.
    static int[] LineStarts(string text)
    {
        var starts = new List<int> { 0 };
        for (var i = 0; i < text.Length; i++)
        {
            if (!IsLineBreak(text[i]))
            {
                continue;
            }
            if (text[i] == '\r' && i + 1 < text.Length && text[i + 1] == '\n')
            {
                i++;
            }
            starts.Add(i + 1);
        }
        return [.. starts];
    }

    // The lines as a diff shows them: without line breaks, and without the empty line after a final line break.
    static List<string> SplitLines(string text)
    {
        var starts = LineStarts(text);
        var lines = new List<string>();
        for (var i = 0; i < starts.Length; i++)
        {
            if (i + 1 == starts.Length)
            {
                if (starts[i] < text.Length)
                {
                    lines.Add(text[starts[i]..]);
                }
                break;
            }
            var end = starts[i + 1];
            end -= end >= 2 && text[end - 2] == '\r' && text[end - 1] == '\n' ? 2 : 1;
            lines.Add(text[starts[i]..end]);
        }
        return lines;
    }
}

// Builds rename plans from a WorkspaceEdit and applies them, checking every file before replacing any: every file's
// hash re-checked, every temp file written before any file is replaced. The write and move operations can be swapped out to test failures.
[UnsupportedOSPlatform("windows")]
public sealed partial class WorkspaceEditApplier(
    Action<string, byte[], UnixFileMode>? writeFile = null, Action<string, string>? moveFile = null)
{
    public const string TempSuffix = ".dotrush-cc.tmp";

    static readonly TimeSpan StalePlanAge = TimeSpan.FromHours(1);

    public static readonly JsonSerializerOptions JsonOptions = new(JsonSerializerDefaults.Web)
    {
        WriteIndented = true,
        Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
        RespectNullableAnnotations = true,
        RespectRequiredConstructorParameters = true,
    };

    readonly Action<string, byte[], UnixFileMode> writeFile = writeFile ?? WriteNewFile;
    readonly Action<string, string> moveFile = moveFile ?? ((from, to) => File.Move(from, to, overwrite: true));

    // \z, not $: $ also matches before a trailing newline.
    [GeneratedRegex(@"^[0-9a-f]{12}\z")]
    private static partial Regex PlanIdPattern();

    // Resolves every URI to its real path, checks each file's edits against its text, and computes hashes and the
    // diff. Nothing is written.
    public static EditPreview Preview(string workspace, string oldName, string newName,
        IReadOnlyDictionary<string, IReadOnlyList<LspTextEdit>> changes)
    {
        var root = Posix.RealPath(workspace) ?? throw new WorkspaceEditException($"the workspace {workspace} does not exist");
        var byPath = new SortedDictionary<string, List<LspTextEdit>>(StringComparer.Ordinal);
        var uriByPath = new Dictionary<string, string>(StringComparer.Ordinal);
        foreach (var (uri, edits) in changes)
        {
            var path = ResolveUri(uri);
            if (!byPath.TryGetValue(path, out var list))
            {
                byPath[path] = list = [];
                uriByPath[path] = uri;
            }
            list.AddRange(edits);
        }

        var planChanges = new SortedDictionary<string, IReadOnlyList<LspTextEdit>>(StringComparer.Ordinal);
        var hashes = new SortedDictionary<string, string>(StringComparer.Ordinal);
        var uris = new SortedDictionary<string, string>(StringComparer.Ordinal);
        var files = new List<FilePreview>();
        var diff = new StringBuilder();
        foreach (var (path, edits) in byPath)
        {
            if (edits.Count == 0)
            {
                continue;
            }
            var document = SourceDocument.Load(path);
            var relative = Path.GetRelativePath(root, path);
            diff.Append(document.Diff(relative, edits));
            planChanges[path] = edits;
            hashes[path] = Sha256(document.Bytes);
            uris[path] = uriByPath[path];
            files.Add(new(path, relative, edits.Count, IsOutsideWorkspace(path, root), IsOutsideRoot(relative), document));
        }
        return new(new(root, oldName, newName, planChanges, hashes, uris), diff.ToString(), files);
    }

    // A file URI as a fully resolved real path, symlinks included.
    public static string ResolveUri(string uri)
    {
        if (!Uri.TryCreate(uri, UriKind.Absolute, out var parsed) || !parsed.IsFile)
        {
            throw new WorkspaceEditException($"{uri} is not a file URI");
        }
        var path = parsed.LocalPath;
        return Posix.RealPath(path) ?? throw new WorkspaceEditException($"{path} does not exist");
    }

    // Outside the workspace root, or under a bin or obj directory inside it. Both paths are real paths.
    public static bool IsOutsideWorkspace(string path, string root)
    {
        var relative = Path.GetRelativePath(root, path);
        if (relative == "." || IsOutsideRoot(relative))
        {
            return true;
        }
        return relative.Split('/').Any(segment =>
            segment.Equals("bin", StringComparison.OrdinalIgnoreCase) || segment.Equals("obj", StringComparison.OrdinalIgnoreCase));
    }

    // A relative path that leaves the root it was computed from.
    public static bool IsOutsideRoot(string relative) =>
        relative == ".." || relative.StartsWith("../", StringComparison.Ordinal) || Path.IsPathRooted(relative);

    public static string Sha256(byte[] bytes) => Convert.ToHexStringLower(SHA256.HashData(bytes));

    // Saves the plan and its full diff under <session>/edits/ and returns the new plan id.
    public static string SavePlan(string sessionDir, EditPreview preview)
    {
        var edits = Path.Combine(sessionDir, "edits");
        try
        {
            Directory.CreateDirectory(edits);
            string id;
            do
            {
                id = Convert.ToHexStringLower(RandomNumberGenerator.GetBytes(6));
            }
            while (File.Exists(Path.Combine(edits, id + ".json")));
            File.WriteAllText(Path.Combine(edits, id + ".diff"), preview.Diff);
            File.WriteAllBytes(Path.Combine(edits, id + ".json"), JsonSerializer.SerializeToUtf8Bytes(preview.Plan, JsonOptions));
            return id;
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException)
        {
            throw new WorkspaceEditException($"cannot save the rename plan in {edits}: {e.Message}");
        }
    }

    // Plans a preview saved and nobody applied (a rename the user did not confirm) would otherwise stay until the
    // language server restarts, which is what clears edits/.
    public static void DeleteStalePlans(string sessionDir) =>
        StaleFiles.Delete(Path.Combine(sessionDir, "edits"), StalePlanAge, ".json", ".diff");

    public static string DiffPath(string sessionDir, string planId) => PlanPath(sessionDir, planId, ".diff");

    public static EditPlan LoadPlan(string sessionDir, string planId)
    {
        var path = PlanPath(sessionDir, planId, ".json");
        byte[] bytes;
        try
        {
            bytes = File.ReadAllBytes(path);
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException)
        {
            throw UnknownPlan(planId);
        }
        try
        {
            var plan = JsonSerializer.Deserialize<EditPlan>(bytes, JsonOptions);
            // A JSON null inside the maps survives the record's own nullability checks, which only cover its members.
            if (plan is not null && plan.Changes.Keys.All(plan.Files.ContainsKey)
                && plan.Changes.Values.All(edits => edits is not null && edits.All(edit => edit is not null))
                && plan.Files.Values.All(hash => hash is not null)
                && (plan.Uris is null || plan.Uris.Values.All(uri => uri is not null)))
            {
                return plan;
            }
        }
        catch (JsonException)
        {
        }
        throw new WorkspaceEditException($"plan '{planId}' cannot be read; run rename preview again");
    }

    // Applies a saved plan and deletes it. Returns the changed files in path order.
    public IReadOnlyList<string> Apply(string sessionDir, string planId, bool allowOutsideWorkspace) =>
        Apply(sessionDir, planId, LoadPlan(sessionDir, planId), allowOutsideWorkspace);

    // The same for a plan already read, so a caller that needs the plan itself does not load and validate it twice
    // (and cannot see two different versions of it).
    public IReadOnlyList<string> Apply(string sessionDir, string planId, EditPlan plan, bool allowOutsideWorkspace)
    {
        var root = Posix.RealPath(plan.Workspace) ?? plan.Workspace;
        var paths = plan.Changes.Keys.Order(StringComparer.Ordinal).ToList();

        var outside = paths.Where(path => IsOutsideWorkspace(path, root)).ToList();
        if (outside.Count > 0 && !allowOutsideWorkspace)
        {
            throw new WorkspaceEditException(
                $"{string.Join(", ", outside)} {(outside.Count == 1 ? "is" : "are")} outside the workspace {root} (or under bin/obj); apply with --outside-workspace to edit {(outside.Count == 1 ? "it" : "them")}");
        }

        var updates = new List<(string Path, string Temp, byte[] Bytes, UnixFileMode Mode)>();
        foreach (var path in paths)
        {
            // A plan key is the real path the preview resolved: if it no longer resolves to itself, or the bytes differ,
            // the preview is stale.
            byte[] bytes;
            UnixFileMode mode;
            try
            {
                if (Posix.RealPath(path) != path)
                {
                    throw Stale(path);
                }
                bytes = File.ReadAllBytes(path);
                mode = File.GetUnixFileMode(path);
            }
            catch (Exception e) when (e is IOException or UnauthorizedAccessException)
            {
                throw Stale(path);
            }
            if (Sha256(bytes) != plan.Files[path])
            {
                throw Stale(path);
            }
            var document = SourceDocument.FromBytes(path, bytes);
            updates.Add((path, path + TempSuffix, document.Encode(document.Apply(plan.Changes[path])), mode));
        }

        for (var i = 0; i < updates.Count; i++)
        {
            var update = updates[i];
            try
            {
                writeFile(update.Temp, update.Bytes, update.Mode);
                File.SetUnixFileMode(update.Temp, update.Mode);
            }
            catch (Exception e) when (e is IOException or UnauthorizedAccessException)
            {
                foreach (var written in updates.Take(i + 1))
                {
                    TryDelete(written.Temp);
                }
                throw new WorkspaceEditException($"cannot write {update.Temp}: {e.Message}; no file was changed", [], paths);
            }
        }

        for (var i = 0; i < updates.Count; i++)
        {
            try
            {
                moveFile(updates[i].Temp, updates[i].Path);
            }
            catch (Exception e) when (e is IOException or UnauthorizedAccessException)
            {
                foreach (var pending in updates.Skip(i))
                {
                    TryDelete(pending.Temp);
                }
                var changed = paths.Take(i).ToList();
                var unchanged = paths.Skip(i).ToList();
                // The files already replaced no longer match the plan's hashes, so it could never be applied again.
                DeletePlan(sessionDir, planId);
                throw new WorkspaceEditException(
                    $"cannot replace {updates[i].Path}: {e.Message}; changed: {List(changed)}; unchanged: {List(unchanged)}",
                    changed, unchanged);
            }
        }

        DeletePlan(sessionDir, planId);
        return paths;
    }

    static void DeletePlan(string sessionDir, string planId)
    {
        TryDelete(PlanPath(sessionDir, planId, ".json"));
        TryDelete(PlanPath(sessionDir, planId, ".diff"));
    }

    static string PlanPath(string sessionDir, string planId, string extension) =>
        PlanIdPattern().IsMatch(planId)
            ? Path.Combine(sessionDir, "edits", planId + extension)
            : throw UnknownPlan(planId);

    static WorkspaceEditException UnknownPlan(string planId) =>
        new($"unknown plan '{planId}'; run rename preview again");

    static WorkspaceEditException Stale(string path) => new($"{path} changed since preview; run rename preview again");

    static string List(IReadOnlyList<string> paths) => paths.Count == 0 ? "none" : string.Join(", ", paths);

    // The temp file sits at a predictable path next to its target, so it is created, never opened: an entry already
    // there (a stale temp, or a symlink planted at that name) is removed first rather than written through. The mode
    // is set as the file is created, so a private source file is never world-readable at its temp path.
    static void WriteNewFile(string path, byte[] bytes, UnixFileMode mode)
    {
        TryDelete(path);
        using var stream = new FileStream(path, new FileStreamOptions
        {
            Mode = FileMode.CreateNew,
            Access = FileAccess.Write,
            UnixCreateMode = mode,
        });
        stream.Write(bytes);
    }

    static void TryDelete(string path)
    {
        try
        {
            File.Delete(path);
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException)
        {
        }
    }
}
