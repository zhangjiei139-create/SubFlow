from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import subtitle_tool_core as core


def media(video_tag="00:10:00.000000000", *, container_seconds=600, other_seconds=600):
    props = {"num_index_entries": 100}
    if video_tag is not None:
        props["tag_duration"] = video_tag
    return {
        "container": {"recognized": True, "properties": {"duration": container_seconds * 10**9}},
        "tracks": [
            {"id": 0, "type": "video", "codec": "HEVC", "properties": props},
            {"id": 1, "type": "audio", "codec": "AAC", "properties": {"tag_duration": f"00:{other_seconds // 60:02}:00.000"}},
            {"id": 2, "type": "subtitles", "properties": {"tag_duration": f"00:{other_seconds // 60:02}:00.000"}},
        ],
    }


def probe_result(stream):
    return subprocess.CompletedProcess([], 0, json.dumps({"streams": [stream]}).encode("utf-8"), b"")


def packets_result(packets):
    return subprocess.CompletedProcess([], 0, json.dumps({"packets": packets}).encode("utf-8"), b"")


class OutputVideoDurationFallbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = self.root / "movie.mkv"
        self.source.write_bytes(b"source")
        self.output = self.root / "movie.partial.mkv"
        self.output.write_bytes(b"o" * (1024 * 1024))
        self.ffmpeg = self.root / "ffmpeg.exe"
        self.ffprobe = self.root / ("ffprobe.exe" if core.os.name == "nt" else "ffprobe")
        self.ffprobe.write_bytes(b"mock tool")
        self.addCleanup(patch.stopall)
        patch.object(core, "FFMPEG", str(self.ffmpeg)).start()
        self.messages = []

    def validate(self, source_media, output_media, *, source_path=True, cancel_event=None):
        with patch.object(core, "inspect_media", return_value=output_media):
            return core.validate_media_output(
                self.output, source_media, 1, 1, log=self.messages.append,
                input_path=self.source if source_path else None, cancel_event=cancel_event,
            )

    def test_known_primary_tags_need_no_probe(self):
        with patch.object(core, "run_command") as run:
            result = self.validate(media(), media())
        run.assert_not_called()
        self.assertTrue(result["video_duration_verified"])
        self.assertIn("已与源片比对", self.messages[-1])

    def test_missing_source_tag_uses_only_primary_stream_metadata(self):
        with patch.object(core, "run_command", return_value=probe_result({"duration": "600.000000000"})) as run:
            result = self.validate(media(None), media())
        args = run.call_args.args[0]
        self.assertEqual(args[-1], str(self.source))
        self.assertEqual(args[args.index("-select_streams") + 1], "v:0")
        self.assertIn("-nofind_stream_info", args)
        self.assertNotIn("-show_packets", args)
        self.assertNotIn("-show_frames", args)
        self.assertNotIn("format=duration", args)
        self.assertTrue(result["video_duration_verified"])

    def test_missing_output_tag_is_probed_independently(self):
        with patch.object(core, "run_command", return_value=probe_result({"duration": "600.000"})) as run:
            result = self.validate(media(), media(None))
        self.assertEqual(run.call_args.args[0][-1], str(self.output))
        self.assertTrue(result["video_duration_verified"])

    def test_missing_both_tags_needs_two_independent_probes(self):
        with patch.object(core, "run_command", side_effect=[probe_result({"duration": "600"}), probe_result({"duration": "610"})]) as run:
            with self.assertRaisesRegex(RuntimeError, "主视频时长不一致"):
                self.validate(media(None), media(None))
        self.assertEqual(run.call_count, 2)

    def test_ffprobe_duration_tag_used_when_numeric_duration_is_na(self):
        with patch.object(core, "run_command", return_value=probe_result({"duration": "N/A", "tags": {"DURATION": "00:10:00.000000000"}})):
            result = self.validate(media(None), media())
        self.assertTrue(result["video_duration_verified"])

    def test_long_audio_and_subtitle_container_duration_is_never_compared(self):
        with patch.object(core, "run_command", return_value=probe_result({"duration": "600"})):
            result = self.validate(media(None, container_seconds=800, other_seconds=800), media(container_seconds=1200, other_seconds=1200))
        self.assertEqual(result["input_video_duration_ns"], 600 * 10**9)
        self.assertTrue(result["video_duration_verified"])

    def test_probe_ignores_format_duration_without_video_duration(self):
        payload = {"streams": [{}], "format": {"duration": "600"}}
        with patch.object(core, "run_command", return_value=subprocess.CompletedProcess([], 0, json.dumps(payload).encode(), b"")):
            result = self.validate(media(None), media())
        self.assertFalse(result["video_duration_verified"])
        self.assertEqual(result["input_video_duration_ns"], 0)
        self.assertIn("主视频时长未核验", self.messages[-1])
        self.assertNotIn("输出验证通过", self.messages[-1])

    def test_existing_file_name_can_supply_source_for_legacy_callers(self):
        source = media(None)
        source["file_name"] = str(self.source)
        with patch.object(core, "run_command", return_value=probe_result({"duration": "600"})) as run:
            result = self.validate(source, media(), source_path=False)
        self.assertEqual(run.call_args.args[0][-1], str(self.source))
        self.assertTrue(result["video_duration_verified"])

    def test_missing_source_path_marks_duration_unverified(self):
        with patch.object(core, "run_command") as run:
            result = self.validate(media(None), media(), source_path=False)
        run.assert_not_called()
        self.assertFalse(result["video_duration_verified"])
        self.assertTrue(any("没有可追溯" in line for line in self.messages))

    def test_unavailable_probe_does_not_claim_duration_verified(self):
        self.ffprobe.unlink()
        with patch.object(core, "run_command") as run:
            result = self.validate(media(None), media())
        run.assert_not_called()
        self.assertFalse(result["video_duration_verified"])

    def test_probe_timeout_is_not_user_cancellation(self):
        def timed_out(args, *, log, cancel_event):
            cancel_event.timed_out = True
            raise core.CancelledError("deadline")
        with patch.object(core, "run_command", side_effect=timed_out):
            result = self.validate(media(None), media())
        self.assertFalse(result["video_duration_verified"])
        self.assertTrue(any("5 秒单步上限" in line for line in self.messages))

    def test_user_cancel_while_probing_is_propagated(self):
        cancel = threading.Event()
        def user_cancel(args, *, log, cancel_event):
            cancel.set()
            raise core.CancelledError("stopped")
        with patch.object(core, "run_command", side_effect=user_cancel):
            with self.assertRaises(core.CancelledError):
                self.validate(media(None), media(), cancel_event=cancel)
        self.assertFalse(any("验证通过" in line for line in self.messages))

    def test_already_cancelled_never_starts_output_inspection(self):
        cancel = threading.Event()
        cancel.set()
        with patch.object(core, "inspect_media") as inspect:
            with self.assertRaises(core.CancelledError):
                core.validate_media_output(self.output, media(), 1, 1, cancel_event=cancel)
        inspect.assert_not_called()

    def test_probe_tool_error_or_corrupt_json_is_reported_without_false_pass(self):
        for failure in (RuntimeError("bad probe"), subprocess.CompletedProcess([], 0, b"not-json", b"")):
            with self.subTest(failure=failure):
                self.messages.clear()
                kwargs = {"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure}
                with patch.object(core, "run_command", **kwargs):
                    result = self.validate(media(None), media())
                self.assertFalse(result["video_duration_verified"])
                self.assertTrue(any("未取得可靠数据" in line for line in self.messages))

    def test_nonfinite_zero_negative_probe_duration_is_not_reliable(self):
        for value in ("NaN", "Infinity", "0", "-1"):
            with self.subTest(value=value), patch.object(core, "run_command", return_value=probe_result({"duration": value})):
                result = self.validate(media(None), media())
                self.assertFalse(result["video_duration_verified"])

    def test_primary_video_missing_tag_never_uses_second_video(self):
        source = media(None)
        source["tracks"].append({"type": "video", "properties": {"tag_duration": "00:10:00.000"}})
        self.assertEqual(core.video_track_duration_ns(source), 0)

    def test_tag_duration_parse_is_nanosecond_exact_and_rejects_invalid_time(self):
        self.assertEqual(core._duration_tag_ns("01:02:03.123456789"), 3723123456789)
        self.assertEqual(core._duration_tag_ns("00:60:00"), 0)
        self.assertEqual(core._duration_tag_ns("00:00:60"), 0)

    def test_track_counts_and_audio_specs_still_fail_before_duration_probing(self):
        output = media(None)
        output["tracks"] = output["tracks"][:-1]
        with patch.object(core, "run_command") as run:
            with self.assertRaisesRegex(RuntimeError, "字幕轨 0/1"):
                self.validate(media(None), output)
        run.assert_not_called()

    def test_tail_packets_can_fill_source_without_duration_metadata(self):
        source = media(None)
        source["tracks"][0]["properties"]["minimum_timestamp"] = 0
        results = [probe_result({}), packets_result([
            {"pts_time": "580.000", "duration_time": "0.040"},
            {"pts_time": "599.960", "duration_time": "0.040"},
        ])]
        with patch.object(core, "run_command", side_effect=results) as run:
            result = self.validate(source, media())
        self.assertTrue(result["video_duration_verified"])
        self.assertEqual(result["input_video_duration_ns"], 600 * 10**9)
        args = run.call_args.args[0]
        self.assertEqual(args[args.index("-read_intervals") + 1], "580.000000%")
        self.assertIn("-nofind_stream_info", args)
        self.assertNotIn("-show_frames", args)
        self.assertEqual(run.call_count, 2)

    def test_container_duration_is_only_a_tail_seek_hint(self):
        source = media(None, container_seconds=650, other_seconds=650)
        source["tracks"][0]["properties"]["minimum_timestamp"] = 0
        results = [probe_result({}), packets_result([{"pts_time": "599.960", "duration_time": "0.040"}])]
        with patch.object(core, "run_command", side_effect=results) as run:
            result = self.validate(source, media())
        self.assertEqual(run.call_args.args[0][run.call_args.args[0].index("-read_intervals") + 1], "630.000000%")
        self.assertEqual(result["input_video_duration_ns"], 600 * 10**9)
        self.assertTrue(result["video_duration_verified"])

    def test_tail_with_no_video_packet_does_not_claim_duration(self):
        source = media(None)
        source["tracks"][0]["properties"]["minimum_timestamp"] = 0
        with patch.object(core, "run_command", side_effect=[probe_result({}), packets_result([])]):
            result = self.validate(source, media())
        self.assertFalse(result["video_duration_verified"])
        self.assertEqual(result["input_video_duration_ns"], 0)

    def test_tail_requires_normal_eof_not_timed_out_partial_output(self):
        source = media(None)
        source["tracks"][0]["properties"]["minimum_timestamp"] = 0
        def run(args, *, log, cancel_event):
            if "-read_intervals" not in args:
                return probe_result({})
            cancel_event.timed_out = True
            return packets_result([{"pts_time": "599.960", "duration_time": "0.040"}])
        with patch.object(core, "run_command", side_effect=run):
            result = self.validate(source, media())
        self.assertFalse(result["video_duration_verified"])
        self.assertFalse(any("实际视频包起止补查" in line for line in self.messages))

    def test_tail_refuses_a_shortened_primary_video_even_with_same_container_hint(self):
        output = media(None)
        output["tracks"][0]["properties"]["minimum_timestamp"] = 0
        with patch.object(core, "run_command", side_effect=[probe_result({}), packets_result([{"pts_time": "589.960", "duration_time": "0.040"}])]):
            with self.assertRaisesRegex(RuntimeError, "输入 600.0 秒，输出 590.0 秒"):
                self.validate(media(), output)

    def test_tail_duration_subtracts_known_video_origin(self):
        source = media(None, container_seconds=601)
        source["tracks"][0]["properties"]["minimum_timestamp"] = 1_000_000_000
        with patch.object(core, "run_command", side_effect=[probe_result({}), packets_result([{"pts_time": "600.960", "duration_time": "0.040"}])]):
            result = self.validate(source, media())
        self.assertEqual(result["input_video_duration_ns"], 600 * 10**9)
        self.assertTrue(result["video_duration_verified"])

    def test_missing_origin_is_filled_with_only_five_head_packets(self):
        results = [probe_result({}), packets_result([{"pts_time": "1.000"}, {"pts_time": "1.040"}]),
                   packets_result([{"pts_time": "600.960", "duration_time": "0.040"}])]
        with patch.object(core, "run_command", side_effect=results) as run:
            result = self.validate(media(None, container_seconds=601), media())
        args = run.call_args_list[1].args[0]
        self.assertEqual(args[args.index("-read_intervals") + 1], "0%+#5")
        self.assertEqual(result["input_video_duration_ns"], 600 * 10**9)
        self.assertTrue(result["video_duration_verified"])

    def test_unavailable_origin_stops_before_tail_read(self):
        with patch.object(core, "run_command", side_effect=[probe_result({}), packets_result([])]) as run:
            result = self.validate(media(None), media())
        self.assertFalse(result["video_duration_verified"])
        self.assertEqual(run.call_count, 2)

    def test_default_video_frame_duration_fills_missing_packet_duration(self):
        source = media(None)
        source["tracks"][0]["properties"].update(minimum_timestamp=0, default_duration=40_000_000)
        with patch.object(core, "run_command", side_effect=[probe_result({}), packets_result([{"pts_time": "599.960"}])]):
            result = self.validate(source, media())
        self.assertTrue(result["video_duration_verified"])

    def test_unknown_last_video_packet_duration_is_not_guessed(self):
        source = media(None)
        source["tracks"][0]["properties"]["minimum_timestamp"] = 0
        with patch.object(core, "run_command", side_effect=[probe_result({}), packets_result([{"pts_time": "599.960"}])]):
            result = self.validate(source, media())
        self.assertFalse(result["video_duration_verified"])

    def test_short_container_without_a_tag_never_uses_whole_file_probe(self):
        source = media(None, container_seconds=30)
        source["tracks"][0]["properties"]["minimum_timestamp"] = 0
        with patch.object(core, "run_command", return_value=probe_result({})) as run:
            result = self.validate(source, media())
        self.assertFalse(result["video_duration_verified"])
        self.assertEqual(run.call_count, 1)

    def test_each_probe_honors_remaining_total_ten_second_budget(self):
        clock = [0.0]
        durations = []
        def run(args, *, log, cancel_event):
            durations.append(cancel_event.deadline - clock[0])
            if "-read_intervals" not in args:
                clock[0] = 4.0
                return probe_result({})
            if "0%+#5" in args:
                clock[0] = 8.0
                return packets_result([{"pts_time": "0.000"}])
            clock[0] = 9.0
            return packets_result([{"pts_time": "599.960", "duration_time": "0.040"}])
        with patch.object(core.time, "monotonic", side_effect=lambda: clock[0]), patch.object(core, "run_command", side_effect=run):
            result = self.validate(media(None), media())
        self.assertTrue(result["video_duration_verified"])
        self.assertEqual(durations, [5.0, 5.0, 2.0])

    def test_completed_tail_after_its_deadline_is_not_accepted(self):
        source = media(None)
        source["tracks"][0]["properties"]["minimum_timestamp"] = 0
        clock = [0.0]
        def run(args, *, log, cancel_event):
            if "-read_intervals" not in args:
                clock[0] = 4.0
                return probe_result({})
            clock[0] = 10.0
            return packets_result([{"pts_time": "599.960", "duration_time": "0.040"}])
        with patch.object(core.time, "monotonic", side_effect=lambda: clock[0]), patch.object(core, "run_command", side_effect=run):
            result = self.validate(source, media())
        self.assertFalse(result["video_duration_verified"])

    def test_reordered_tail_pts_use_maximum_end_instead_of_last_packet(self):
        source = media(None)
        source["tracks"][0]["properties"]["minimum_timestamp"] = 0
        packets = [{"pts_time": "599.840", "duration_time": "0.040"},
                   {"pts_time": "599.960", "duration_time": "0.040"},
                   {"pts_time": "599.920", "duration_time": "0.040"}]
        with patch.object(core, "run_command", side_effect=[probe_result({}), packets_result(packets)]):
            result = self.validate(source, media())
        self.assertEqual(result["input_video_duration_ns"], 600 * 10**9)
        self.assertTrue(result["video_duration_verified"])

    def test_tail_error_on_stderr_is_not_normal_eof_even_with_exit_zero(self):
        source = media(None)
        source["tracks"][0]["properties"]["minimum_timestamp"] = 0
        tail = packets_result([{"pts_time": "599.960", "duration_time": "0.040"}])
        tail.stderr = b"File ended prematurely"
        with patch.object(core, "run_command", side_effect=[probe_result({}), tail]):
            result = self.validate(source, media())
        self.assertFalse(result["video_duration_verified"])
        self.assertTrue(any("未采信结果" in line for line in self.messages))

    def test_missing_seek_index_never_falls_back_to_sequential_read(self):
        for value in (None, 0, False):
            with self.subTest(value=value):
                source = media(None)
                source["tracks"][0]["properties"]["num_index_entries"] = value
                with patch.object(core, "run_command", return_value=probe_result({})) as run:
                    result = self.validate(source, media())
                self.assertFalse(result["video_duration_verified"])
                self.assertEqual(run.call_count, 1)


if __name__ == "__main__":
    unittest.main()
