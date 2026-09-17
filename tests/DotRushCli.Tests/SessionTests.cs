using System.Runtime.Versioning;

namespace DotRushCli.Tests;

// Session lookup, the `session` command and channel readiness, against runtime dirs laid out the way
// lsp-proxy.py writes them into a temp data dir.
[UnsupportedOSPlatform("windows")]
public sealed class SessionTests : IDisposable
{
    static readonly int LivePid = Environment.ProcessId;
    // Past the largest pid macOS and Linux hand out, so nothing can be running under it: the pid of an exited
    // process would be reused eventually and silently turn these tests' "dead proxy" into a live one.
    const int DeadPid = 2147483000;

    readonly DirectoryInfo root = Directory.CreateTempSubdirectory("dotrush-cli-session-");
    readonly string data;
    readonly string ws;
    readonly string project;

    public SessionTests()
    {
        data = Path.Combine(root.FullName, "data");
        ws = Path.Combine(data, "ws");
        project = Directory.CreateDirectory(Path.Combine(root.FullName, "project")).FullName;
        Directory.CreateDirectory(ws);
    }

    public void Dispose()
    {
        try { root.Delete(recursive: true); } catch (IOException) { } catch (UnauthorizedAccessException) { }
    }

    sealed record SessionDirSpec(
        string? Workspace = null,
        string? SessionId = null,
        int? Pid = null,
        string? ClaudePid = null,
        bool Responses = true,
        bool LoadCompleted = true,
        string? Target = null,
        string? Diagnostics = """{"publishes": 0, "files": {}}""");

    string MakeDir(string name, SessionDirSpec spec)
    {
        var dir = Directory.CreateDirectory(Path.Combine(ws, name)).FullName;
        if (spec.Workspace is not null) File.WriteAllText(Path.Combine(dir, "workspace.txt"), spec.Workspace + "\n");
        if (spec.SessionId is not null) File.WriteAllText(Path.Combine(dir, "session.txt"), spec.SessionId + "\n");
        if (spec.Pid is not null) File.WriteAllText(Path.Combine(dir, "pid"), spec.Pid + "\n");
        if (spec.ClaudePid is not null) File.WriteAllText(Path.Combine(dir, "claude-pid"), spec.ClaudePid + "\n");
        if (spec.Responses) Directory.CreateDirectory(Path.Combine(dir, "responses"));
        if (spec.LoadCompleted) File.WriteAllText(Path.Combine(dir, "load-completed"), "");
        if (spec.Target is not null) File.WriteAllText(Path.Combine(dir, "target.json"), spec.Target);
        if (spec.Diagnostics is not null) File.WriteAllText(Path.Combine(dir, "diagnostics.json"), spec.Diagnostics);
        return dir;
    }

    Dictionary<string, string?> Env(string? sessionId = null, string? projectDir = null)
    {
        var env = new Dictionary<string, string?> { ["DOTRUSH_DATA_DIR"] = data };
        if (sessionId is not null) env["DOTRUSH_SESSION_ID"] = sessionId;
        if (projectDir is not null) env["CLAUDE_PROJECT_DIR"] = projectDir;
        return env;
    }

    CliResult Run(Dictionary<string, string?> env, string? cwd, params string[] args) => Cli.Run(env, cwd ?? project, args);

    string NoSessionMessage =>
        $"dotrush-cli: no DotRush language server has started in this session (looked in {ws}); run any C# LSP operation first\n";

    // --- lookup -----------------------------------------------------------------------------------

    [Fact]
    public void A_session_id_picks_the_dir_recording_it()
    {
        MakeDir("sess-aaaaaaaaaaaa", new(Workspace: project, SessionId: "session-a", Pid: LivePid));
        var b = MakeDir("sess-bbbbbbbbbbbb", new(Workspace: project, SessionId: "session-b", Pid: LivePid));

        var result = Run(Env(sessionId: "session-b"), null, "session", "--dir");

        Assert.Equal((0, b + "\n", ""), result);
    }

    [Fact]
    public void Agterm_session_id_is_used_when_dotrush_session_id_is_unset()
    {
        var a = MakeDir("sess-aaaaaaaaaaaa", new(Workspace: project, SessionId: "session-a", Pid: LivePid));
        MakeDir("sess-bbbbbbbbbbbb", new(Workspace: project, SessionId: "session-b", Pid: LivePid));
        var env = Env();
        env["DOTRUSH_SESSION_ID"] = "";
        env["AGTERM_SESSION_ID"] = "session-a";

        Assert.Equal((0, a + "\n", ""), Run(env, null, "session", "--dir"));
    }

    [Fact]
    public void Claude_processes_sharing_an_agterm_session_id_each_get_their_own_dir()
    {
        MakeDir("sess-aaaaaaaaaaaa", new(Workspace: project, SessionId: "tab", Pid: LivePid, ClaudePid: "100"));
        var b = MakeDir("sess-bbbbbbbbbbbb", new(Workspace: project, SessionId: "tab", Pid: LivePid, ClaudePid: "200"));
        var env = Env();
        env["AGTERM_SESSION_ID"] = "tab";
        env["CLAUDE_PID"] = "200";

        Assert.Equal((0, b + "\n", ""), Run(env, null, "session", "--dir"));
    }

    [Fact]
    public void Another_claude_processs_dir_in_the_same_agterm_tab_is_never_picked()
    {
        MakeDir("sess-aaaaaaaaaaaa", new(Workspace: project, SessionId: "tab", Pid: LivePid, ClaudePid: "100"));
        var env = Env();
        env["AGTERM_SESSION_ID"] = "tab";
        env["CLAUDE_PID"] = "200";

        Assert.Equal((1, "", NoSessionMessage), Run(env, null, "session", "--dir"));
    }

    [Fact]
    public void An_agterm_dir_without_a_claude_pid_still_matches()
    {
        var a = MakeDir("sess-aaaaaaaaaaaa", new(Workspace: project, SessionId: "tab", Pid: LivePid));
        var env = Env();
        env["AGTERM_SESSION_ID"] = "tab";
        env["CLAUDE_PID"] = "200";

        Assert.Equal((0, a + "\n", ""), Run(env, null, "session", "--dir"));
    }

    [Fact]
    public void An_explicit_session_id_ignores_the_claude_pid()
    {
        var a = MakeDir("sess-aaaaaaaaaaaa", new(Workspace: project, SessionId: "session-a", Pid: LivePid));
        var env = Env(sessionId: "session-a");
        env["CLAUDE_PID"] = "200";

        Assert.Equal((0, a + "\n", ""), Run(env, null, "session", "--dir"));
    }

    [Fact]
    public void Without_a_session_id_the_workspace_matches_a_dir_without_the_sess_prefix()
    {
        MakeDir("sess-aaaaaaaaaaaa", new(Workspace: project, SessionId: "session-a", Pid: LivePid));
        var hashed = MakeDir("0123456789ab", new(Workspace: project, Pid: LivePid));

        Assert.Equal((0, hashed + "\n", ""), Run(Env(), null, "session", "--dir"));
    }

    [Fact]
    public void Claude_project_dir_is_matched_instead_of_the_cwd()
    {
        var other = Directory.CreateDirectory(Path.Combine(root.FullName, "other")).FullName;
        MakeDir("0123456789ab", new(Workspace: project, Pid: LivePid));
        var otherDir = MakeDir("ba9876543210", new(Workspace: other, Pid: LivePid));

        Assert.Equal((0, otherDir + "\n", ""), Run(Env(projectDir: other), project, "session", "--dir"));
    }

    [Fact]
    public void A_workspace_reached_through_a_symlink_matches_its_real_path()
    {
        var link = Path.Combine(root.FullName, "link");
        Directory.CreateSymbolicLink(link, project);
        var hashed = MakeDir("0123456789ab", new(Workspace: Posix.RealPath(project), Pid: LivePid));

        Assert.Equal((0, hashed + "\n", ""), Run(Env(), link, "session", "--dir"));
    }

    [Fact]
    public void A_session_id_that_matches_nothing_never_picks_another_sessions_dir()
    {
        MakeDir("sess-aaaaaaaaaaaa", new(Workspace: project, SessionId: "session-a", Pid: LivePid));

        Assert.Equal((1, "", NoSessionMessage), Run(Env(sessionId: "session-z"), null, "session", "--dir"));
    }

    [Fact]
    public void A_session_id_that_matches_nothing_falls_back_to_the_workspace_dir()
    {
        MakeDir("sess-aaaaaaaaaaaa", new(Workspace: project, SessionId: "session-a", Pid: LivePid));
        var hashed = MakeDir("0123456789ab", new(Workspace: project, Pid: LivePid));

        Assert.Equal((0, hashed + "\n", ""), Run(Env(sessionId: "session-z"), null, "session", "--dir"));
    }

    [Fact]
    public void With_several_matches_the_dir_with_a_live_pid_wins()
    {
        var live = MakeDir("sess-aaaaaaaaaaaa", new(Workspace: project, SessionId: "same", Pid: LivePid));
        File.SetLastWriteTimeUtc(Path.Combine(live, "pid"), DateTime.UtcNow.AddHours(-1));
        MakeDir("sess-bbbbbbbbbbbb", new(Workspace: project, SessionId: "same", Pid: DeadPid));

        Assert.Equal((0, live + "\n", ""), Run(Env(sessionId: "same"), null, "session", "--dir"));
    }

    [Fact]
    public void With_several_dead_matches_the_newest_pid_file_wins()
    {
        var older = MakeDir("0123456789ab", new(Workspace: project, Pid: DeadPid));
        File.SetLastWriteTimeUtc(Path.Combine(older, "pid"), DateTime.UtcNow.AddHours(-1));
        var newer = MakeDir("ba9876543210", new(Workspace: project, Pid: DeadPid));

        Assert.Equal((0, newer + "\n", ""), Run(Env(), null, "session", "--dir"));
    }

    // --- lookup in unready states ----------------------------------------------------------------

    [Fact]
    public void Session_dir_is_found_while_the_proxy_is_dead_predates_the_channel_and_has_loaded_nothing()
    {
        var dir = MakeDir("sess-aaaaaaaaaaaa", new(Workspace: project, SessionId: "session-a", Pid: DeadPid,
            Responses: false, LoadCompleted: false, Diagnostics: null));

        Assert.Equal((0, dir + "\n", ""), Run(Env(sessionId: "session-a"), null, "session", "--dir"));
    }

    [Fact]
    public void Without_a_session_dir_it_says_to_start_the_language_server()
    {
        Assert.Equal((1, "", NoSessionMessage), Run(Env(), null, "session", "--dir"));
        Assert.Equal((1, "", NoSessionMessage), Run(Env(), null, "session"));
    }

    [Fact]
    public void Without_a_ws_dir_it_says_to_start_the_language_server()
    {
        Directory.Delete(ws);

        Assert.Equal((1, "", NoSessionMessage), Run(Env(), null, "session"));
    }

    [Fact]
    public void Without_the_data_dir_variable_it_refuses()
    {
        var result = Run([], null, "session");

        Assert.Equal(1, result.Exit);
        Assert.Equal("", result.Stdout);
        Assert.Contains("DOTRUSH_DATA_DIR is not set", result.Stderr);
    }

    [Fact]
    public void Unexpected_session_arguments_are_a_usage_error()
    {
        var result = Run(Env(), null, "session", "--frobnicate");

        Assert.Equal(2, result.Exit);
        Assert.Equal("", result.Stdout);
        Assert.Contains("session [--dir]", result.Stderr);
    }

    // --- session output --------------------------------------------------------------------------

    [Fact]
    public void Session_prints_the_state_of_a_ready_session()
    {
        var dir = MakeDir("sess-aaaaaaaaaaaa", new(Workspace: project, SessionId: "session-a", Pid: LivePid,
            Target: """{"path": "/repo/App.sln"}""" + "\n", Diagnostics: """{"publishes": 7, "files": {}}"""));

        var result = Run(Env(sessionId: "session-a"), null, "session");

        Assert.Equal((0, $$"""
            dir: {{dir}}
            workspace: {{project}}
            proxy: running (pid {{LivePid}})
            load: completed
            target: {"path": "/repo/App.sln"}
            publishes: 7
            channel: available

            """, ""), result);
    }

    [Fact]
    public void Session_prints_the_state_of_an_unready_session()
    {
        var dir = MakeDir("sess-aaaaaaaaaaaa", new(Workspace: project, SessionId: "session-a", Pid: DeadPid,
            Responses: false, LoadCompleted: false, Diagnostics: null));

        var result = Run(Env(sessionId: "session-a"), null, "session");

        Assert.Equal((0, $"""
            dir: {dir}
            workspace: {project}
            proxy: not running
            load: not completed (no project loaded yet, or still loading)
            target: none chosen
            publishes: no capture (older proxy)
            channel: unavailable (older proxy)

            """, ""), result);
    }

    [Fact]
    public void Session_reports_unknown_workspace_no_pid_and_zero_publishes()
    {
        var dir = MakeDir("sess-aaaaaaaaaaaa", new(SessionId: "session-a", Diagnostics: """{"files": {}}"""));

        var result = Run(Env(sessionId: "session-a"), null, "session");

        Assert.Equal((0, $"""
            dir: {dir}
            workspace: unknown
            proxy: not running
            load: completed
            target: none chosen
            publishes: 0
            channel: available

            """, ""), result);
    }

    [Fact]
    public void Session_reports_unknown_publishes_for_an_unreadable_diagnostics_file()
    {
        MakeDir("sess-aaaaaaaaaaaa", new(Workspace: project, SessionId: "session-a", Pid: LivePid,
            Diagnostics: "{not json"));

        var result = Run(Env(sessionId: "session-a"), null, "session");

        Assert.Equal((0, ""), (result.Exit, result.Stderr));
        Assert.Contains("\npublishes: unknown\n", result.Stdout);
    }

    // --- channel readiness -----------------------------------------------------------------------

    (int Exit, SessionState? Session, string Stderr) RequireChannel(Dictionary<string, string?> env)
    {
        var stderr = new StringWriter();
        var exit = Session.RequireChannel(new CommandContext(env, project, new StringWriter(), stderr), out var session);
        return (exit, session, stderr.ToString());
    }

    [Fact]
    public void A_ready_session_has_a_channel()
    {
        var dir = MakeDir("sess-aaaaaaaaaaaa", new(Workspace: project, SessionId: "session-a", Pid: LivePid));

        var (exit, session, stderr) = RequireChannel(Env(sessionId: "session-a"));

        Assert.Equal(0, exit);
        Assert.Equal(dir, session?.Dir);
        Assert.Equal("", stderr);
    }

    [Fact]
    public void The_channel_requires_a_session_dir()
    {
        var (exit, session, stderr) = RequireChannel(Env(sessionId: "session-a"));

        Assert.Equal((1, null, NoSessionMessage), (exit, session, stderr));
    }

    [Fact]
    public void The_channel_requires_a_running_proxy()
    {
        MakeDir("sess-aaaaaaaaaaaa", new(Workspace: project, SessionId: "session-a", Pid: DeadPid,
            Responses: false, LoadCompleted: false));

        var (exit, session, stderr) = RequireChannel(Env(sessionId: "session-a"));

        Assert.Equal((1, null,
            "dotrush-cli: the DotRush language server for this session is not running; run any C# LSP operation to start it\n"),
            (exit, session, stderr));
    }

    [Fact]
    public void The_channel_requires_a_proxy_with_a_responses_dir()
    {
        MakeDir("sess-aaaaaaaaaaaa", new(Workspace: project, SessionId: "session-a", Pid: LivePid,
            Responses: false, LoadCompleted: false));

        var (exit, session, stderr) = RequireChannel(Env(sessionId: "session-a"));

        Assert.Equal((1, null,
            "dotrush-cli: the running DotRush proxy predates the request channel; restart Claude Code\n"),
            (exit, session, stderr));
    }

    [Fact]
    public void The_channel_requires_a_completed_load()
    {
        MakeDir("sess-aaaaaaaaaaaa", new(Workspace: project, SessionId: "session-a", Pid: LivePid,
            LoadCompleted: false));

        var (exit, session, stderr) = RequireChannel(Env(sessionId: "session-a"));

        Assert.Equal((1, null,
            "dotrush-cli: DotRush has not finished loading a project in this session; choose one with dotrush-pick-project, or wait for the load to finish and retry\n"),
            (exit, session, stderr));
    }

    [Theory]
    [InlineData("not-a-pid")]
    [InlineData("0")]
    [InlineData("-3")]
    public void A_pid_that_is_not_a_positive_number_means_the_proxy_is_not_running(string pid)
    {
        var dir = MakeDir("sess-aaaaaaaaaaaa", new(Workspace: project, SessionId: "session-a"));
        File.WriteAllText(Path.Combine(dir, "pid"), pid + "\n");

        var result = Run(Env(sessionId: "session-a"), null, "session");

        Assert.Equal((0, ""), (result.Exit, result.Stderr));
        Assert.Contains("\nproxy: not running\n", result.Stdout);
    }

    [Fact]
    public void A_diagnostics_file_that_is_not_an_object_publishes_zero()
    {
        MakeDir("sess-aaaaaaaaaaaa", new(Workspace: project, SessionId: "session-a", Pid: LivePid, Diagnostics: "[]"));

        var result = Run(Env(sessionId: "session-a"), null, "session");

        Assert.Equal((0, ""), (result.Exit, result.Stderr));
        Assert.Contains("\npublishes: 0\n", result.Stdout);
    }
}
