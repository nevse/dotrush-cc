import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ANALYZE = ROOT / "plugins/dotrush/scripts/analyze-gcdump.py"
SUMMARIZE = ROOT / "plugins/dotrush/scripts/summarize-speedscope.py"
PROFILE_SH = ROOT / "plugins/dotrush/scripts/dotrush-profile.sh"
INSTALL_SH = ROOT / "plugins/dotrush/scripts/install-dotrush.sh"
PROXY = ROOT / "plugins/dotrush/bin/lsp-proxy.py"
PINNED = json.loads((ROOT / "plugins/dotrush/dotrush-version.json").read_text(encoding="utf-8"))


CORELIB = "/usr/local/share/dotnet/shared/Microsoft.NETCore.App/10.0.10/System.Private.CoreLib.dll"
NEWER_CORELIB = "/usr/local/share/dotnet/shared/Microsoft.NETCore.App/10.0.11/System.Private.CoreLib.dll"
APP = "/work/ProfileTarget/bin/Release/net10.0/ProfileTarget.dll"


def gcdump_graph(types, objects, **fields):
    """Lay out a heap graph the way `dotnet-gcdump --format Json` does.

    Each object is (type index, size, immediate dominator, retained size); object 0 is the root.
    """
    return {
        "version": 1,
        "creationTool": "dotnet-gcdump",
        **fields,
        "rootIndex": 0,
        "types": types,
        "nodes": [value for type_index, size, _, _ in objects for value in (type_index, size, 0)],
        "edges": [],
        "addresses": [0 if size == 0 else 0x10000 + 64 * index for index, (_, size, _, _) in enumerate(objects)],
        "dominators": [dominator for _, _, dominator, _ in objects],
        "retainedSizes": [retained for _, _, _, retained in objects],
    }


def run_analyze(*args, check=True):
    return subprocess.run(
        [sys.executable, str(ANALYZE), *map(str, args)], check=check, capture_output=True, text=True
    )


def section(output, header):
    """Return the rows under one tab-separated header, up to the next blank line."""
    return output.split(header + "\n", 1)[1].split("\n\n", 1)[0].splitlines()


def make_bundle(directory):
    directory.mkdir(parents=True)
    for tool in ("dotnet-trace.dll", "dotnet-gcdump.dll"):
        (directory / tool).write_bytes(b"")
    return directory


def call_helper(function, *args, env=None):
    """Source dotrush-profile.sh (its --help branch does not exit) and call one function."""
    quoted = " ".join(f"'{arg}'" for arg in args)
    environment = {"PATH": os.environ.get("PATH", ""), "HOME": "/home/tester"}
    environment.update(env or {})
    return subprocess.run(
        ["bash", "-c", f'source "$1" --help >/dev/null; {function} {quoted}', "_", str(PROFILE_SH)],
        capture_output=True,
        text=True,
        env=environment,
    )


REPORT_TYPES = [
    {"name": "[.NET Roots]"},
    {"name": "[static var Holder.Cache]"},
    {"name": "System.Collections.Generic.List<LeakedPayload>", "module": CORELIB},
    {"name": "LeakedPayload[] (Bytes > 10K)", "module": CORELIB},
    {"name": "LeakedPayload", "module": APP},
    {"name": "LinkedNode", "module": APP},
    {"name": "System.String", "module": CORELIB},
    {"name": "LeakedPayload[]", "module": CORELIB},
]
# A static List owning a bucketed array of two payloads and a small unbucketed array, a
# three-node linked list whose nodes dominate each other, and a string.
REPORT_OBJECTS = [
    (0, 0, -1, 40398),
    (1, 0, 0, 40296),
    (2, 32, 1, 40296),
    (3, 40000, 2, 40200),
    (4, 100, 3, 100),
    (4, 100, 3, 100),
    (5, 24, 0, 72),
    (5, 24, 6, 48),
    (5, 24, 7, 24),
    (6, 30, 0, 30),
    (7, 64, 2, 64),
]


class GcdumpGraphTests(unittest.TestCase):
    def report(self, document, *options, **dump_options):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "heap.gcdump.json"
            path.write_text(json.dumps(document, **dump_options), encoding="utf-8")
            return run_analyze(*options, "report", path, "--limit", "10").stdout

    def test_sums_exact_bytes_per_type_and_merges_size_buckets(self):
        result = self.report(gcdump_graph(REPORT_TYPES, REPORT_OBJECTS, processId=42))

        self.assertIn("ProcessId\t42", result)
        self.assertIn("HeapBytes\t40398", result)
        self.assertIn("HeapObjects\t9", result)
        types = section(result, "Bytes\tPercent\tCount\tType")
        # The bucketed and unbucketed arrays are one type: 40000 + 64 bytes over two objects.
        self.assertEqual(types[0], "40064\t99.17%\t2\tLeakedPayload[]  [System.Private.CoreLib.dll]")
        self.assertIn("72\t0.18%\t3\tLinkedNode  [ProfileTarget.dll]", types)
        # Root and static-var nodes are graph structure, not objects.
        self.assertFalse(any("[.NET Roots]" in row or "[static var" in row for row in types))
        self.assertEqual(len(types), 5)

    def test_ranks_retained_objects_with_their_dominator_chain(self):
        result = self.report(gcdump_graph(REPORT_TYPES, REPORT_OBJECTS))

        retained = section(result, "RetainedBytes\tPercent\tType\tRetainedBy")
        self.assertEqual(
            retained[0],
            "40296\t99.75%\tSystem.Collections.Generic.List<LeakedPayload>  [System.Private.CoreLib.dll]\t"
            "[static var Holder.Cache] <- [.NET Roots]",
        )
        self.assertEqual(
            retained[1],
            "40200\t99.51%\tLeakedPayload[]  [System.Private.CoreLib.dll]\t"
            "System.Collections.Generic.List<LeakedPayload> <- [static var Holder.Cache] <- [.NET Roots]",
        )
        # Each list node dominates the next, so ranking every one would fill the table with the
        # list's own tail. Only the head is listed.
        self.assertEqual(sum("LinkedNode" in row for row in retained), 1)
        self.assertIn("72\t0.18%\tLinkedNode  [ProfileTarget.dll]\t[.NET Roots]", retained)
        self.assertEqual(len(retained), 7)

    def test_output_does_not_depend_on_where_the_stream_splits_the_file(self):
        # The per-object arrays are parsed a buffer at a time. A boundary can fall inside a number,
        # a triplet, a key or a multi-byte character; none of that may change the report.
        types = REPORT_TYPES + [{"name": "Кэш<Ключ,Значение[]> \"x\"", "module": APP}]
        objects = [(0, 0, -1, 40446)] + REPORT_OBJECTS[1:] + [(8, 48, 0, 48)]
        document = gcdump_graph(types, objects)

        expected = self.report(document, ensure_ascii=False)
        self.assertIn("Кэш<Ключ,Значение[]> \"x\"  [ProfileTarget.dll]", expected)
        for chunk_bytes in ("1", "2", "7", "64"):
            with self.subTest(chunk_bytes=chunk_bytes):
                self.assertEqual(self.report(document, "--chunk-bytes", chunk_bytes, ensure_ascii=False), expected)
        # Indentation puts whitespace between every token.
        self.assertEqual(self.report(document, indent=1, ensure_ascii=False), expected)

    def test_diff_ranks_exact_byte_deltas_across_runtime_versions(self):
        baseline = gcdump_graph(
            [
                {"name": "[.NET Roots]"},
                {"name": "LeakedPayload", "module": APP},
                {"name": "System.Collections.Generic.List<LeakedPayload>", "module": CORELIB},
                {"name": "LeakedPayload[] (Bytes > 1K)", "module": CORELIB},
                {"name": "System.String", "module": CORELIB},
            ],
            [(0, 0, -1, 1162), (1, 100, 0, 100), (2, 32, 0, 32), (3, 1000, 0, 1000), (4, 30, 0, 30)],
        )
        # Type indexes are assigned per dump and the runtime moved to 10.0.11 in between; rows still
        # match on type name and module file name.
        current = gcdump_graph(
            [
                {"name": "[.NET Roots]"},
                {"name": "LeakedPayload[]", "module": NEWER_CORELIB},
                {"name": "LeakedPayload", "module": APP},
                {"name": "LeakedPayload[] (Bytes > 1K)", "module": NEWER_CORELIB},
                {"name": "System.Collections.Generic.List<LeakedPayload>", "module": NEWER_CORELIB},
            ],
            [(0, 0, -1, 1596), (2, 100, 0, 100), (2, 100, 0, 100), (2, 100, 0, 100),
             (4, 32, 0, 32), (3, 1200, 0, 1200), (1, 64, 0, 64)],
        )
        with tempfile.TemporaryDirectory() as directory:
            paths = []
            for name, document in (("baseline", baseline), ("current", current)):
                path = Path(directory) / f"{name}.gcdump.json"
                path.write_text(json.dumps(document), encoding="utf-8")
                paths.append(path)
            result = run_analyze("diff", *paths, "--limit", "10").stdout

        self.assertIn("HeapBytes\t1162\t1596\t+434", result)
        self.assertIn("HeapObjects\t4\t6\t+2", result)
        rows = section(result, "DeltaBytes\tDeltaCount\tBaselineBytes\tCurrentBytes\tBaselineCount\tCurrentCount\tType")
        self.assertEqual(rows, [
            "+264\t+1\t1000\t1264\t1\t2\tLeakedPayload[]  [System.Private.CoreLib.dll]",
            "+200\t+2\t100\t300\t1\t3\tLeakedPayload  [ProfileTarget.dll]",
            "-30\t-1\t30\t0\t1\t0\tSystem.String  [System.Private.CoreLib.dll]",
        ])

    def test_fails_on_a_graph_with_no_objects(self):
        document = gcdump_graph([{"name": "[.NET Roots]"}], [(0, 0, -1, 0)])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "empty.gcdump.json"
            path.write_text(json.dumps(document), encoding="utf-8")
            completed = run_analyze("report", path, check=False)

        self.assertEqual(completed.returncode, 1)
        self.assertIn("no heap objects", completed.stderr)


class ProfileHelperTests(unittest.TestCase):
    def test_duration_must_carry_all_three_time_fields(self):
        # dotnet-trace parses the two-field form as hh:mm, so "00:30" would attach for 30 minutes.
        rejected = call_helper("require_duration", "00:30")
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("mm:ss is rejected", rejected.stderr)

        for accepted in ("00:00:30", "00:02:00", "0:00:30", "01:00:00", "23:59:59",
                         "00:00:10:00", "01:00:00:00", "999:23:59:59"):
            with self.subTest(duration=accepted):
                self.assertEqual(call_helper("require_duration", accepted).returncode, 0)

    def test_duration_rejects_a_field_that_timespan_would_reinterpret(self):
        # TimeSpan.Parse rolls an out-of-range leading field into the next unit up: "24:00:00"
        # is d:hh:mm, i.e. 24 days, and it is the natural way to write "trace for a day".
        for rejected in ("24:00:00", "48:12:30", "99:00:00"):
            with self.subTest(duration=rejected):
                result = call_helper("require_duration", rejected)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("day field", result.stderr)

        # Minutes and seconds past 59 throw in TimeSpan.Parse rather than rolling over; reject
        # them here so the failure lands before the attach rather than after it.
        for rejected in ("00:60:00", "00:00:60", "00:99:99"):
            with self.subTest(duration=rejected):
                self.assertNotEqual(call_helper("require_duration", rejected).returncode, 0)

    def test_duration_rejects_input_that_is_not_a_clock_value(self):
        for rejected in ("", "30", "abc", "00:00:30 ", "-00:00:30", "00:00:30.5"):
            with self.subTest(duration=rejected):
                self.assertNotEqual(call_helper("require_duration", rejected).returncode, 0)

    def test_duration_rejects_an_all_zero_value(self):
        # TimeSpan.Zero means "no duration" to CollectCommand, which then never arms its stop
        # timer: the trace would run unbounded, which is what this check exists to prevent.
        for rejected in ("00:00:00", "0:00:00", "00:00:00:00", "000:00:00:00"):
            with self.subTest(duration=rejected):
                result = call_helper("require_duration", rejected)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("greater than zero", result.stderr)

    def test_output_dir_never_defaults_into_the_working_directory(self):
        # Four branches, in precedence order. None may resolve under $PWD: skills run from the
        # user's repository and the plugin promises to write nothing there.
        env = {"DOTRUSH_PROFILE_OUTPUT_DIR": "/from/env", "CLAUDE_PLUGIN_DATA": "/plugin/data"}

        self.assertEqual(call_helper("output_dir", "/explicit", env=env).stdout.strip(), "/explicit")
        self.assertEqual(call_helper("output_dir", "", env=env).stdout.strip(), "/from/env")

        without_env_var = {"CLAUDE_PLUGIN_DATA": "/plugin/data"}
        self.assertEqual(
            call_helper("output_dir", "", env=without_env_var).stdout.strip(), "/plugin/data/profiles"
        )

        self.assertEqual(
            call_helper("output_dir", "", env={"XDG_CACHE_HOME": "/xdg"}).stdout.strip(),
            "/xdg/dotrush-cc/profiles",
        )
        self.assertEqual(
            call_helper("output_dir", "").stdout.strip(), "/home/tester/.cache/dotrush-cc/profiles"
        )

    def test_speedscope_path_replaces_the_last_extension_like_changeextension(self):
        # dotnet-trace names the converted file with Path.ChangeExtension(--output,
        # "speedscope.json"): it replaces the last dotted segment, and appends only when the
        # file name has no dot. Stripping ".nettrace" disagrees on every other shape.
        cases = {
            "/p/trace_20260101T120000Z_42_9.nettrace": "/p/trace_20260101T120000Z_42_9.speedscope.json",
            "/p/MyApp.Service_20260101.nettrace": "/p/MyApp.Service_20260101.speedscope.json",
            "/p/MyApp.exe_20260101.nettrace": "/p/MyApp.exe_20260101.speedscope.json",
            "/p/v1.2-run.nettrace": "/p/v1.2-run.speedscope.json",
            "/p/run.trace": "/p/run.speedscope.json",
            "/p/nodots": "/p/nodots.speedscope.json",
            "/dotted.dir/run.nettrace": "/dotted.dir/run.speedscope.json",
            "bare.nettrace": "bare.speedscope.json",
        }
        for target, expected in cases.items():
            with self.subTest(target=target):
                result = call_helper("speedscope_report_path", target)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), expected)

    def test_gcdump_json_path_follows_the_converter_naming(self):
        cases = {
            "/p/heap_20260101T120000Z_42_9.gcdump": "/p/heap_20260101T120000Z_42_9.gcdump.json",
            "/p/heap.gcdump.json": "/p/heap.gcdump.json",
            "/p/MyApp.Service.dump": "/p/MyApp.Service.gcdump.json",
            "/p/nodots": "/p/nodots.gcdump.json",
        }
        for target, expected in cases.items():
            with self.subTest(target=target):
                result = call_helper("gcdump_json_path", target)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), expected)

    def test_heapstat_reports_from_older_versions_are_refused(self):
        result = call_helper("ensure_gcdump_json", "/p/heap.heapstat.txt")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no longer read", result.stderr)

    def test_diagnostics_dir_override_wins_and_must_hold_both_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            chosen = make_bundle(home / "custom")
            env = {"HOME": str(home), "DOTRUSH_DIAGNOSTICS_DIR": str(chosen)}
            chosen_result = call_helper("resolve_bundle", env=env)
            env["DOTRUSH_DIAGNOSTICS_DIR"] = str(home / "missing")
            rejected = call_helper("resolve_bundle", env=env)

        self.assertEqual(chosen_result.stdout.strip(), str(chosen), chosen_result.stderr)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("holds no dotnet-trace.dll", rejected.stderr)


class SpeedscopeTests(unittest.TestCase):
    def test_attributes_cpu_marker_to_managed_parent_and_separates_blocked_time(self):
        document = {
            "$schema": "https://www.speedscope.app/file-format-schema.json",
            "shared": {
                "frames": [
                    {"name": "Process64 ProfileTarget (42)"},
                    {"name": "ProfileTarget!HotPath.Run()"},
                    {"name": "CPU_TIME"},
                    {"name": "System.Console!ReadLine()"},
                    {"name": "UNMANAGED_CODE_TIME"},
                ]
            },
            "profiles": [
                {
                    "type": "evented",
                    "name": "Thread (1)",
                    "unit": "milliseconds",
                    "startValue": 0,
                    "endValue": 10,
                    "events": [
                        {"type": "O", "frame": 0, "at": 0},
                        {"type": "O", "frame": 1, "at": 0},
                        {"type": "O", "frame": 2, "at": 0},
                        {"type": "C", "frame": 2, "at": 8},
                        {"type": "C", "frame": 1, "at": 8},
                        {"type": "O", "frame": 3, "at": 8},
                        {"type": "O", "frame": 4, "at": 8},
                        {"type": "C", "frame": 4, "at": 10},
                        {"type": "C", "frame": 3, "at": 10},
                        {"type": "C", "frame": 0, "at": 10},
                    ],
                }
            ],
        }

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "trace.speedscope.json"
            source.write_text(json.dumps(document), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(SUMMARIZE), str(source), "--limit", "10"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout

        self.assertIn("ManagedSampledTime\t8.00", result)
        self.assertIn("UnmanagedOrBlockedTime\t2.00", result)
        self.assertIn("100.00%\t8.00\tProfileTarget!HotPath.Run()", result)
        self.assertNotIn("CPU_TIME\n", result)
        self.assertNotIn("Process64 ProfileTarget", result)


    def test_reports_an_all_unmanaged_capture_instead_of_failing(self):
        # An idle or all-interop target samples no managed frames. That is the answer the CPU
        # skill needs — the capture was idle — so it must be reported, not raised as an error.
        document = {
            "$schema": "https://www.speedscope.app/file-format-schema.json",
            "shared": {
                "frames": [
                    {"name": "Process64 ProfileTarget (42)"},
                    {"name": "System.Console!ReadLine()"},
                    {"name": "UNMANAGED_CODE_TIME"},
                ]
            },
            "profiles": [
                {
                    "type": "evented",
                    "name": "Thread (1)",
                    "unit": "milliseconds",
                    "startValue": 0,
                    "endValue": 10,
                    "events": [
                        {"type": "O", "frame": 0, "at": 0},
                        {"type": "O", "frame": 1, "at": 0},
                        {"type": "O", "frame": 2, "at": 0},
                        {"type": "C", "frame": 2, "at": 10},
                        {"type": "C", "frame": 1, "at": 10},
                        {"type": "C", "frame": 0, "at": 10},
                    ],
                }
            ],
        }

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "idle.speedscope.json"
            source.write_text(json.dumps(document), encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, str(SUMMARIZE), str(source), "--limit", "10"],
                capture_output=True,
                text=True,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("ManagedSampledTime\t0.00", completed.stdout)
        self.assertIn("UnmanagedOrBlockedTime\t10.00", completed.stdout)
        self.assertIn("(no managed samples)", completed.stdout)

    def test_separates_wall_clock_from_the_thread_summed_total(self):
        # Two threads covering the same 10ms window: the spans sum to 20 but only 10ms elapsed.
        # Reporting the sum as "ProfileDuration" read as elapsed time and inflated with thread
        # count, so a 30s capture of a 20-thread service claimed ten minutes.
        def thread(name):
            return {
                "type": "evented", "name": name, "unit": "milliseconds",
                "startValue": 0, "endValue": 10,
                "events": [
                    {"type": "O", "frame": 0, "at": 0},
                    {"type": "O", "frame": 1, "at": 0},
                    {"type": "C", "frame": 1, "at": 10},
                    {"type": "C", "frame": 0, "at": 10},
                ],
            }

        document = {
            "shared": {"frames": [{"name": "Process64 T (1)"}, {"name": "App!Work()"}]},
            "profiles": [thread("Thread (1)"), thread("Thread (2)")],
        }
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "two.speedscope.json"
            source.write_text(json.dumps(document), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(SUMMARIZE), str(source)],
                check=True, capture_output=True, text=True,
            ).stdout

        self.assertIn("Threads\t2", result)
        self.assertIn("WallClockDuration\t10.00", result)
        self.assertIn("SampledThreadTime\t20.00", result)
        self.assertNotIn("ProfileDuration", result)

    def test_trims_il_parameter_lists_but_keeps_them_to_break_ties(self):
        # Full IL signatures run past 400 characters and bury the method name. Two overloads
        # that would print identically must keep their signatures rather than read as one row.
        overload_a = "Lib!Type.Send(class System.String,int32,value class System.TimeSpan)"
        overload_b = "Lib!Type.Send(class System.Object)"
        document = {
            "shared": {
                "frames": [
                    {"name": "Process64 T (1)"},
                    {"name": "App!Only(class System.String,int32,bool,class System.Uri)"},
                    {"name": overload_a},
                    {"name": overload_b},
                ]
            },
            "profiles": [{
                "type": "evented", "name": "Thread (1)", "unit": "milliseconds",
                "startValue": 0, "endValue": 30,
                "events": [
                    {"type": "O", "frame": 0, "at": 0},
                    {"type": "O", "frame": 1, "at": 0}, {"type": "C", "frame": 1, "at": 10},
                    {"type": "O", "frame": 2, "at": 10}, {"type": "C", "frame": 2, "at": 20},
                    {"type": "O", "frame": 3, "at": 20}, {"type": "C", "frame": 3, "at": 30},
                    {"type": "C", "frame": 0, "at": 30},
                ],
            }],
        }
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "names.speedscope.json"
            source.write_text(json.dumps(document), encoding="utf-8")
            result = subprocess.run(
                [sys.executable, str(SUMMARIZE), str(source)],
                check=True, capture_output=True, text=True,
            ).stdout

        # Unambiguous name loses its parameter list.
        self.assertIn("App!Only(...)", result)
        self.assertNotIn("class System.Uri", result)
        # The two overloads would collide, so both keep their full signatures.
        self.assertIn(overload_a, result)
        self.assertIn(overload_b, result)

    def test_still_fails_when_the_document_has_no_profiles(self):
        document = {"shared": {"frames": []}, "profiles": []}
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "empty.speedscope.json"
            source.write_text(json.dumps(document), encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, str(SUMMARIZE), str(source)], capture_output=True, text=True
            )

        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("no profiles found", completed.stderr)


def dead_pid():
    """A PID that belonged to a process which has already exited."""
    process = subprocess.Popen(["true"])
    process.wait()
    return process.pid


def make_zip(path, *names):
    source = path.with_suffix("")
    source.mkdir()
    for name in names:
        (source / name).write_text("")
    subprocess.run(["zip", "-qr", str(path), "."], cwd=source, check=True)
    return path


# Stand-ins for what an install runs; each logs its arguments. curl answers for the release bundles
# named in $STUB_ASSETS and serves $STUB_<COMPONENT>_ZIP; git and dotnet do just enough for a build
# to look finished.
STUB_CURL = r"""#!/usr/bin/env python3
import os, shutil, sys
args = sys.argv[1:]
open(os.environ["STUB_LOG"], "a").write("curl " + " ".join(args) + "\n")
if os.environ.get("STUB_OFFLINE"):
    sys.exit(6)
url = args[-1]
component = next((name for name in ("LanguageServer", "Diagnostics") if url.endswith(f"/DotRush.Bundle.{name}.zip")), None)
if component is None or component not in os.environ.get("STUB_ASSETS", "").split():
    sys.exit(22)
if "-fsIL" not in args and "-o" in args:
    shutil.copy(os.environ[f"STUB_{component.upper()}_ZIP"], args[args.index("-o") + 1])
"""
STUB_GIT = r"""#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
open(os.environ["STUB_LOG"], "a").write("git " + " ".join(args) + "\n")
if "init" in args:
    os.makedirs(args[-1], exist_ok=True)
elif "rev-parse" in args:
    print("89a1406c47ec9046b33cdaca6150aa3d17e31bd8" if "Diagnostics" in args[-1] else "5e1f0c2d3b4a59687766554433221100ffeeddcc")
elif "config" in args:
    print("https://github.com/JaneySprings/" + ("diagnostics.git" if "Diagnostics" in args[-1] else "LanguageServer.Framework.git"))
"""
STUB_DOTNET = r"""#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
open(os.environ["STUB_LOG"], "a").write("dotnet " + " ".join(args) + "\n")
if "--list-sdks" in args:
    print("10.0.108 [/sdk]")
elif "publish" in args:
    if os.environ.get("STUB_BUILD_FAILS"):
        print("error CS0000: stub build failure")
        sys.exit(1)
    out = args[args.index("-o") + 1]
    os.makedirs(out, exist_ok=True)
    project = os.path.basename(args[args.index("publish") + 1])
    name = "DotRush.dll" if project == "DotRush.Roslyn.Server.csproj" else project.replace(".csproj", ".dll")
    open(os.path.join(out, name), "w").close()
elif "--help" in args and os.environ.get("STUB_JSON"):
    print("  --format <GCDump|Json>")
"""
COMMIT = "b720de7ca8d44e125fd56bad860564bd48e31281"


class InstallTests(unittest.TestCase):
    """install-dotrush.sh, the proxy and the profiling helper against stub curl, git and dotnet."""

    def setUp(self):
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)
        self.data = self.root / "data"
        stubs = self.root / "stubs"
        stubs.mkdir()
        for name, body in (("curl", STUB_CURL), ("git", STUB_GIT), ("dotnet", STUB_DOTNET)):
            (stubs / name).write_text(body)
            (stubs / name).chmod(0o755)
        self.log = self.root / "stub.log"
        self.env = {
            "PATH": f"{stubs}:{os.environ.get('PATH', '')}",
            "HOME": str(self.root),
            "CLAUDE_PLUGIN_DATA": str(self.data),
            "DOTRUSH_DOTNET": str(stubs / "dotnet"),
            "STUB_LOG": str(self.log),
            "STUB_LANGUAGESERVER_ZIP": str(make_zip(self.root / "server.zip", "DotRush.dll", "DotRush.runtimeconfig.json")),
            "STUB_DIAGNOSTICS_ZIP": str(make_zip(self.root / "diagnostics.zip", "dotnet-trace.dll", "dotnet-gcdump.dll")),
        }

    def tearDown(self):
        self._directory.cleanup()

    def install(self, component, **env):
        return subprocess.run(
            ["bash", str(INSTALL_SH), component, str(self.data / component)],
            capture_output=True, text=True, env={**self.env, **env},
        )

    def calls(self, tool):
        lines = self.log.read_text().splitlines() if self.log.exists() else []
        return [line for line in lines if line.startswith(tool + " ")]

    def recorded(self, component):
        directory = self.data / component
        return (directory / ".dotrush-ref").read_text().strip(), (directory / ".dotrush-source").read_text().strip()

    def test_ref_and_repository_come_from_the_pin_and_can_be_overridden(self):
        self.assertEqual(call_helper("dotrush_ref", env=self.env).stdout.strip(), PINNED["ref"])
        self.assertEqual(call_helper("dotrush_repo", env=self.env).stdout.strip(), PINNED["repository"])
        overridden = {**self.env, "DOTRUSH_REF": "2026.10", "DOTRUSH_REPO": "someone/DotRush"}
        self.assertEqual(call_helper("dotrush_ref", env=overridden).stdout.strip(), "2026.10")
        self.assertEqual(call_helper("dotrush_repo", env=overridden).stdout.strip(), "someone/DotRush")
        # The ref goes into URLs and install records.
        for rejected in ("../escape", "tag with space", "-flag"):
            with self.subTest(ref=rejected):
                self.assertNotEqual(call_helper("dotrush_ref", env={**self.env, "DOTRUSH_REF": rejected}).returncode, 0)

    def test_a_commit_is_built_for_both_components_without_asking_github(self):
        for component in ("server", "diagnostics"):
            with self.subTest(component=component):
                result = self.install(component, DOTRUSH_REF=COMMIT)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.recorded(component), (COMMIT, "build"))

        self.assertEqual(self.calls("curl"), [])
        git = self.calls("git")
        self.assertTrue(any(f"https://github.com/JaneySprings/DotRush.git {COMMIT}" in call for call in git), git)
        # Each component's submodule is fetched at the commit the DotRush checkout pins.
        self.assertTrue(any("LanguageServer.Framework.git 5e1f0c2d" in call for call in git), git)
        self.assertTrue(any("diagnostics.git 89a1406c" in call for call in git), git)
        publishes = [call for call in self.calls("dotnet") if " publish " in call]
        self.assertEqual(len(publishes), 3)
        # Published as DotRush's own server task does, which is what the LanguageServer bundle zips:
        # no runtime identifier, no native launcher.
        self.assertIn("src/DotRush.Roslyn.Server/DotRush.Roslyn.Server.csproj -c Release -o ", publishes[0])
        self.assertNotIn(" -r ", publishes[0])
        self.assertTrue((self.data / "server" / "DotRush.dll").exists())
        for tool, call in zip(("dotnet-trace", "dotnet-gcdump"), publishes[1:]):
            self.assertIn(f"src/DotRush.Debugging.Diagnostics/src/Tools/{tool}/{tool}.csproj", call)

    def test_a_release_with_every_bundle_is_downloaded_for_both_components(self):
        for component in ("server", "diagnostics"):
            with self.subTest(component=component):
                result = self.install(component, DOTRUSH_REF="2026.10", STUB_ASSETS="LanguageServer Diagnostics")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.recorded(component), ("2026.10", "release"))

        self.assertTrue((self.data / "server" / "DotRush.dll").exists())
        self.assertTrue((self.data / "diagnostics" / "dotnet-gcdump.dll").exists())
        self.assertEqual(self.calls("git"), [])

    def test_a_release_missing_either_bundle_is_built_for_both_components(self):
        # A release with the server bundle and no diagnostics bundle. Downloading one and building the
        # other would put two differently produced halves of one version side by side.
        result = self.install("server", DOTRUSH_REF="2026.10", STUB_ASSETS="LanguageServer")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.recorded("server"), ("2026.10", "build"))
        downloads = [call for call in self.calls("curl") if "-fsIL" not in call]
        self.assertEqual(downloads, [])

    def test_an_install_at_the_pinned_ref_is_left_alone(self):
        self.assertEqual(self.install("server", DOTRUSH_REF=COMMIT).returncode, 0)
        self.log.unlink()
        result = self.install("server", DOTRUSH_REF=COMMIT)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(self.log.exists())

    def test_moving_the_pin_replaces_the_install_whole(self):
        self.assertEqual(self.install("server", DOTRUSH_REF="2026.10", STUB_ASSETS="LanguageServer Diagnostics").returncode, 0)
        (self.data / "server" / "left-by-2026.10.dll").write_text("")
        result = self.install("server", DOTRUSH_REF=COMMIT)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.recorded("server"), (COMMIT, "build"))
        self.assertFalse((self.data / "server" / "left-by-2026.10.dll").exists())
        # No lock, staging directory or build log is left behind.
        self.assertEqual(sorted(p.name for p in self.data.iterdir()), ["server"])

    def test_takes_over_a_lock_left_by_a_killed_installer(self):
        lock = self.data / "server.lock"
        lock.mkdir(parents=True)
        (lock / "pid").write_text(str(dead_pid()))
        result = self.install("server", DOTRUSH_REF=COMMIT)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(lock.exists())

    def test_a_failed_build_keeps_the_previous_install_and_its_log(self):
        self.assertEqual(self.install("server", DOTRUSH_REF="2026.10", STUB_ASSETS="LanguageServer Diagnostics").returncode, 0)
        result = self.install("server", DOTRUSH_REF=COMMIT, STUB_BUILD_FAILS="1")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("stub build failure", result.stderr)
        self.assertEqual(self.recorded("server"), ("2026.10", "release"))
        self.assertEqual(sorted(p.name for p in self.data.iterdir()), ["server", "server-build.log"])

    def test_an_unreachable_github_fails_instead_of_building(self):
        result = self.install("server", DOTRUSH_REF="2026.10", STUB_OFFLINE="1")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("cannot check", result.stderr)
        self.assertEqual(self.calls("git"), [])
        self.assertFalse((self.data / "server.lock").exists())

    def test_skills_find_the_servers_data_dir_without_claude_plugin_data(self):
        # Claude Code sets CLAUDE_PLUGIN_DATA for the language server but not for the Bash a skill
        # runs. The profiling tools must still land beside the server, not in the user cache.
        plugin = self.root / ".claude/plugins/cache/dotrush-cc/dotrush/0.5.0"
        shutil.copytree(ROOT / "plugins/dotrush/scripts", plugin / "scripts")
        without = {key: value for key, value in self.env.items() if key != "CLAUDE_PLUGIN_DATA"}

        def data_dir(library, env):
            result = subprocess.run(
                ["bash", "-c", 'source "$1"; dotrush_data_dir', "_", str(library)],
                capture_output=True, text=True, env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout.strip()

        installed = plugin / "scripts/dotrush-install.sh"
        checkout = ROOT / "plugins/dotrush/scripts/dotrush-install.sh"
        self.assertEqual(data_dir(installed, without), str(self.root / ".claude/plugins/data/dotrush-dotrush-cc"))
        self.assertEqual(data_dir(checkout, without), str(self.root / ".cache/dotrush-cc"))
        self.assertEqual(data_dir(installed, self.env), str(self.data))

    def test_proxy_runs_the_installer_only_for_a_missing_or_stale_server(self):
        installer = self.root / "installer.sh"
        calls = self.root / "installer.calls"
        installer.write_text(f'#!/bin/sh\necho "$@" >> "{calls}"\n')
        check = (
            "import importlib.util\n"
            f"spec = importlib.util.spec_from_file_location('proxy', {str(PROXY)!r})\n"
            "proxy = importlib.util.module_from_spec(spec); spec.loader.exec_module(proxy)\n"
            "print(proxy.ensure_server())\n"
        )
        server = self.data / "server"
        env = {**self.env, "DOTRUSH_SERVER_DIR": str(server), "DOTRUSH_INSTALL_SCRIPT": str(installer),
               "DOTRUSH_PROXY_LOG": ""}

        self.assertEqual(self.install("server", DOTRUSH_REF=PINNED["ref"]).returncode, 0)
        subprocess.run([sys.executable, "-c", check], env=env, check=True, capture_output=True)
        self.assertFalse(calls.exists())

        (server / ".dotrush-ref").write_text("2026.01\n")
        result = subprocess.run([sys.executable, "-c", check], env=env, check=True, capture_output=True, text=True)
        self.assertEqual(calls.read_text().split(), ["server", str(server)])
        # The stub installer changed nothing, so the previous server is still what runs.
        self.assertEqual(result.stdout.strip(), str(server / "DotRush.dll"))

    def test_proxy_runs_the_server_dll_through_the_dotnet_host(self):
        # The LanguageServer bundle has DotRush.dll and no native launcher; an explicit
        # DOTRUSH_REAL_BIN launcher still runs as it is.
        check = (
            "import importlib.util, sys\n"
            f"spec = importlib.util.spec_from_file_location('proxy', {str(PROXY)!r})\n"
            "proxy = importlib.util.module_from_spec(spec); spec.loader.exec_module(proxy)\n"
            "print(proxy.server_command(sys.argv[1]))\n"
        )
        env = {**self.env, "DOTRUSH_PROXY_LOG": ""}
        dotnet = self.env["DOTRUSH_DOTNET"]
        dll = subprocess.run([sys.executable, "-c", check, "/srv/DotRush.dll"], env=env, check=True, capture_output=True, text=True)
        launcher = subprocess.run([sys.executable, "-c", check, "/opt/DotRush"], env=env, check=True, capture_output=True, text=True)

        self.assertEqual(dll.stdout.strip(), str([dotnet, "/srv/DotRush.dll"]))
        self.assertEqual(launcher.stdout.strip(), str(["/opt/DotRush"]))

    def test_profiling_installs_diagnostics_like_the_server_and_drops_the_nuget_tools(self):
        legacy = self.data / "diagnostics-tools"
        legacy.mkdir(parents=True)
        result = call_helper("resolve_bundle", env={**self.env, "DOTRUSH_REF": "2026.10", "STUB_ASSETS": "LanguageServer Diagnostics"})

        self.assertEqual(result.stdout.strip(), str(self.data / "diagnostics"), result.stderr)
        self.assertEqual(self.recorded("diagnostics"), ("2026.10", "release"))
        self.assertFalse(legacy.exists())

    def test_json_commands_refuse_a_pinned_build_without_the_format(self):
        env = {**self.env, "DOTRUSH_REF": COMMIT}
        without = call_helper("resolve_bundle", "json", env=env)
        with_format = call_helper("resolve_bundle", "json", env={**env, "STUB_JSON": "1"})

        self.assertNotEqual(without.returncode, 0)
        self.assertIn("has no --format Json", without.stderr)
        self.assertEqual(with_format.returncode, 0, with_format.stderr)


SUMMARIZE_DIAGNOSTICS = ROOT / "plugins/dotrush/scripts/summarize-diagnostics.py"
DIAGNOSTICS_SH = ROOT / "plugins/dotrush/scripts/dotrush-diagnostics.sh"


def lsp_frame(message):
    body = json.dumps(message).encode("utf-8")
    return f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body


def publish(uri, *diagnostics):
    return {"jsonrpc": "2.0", "method": "textDocument/publishDiagnostics",
            "params": {"uri": uri, "diagnostics": list(diagnostics)}}


def diagnostic(line, character, severity, code, message):
    return {"range": {"start": {"line": line, "character": character}, "end": {"line": line, "character": character + 1}},
            "severity": severity, "code": code, "message": message}


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def write_store(self, path, files, publishes=1, updated=None):
        path.write_text(json.dumps({"publishes": publishes, "updated": time.time() - 60 if updated is None else updated,
                                    "files": files}))

    def test_proxy_forwards_frames_verbatim_and_mirrors_published_diagnostics(self):
        store = self.root / "diagnostics.json"
        a, b = "file:///src/A.cs", "file:///src/B.cs"
        stream = b"".join([
            lsp_frame(publish(a, diagnostic(1, 2, 1, "CS0029", "bad"), diagnostic(3, 0, 2, "CS0219", "unused"))),
            # Mentions the method without being the notification, so it must not count as a publish.
            lsp_frame({"jsonrpc": "2.0", "id": 7, "result": {"note": "textDocument/publishDiagnostics"}}),
            lsp_frame(publish(b, diagnostic(0, 0, 2, "CS0168", "declared"))),
            lsp_frame(publish(a)),
            lsp_frame({"jsonrpc": "2.0", "method": "dotrush/loadCompleted"}),
        ])
        script = (
            "import importlib.util, os, sys\n"
            f"spec = importlib.util.spec_from_file_location('proxy', {str(PROXY)!r})\n"
            "proxy = importlib.util.module_from_spec(spec); spec.loader.exec_module(proxy)\n"
            "os.makedirs(proxy.WS_DIR)\n"
            "store = proxy.DiagnosticsStore(sys.argv[1])\n"
            "proxy.pump_server_to_client(sys.stdin, store)\n"
            "store.write()\n"
            "sys.stderr.write(proxy.LOAD_COMPLETED_FILE)\n"
        )
        env = {**os.environ, "DOTRUSH_PROXY_LOG": "", "DOTRUSH_DATA_DIR": str(self.root), "DOTRUSH_SESSION_ID": "pump"}
        result = subprocess.run([sys.executable, "-c", script, str(store)], input=stream, env=env, capture_output=True, check=True)

        self.assertEqual(result.stdout, stream)
        self.assertTrue(Path(result.stderr.decode()).is_file())
        data = json.loads(store.read_text())
        self.assertEqual(data["publishes"], 3)
        self.assertEqual(list(data["files"]), [b])
        self.assertIsInstance(data["updated"], float)

    def test_summary_lists_errors_first_with_one_based_positions_relative_to_the_root(self):
        store = self.root / "diagnostics.json"
        self.write_store(store, {
            (self.root / "b/Z.cs").as_uri(): [diagnostic(9, 4, 2, "CS0219", "unused\nsecond line")],
            (self.root / "a/Y.cs").as_uri(): [diagnostic(0, 0, 1, "CS0029", "bad"), diagnostic(2, 1, 2, "CS0219", "unused")],
            "file:///elsewhere/X.cs": [diagnostic(5, 5, 3, None, "fyi")],
            # Roslyn marks unnecessary usings in generated obj/ files as hints; they are counted, not listed.
            "file:///obj/Generated.cs": [diagnostic(1, 0, 4, "CS8019", "Unnecessary using directive.")],
        }, publishes=4)

        result = subprocess.run([sys.executable, str(SUMMARIZE_DIAGNOSTICS), str(store), "--root", str(self.root), "--count", "3"],
                                capture_output=True, text=True, check=True)
        with_hints = subprocess.run([sys.executable, str(SUMMARIZE_DIAGNOSTICS), str(store), "--hints"],
                                    capture_output=True, text=True, check=True)

        self.assertIn("Files: 3  Errors: 1  Warnings: 2  Infos: 1  Hints: 1", result.stdout)
        self.assertNotIn("CS8019", result.stdout)
        self.assertIn("1 hint hidden; pass --hints to list", result.stdout)
        self.assertIn("/obj/Generated.cs:2:1  hint  CS8019", with_hints.stdout)
        self.assertIn("      2  warning  CS0219", result.stdout)
        listed = section(result.stdout, "Diagnostics (errors first):")
        self.assertEqual(listed[:3], [
            "  a/Y.cs:1:1  error  CS0029  bad",
            "  a/Y.cs:3:2  warning  CS0219  unused",
            "  b/Z.cs:10:5  warning  CS0219  unused",
        ])
        self.assertIn("... 1 more; pass a larger count", result.stdout)

    def test_waiting_reports_only_once_a_publish_passes_the_baseline(self):
        store = self.root / "diagnostics.json"
        self.write_store(store, {}, publishes=5)
        timed_out = subprocess.run([sys.executable, str(SUMMARIZE_DIAGNOSTICS), str(store), "--after", "5", "--timeout", "0.3"],
                                   capture_output=True, text=True)
        self.assertEqual(timed_out.returncode, 3, timed_out.stderr)
        self.assertIn("No diagnostics were published within 0.3s.", timed_out.stdout)

        self.write_store(store, {"file:///A.cs": [diagnostic(0, 0, 1, "CS1002", "; expected")]}, publishes=6)
        done = subprocess.run([sys.executable, str(SUMMARIZE_DIAGNOSTICS), str(store), "--after", "5", "--timeout", "0.3"],
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("CS1002", done.stdout)

    def session(self, pid):
        ws = self.root / "data/ws/sess-test"
        ws.mkdir(parents=True)
        (ws / "session.txt").write_text("test-session\n")
        (ws / "workspace.txt").write_text(f"{self.root}\n")
        (ws / "pid").write_text(f"{pid}\n")
        (ws / "target.json").write_text("{}\n")
        (ws / "load-completed").write_text("2026-09-13T18:00:00\n")
        self.write_store(ws / "diagnostics.json", {}, publishes=2)
        os.mkfifo(ws / "inject.fifo")
        return ws

    def run_driver(self, *args, **env):
        env = {**os.environ, "CLAUDE_PLUGIN_DATA": str(self.root / "data"), "DOTRUSH_SESSION_ID": "test-session",
               "DOTRUSH_DIAGNOSTICS_QUIET": "0", "DOTRUSH_DIAGNOSTICS_TIMEOUT": "10", **env}
        return subprocess.run(["bash", str(DIAGNOSTICS_SH), *args], capture_output=True, text=True, env=env, timeout=30)

    def test_solution_injects_the_request_and_reports_what_the_server_publishes_next(self):
        import threading

        ws = self.session(os.getpid())
        injected = []

        def fake_proxy():
            with open(ws / "inject.fifo") as fifo:
                injected.append(fifo.readline().strip())
            self.write_store(ws / "diagnostics.json", {(self.root / "A.cs").as_uri(): [diagnostic(4, 8, 1, "CS0029", "bad")]},
                             publishes=3, updated=time.time())

        reader = threading.Thread(target=fake_proxy)
        reader.start()
        result = self.run_driver("solution")
        reader.join(5)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([json.loads(line) for line in injected], [{"method": "dotrush/solutionDiagnostics", "params": {}}])
        self.assertIn("A.cs:5:9  error  CS0029  bad", result.stdout)

    def test_solution_refuses_a_session_whose_proxy_is_gone_or_predates_capture(self):
        ws = self.session(dead_pid())
        gone = self.run_driver("solution")
        self.assertNotEqual(gone.returncode, 0)
        self.assertIn("is not running", gone.stderr)

        (ws / "pid").write_text(f"{os.getpid()}\n")
        (ws / "diagnostics.json").unlink()
        older = self.run_driver("solution")
        self.assertNotEqual(older.returncode, 0)
        self.assertIn("predates diagnostics capture", older.stderr)

    def test_solution_refuses_to_wait_on_a_server_that_never_completed_a_load(self):
        # DotRush starts its analysis worker only after its first load completes, so the request would
        # sit unanswered until the timeout.
        ws = self.session(os.getpid())
        (ws / "load-completed").unlink()

        result = self.run_driver("solution")

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("has not finished loading a project", result.stderr)
        self.assertIn("load: not completed", self.run_driver("where").stdout)

    def test_without_a_session_dir_it_says_to_start_the_language_server(self):
        result = self.run_driver("report")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no DotRush language server has started", result.stderr)

if __name__ == "__main__":
    unittest.main()
