import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMPARE = ROOT / "plugins/dotrush/scripts/compare-heapstats.py"
SUMMARIZE = ROOT / "plugins/dotrush/scripts/summarize-speedscope.py"
PROFILE_SH = ROOT / "plugins/dotrush/scripts/dotrush-profile.sh"


def run_compare(baseline_text, current_text, limit="10"):
    with tempfile.TemporaryDirectory() as directory:
        directory = Path(directory)
        baseline = directory / "baseline.txt"
        current = directory / "current.txt"
        baseline.write_text(baseline_text, encoding="utf-8")
        current.write_text(current_text, encoding="utf-8")
        return subprocess.run(
            [sys.executable, str(COMPARE), str(baseline), str(current), "--limit", limit],
            check=True,
            capture_output=True,
            text=True,
        ).stdout


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


class HeapStatsTests(unittest.TestCase):
    def test_multiplies_object_size_by_count_and_merges_size_buckets(self):
        baseline = """        1,024  GC Heap bytes
           10  GC Heap objects

   Object Bytes     Count  Type
             24         4  System.Byte[]  [System.Private.CoreLib.dll]
"""
        current = """      104,920  GC Heap bytes
           12  GC Heap objects

   Object Bytes     Count  Type
         51,224         2  System.Byte[] (Bytes > 10K)  [System.Private.CoreLib.dll]
             24         1  System.Byte[]  [System.Private.CoreLib.dll]
             32         2  LeakedPayload  [ProfileTarget.dll]
"""
        result = run_compare(baseline, current)

        self.assertIn("HeapBytes\t1024\t104920\t+103896", result)
        # System.Byte[] merges the bucketed and unbucketed rows: 4 -> 3 objects.
        self.assertIn("-1\t4\t3\t51224\tSystem.Byte[]", result)
        self.assertIn("+2\t0\t2\t32\tLeakedPayload", result)
        # The fabricated retained-bytes column is gone.
        self.assertNotIn("DeltaBytes", result)

    def test_merges_size_buckets_for_types_with_no_module_name(self):
        # dotnet-gcdump omits the trailing [module] bracket when ModuleName is empty, which is
        # what PerfView produces for CCWs and `UNKNOWN 0x...` types. Both rows must merge into
        # one, or a leak crossing a bucket boundary splits into two opposing deltas.
        baseline = """        1,024  GC Heap bytes
           10  GC Heap objects

   Object Bytes     Count  Type
             24         4  UNKNOWN 0x7f0102030405
"""
        current = """      104,920  GC Heap bytes
           12  GC Heap objects

   Object Bytes     Count  Type
         51,224         2  UNKNOWN 0x7f0102030405 (Bytes > 10K)
             24         1  UNKNOWN 0x7f0102030405
"""
        result = run_compare(baseline, current)

        self.assertIn("-1\t4\t3\t51224\tUNKNOWN 0x7f0102030405", result)
        self.assertNotIn("(Bytes > 10K)", result)

    def test_ranks_a_growing_type_by_count_even_when_its_sampled_size_shrank(self):
        # gcdump prints one size per type, taken from the first object PerfView stored. Ranking
        # on size*count let a bucket whose sample fell from 9000 to 1100 read as a large negative
        # delta while it actually held more objects, burying the type that was growing.
        baseline = """      100,000  GC Heap bytes
           10  GC Heap objects

   Object Bytes     Count  Type
          9,000         2  System.Byte[] (Bytes > 1K)  [System.Private.CoreLib.dll]
"""
        current = """      900,000  GC Heap bytes
           40  GC Heap objects

   Object Bytes     Count  Type
          1,100        30  System.Byte[] (Bytes > 1K)  [System.Private.CoreLib.dll]
"""
        result = run_compare(baseline, current)

        self.assertIn("+28\t2\t30\t1100\tSystem.Byte[]", result)
        self.assertNotIn("-", result.split("SampleObjectBytes")[1].split("\n")[1])

    def test_reads_counts_grouped_by_a_non_comma_separator(self):
        # `dotnet-gcdump report` formats with the current culture's N0: de-DE groups on ".",
        # de-CH on "'", fr-FR on a narrow no-break space.
        for separator in (".", "'", "\u00a0", "\u202f", "\u2009",
                          "\u066c", "\u060c", "\u2e41", "\u12c8"):
            with self.subTest(separator=separator):
                baseline = f"""        1{separator}024  GC Heap bytes
           10  GC Heap objects

   Object Bytes     Count  Type
             24         4  System.Byte[]  [System.Private.CoreLib.dll]
"""
                current = f"""      104{separator}920  GC Heap bytes
           12  GC Heap objects

   Object Bytes     Count  Type
         51{separator}224         2  System.Byte[]  [System.Private.CoreLib.dll]
"""
                result = run_compare(baseline, current)

                self.assertIn("HeapBytes\t1024\t104920\t+103896", result)
                self.assertIn("-2\t4\t2\t51224\tSystem.Byte[]", result)


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


if __name__ == "__main__":
    unittest.main()
