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
# Prepended to a `python -c` script: loads lsp-proxy.py as `proxy`, with the modules the scripts use imported.
PROXY_IMPORT = (
    "import importlib.util, os, select, sys, threading, time\n"
    f"spec = importlib.util.spec_from_file_location('proxy', {str(PROXY)!r})\n"
    "proxy = importlib.util.module_from_spec(spec); spec.loader.exec_module(proxy)\n"
)
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


def untagged_capture():
    """Two 10 ms threads whose every sample carries the UNMANAGED_CODE_TIME tag.

    Thread 7 works: App!Hot.Outer() with App!Hot.Inner() below it for 6 ms, then Outer alone for
    4 ms. Thread 8 sits in App!Waiter.Park() for the whole capture.
    """
    frames = ["Process64 T (1)", "App!Hot.Outer()", "App!Hot.Inner()", "UNMANAGED_CODE_TIME", "App!Waiter.Park()"]

    def opened(frame, at):
        return {"type": "O", "frame": frame, "at": at}

    def closed(frame, at):
        return {"type": "C", "frame": frame, "at": at}

    worker = [
        opened(0, 0), opened(1, 0),
        opened(2, 0), opened(3, 0), closed(3, 4), closed(2, 4),
        opened(3, 4), closed(3, 6),
        opened(2, 6), opened(3, 6), closed(3, 8), closed(2, 8),
        opened(3, 8), closed(3, 10),
        closed(1, 10), closed(0, 10),
    ]
    waiter = [opened(0, 0), opened(4, 0), opened(3, 0), closed(3, 10), closed(4, 10), closed(0, 10)]

    def thread(name, events):
        return {"type": "evented", "name": name, "unit": "milliseconds", "startValue": 0, "endValue": 10,
                "events": events}

    return {
        "shared": {"frames": [{"name": name} for name in frames]},
        "profiles": [thread("Thread (7)", worker), thread("Thread (8)", waiter)],
    }


def block(output, heading):
    """Return the text of the report section whose heading starts with `heading`."""
    return output.split("\n" + heading, 1)[1].split("\n\n", 1)[0]


def summarize(document, *args):
    with tempfile.TemporaryDirectory() as directory:
        source = Path(directory) / "trace.speedscope.json"
        source.write_text(json.dumps(document), encoding="utf-8")
        return subprocess.run(
            [sys.executable, str(SUMMARIZE), str(source), *args], check=True, capture_output=True, text=True
        ).stdout


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
        # The blocked ReadLine stays out: the runtime tags samples, so the tag is trusted.
        self.assertNotIn("ReadLine", block(result, "=== Top 10 functions by exclusive managed CPU"))
        self.assertNotIn("Warning", result)


    def test_ranks_on_stack_time_when_no_sample_is_tagged_managed(self):
        # .NET 9 and 10.0.0-10.0.3 on macOS arm64 tag every sample unmanaged, even a pure managed
        # loop. Dropping those samples left a ranking of the untagged gaps between events and
        # discarded 100% of a 30 s capture's thread time, so the managed frames above the tag are
        # ranked instead, the thread table shows which thread worked, and the report says why.
        document = untagged_capture()
        result = summarize(document, "--limit", "10")

        self.assertIn("ManagedSampledTime\t0.00", result)
        self.assertIn("UnmanagedOrBlockedTime\t20.00", result)
        self.assertIn("ManagedOnStackTime\t20.00", result)
        self.assertIn("Warning\tNo sample in the capture is tagged as running managed code", result)
        self.assertIn("Runtime\tunknown", result)
        threads = block(result, "=== Top 10 threads by stack changes")
        self.assertLess(threads.index("Thread (7)"), threads.index("Thread (8)"))
        self.assertIn("Thread (8)\t100.00% App!Waiter.Park()", threads)
        exclusive = block(result, "=== Top 10 functions by exclusive on-stack time")
        self.assertIn("50.00%\t10.00\tApp!Waiter.Park()", exclusive)
        self.assertIn("30.00%\t6.00\tApp!Hot.Inner()", exclusive)
        self.assertIn("50.00%\t10.00\tApp!Hot.Outer()", block(result, "=== Top 10 functions by inclusive on-stack time"))
        self.assertNotIn("UNMANAGED_CODE_TIME", result)

    def test_thread_option_ranks_one_thread_against_its_own_time(self):
        result = summarize(untagged_capture(), "--thread", "7")

        self.assertIn("SelectedThread\tThread (7)", result)
        self.assertIn("SampledThreadTime\t10.00", result)
        self.assertIn("ManagedOnStackTime\t10.00", result)
        self.assertIn("60.00%\t6.00\tApp!Hot.Inner()", result)
        self.assertNotIn("Waiter.Park", result)
        self.assertNotIn("threads by", result)

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "t.speedscope.json"
            source.write_text(json.dumps(untagged_capture()), encoding="utf-8")
            missing = subprocess.run(
                [sys.executable, str(SUMMARIZE), str(source), "--thread", "9"], capture_output=True, text=True
            )
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("no thread 9", missing.stderr)

    def test_one_tagged_sample_keeps_untagged_waits_out_of_the_rankings(self):
        document = untagged_capture()
        frames = document["shared"]["frames"]
        frames.append({"name": "CPU_TIME"})
        cpu = len(frames) - 1
        events = document["profiles"][0]["events"]
        # Replace the first leaf of the working thread with a CPU_TIME tag.
        events[3] = {"type": "O", "frame": cpu, "at": 0}
        events[4] = {"type": "C", "frame": cpu, "at": 4}
        result = summarize(document)

        self.assertNotIn("Warning", result)
        self.assertNotIn("ManagedOnStackTime", result)
        self.assertIn("=== Top 30 threads by managed CPU ===", result)
        self.assertIn("100.00%\t4.00\tApp!Hot.Outer()", result)
        self.assertNotIn("Waiter.Park", block(result, "=== Top 30 functions by exclusive managed CPU"))

    def test_reports_a_capture_without_managed_frames_instead_of_failing(self):
        # A target parked in native code samples no managed frames. That is the answer the CPU
        # skill needs, so it must be reported, not raised as an error.
        document = {
            "$schema": "https://www.speedscope.app/file-format-schema.json",
            "shared": {
                "frames": [
                    {"name": "Process64 ProfileTarget (42)"},
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
        self.assertNotIn("Warning", completed.stdout)

    def test_names_the_runtime_from_the_corelib_path_in_the_nettrace(self):
        corelib = "/usr/local/share/dotnet/shared/Microsoft.NETCore.App/10.0.0/System.Private.CoreLib.dll"
        windows = r"C:\Program Files\dotnet\shared\Microsoft.NETCore.App\9.0.20\System.Private.CoreLib.dll"
        cases = {
            # Odd offset: the string need not start on an even byte of the file.
            "unix": (b"Nettrace\x00\x01\x02" + corelib.encode("utf-16-le") + b"\x00\x00", "10.0.0"),
            "windows": (b"\x00" * 8 + windows.encode("utf-16-le"), "9.0.20"),
            "none": (b"Nettrace" + "App.dll".encode("utf-16-le"), "unknown"),
        }
        for name, (content, expected) in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as directory:
                trace = Path(directory) / "t.nettrace"
                trace.write_bytes(content)
                self.assertIn(f"Runtime\t{expected}", summarize(untagged_capture(), "--nettrace", str(trace)))

    def test_trace_report_reads_the_runtime_from_the_sibling_nettrace_and_passes_the_thread(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / "trace_20260101T000000Z_1_2"
            speedscope = base.with_name(base.name + ".speedscope.json")
            speedscope.write_text(json.dumps(untagged_capture()), encoding="utf-8")
            corelib = "/dotnet/shared/Microsoft.NETCore.App/10.0.3/System.Private.CoreLib.dll"
            base.with_name(base.name + ".nettrace").write_bytes(corelib.encode("utf-16-le"))
            environment = {"PATH": os.environ.get("PATH", ""), "HOME": directory}

            def run(*args):
                return subprocess.run(
                    ["bash", str(PROFILE_SH), "trace-report", str(speedscope), *args],
                    capture_output=True, text=True, env=environment,
                )

            ranked = run("5", "7")
            rejected = run("5", "Thread (7)")

        self.assertEqual(ranked.returncode, 0, ranked.stderr)
        self.assertIn("Runtime\t10.0.3", ranked.stdout)
        self.assertIn("SelectedThread\tThread (7)", ranked.stdout)
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn("expected a numeric thread id", rejected.stderr)

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


# Stands in for `dotnet <tool>.dll ...`: logs each call, and for `collect` writes the --output file
# (unless STUB_NO_TRACE is set) and exits with STUB_EXIT; `convert` writes the speedscope file.
FAKE_DOTNET = r"""#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open(os.environ["STUB_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(args) + "\n")
if args[1] == "ps":
    sys.stdout.write(os.environ.get("STUB_PS", ""))
    sys.exit(0)
output = args[args.index("--output") + 1]
if args[1] == "collect":
    if not os.environ.get("STUB_NO_TRACE"):
        open(output, "wb").close()
    print("collect chatter")
    sys.exit(int(os.environ.get("STUB_EXIT", "0")))
if args[1] == "convert":
    with open(output[: -len(".nettrace")] + ".speedscope.json", "w", encoding="utf-8") as target:
        target.write(os.environ["STUB_SPEEDSCOPE"])
"""


def sampled_capture(threads):
    """A sampled speedscope document: {thread name: [(frame names, weight), ...]}; frames run root to leaf."""
    frames = []
    profiles = []
    for name, samples in threads.items():
        stacks = []
        for stack, _ in samples:
            for frame in stack:
                if frame not in frames:
                    frames.append(frame)
            stacks.append([frames.index(frame) for frame in stack])
        total = sum(weight for _, weight in samples)
        profiles.append({"type": "sampled", "name": name, "unit": "milliseconds", "startValue": 0,
                         "endValue": total, "samples": stacks, "weights": [weight for _, weight in samples]})
    return {"shared": {"frames": [{"name": frame} for frame in frames]}, "profiles": profiles}


ROOT_FRAME = "Process64 App (1)"


def cpu(*frames):
    return [ROOT_FRAME, *frames, "CPU_TIME"]


def untagged(*frames):
    return [ROOT_FRAME, *frames, "UNMANAGED_CODE_TIME"]


def rows(output, heading):
    """The data rows of a diff section: its heading's rest and the column header are dropped."""
    return [line.split("\t") for line in block(output, heading).splitlines()[2:]]


class TraceDiffTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.root)

    def write(self, name, document):
        path = self.root / name
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def diff(self, baseline, current, *args):
        return subprocess.run(
            [sys.executable, str(SUMMARIZE), str(self.write("current.speedscope.json", current)),
             "--baseline", str(self.write("baseline.speedscope.json", baseline)), *args],
            capture_output=True, text=True,
        )

    def test_ranks_functions_by_change_in_their_share_of_each_capture(self):
        # The current capture is half as long, so Hot's weight falls by 60 but its share only by 40 points.
        baseline = sampled_capture({"Thread (1)": [(cpu("App!Main()", "App!Hot()"), 80), (cpu("App!Main()", "App!Cold()"), 20)]})
        current = sampled_capture({"Thread (9)": [(cpu("App!Main()", "App!Hot()"), 20), (cpu("App!Main()", "App!Cold()"), 20),
                                                  (cpu("App!Main()", "App!New()"), 10)]})

        result = self.diff(baseline, current, "--limit", "10")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ManagedSampledTime\t100.00 -> 50.00\n", result.stdout)
        self.assertIn("Threads\t1 -> 1\n", result.stdout)
        self.assertNotIn("Warning", result.stdout)
        # Equal changes are ordered by name.
        self.assertEqual(rows(result.stdout, "=== Top 10 functions by change in exclusive managed CPU"), [
            ["80.00", "40.00", "-40.00", "80.00", "20.00", "App!Hot()"],
            ["20.00", "40.00", "+20.00", "20.00", "20.00", "App!Cold()"],
            ["0.00", "20.00", "+20.00", "0.00", "10.00", "App!New()"],
        ])
        inclusive = rows(result.stdout, "=== Top 10 functions by change in inclusive managed CPU")
        self.assertEqual(inclusive[-1], ["100.00", "100.00", "+0.00", "100.00", "50.00", "App!Main()"])

    def test_limit_keeps_the_largest_changes(self):
        baseline = sampled_capture({"Thread (1)": [(cpu("App!A()"), 50), (cpu("App!B()"), 45), (cpu("App!C()"), 5)]})
        current = sampled_capture({"Thread (1)": [(cpu("App!A()"), 10), (cpu("App!B()"), 45), (cpu("App!C()"), 45)]})

        result = self.diff(baseline, current, "--limit", "2")

        self.assertEqual([row[5] for row in rows(result.stdout, "=== Top 2 functions by change in exclusive")],
                         ["App!A()", "App!C()"])

    def test_an_untagged_capture_puts_both_on_time_on_stack(self):
        # The baseline's blocked sample counts as on-stack time once the current capture has no managed tag.
        baseline = sampled_capture({"Thread (1)": [(cpu("App!Hot()"), 30), (untagged("App!Wait()"), 70)]})
        current = sampled_capture({"Thread (2)": [(untagged("App!Hot()"), 20), (untagged("App!Wait()"), 80)]})

        result = self.diff(baseline, current)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ManagedOnStackTime\t100.00 -> 100.00\n", result.stdout)
        self.assertIn("Warning\tNo sample in the current capture is tagged", result.stdout)
        self.assertEqual(rows(result.stdout, "=== Top 30 functions by change in exclusive on-stack time"), [
            ["30.00", "20.00", "-10.00", "30.00", "20.00", "App!Hot()"],
            ["70.00", "80.00", "+10.00", "70.00", "80.00", "App!Wait()"],
        ])

    def test_compares_one_thread_from_each_capture(self):
        baseline = sampled_capture({"Thread (1)": [(cpu("App!Hot()"), 10)], "Thread (2)": [(cpu("App!Idle()"), 90)]})
        current = sampled_capture({"Thread (7)": [(cpu("App!Hot()"), 5), (cpu("App!Fast()"), 5)],
                                   "Thread (8)": [(cpu("App!Idle()"), 90)]})

        result = self.diff(baseline, current, "--baseline-thread", "1", "--thread", "7")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("BaselineThread\tThread (1)\nCurrentThread\tThread (7)\n", result.stdout)
        self.assertEqual({row[5]: row[2] for row in rows(result.stdout, "=== Top 30 functions by change in exclusive")},
                         {"App!Hot()": "-50.00", "App!Fast()": "+50.00"})

        one_sided = self.diff(baseline, current, "--thread", "7")
        self.assertEqual(one_sided.returncode, 2)
        self.assertIn("in both captures or in neither", one_sided.stderr)

    def trace_diff(self, *args, **env):
        if not (self.root / "bundle").exists():
            make_bundle(self.root / "bundle")
        dotnet = self.root / "dotnet-stub"
        dotnet.write_text(FAKE_DOTNET, encoding="utf-8")
        dotnet.chmod(0o755)
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(self.root),
            "DOTRUSH_DOTNET": str(dotnet),
            "DOTRUSH_DIAGNOSTICS_DIR": str(self.root / "bundle"),
            "STUB_LOG": str(self.root / "calls.log"),
            **env,
        }
        return subprocess.run(["bash", str(PROFILE_SH), "trace-diff", *map(str, args)],
                              capture_output=True, text=True, env=environment)

    def test_task_events_leave_their_pseudo_frames_out_and_count_awaits_as_blocked(self):
        # With TPL task events the converter stitches a task's stack under its starter's, joined by
        # STARTING TASK, and ends an awaiting stack in AWAIT_TIME.
        stitched = [ROOT_FRAME, "App!Main()", "App!Loop()", "STARTING TASK", "App!Work()"]
        document = sampled_capture({"Thread (1)": [
            (stitched + ["CPU_TIME"], 30),
            (stitched + ["STARTING TASK", "CPU_TIME"], 10),
            (stitched + ["AWAIT_TIME"], 60),
            ([ROOT_FRAME, "App!Main()", "UNKNOWN_ASYNC", "App!Late()", "CPU_TIME"], 10),
        ]})
        path = self.write("tpl.speedscope.json", document)

        report = subprocess.run([sys.executable, str(SUMMARIZE), str(path)], capture_output=True, text=True)

        self.assertEqual(report.returncode, 0, report.stderr)
        self.assertIn("ManagedSampledTime\t50.00\nUnmanagedOrBlockedTime\t60.00\n", report.stdout)
        self.assertIn("Warning\tThe capture has TPL task events", report.stdout)
        self.assertNotIn("ManagedOnStackTime", report.stdout)
        self.assertEqual(block(report.stdout, "=== Top 30 functions by exclusive managed CPU").splitlines()[2:], [
            "80.00%\t40.00\tApp!Work()",
            "20.00%\t10.00\tApp!Late()",
        ])
        self.assertNotIn("STARTING TASK", report.stdout)
        self.assertNotIn("UNKNOWN_ASYNC", report.stdout)

        plain = sampled_capture({"Thread (1)": [(cpu("App!Work()"), 10)]})
        result = self.diff(plain, document)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Warning\tThe current capture has TPL task events", result.stdout)
        self.assertEqual(rows(result.stdout, "=== Top 30 functions by change in exclusive managed CPU")[0],
                         ["0.00", "20.00", "+20.00", "0.00", "10.00", "App!Late()"])

    def test_the_helper_converts_a_nettrace_and_reads_a_speedscope_file(self):
        baseline = self.write("base.speedscope.json", sampled_capture({"Thread (1)": [(cpu("App!Hot()"), 10)]}))
        (self.root / "base.nettrace").write_bytes(CORELIB.replace("10.0.10", "10.0.3").encode("utf-16-le"))
        current = self.root / "current.nettrace"
        current.write_bytes(NEWER_CORELIB.encode("utf-16-le"))
        converted = sampled_capture({"Thread (4)": [(cpu("App!Hot()"), 5), (cpu("App!New()"), 5)]})

        result = self.trace_diff(baseline, current, 5, STUB_SPEEDSCOPE=json.dumps(converted))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"Current\t{self.root / 'current.speedscope.json'}\n", result.stdout)
        self.assertIn("Runtime\t10.0.3 -> 10.0.11\n", result.stdout)
        self.assertIn("=== Top 5 functions by change in exclusive managed CPU", result.stdout)
        calls = [json.loads(line) for line in (self.root / "calls.log").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([call[1] for call in calls], ["convert"])

    def test_the_helper_refuses_bad_arguments(self):
        trace = self.write("t.speedscope.json", sampled_capture({"Thread (1)": [(cpu("App!Hot()"), 10)]}))
        cases = {
            (trace,): "needs baseline and current",
            (trace, trace, 0): "positive count",
            (trace, trace, 5, 1): "a thread id for each capture",
            (trace, trace, 5, 1, "x"): "numeric thread ids",
            (trace, trace, 5, 1, 2, 3): "at most a count and two thread ids",
            (trace, self.root / "missing.nettrace"): "trace not found",
        }
        for args, message in cases.items():
            with self.subTest(args=args):
                result = self.trace_diff(*args)
                self.assertEqual(result.returncode, 1)
                self.assertIn(message, result.stderr)


MAIN, TO_VALUE, OTHER = "App!Program.Main()", "App!Sheet.ToValue()", "App!Program.Other()"
GET_REFERENCE, WRITE, IDENT = "App!Sheet.GetReference(int32)", "App!Sheet.WriteSheetName()", "App!Sheet.IsSheetNameIdent()"


def sheet_capture():
    """GetReference runs 91 of 100 ms: 81 under ToValue and 10 under Other; 50 of it in WriteSheetName, which
    spends 30 in IsSheetNameIdent."""
    return sampled_capture({"Thread (1)": [
        (cpu(MAIN, TO_VALUE, GET_REFERENCE, WRITE, IDENT), 30),
        (cpu(MAIN, TO_VALUE, GET_REFERENCE, WRITE), 20),
        (cpu(MAIN, TO_VALUE, GET_REFERENCE), 31),
        (cpu(MAIN, TO_VALUE), 4),
        (cpu(MAIN, OTHER, GET_REFERENCE), 10),
        (cpu(MAIN, "App!Program.Idle()"), 5),
    ]})


class FocusTests(unittest.TestCase):
    def report(self, document, *args):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "trace.speedscope.json"
            source.write_text(json.dumps(document), encoding="utf-8")
            return subprocess.run([sys.executable, str(SUMMARIZE), str(source), *args], capture_output=True, text=True)

    def focus(self, document, *args):
        result = self.report(document, *args)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def test_prints_the_callers_and_callees_of_one_function(self):
        result = self.focus(sheet_capture(), "--focus", "GetReference")

        self.assertIn(f"Focus\t{GET_REFERENCE}\n", result)
        self.assertIn("FocusInclusive\t91.00%\t91.00\n", result)
        self.assertIn("FocusExclusive\t41.00%\t41.00\n", result)
        self.assertEqual(rows(result, "=== Callers of the focus function"), [
            ["81.00%", "89.01%", "81.00", "App!Sheet.ToValue()"],
            ["81.00%", "89.01%", "81.00", "  App!Program.Main()"],
            ["10.00%", "10.99%", "10.00", "App!Program.Other()"],
            ["10.00%", "10.99%", "10.00", "  App!Program.Main()"],
        ])
        self.assertEqual(rows(result, "=== Callees of the focus function"), [
            ["50.00%", "54.95%", "50.00", "App!Sheet.WriteSheetName()"],
            ["30.00%", "32.97%", "30.00", "  App!Sheet.IsSheetNameIdent()"],
            ["20.00%", "21.98%", "20.00", "  (self)"],
            ["41.00%", "45.05%", "41.00", "(self)"],
        ])
        # The trees replace the thread table and the flat rankings.
        self.assertNotIn("threads by", result)
        self.assertNotIn("functions by", result)

    def test_rows_past_the_count_or_under_one_percent_are_folded_and_depth_caps_the_tree(self):
        document = sheet_capture()
        document["profiles"][0]["weights"][2] += 1000  # GetReference's self time dwarfs WriteSheetName
        folded = self.focus(document, "--focus", "GetReference", "--limit", "1", "--depth", "1")

        self.assertEqual([row[3] for row in rows(folded, "=== Callees of the focus function")],
                         ["(self)", "(1 more)"])
        self.assertEqual([row[3] for row in rows(folded, "=== Callers of the focus function")],
                         ["App!Sheet.ToValue()", "(1 more)"])
        self.assertIn("1 levels up", folded)

        tiny = self.focus(document, "--focus", "GetReference")
        callees = [row[3] for row in rows(tiny, "=== Callees of the focus function")]
        self.assertEqual(callees, ["(self)", "App!Sheet.WriteSheetName()", "  App!Sheet.IsSheetNameIdent()",
                                   "  (self)"])
        # Other's 10 ms is under 1% of GetReference's 1091, so it is folded.
        self.assertEqual([row[3] for row in rows(tiny, "=== Callers of the focus function")],
                         ["App!Sheet.ToValue()", "  App!Program.Main()", "(1 more)"])

    def test_a_recursive_function_is_counted_once_from_its_outermost_call(self):
        rec = "App!Tree.Walk()"
        result = self.focus(sampled_capture({"Thread (1)": [
            (cpu(MAIN, rec, rec, "App!Tree.Leaf()"), 6),
            (cpu(MAIN, rec), 4),
        ]}), "--focus", "Walk")

        self.assertIn("FocusInclusive\t100.00%\t10.00\n", result)
        self.assertEqual(rows(result, "=== Callees of the focus function"), [
            ["60.00%", "60.00%", "6.00", "App!Tree.Walk()"],
            ["60.00%", "60.00%", "6.00", "  App!Tree.Leaf()"],
            ["40.00%", "40.00%", "4.00", "(self)"],
        ])
        self.assertEqual(rows(result, "=== Callers of the focus function"),
                         [["100.00%", "100.00%", "10.00", "App!Program.Main()"]])

    def test_a_caller_that_is_sometimes_outermost_keeps_that_share_in_its_own_row(self):
        result = self.focus(sampled_capture({"Thread (1)": [
            (cpu("App!A()", "App!Focus()"), 5),
            (cpu("App!Root()", "App!A()", "App!Focus()"), 5),
        ]}), "--focus", "Focus")

        self.assertEqual(rows(result, "=== Callers of the focus function"), [
            ["100.00%", "100.00%", "10.00", "App!A()"],
            ["50.00%", "50.00%", "5.00", "  (no caller: outermost managed frame)"],
            ["50.00%", "50.00%", "5.00", "  App!Root()"],
        ])

    def test_the_name_is_matched_whole_first_and_an_ambiguous_one_is_refused_with_candidates(self):
        for needle in (GET_REFERENCE, "App!Sheet.GetReference(...)", "App!Sheet.GetReference", "sheet.getreference"):
            with self.subTest(needle=needle):
                self.assertIn(f"Focus\t{GET_REFERENCE}\n", self.focus(sheet_capture(), "--focus", needle))
        # A whole trailing name wins over the functions that merely contain it.
        self.assertIn(f"Focus\t{WRITE}\n", self.focus(sheet_capture(), "--focus", "WriteSheetName"))

        ambiguous = self.report(sheet_capture(), "--focus", "SheetName")
        self.assertEqual(ambiguous.returncode, 1)
        self.assertIn("'SheetName' matches 2 functions", ambiguous.stderr)
        self.assertIn(f"50.00\t{WRITE}", ambiguous.stderr)
        self.assertIn(f"30.00\t{IDENT}", ambiguous.stderr)
        self.assertNotIn("Traceback", ambiguous.stderr)

        # Parameterless frames match by their trailing name too, and an overload is not preferred for having
        # parameters: `Run` names both overloads below and is refused, while `RunAll` never matches it.
        overloads = sampled_capture({"Thread (1)": [
            (cpu(MAIN, "App!Program.Run()"), 90),
            (cpu(MAIN, "App!Worker.Run(int32)"), 5),
            (cpu(MAIN, "App!Program.RunAll()"), 5),
        ]})
        for needle, expected in (
            ("Program.Run", "App!Program.Run()"),
            ("app!program.run", "App!Program.Run()"),
            ("App!Program.Run", "App!Program.Run()"),
            ("Program.Run(...)", "App!Program.Run()"),
            ("Worker.Run", "App!Worker.Run(int32)"),
            ("RunAll", "App!Program.RunAll()"),
            ("Sheet.GetReference(...)", None),
        ):
            with self.subTest(needle=needle):
                if expected is None:
                    self.assertIn(f"Focus\t{GET_REFERENCE}\n", self.focus(sheet_capture(), "--focus", needle))
                else:
                    self.assertIn(f"Focus\t{expected}\n", self.focus(overloads, "--focus", needle))
        # Overloads on one type: a trimmed row name names both, a whole signature names one.
        same_type = sampled_capture({"Thread (1)": [
            (cpu(MAIN, "App!Program.Run()"), 60),
            (cpu(MAIN, "App!Program.Run(int32)"), 40),
        ]})
        for needle in ("App!Program.Run()", "App!Program.Run(int32)"):
            with self.subTest(needle=needle):
                self.assertIn(f"Focus\t{needle}\n", self.focus(same_type, "--focus", needle))
        for needle in ("App!Program.Run(...)", "Program.Run(...)", "Program.Run"):
            with self.subTest(needle=needle):
                trimmed = self.report(same_type, "--focus", needle)
                self.assertEqual(trimmed.returncode, 1)
                self.assertIn("matches 2 functions", trimmed.stderr)
                self.assertIn("60.00\tApp!Program.Run()\n", trimmed.stderr)
                self.assertIn("40.00\tApp!Program.Run(int32)\n", trimmed.stderr)

        both = self.report(overloads, "--focus", "Run")
        self.assertEqual(both.returncode, 1)
        self.assertIn("'Run' matches 2 functions", both.stderr)
        self.assertNotIn("RunAll", both.stderr)

        missing = self.report(sheet_capture(), "--focus", "Nowhere")
        self.assertEqual(missing.returncode, 1)
        self.assertIn("no sampled function matches --focus 'Nowhere'", missing.stderr)

    def test_an_untagged_capture_builds_the_trees_from_time_on_stack(self):
        result = self.focus(untagged_capture(), "--focus", "Outer")

        self.assertIn("Warning\tNo sample in the capture is tagged", result)
        self.assertIn("FocusInclusive\t50.00%\t10.00\n", result)
        self.assertEqual(rows(result, "=== Callees of the focus function"), [
            ["30.00%", "60.00%", "6.00", "App!Hot.Inner()"],
            ["20.00%", "40.00%", "4.00", "(self)"],
        ])
        self.assertIn("by inclusive on-stack time", result)

    def test_focus_is_refused_for_a_comparison_and_depth_must_be_positive(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "t.speedscope.json"
            source.write_text(json.dumps(sheet_capture()), encoding="utf-8")
            compared = subprocess.run([sys.executable, str(SUMMARIZE), str(source), "--baseline", str(source),
                                       "--focus", "Main"], capture_output=True, text=True)
        self.assertEqual(compared.returncode, 2)
        self.assertIn("--focus reports one capture", compared.stderr)
        self.assertIn("--depth must be positive", self.report(sheet_capture(), "--focus", "Main", "--depth", "0").stderr)

    def test_the_helper_passes_focus_and_depth_and_refuses_bad_options(self):
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory) / "t.speedscope.json"
            trace.write_text(json.dumps(sheet_capture()), encoding="utf-8")
            environment = {"PATH": os.environ.get("PATH", ""), "HOME": directory}

            def run(*args):
                return subprocess.run(["bash", str(PROFILE_SH), "trace-report", *map(str, args)],
                                      capture_output=True, text=True, env=environment)

            focused = run(trace, 5, "--focus", "Sheet.GetReference", "--depth", "1")
            options_first = run(trace, "--focus", "WriteSheetName", 5, 1)
            cases = {
                (trace, "--depth", 2): "--depth needs --focus",
                (trace, "--focus"): "--focus needs a value",
                (trace, "--focus", "A", "--focus", "B"): "--focus is given twice",
                (trace, "--focus", "Main", "--depth", "x"): "positive --depth",
                (trace, "--focus", "Main", "--top", 3): "unknown trace-report option: --top",
                (trace, 5, 1, 2): "takes a trace, a count and a thread id",
                (trace, "--focus", "Nowhere"): "no sampled function matches",
            }
            refused = {args: run(*args) for args in cases}

        self.assertEqual(focused.returncode, 0, focused.stderr)
        self.assertIn(f"Focus\t{GET_REFERENCE}\n", focused.stdout)
        self.assertIn("1 levels down", focused.stdout)
        self.assertEqual(options_first.returncode, 0, options_first.stderr)
        self.assertIn("SelectedThread\tThread (1)", options_first.stdout)
        self.assertIn(f"Focus\t{WRITE}\n", options_first.stdout)
        for args, message in cases.items():
            with self.subTest(args=args):
                self.assertEqual(refused[args].returncode, 1)
                self.assertIn(message, refused[args].stderr)


class ProcessListTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        dotnet = self.root / "dotnet-stub"
        dotnet.write_text(FAKE_DOTNET, encoding="utf-8")
        dotnet.chmod(0o755)
        self.log = self.root / "calls.log"
        # Two processes the tool would list identically, told apart only by their arguments.
        self.hosts = [
            subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", f"/w/{name}.dll", "--filter", name])
            for name in ("testhost", "Other.Tests")
        ]
        for host in self.hosts:
            self.addCleanup(host.wait)
            self.addCleanup(host.kill)
        gone = subprocess.Popen([sys.executable, "-c", "pass"])
        gone.wait()
        self.gone = gone.pid
        listing = "".join(f" {host.pid}  dotnet  /usr/local/share/dotnet/dotnet   \n" for host in self.hosts)
        self.env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(self.root),
            "DOTRUSH_DOTNET": str(dotnet),
            "DOTRUSH_DIAGNOSTICS_DIR": str(make_bundle(self.root / "bundle")),
            "STUB_LOG": str(self.log),
            # A pid that no longer exists keeps the tool's path as its command.
            "STUB_PS": listing + f" {self.gone}  dotnet  /usr/local/share/dotnet/dotnet\n",
        }

    def ps(self, *args):
        return subprocess.run(["bash", str(PROFILE_SH), "ps", *args], capture_output=True, text=True, env=self.env)

    def test_rows_carry_elapsed_time_assembly_and_command_line(self):
        result = self.ps("gcdump")

        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(lines[0], "PID\tELAPSED\tNAME\tASSEMBLY\tCOMMAND")
        rows = [line.split("\t") for line in lines[1:]]
        self.assertEqual([row[0] for row in rows], [str(host.pid) for host in self.hosts] + [str(self.gone)])
        self.assertEqual([row[3] for row in rows], ["testhost.dll", "Other.Tests.dll", "dotnet"])
        self.assertRegex(rows[0][1], r"^\d+:\d\d$")
        self.assertTrue(rows[0][4].endswith("/w/testhost.dll --filter testhost"), rows[0][4])
        self.assertEqual(rows[2][1:], ["-", "dotnet", "dotnet", "/usr/local/share/dotnet/dotnet"])
        self.assertEqual(json.loads(self.log.read_text(encoding="utf-8").splitlines()[0])[1:], ["ps"])

    def test_filter_keeps_matching_rows_ignoring_case(self):
        result = self.ps("--filter", "TESTHOST")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([line.split("\t")[0] for line in result.stdout.splitlines()[1:]], [str(self.hosts[0].pid)])
        self.assertIn("dotnet-trace.dll", self.log.read_text(encoding="utf-8"))

    def test_a_filter_matching_nothing_fails_and_says_how_many_were_listed(self):
        result = self.ps("trace", "--filter", "nothing-like-this")

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("no .NET process matches 'nothing-like-this' (3 listed)", result.stderr)

    def test_a_long_command_line_keeps_its_tail(self):
        long_host = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "x" * 300, "/w/Tail.dll"])
        self.addCleanup(long_host.wait)
        self.addCleanup(long_host.kill)
        self.env["STUB_PS"] = f"{long_host.pid} dotnet /usr/local/share/dotnet/dotnet\n"

        result = self.ps()

        command = result.stdout.splitlines()[1].split("\t")[4]
        self.assertEqual(len(command), 200)
        self.assertTrue(command.startswith("…") and command.endswith("x /w/Tail.dll"), command)

    def test_unknown_arguments_are_refused(self):
        for args in (["trace", "--filter"], ["trace", "--grep", "x"], ["trace", "--filter", "x", "y"], ["other"]):
            with self.subTest(args=args):
                result = self.ps(*args)
                self.assertEqual(result.returncode, 1)
                self.assertIn("dotrush-profile:", result.stderr)


class TraceLaunchTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        dotnet = self.root / "dotnet-stub"
        dotnet.write_text(FAKE_DOTNET, encoding="utf-8")
        dotnet.chmod(0o755)
        self.log = self.root / "calls.log"
        self.out = self.root / "out"
        self.env = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(self.root),
            "DOTRUSH_DOTNET": str(dotnet),
            "DOTRUSH_DIAGNOSTICS_DIR": str(make_bundle(self.root / "bundle")),
            "STUB_LOG": str(self.log),
            "STUB_SPEEDSCOPE": json.dumps(untagged_capture()),
        }

    def trace(self, *args, **env):
        return subprocess.run(
            ["bash", str(PROFILE_SH), "trace", *args],
            capture_output=True, text=True, env={**self.env, **env},
        )

    def calls(self):
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def test_launches_the_command_and_reports_its_exit_code_with_the_artifacts(self):
        # A failing command (a red test) still leaves a trace worth reading.
        result = self.trace("--launch", "00:00:10", str(self.out), "--", "./App", "--filter", "Name=Spin",
                            STUB_EXIT="3")

        self.assertEqual(result.returncode, 0, result.stderr)
        keys = dict(line.split("=", 1) for line in result.stdout.splitlines())
        self.assertEqual(list(keys), ["TRACE", "EXIT", "SPEEDSCOPE", "REPORT"])
        self.assertEqual(keys["EXIT"], "3")
        self.assertRegex(Path(keys["TRACE"]).name, r"^trace_\d{8}T\d{6}Z_launch_\d+\.nettrace$")
        self.assertEqual(Path(keys["TRACE"]).parent, self.out)
        self.assertIn("SampledThreadTime", Path(keys["REPORT"]).read_text(encoding="utf-8"))
        self.assertIn("collect chatter", result.stderr)
        collect = self.calls()[0]
        self.assertEqual(collect[1:4], ["collect", "--duration", "00:00:10"])
        self.assertNotIn("--process-id", collect)
        self.assertEqual(collect[collect.index("--"):], ["--", "./App", "--filter", "Name=Spin"])

    def test_defaults_the_duration_and_output_dir(self):
        result = self.trace("--launch", "--", "dotnet", "App.dll", DOTRUSH_PROFILE_OUTPUT_DIR=str(self.out))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"TRACE={self.out}/", result.stdout)
        self.assertIn("00:00:30", self.calls()[0])

    def test_fails_with_the_exit_code_when_no_trace_was_written(self):
        result = self.trace("--launch", "00:00:10", str(self.out), "--", "./missing", STUB_EXIT="3",
                            STUB_NO_TRACE="1")

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("(exit 3)", result.stderr)

    def test_refuses_sdk_commands_whose_children_would_hang(self):
        # Every .NET process the command starts inherits the suspended diagnostic port and never resumes.
        for command in (["dotnet", "test"], ["/usr/local/share/dotnet/dotnet", "run", "--project", "x"], ["dotnet"]):
            with self.subTest(command=command):
                result = self.trace("--launch", "--", *command)
                self.assertEqual(result.returncode, 1)
                self.assertIn("would hang on the suspended diagnostic port", result.stderr)
        for command in (["dotnet", "exec", "App.dll"], ["dotnet", "bin/App.dll", "arg"], ["./dotnet-app"]):
            with self.subTest(command=command):
                self.assertEqual(self.trace("--launch", str(self.out), "--", *command).returncode, 1)
                self.assertEqual(self.trace("--launch", "00:00:01", str(self.out), "--", *command).returncode, 0)
        self.assertEqual(len(self.calls()), 6)

    def test_rejects_malformed_launch_arguments_before_running_anything(self):
        cases = {
            ("--launch",): "needs -- before the command",
            ("--launch", "00:00:05", str(self.out)): "needs -- before the command",
            ("--launch", "--"): "needs a command after --",
            ("--launch", "00:00:05", "a", "b", "--", "./App"): "at most a duration and an output dir",
            ("--launch", "00:30", "--", "./App"): "mm:ss is rejected",
        }
        for args, message in cases.items():
            with self.subTest(args=args):
                result = self.trace(*args)
                self.assertEqual(result.returncode, 1)
                self.assertIn(message, result.stderr)
        self.assertEqual(self.calls(), [])

    def collect_options(self):
        collect = self.calls()[-2]  # the last call converts the trace
        end =collect.index("--show-child-io") if "--show-child-io" in collect else len(collect)
        return collect[collect.index("--output") + 2:end]

    def test_passes_trace_options_and_always_keeps_the_sampler(self):
        # The report is built from the thread-time sampler, which dotnet-trace drops once --profile or
        # --providers is given.
        cases = {
            ("--profile", "gc-verbose"): ["--profile", "gc-verbose,dotnet-sampled-thread-time"],
            ("--profile", "dotnet-sampled-thread-time,database"): ["--profile", "dotnet-sampled-thread-time,database"],
            ("--providers", "System.Threading.Tasks.TplEventSource:0x1C3:5"): [
                "--profile", "dotnet-common,dotnet-sampled-thread-time",
                "--providers", "System.Threading.Tasks.TplEventSource:0x1C3:5",
            ],
            ("--buffersize", "512"): ["--buffersize", "512"],
            (): [],
        }
        for options, expected in cases.items():
            with self.subTest(options=options):
                launched = self.trace("--launch", "00:00:10", *options, str(self.out), "--", "./App")
                self.assertEqual(launched.returncode, 0, launched.stderr)
                self.assertEqual(self.collect_options(), expected)
                self.assertEqual(self.calls()[-2][-3:], ["--show-child-io", "--", "./App"])

                attached = self.trace(*options, "42", "00:00:10", str(self.out))
                self.assertEqual(attached.returncode, 0, attached.stderr)
                self.assertEqual(self.calls()[-2][1:4], ["collect", "--process-id", "42"])
                self.assertEqual(self.collect_options(), expected)

    def test_rejects_malformed_trace_options_before_running_anything(self):
        cases = {
            ("42", "--profile"): "--profile needs a value",
            ("42", "--profile", "--buffersize", "1"): "--profile needs a value",
            ("42", "--profile", "gc verbose"): "comma-separated profile names",
            ("42", "--profile", "a", "--profile", "b"): "--profile given twice",
            ("42", "--providers", "A:1 B:2"): "must not contain whitespace",
            ("42", "--buffersize", "0"): "size in MB",
            ("42", "--buffersize", "big"): "size in MB",
            ("42", "--clrevents", "gc"): "unknown trace option '--clrevents'",
            ("42", "--launch"): "--launch must come right after trace",
            ("42", "00:00:05", "out", "extra"): "a pid, a duration and an output dir",
            ("42", "--", "./App"): "only trace --launch takes a command",
            ("--launch", "--profile", "--", "./App"): "--profile needs a value",
        }
        for args, message in cases.items():
            with self.subTest(args=args):
                result = self.trace(*args)
                self.assertEqual(result.returncode, 1)
                self.assertIn(message, result.stderr)
        self.assertEqual(self.calls(), [])

    def test_attach_still_takes_a_pid(self):
        result = self.trace("42", "00:00:10", str(self.out))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertRegex(result.stdout, r"TRACE=.*_42_\d+\.nettrace\nSPEEDSCOPE=")
        self.assertNotIn("EXIT=", result.stdout)
        self.assertEqual(self.calls()[0][1:4], ["collect", "--process-id", "42"])
        self.assertNotIn("--", self.calls()[0])


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
            PROXY_IMPORT
            + "print(proxy.ensure_server())\n"
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
            PROXY_IMPORT
            + "print(proxy.server_command(sys.argv[1]))\n"
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
CLI_TOOLS = ROOT / "plugins/dotrush/tools"


def build_cli(directory):
    """Builds DotRushCli into directory, keeping build output out of the repo tree; returns the dir for DOTRUSH_CLI_DIR."""
    output = directory / "cli"
    result = subprocess.run([os.environ.get("DOTRUSH_DOTNET") or "dotnet", "build", "DotRushCli/DotRushCli.csproj",
                             "-c", "Release", "--nologo", "--artifacts-path", str(directory / "artifacts"), "-o", str(output)],
                            cwd=CLI_TOOLS, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=600)
    if result.returncode != 0 or not (output / "DotRushCli.dll").is_file():
        raise RuntimeError(f"building DotRushCli failed:\n{result.stdout}\n{result.stderr}")
    return output


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
    @classmethod
    def setUpClass(cls):
        # dotrush-diagnostics.sh finds the session through the CLI; one real build serves the whole class.
        cli = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cli.cleanup)
        cls.cli_dir = build_cli(Path(cli.name))

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
            PROXY_IMPORT
            + "os.makedirs(proxy.WS_DIR)\n"
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

    def pump(self, stream, make_responses=True):
        """Runs the proxy's server->client pump over stream; returns (stdout, WS_DIR).

        responses/ is prepared as a proxy start would prepare it, unless make_responses is False."""
        setup = ("os.makedirs(os.path.join(proxy.WS_DIR, 'responses'))\n" if make_responses
                 else "os.makedirs(proxy.WS_DIR)\n")
        script = (
            PROXY_IMPORT
            + f"{setup}"
            "proxy.pump_server_to_client(sys.stdin)\n"
            "sys.stderr.write(proxy.WS_DIR)\n"
        )
        env = {**os.environ, "DOTRUSH_PROXY_LOG": "", "DOTRUSH_DATA_DIR": str(self.root), "DOTRUSH_SESSION_ID": "route"}
        result = subprocess.run([sys.executable, "-c", script], input=stream, env=env, capture_output=True, check=True)
        return result.stdout, Path(result.stderr.decode())

    def test_proxy_routes_dotrush_cc_responses_to_files_and_forwards_everything_else_verbatim(self):
        uuid = "0f8fad5b-d9cb-469f-a165-70867728950e"
        ours = {"jsonrpc": "2.0", "id": f"dotrush-cc:{uuid}", "result": {"contents": "class Greeter"}}
        forwarded = b"".join([
            lsp_frame({"jsonrpc": "2.0", "id": 3, "result": None}),
            lsp_frame({"jsonrpc": "2.0", "id": "other:1", "result": None}),
            lsp_frame({"jsonrpc": "2.0", "id": 4, "result": {"text": f"dotrush-cc:{uuid}"}}),
            b'Content-Length: 20\r\n\r\n{"id":"dotrush-cc:x"',
        ])
        body = json.dumps(ours).encode("utf-8")

        stdout, ws = self.pump(lsp_frame(ours) + forwarded)

        self.assertEqual(stdout, forwarded)
        self.assertEqual(os.listdir(ws / "responses"), [f"{uuid}.json"])
        self.assertEqual((ws / "responses" / f"{uuid}.json").read_bytes(), body)

    def test_proxy_drops_dotrush_cc_responses_whose_id_is_not_a_uuid(self):
        stream = b"".join(lsp_frame({"jsonrpc": "2.0", "id": f"dotrush-cc:{suffix}", "result": 1}) for suffix in ("../x", ""))

        stdout, ws = self.pump(stream)

        self.assertEqual(stdout, b"")
        self.assertEqual(os.listdir(ws / "responses"), [])
        self.assertEqual(sorted(p.relative_to(self.root).as_posix() for p in self.root.rglob("*")),
                         ["ws", ws.relative_to(self.root).as_posix(), ws.relative_to(self.root).as_posix() + "/responses"])

    def test_a_response_is_withheld_even_when_it_cannot_be_written(self):
        # responses/ is the channel's capability marker, created once at proxy start. A missing one is not
        # recreated here: the write fails and is logged, and the response is still kept from Claude Code
        # rather than forwarded to it, where it would answer a request Claude Code never sent.
        uuid = "0f8fad5b-d9cb-469f-a165-70867728950e"
        frame = lsp_frame({"jsonrpc": "2.0", "id": f"dotrush-cc:{uuid}", "result": 1})

        stdout, ws = self.pump(frame, make_responses=False)

        self.assertEqual(stdout, b"")
        self.assertFalse((ws / "responses").exists())

    def test_workspace_dir_setup_empties_responses_and_removes_saved_edits(self):
        import hashlib

        ws = self.root / "ws" / ("sess-" + hashlib.sha1(b"setup").hexdigest()[:12])
        (ws / "responses").mkdir(parents=True)
        (ws / "responses" / "stale.json").write_text("{}")
        (ws / "edits").mkdir()
        (ws / "edits" / "0123456789ab.json").write_text("{}")
        script = (
            PROXY_IMPORT
            + "proxy.ensure_workspace_dir()\n"
        )
        env = {**os.environ, "DOTRUSH_PROXY_LOG": "", "DOTRUSH_DATA_DIR": str(self.root), "DOTRUSH_SESSION_ID": "setup"}
        subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, check=True)

        self.assertTrue((ws / "responses").is_dir())
        self.assertEqual(os.listdir(ws / "responses"), [])
        self.assertFalse((ws / "edits").exists())

    def test_an_agterm_session_dir_is_keyed_on_the_launching_claude_process(self):
        # Every Claude process started from one agterm tab (background jobs too) inherits its
        # AGTERM_SESSION_ID, so the parent pid keeps their proxies apart.
        import hashlib

        script = PROXY_IMPORT + "proxy.ensure_workspace_dir()\nprint(proxy.WS_DIR)\n"
        env = {k: v for k, v in os.environ.items() if k != "DOTRUSH_SESSION_ID"}
        env.update({"DOTRUSH_PROXY_LOG": "", "DOTRUSH_DATA_DIR": str(self.root), "AGTERM_SESSION_ID": "tab"})
        # The shell is the proxy's parent; the trailing command stops it from exec-ing the proxy in its place.
        runs = [subprocess.run(["/bin/sh", "-c", 'echo $$; "$0" -c "$1"; exit $?', sys.executable, script],
                               env=env, capture_output=True, text=True, check=True).stdout.split()
                for _ in range(2)]

        dirs = []
        for parent, ws in runs:
            key = hashlib.sha1(f"tab:{parent}".encode()).hexdigest()[:12]
            self.assertEqual(ws, str(self.root / "ws" / f"sess-{key}"))
            self.assertEqual(Path(ws, "session.txt").read_text(), "tab\n")
            self.assertEqual(Path(ws, "claude-pid").read_text(), parent + "\n")
            dirs.append(ws)
        self.assertNotEqual(dirs[0], dirs[1])

    def test_an_explicit_session_id_is_used_without_the_parent_pid(self):
        import hashlib

        script = PROXY_IMPORT + "proxy.ensure_workspace_dir()\nprint(proxy.WS_DIR)\n"
        env = {**os.environ, "DOTRUSH_PROXY_LOG": "", "DOTRUSH_DATA_DIR": str(self.root),
               "DOTRUSH_SESSION_ID": "explicit", "AGTERM_SESSION_ID": "tab"}
        ws = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, check=True).stdout.strip()

        self.assertEqual(ws, str(self.root / "ws" / ("sess-" + hashlib.sha1(b"explicit").hexdigest()[:12])))
        self.assertFalse(Path(ws, "claude-pid").exists())

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
               "DOTRUSH_CLI_DIR": str(self.cli_dir),
               "DOTRUSH_DIAGNOSTICS_QUIET": "0", "DOTRUSH_DIAGNOSTICS_TIMEOUT": "10", **env}
        return subprocess.run(["bash", str(DIAGNOSTICS_SH), *args], capture_output=True, text=True, env=env, timeout=30)

    def run_solution_with_fake_proxy(self, ws):
        """Runs `solution` while a thread plays the proxy: it reads the injected line and publishes one error."""
        import threading

        injected = []

        def fake_proxy():
            with open(ws / "inject.fifo") as fifo:
                injected.append(fifo.readline().strip())
            self.write_store(ws / "diagnostics.json", {(self.root / "A.cs").as_uri(): [diagnostic(4, 8, 1, "CS0029", "bad")]},
                             publishes=3, updated=time.time())

        # A daemon, so a driver that never writes to the FIFO fails the test instead of hanging the run.
        reader = threading.Thread(target=fake_proxy, daemon=True)
        reader.start()
        result = self.run_driver("solution")
        reader.join(5)
        return result, injected

    def test_solution_injects_the_request_and_reports_what_the_server_publishes_next(self):
        ws = self.session(os.getpid())

        result, injected = self.run_solution_with_fake_proxy(ws)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([json.loads(line) for line in injected], [{"method": "dotrush/solutionDiagnostics", "params": {}}])
        self.assertIn("A.cs:5:9  error  CS0029  bad", result.stdout)

    def test_solution_works_against_a_proxy_that_predates_the_request_channel(self):
        # A 0.6.x proxy has no responses/; diagnostics never needed it.
        ws = self.session(os.getpid())
        self.assertFalse((ws / "responses").exists())

        result, injected = self.run_solution_with_fake_proxy(ws)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(injected), 1)
        self.assertIn("A.cs:5:9  error  CS0029  bad", result.stdout)

    def test_report_reads_the_last_results_after_the_proxy_is_gone(self):
        ws = self.session(dead_pid())
        (ws / "load-completed").unlink()
        self.write_store(ws / "diagnostics.json", {(self.root / "B.cs").as_uri(): [diagnostic(1, 2, 2, "CS0219", "unused")]})

        result = self.run_driver("report")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("B.cs:2:3  warning  CS0219  unused", result.stdout)

    def test_where_prints_the_session_state_and_whether_the_request_channel_is_available(self):
        ws = self.session(os.getpid())

        older = self.run_driver("where")
        self.assertEqual(older.returncode, 0, older.stderr)
        self.assertEqual(older.stdout.splitlines(), [
            f"dir: {ws}",
            f"workspace: {self.root}",
            f"proxy: running (pid {os.getpid()})",
            "load: completed",
            "target: {}",
            "publishes: 2",
            "channel: unavailable (older proxy)",
        ])

        (ws / "responses").mkdir()
        self.assertEqual(self.run_driver("where").stdout.splitlines()[-1], "channel: available")

    def test_lookup_never_takes_another_sessions_dir_for_the_same_workspace(self):
        self.session(os.getpid())
        other = {"DOTRUSH_SESSION_ID": "other-session", "CLAUDE_PROJECT_DIR": str(self.root)}

        result = self.run_driver("where", **other)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("no DotRush language server has started", result.stderr)

        # A proxy started without a session id is still found by its workspace.
        shared = self.root / "data/ws/0123456789ab"
        shared.mkdir()
        (shared / "workspace.txt").write_text(f"{self.root}\n")
        found = self.run_driver("where", **other)
        self.assertEqual(found.returncode, 0, found.stderr)
        self.assertEqual(found.stdout.splitlines()[0], f"dir: {shared}")

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


class InjectorTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root, True)
        self.fifo = self.root / "inject.fifo"
        os.mkfifo(self.fifo)

    def run_proxy_script(self, body):
        script = (
            PROXY_IMPORT
            + f"path = {str(self.fifo)!r}\n"
            + body
        )
        env = {**os.environ, "DOTRUSH_PROXY_LOG": "", "DOTRUSH_DATA_DIR": str(self.root),
               "DOTRUSH_SESSION_ID": "injector", "DOTRUSH_INJECT_FIFO": str(self.fifo)}
        result = subprocess.run([sys.executable, "-c", script], env=env, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.split()

    def test_the_fifo_reader_never_sees_end_of_file_when_a_writer_leaves(self):
        # A reader that reaches end of file closes and reopens, and lines a writer puts into the pipe between
        # that end of file and the close are freed with the pipe, with no error to the writer. The injector
        # holds a write end of its own, so the last writer leaving is not an end of file.
        out = self.run_proxy_script(
            "fifo, held = proxy.open_fifo_for_reading(path)\n"
            "w = os.open(path, os.O_WRONLY); os.write(w, b'{\"method\":\"a\"}\\n'); os.close(w)\n"
            "print(fifo.readline().strip().replace(' ', ''))\n"
            "readable, _, _ = select.select([fifo], [], [], 0.3)\n"
            "print('eof' if readable else 'waiting')\n"
            "w = os.open(path, os.O_WRONLY | os.O_NONBLOCK); os.write(w, b'{\"method\":\"b\"}\\n'); os.close(w)\n"
            "print(fifo.readline().strip().replace(' ', ''))\n"
        )

        self.assertEqual(out, ['{"method":"a"}', "waiting", '{"method":"b"}'])

    def test_the_injector_frames_every_line_from_writers_that_come_and_go(self):
        out = self.run_proxy_script(
            "class Sink:\n"
            "    def __init__(self): self.data = bytearray()\n"
            "    def write(self, b): self.data += b\n"
            "    def flush(self): pass\n"
            "sink = Sink()\n"
            "threading.Thread(target=proxy.injector, args=(sink,), daemon=True).start()\n"
            "for n in range(50):\n"
            "    w = os.open(path, os.O_WRONLY)\n"
            "    os.write(w, ('{\"method\":\"m%d\"}\\n' % n).encode())\n"
            "    os.close(w)\n"
            "deadline = time.time() + 10\n"
            "while sink.data.count(b'Content-Length') < 50 and time.time() < deadline:\n"
            "    time.sleep(0.01)\n"
            "print(sink.data.count(b'Content-Length'))\n"
            "print(all(('\"m%d\"' % n).encode() in sink.data for n in range(50)))\n"
        )

        self.assertEqual(out, ["50", "True"])

    def test_a_deeply_nested_line_is_skipped_and_later_lines_still_go_through(self):
        # Python 3.9's json.loads raises RecursionError on deep nesting (3.14 parses it), so fake that here.
        out = self.run_proxy_script(
            "import io\n"
            "real_loads = proxy.json.loads\n"
            "def loads(s):\n"
            "    if s.startswith('[['): raise RecursionError('maximum recursion depth exceeded')\n"
            "    return real_loads(s)\n"
            "proxy.json.loads = loads\n"
            "class Sink:\n"
            "    def __init__(self): self.data = bytearray()\n"
            "    def write(self, b): self.data += b\n"
            "    def flush(self): pass\n"
            "sink = Sink()\n"
            "lines = '[[1]]\\n' + '{\"method\":\"after\"}\\n'\n"
            "print(proxy.inject_lines(io.StringIO(lines), sink))\n"
            "print(b'\"after\"' in sink.data)\n"
        )

        self.assertEqual(out, ["True", "True"])


if __name__ == "__main__":
    unittest.main()
