from __future__ import annotations

import copy
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import batch_core as batch
import subtitle_tool_core as core
from profile_model import BatchPlan, PreferenceProfile


def track(track_id, kind, codec, **props):
    return {"id": track_id, "type": kind, "codec": codec, "properties": props}


class BatchOutputOwnershipTests(unittest.TestCase):
    def test_single_file_keeps_name_and_existing_siblings_get_distinct_names(self):
        with tempfile.TemporaryDirectory() as folder:
            first, second = Path(folder) / "Movie.mkv", Path(folder) / "Movie.mp4"
            first.touch()
            self.assertEqual(Path(batch.batch_output_path(first)).name, "Movie.SF.mkv")
            second.touch()
            self.assertEqual(Path(batch.batch_output_path(first)).name, "Movie.mkv.SF.mkv")
            self.assertEqual(Path(batch.batch_output_path(second)).name, "Movie.mp4.SF.mkv")

    def test_queued_inputs_also_detect_collision_and_outputs_are_not_sources(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "Movie.mkv"
            other = Path(folder) / "Movie.mp4"
            self.assertNotEqual(batch.batch_output_path(source, [other]), batch.batch_output_path(other, [source]))
            self.assertEqual(Path(batch.batch_output_path(source, [Path(folder) / "Movie.SF.mkv"])).name, "Movie.SF.mkv")

    def test_output_claim_rejects_alias_and_releases_after_failure(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "Movie.SF.mkv"
            with self.assertRaisesRegex(ValueError, "test"):
                with batch.claim_output_path(output):
                    with self.assertRaisesRegex(RuntimeError, "同一输出"):
                        with batch.claim_output_path(output.parent / "." / output.name):
                            self.fail("duplicate claim accepted")
                    raise ValueError("test")
            with batch.claim_output_path(output):
                pass

    def test_processing_rechecks_a_sibling_added_after_analysis(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "Movie.mkv"
            source.touch()
            plan = BatchPlan(str(source), 1, output_path=batch.batch_output_path(source))
            (Path(folder) / "Movie.mp4").touch()
            with patch.object(batch, "_process_plan_impl", return_value="done"):
                batch.process_plan(plan, PreferenceProfile(1, "test"), lambda _: None, None)
            self.assertEqual(Path(plan.output_path).name, "Movie.mkv.SF.mkv")


class BatchAudioSourceTests(unittest.TestCase):
    def test_missing_container_timestamp_uses_bounded_stream_metadata(self):
        audio = track(7, "audio", "AAC")
        media = {"tracks": [track(1, "video", "AVC"), audio]}
        result = subprocess.CompletedProcess([], 0, b'{"streams":[{"start_time":"1.234567"}]}', b'')
        with patch.object(core, "run_command", return_value=result) as run:
            self.assertEqual(batch._track_origin_ns("input.mp4", media, audio, None), 1234567000)
        self.assertIn("-nofind_stream_info", run.call_args.args[0])
        self.assertIn("a:0", run.call_args.args[0])
        self.assertIsInstance(run.call_args.kwargs["cancel_event"], core._VideoDurationProbeCancel)

    def test_unknown_or_error_timestamp_is_never_assumed_zero(self):
        audio = track(0, "audio", "AAC")
        for stdout, stderr in [(b'{"streams":[{}]}', b''),
                               (b'{"streams":[{"start_time":"0"}]}', b'read error')]:
            with self.subTest(stderr=stderr), patch.object(core, "run_command", return_value=
                    subprocess.CompletedProcess([], 0, stdout, stderr)):
                with self.assertRaises(RuntimeError):
                    batch._track_origin_ns("input.mp4", {"tracks": [audio]}, audio, None)

    def test_track_zero_is_not_replaced_by_default_japanese_audio(self):
        tracks = [
            core.Track(0, "audio", "FLAC", "en", "", False, False, False, False, False),
            core.Track(1, "video", "AVC", "und", "", True, False, False, False, False),
            core.Track(2, "audio", "AAC", "ja", "", True, False, False, False, False),
        ]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "movie.mkv"
            source.write_bytes(b"test")
            plan = BatchPlan(str(source), 1, status="ready", audio_ids=[0, 2],
                             missing_audio_formats=["AC-3"], generated_audio_source_id=0,
                             output_path=str(root / "result.mkv"))
            converted = batch.ConvertedAudio(root / "converted.mka", 0, {})
            profile = PreferenceProfile(1, "test", subtitle_languages=[], audio_policy="universal")
            with patch.object(core, "inspect_tracks", return_value=tracks), \
                 patch.object(batch.pro_core, "process_pro"), \
                 patch.object(batch, "_transcode_audio", return_value=converted) as transcode, \
                 patch.object(batch, "_finalize_with_audio") as finalize:
                batch.process_plan(plan, profile, lambda _: None, None, allow_online_search=False)
            self.assertEqual(transcode.call_args.args[1], 0)
            self.assertEqual(finalize.call_args.args[2][0][2], "en")


class FinalAudioValidationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.base = self.root / "base.mkv"
        self.final = self.root / "final.mkv"
        self.video = track(0, "video", "AVC", minimum_timestamp=0, tag_duration="00:00:06.000000000")
        self.original = track(1, "audio", "AAC", minimum_timestamp=0, audio_channels=2,
                              audio_sampling_frequency=48000, default_track=True, language="eng")
        self.subtitle = track(2, "subtitles", "SubRip/SRT")
        self.base_media = {"container": {"recognized": True}, "tracks": [self.video, self.original, self.subtitle]}
        self.generated_spec = track(0, "audio", "AC-3", minimum_timestamp=0, audio_channels=2,
                                    audio_sampling_frequency=48000, codec_delay=5333333)
        self.converted = batch.ConvertedAudio(self.root / "audio.mka", 2_000_000_000,
                                              {"tracks": [self.generated_spec]})
        self.actual_generated = copy.deepcopy(self.generated_spec)
        self.actual_generated.update(id=2)
        self.actual_generated["properties"].update(minimum_timestamp=2_000_000_000,
            language="eng", track_name="AC-3 Compatible", default_track=False, forced_track=False)
        self.output_media = {"container": {"recognized": True},
                             "tracks": [self.video, self.original, self.actual_generated, self.subtitle]}

    def finalize(self, output_media=None, *, keep_audio=True, cancel=None):
        output_media = output_media or self.output_media
        commands = []
        def run(args, **kwargs):
            commands.append(args)
            Path(args[2]).write_bytes(b"0" * (1024 * 1024))
        def inspect(path, **kwargs):
            return self.base_media if str(path) == str(self.base) else output_media
        with patch.object(core, "run_command", side_effect=run), \
             patch.object(core, "inspect_media", side_effect=inspect):
            batch._finalize_with_audio(self.base, self.final, [(self.converted, "AC-3", "en")],
                                      keep_audio, str(self.base), lambda _: None, cancel)
        return commands

    def test_valid_audio_restores_origin_and_keeps_encoder_delay(self):
        commands = self.finalize()
        self.assertIn("0:2000", commands[0])
        self.assertTrue(self.final.exists())
        self.assertFalse(core.partial_output_path(str(self.final)).exists())

    def test_missing_planned_audio_or_subtitle_is_rejected(self):
        for removed in (self.actual_generated, self.subtitle):
            with self.subTest(kind=removed["type"]):
                bad = copy.deepcopy(self.output_media)
                bad["tracks"].remove(removed)
                with self.assertRaisesRegex(RuntimeError, "输出完整性验证失败"):
                    self.finalize(bad)
                self.assertFalse(self.final.exists())

    def test_generated_codec_channels_language_delay_and_time_are_checked(self):
        mutations = [("codec", "FLAC"), ("audio_channels", 1), ("language", "jpn"),
                     ("codec_delay", 0), ("minimum_timestamp", 0), ("default_track", True)]
        for key, value in mutations:
            with self.subTest(field=key):
                bad = copy.deepcopy(self.output_media)
                item = bad["tracks"][2]
                (item if key == "codec" else item["properties"])[key] = value
                with self.assertRaises(RuntimeError):
                    self.finalize(bad)
                self.assertFalse(self.final.exists())

    def test_compact_audio_is_the_only_default_and_has_no_base_audio_expectation(self):
        output = copy.deepcopy(self.output_media)
        output["tracks"].pop(1)
        output["tracks"][1]["properties"]["default_track"] = True
        self.finalize(output, keep_audio=False)
        self.assertTrue(self.final.exists())

    def test_cancelled_job_does_not_start_or_publish(self):
        event = threading.Event()
        event.set()
        with self.assertRaises(core.CancelledError):
            self.finalize(cancel=event)
        self.assertFalse(self.final.exists())


if __name__ == "__main__":
    unittest.main()
