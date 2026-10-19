import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import audio_priority as priority


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class Process:
    def __init__(self):
        self._handle = (1 << 40) + 123
        self.returncode = None

    def poll(self):
        return self.returncode


class NativeFunction:
    def __init__(self, callback):
        self.callback = callback
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        return self.callback(*args)


class AudioPriorityPolicyTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "decoder.progress"
        self.clock = Clock()
        self.logs = []
        self.process = Process()
        self.current_priority = priority.BELOW_NORMAL_PRIORITY_CLASS
        self.writes = []
        self.addCleanup(patch.stopall)
        patch.object(priority, "IS_WINDOWS", True).start()
        patch.object(priority, "_priority_class", side_effect=lambda process: self.current_priority).start()
        patch.object(priority, "_normal_priority", side_effect=self.promote).start()
        self.policy = priority.AudioPriorityPolicy(180, 180, self.logs.append, clock=self.clock)

    def promote(self, process):
        self.writes.append(process)
        self.current_priority = priority.NORMAL_PRIORITY_CLASS
        return True

    def start(self, seconds=3907.26, *, future=0, policy=None, path=True):
        result = (policy or self.policy).monitor(self.path if path else None, seconds, future)
        result.started(self.process)
        return result

    def report(self, monitor, wall, media, *, status="continue", raw=None):
        self.clock.now = wall
        with self.path.open("ab") as stream:
            stream.write((f"out_time_us={raw if raw is not None else int(media * 1e6)}\n"
                          f"progress={status}\n").encode())
        monitor.poll(self.process)

    def stream(self, monitor, start, end, rate):
        for wall in range(start, end + 1, 2):
            self.report(monitor, wall, wall * rate)

    def test_sustained_budget_risk_promotes_only_current_process_once(self):
        monitor = self.start()
        self.stream(monitor, 2, 50, 10)
        self.assertEqual(self.writes, [self.process])
        self.assertTrue(self.policy.promoted)
        self.assertEqual(self.policy.deadline, 180)
        details = self.policy.snapshot()["promotion"]
        self.assertGreaterEqual(details["elapsed_seconds"], 30)
        self.assertGreater(details["estimated_remaining_seconds"], details["remaining_seconds"])
        self.assertEqual(details["priority_after"], 32)
        self.assertEqual(len([line for line in self.logs if "临近预算风险" in line]), 1)
        json.dumps(self.policy.snapshot())

    def test_fast_complete_preparation_does_not_steal_translation_priority(self):
        monitor = self.start()
        self.stream(monitor, 2, 120, 30)
        self.report(monitor, 128, 3907.26, status="end")
        self.assertEqual(self.writes, [])
        self.assertFalse(self.policy.promoted)

    def test_no_boost_before_thirty_seconds_even_with_extreme_eta(self):
        monitor = self.start()
        self.stream(monitor, 2, 28, 1)
        self.assertEqual(self.writes, [])
        self.stream(monitor, 30, 34, 1)
        self.assertEqual(self.writes, [self.process])

    def test_future_segment_work_is_part_of_remaining_estimate(self):
        monitor = self.start(1000, future=1000)
        self.stream(monitor, 2, 34, 10)
        self.assertTrue(self.policy.promoted)
        self.assertEqual(self.policy.snapshot()["promotion"]["future_media_seconds"], 1000)

    def test_same_current_segment_without_future_does_not_need_rescue(self):
        monitor = self.start(1000)
        self.stream(monitor, 2, 34, 10)
        self.assertEqual(self.writes, [])

    def test_recent_recovery_from_startup_seek_prevents_false_boost(self):
        monitor = self.start(3907.26)
        for wall in range(2, 22, 2):
            self.report(monitor, wall, wall / 2)
        for wall in range(22, 52, 2):
            self.report(monitor, wall, 10 + (wall - 20) * 40)
        self.assertEqual(self.writes, [])

    def test_fast_first_minute_does_not_hide_sustained_late_contention(self):
        monitor = self.start()
        for wall in range(2, 62, 2):
            self.report(monitor, wall, wall * 50)
        self.assertEqual(self.writes, [])
        for wall in range(62, 82, 2):
            self.report(monitor, wall, 3000 + (wall - 60) * .5)
        self.assertTrue(self.policy.promoted)
        # The stale overall average would delay rescue until about 130 seconds.
        self.assertLess(self.policy.snapshot()["promotion"]["elapsed_seconds"], 90)

    def test_brief_late_slowdown_then_recovery_does_not_promote(self):
        monitor = self.start()
        for wall in range(2, 62, 2):
            self.report(monitor, wall, wall * 50)
        for wall in range(62, 72, 2):
            self.report(monitor, wall, 3000 + (wall - 60) * .5)
        for wall in range(72, 82, 2):
            self.report(monitor, wall, 3005 + (wall - 70) * 50)
        self.assertEqual(self.writes, [])

    def test_recorded_complete_139_second_preparation_keeps_background_priority(self):
        # Full FFmpeg progress trace from the successful 2026-10-18 BELOW
        # 3907.26-second evidence decode (127.27s decode + 12s VAD).
        points = [
            (2.922, 58.065167), (4.938, 146.420167), (6.954, 218.387667),
            (8.969, 287.556833), (10.985, 370.390167), (13.0, 437.056833),
            (15.016, 502.9385), (17.032, 576.346), (19.047, 649.054333),
            (21.063, 706.174333), (23.079, 784.761), (25.094, 837.909333),
            (27.11, 897.521), (29.125, 967.525167), (31.141, 1035.784333),
            (33.157, 1092.330167), (35.172, 1167.152667), (37.188, 1237.132667),
            (39.204, 1309.667667), (41.235, 1352.464333), (43.25, 1421.240167),
            (45.266, 1480.607667), (47.282, 1528.991833), (49.297, 1594.274333),
            (51.313, 1665.251833), (53.329, 1729.4585), (55.344, 1796.846833),
            (57.375, 1854.812667), (59.391, 1930.119333), (61.407, 1994.022667),
            (63.422, 2040.617667), (65.438, 2116.689333), (67.454, 2194.361),
            (69.469, 2261.036), (71.485, 2303.476833), (73.5, 2369.119333),
            (75.516, 2410.200167), (77.532, 2468.2985), (79.547, 2559.870167),
            (81.563, 2630.541833), (83.579, 2690.4035), (85.61, 2723.956),
            (87.625, 2796.645167), (89.641, 2838.589333), (91.657, 2922.700167),
            (93.672, 2961.411), (95.688, 3041.9185), (97.704, 3109.2185),
            (99.719, 3138.381), (101.735, 3208.6685), (103.75, 3289.065167),
            (105.766, 3327.576), (107.782, 3393.080167), (109.797, 3464.241),
            (111.829, 3495.081), (113.844, 3541.685167), (115.86, 3609.554333),
            (117.891, 3655.016), (119.907, 3721.496), (121.922, 3783.276833),
            (123.938, 3840.227667), (125.954, 3885.244333), (126.704, 3907.26),
        ]
        monitor = self.start()
        for wall, media in points:
            self.report(monitor, wall, media)
        self.assertEqual(self.writes, [])

    def test_recorded_complete_136_second_preparation_does_not_promote_on_single_dip(self):
        # Unique media updates observed during the later successful natural
        # workload. Arrival times have <= the observer's sampling uncertainty;
        # this is a regression fixture, not another live timing measurement.
        points = [
            (3.887, 89.405167), (4.949, 171.646833), (7.09, 242.055167),
            (9.23, 313.056833), (11.371, 399.4735), (13.512, 467.477667),
            (15.652, 547.196833), (17.793, 627.9585), (19.965, 715.620167),
            (22.09, 764.337667), (23.168, 832.395167), (25.277, 874.635167),
            (27.418, 934.481), (29.574, 1014.874333), (31.715, 1065.532667),
            (34.121, 1101.509333), (36.262, 1157.199333), (37.355, 1174.1485),
            (40.496, 1224.914333), (41.574, 1300.607667), (43.699, 1353.750167),
            (45.824, 1418.447667), (47.98, 1498.580167), (50.105, 1558.339333),
            (52.23, 1633.650167), (54.371, 1704.321833), (56.512, 1777.767667),
            (58.652, 1840.016833), (59.715, 1916.840167), (61.855, 1983.186833),
            (63.996, 2057.804333), (66.137, 2083.596), (68.324, 2149.4085),
            (70.465, 2205.241), (72.605, 2285.146833), (74.746, 2333.800167),
            (75.809, 2392.586833), (77.949, 2457.256), (80.09, 2534.331833),
            (82.23, 2596.057667), (84.371, 2674.055167), (86.496, 2738.854333),
            (88.668, 2796.302667), (90.809, 2859.666833), (91.887, 2936.637667),
            (94.027, 2992.546833), (96.168, 3065.4785), (98.309, 3113.332667),
            (100.512, 3136.016833), (102.699, 3155.0935), (104.84, 3193.536833),
            (107.012, 3249.751833), (108.09, 3315.824333), (110.215, 3388.729333),
            (112.355, 3460.070167), (114.496, 3530.266), (116.637, 3598.261833),
            (118.777, 3661.271), (120.918, 3732.427667), (123.074, 3805.251833),
            (124.152, 3869.699333), (125.699, 3907.26),
        ]
        self.clock.now = .65249
        monitor = self.start()
        for wall, media in points:
            self.report(monitor, wall, media)
        self.assertEqual(self.writes, [])

    def test_three_consecutive_risky_fresh_reports_are_required(self):
        monitor = self.start()
        self.stream(monitor, 2, 28, 10)
        self.report(monitor, 30, 300)
        self.report(monitor, 32, 320)
        self.assertEqual(self.writes, [])
        self.report(monitor, 34, 340)
        self.assertEqual(self.writes, [self.process])

    def test_stalled_reports_do_not_count_as_evidence(self):
        monitor = self.start()
        self.stream(monitor, 2, 28, 10)
        self.report(monitor, 30, 300)
        self.report(monitor, 32, 300)
        self.report(monitor, 34, 300)
        self.report(monitor, 36, 300)
        self.assertEqual(self.writes, [])

    def test_missing_progress_or_no_media_advance_does_not_boost(self):
        monitor = self.start()
        for wall in range(2, 178, 2):
            self.clock.now = wall
            monitor.poll(self.process)
        self.assertEqual(self.writes, [])
        for wall in range(178, 184, 2):
            self.report(monitor, wall, 0)
        self.assertEqual(self.writes, [])

    def test_backward_media_progress_disables_that_process_monitor(self):
        monitor = self.start()
        self.stream(monitor, 2, 28, 10)
        self.report(monitor, 30, 100)
        self.stream(monitor, 32, 80, 10)
        self.assertEqual(self.writes, [])

    def test_malformed_report_interrupts_risk_streak(self):
        monitor = self.start()
        self.stream(monitor, 2, 30, 10)
        self.report(monitor, 32, 0, raw="N/A")
        self.report(monitor, 34, 340)
        self.report(monitor, 36, 360)
        self.assertEqual(self.writes, [])
        self.report(monitor, 38, 380)
        self.assertEqual(self.writes, [self.process])

    def test_observer_gap_rebuilds_a_fresh_window(self):
        monitor = self.start()
        self.stream(monitor, 2, 30, 10)
        self.report(monitor, 60, 600)
        self.report(monitor, 62, 620)
        self.report(monitor, 64, 640)
        self.assertEqual(self.writes, [])

    def test_queued_old_reports_cannot_supply_three_live_risk_reports(self):
        monitor = self.start()
        self.clock.now = 50
        self.path.write_text("".join(f"out_time_us={i * 1000000}\nprogress=continue\n"
                                     for i in range(1, 100)), encoding="ascii")
        monitor.poll(self.process)
        self.assertEqual(self.writes, [])

    def test_partial_records_need_a_complete_progress_block(self):
        monitor = self.start()
        self.stream(monitor, 2, 30, 10)
        self.clock.now = 32
        with self.path.open("ab") as stream:
            stream.write(b"out_time_us=320000000\nprogress=con")
        monitor.poll(self.process)
        self.assertEqual(self.writes, [])
        self.clock.now = 34
        with self.path.open("ab") as stream:
            stream.write(b"tinue\n")
        monitor.poll(self.process)
        self.assertEqual(self.writes, [])
        self.report(monitor, 36, 360)
        self.assertEqual(self.writes, [self.process])

    def test_oversize_or_truncated_file_cannot_trigger_boost(self):
        monitor = self.start()
        self.stream(monitor, 2, 30, 10)
        self.clock.now = 32
        self.path.write_bytes(b"x" * 100000)
        monitor.poll(self.process)
        self.assertEqual(self.writes, [])
        self.path.write_bytes(b"out_time_us=340000000\nprogress=continue\n")
        self.clock.now = 34
        monitor.poll(self.process)
        self.stream(monitor, 36, 80, 10)
        self.assertEqual(self.writes, [])

    def test_deadline_and_completed_process_always_win_over_promotion(self):
        monitor = self.start()
        self.stream(monitor, 2, 28, 10)
        self.report(monitor, 180, 300)
        self.assertEqual(self.writes, [])
        self.process.returncode = 0
        self.report(monitor, 181, 310)
        self.assertEqual(self.writes, [])

    def test_monitor_never_changes_an_unrelated_process(self):
        monitor = self.start()
        unrelated = Process()
        for wall in range(2, 70, 2):
            self.clock.now = wall
            self.path.write_text(f"out_time_us={wall * 1000000}\nprogress=continue\n")
            monitor.poll(unrelated)
        self.assertEqual(self.writes, [])

    def test_non_below_process_and_external_priority_changes_are_respected(self):
        self.current_priority = priority.NORMAL_PRIORITY_CLASS
        monitor = self.start()
        self.stream(monitor, 2, 50, 10)
        self.assertEqual(self.writes, [])
        self.current_priority = priority.BELOW_NORMAL_PRIORITY_CLASS
        monitor2 = self.policy.monitor(self.path, 3907.26)
        monitor2.started(self.process)
        self.path.write_bytes(b"")
        self.current_priority = 0x8000
        for wall in range(52, 100, 2):
            self.report(monitor2, wall, wall * 10)
        self.assertEqual(self.writes, [])

    def test_native_failure_is_nonfatal_and_not_retried(self):
        patch.object(priority, "_normal_priority", return_value=False).start()
        monitor = self.start()
        self.stream(monitor, 2, 80, 10)
        self.assertFalse(self.policy.promoted)
        self.assertTrue(self.policy.snapshot()["attempted"])
        self.assertFalse(self.policy.snapshot()["promotion"]["success"])
        self.assertEqual(priority._normal_priority.call_count, 1)

    def test_vad_without_media_evidence_only_inherits_existing_promotion(self):
        monitor = self.start(path=False)
        self.clock.now = 160
        monitor.poll(self.process)
        default = 0x08000000 | priority.BELOW_NORMAL_PRIORITY_CLASS
        self.assertEqual(monitor.creation_flags(default), default)
        self.assertEqual(self.writes, [])
        inherited = priority.AudioPriorityPolicy(180, 20, self.logs.append,
                                                 initial_promoted=True, clock=self.clock)
        vad = inherited.monitor(None, 0)
        self.assertFalse(inherited.eligible)
        self.assertEqual(vad.creation_flags(default), 0x08000000 | priority.NORMAL_PRIORITY_CLASS)
        self.assertTrue(inherited.promoted)

    def test_explicit_short_budget_and_non_windows_never_gain_eligibility(self):
        short = priority.AudioPriorityPolicy(50, 50, self.logs.append, clock=self.clock)
        monitor = self.start(policy=short)
        self.stream(monitor, 2, 48, 1)
        self.assertEqual(self.writes, [])
        patch.object(priority, "IS_WINDOWS", False).start()
        self.assertFalse(self.policy.eligible)
        self.assertEqual(self.policy.monitor(None, 0).creation_flags(0), 0)

    def test_log_failure_cannot_fail_usable_audio_work(self):
        policy = priority.AudioPriorityPolicy(180, 180, Mock(side_effect=RuntimeError("logger")),
                                              clock=self.clock)
        monitor = self.start(policy=policy)
        self.stream(monitor, 2, 50, 10)
        self.assertTrue(policy.promoted)

    def test_finished_monitor_does_not_raise_priority(self):
        monitor = self.start()
        self.stream(monitor, 2, 30, 10)
        monitor.finished(self.process)
        self.stream(monitor, 32, 80, 10)
        self.assertEqual(self.writes, [])


class NativePriorityHandleTests(unittest.TestCase):
    def test_uses_full_width_borrowed_handle_and_no_pid_lookup(self):
        get = NativeFunction(lambda handle: priority.BELOW_NORMAL_PRIORITY_CLASS)
        set_ = NativeFunction(lambda handle, value: value == priority.NORMAL_PRIORITY_CLASS)
        native = SimpleNamespace(GetPriorityClass=get, SetPriorityClass=set_)
        process = Process()
        with patch.object(priority, "IS_WINDOWS", True), patch.object(priority.ctypes, "WinDLL", return_value=native, create=True):
            self.assertEqual(priority._priority_class(process), priority.BELOW_NORMAL_PRIORITY_CLASS)
            self.assertTrue(priority._normal_priority(process))
        self.assertEqual(get.calls[0][0].value, (1 << 40) + 123)
        self.assertEqual(set_.calls[0][0].value, (1 << 40) + 123)
        self.assertEqual(process._handle, (1 << 40) + 123)

    def test_missing_handle_and_denied_native_calls_are_safe(self):
        native = SimpleNamespace(GetPriorityClass=NativeFunction(lambda handle: 0),
                                 SetPriorityClass=NativeFunction(lambda handle, value: 0))
        with patch.object(priority, "IS_WINDOWS", True), patch.object(priority.ctypes, "WinDLL", return_value=native, create=True):
            self.assertIsNone(priority._priority_class(Process()))
            self.assertFalse(priority._normal_priority(Process()))
            self.assertIsNone(priority._priority_class(SimpleNamespace(pid=123)))
            self.assertFalse(priority._normal_priority(SimpleNamespace(pid=123)))


if __name__ == "__main__":
    unittest.main()
