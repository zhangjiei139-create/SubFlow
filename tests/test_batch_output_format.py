from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import batch_core as batch
import subtitle_fonts
from profile_model import BatchPlan, PreferenceProfile
from subtitle_tool_core import Track


class BatchOutputFormatTests(unittest.TestCase):
    def test_mp4_paths_keep_distinct_sibling_inputs_and_skip_both_output_types(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "Movie.mkv"
            other = Path(folder) / "Movie.mp4"
            source.touch()
            self.assertEqual(Path(batch.batch_output_path(source, output_format="MP4")).name, "Movie.SF.mp4")
            other.touch()
            self.assertEqual(Path(batch.batch_output_path(source, output_format="MP4")).name, "Movie.mkv.SF.mp4")
            self.assertEqual(Path(batch.batch_output_path(other, output_format="MP4")).name, "Movie.mp4.SF.mp4")
            self.assertTrue(batch.is_output_path("Movie.SF.mp4"))
            self.assertTrue(batch.is_output_path("Movie.SF.mkv"))
            self.assertFalse(batch.is_output_path("Movie.mp4"))

    def test_mp4_keeps_default_player_styling_without_enabling_mkv_font_pipeline(self):
        plan = BatchPlan("movie.mp4", 1, output_path="movie.SF.mp4")
        profile = PreferenceProfile(1, "test", subtitle_languages=["en"])
        contexts = []
        with patch.object(batch, "_process_plan_impl", side_effect=lambda *args: contexts.append(subtitle_fonts.CONTEXT.get()) or "done"):
            self.assertEqual(batch.process_plan(plan, profile, lambda text: None, None), "done")
        self.assertEqual(contexts, [None])

    def test_mp4_does_not_silently_ignore_manual_font_size_or_color(self):
        plan = BatchPlan("movie.mkv", 1, output_path="movie.SF.mp4")
        for style in ({"font_id": "builtin:bree"}, {"size_mode": "manual", "size": 38, "size_origin": "user"}, {"color": "#ABCDEF"}):
            with self.subTest(style=style):
                profile = PreferenceProfile(1, "test", subtitle_languages=["en"], subtitle_styles={"en": style})
                with self.assertRaisesRegex(RuntimeError, "MP4 无法应用"):
                    batch.process_plan(plan, profile, lambda text: None, None)

    def test_selected_picture_subtitles_block_mp4_but_unselected_ones_do_not(self):
        plan = BatchPlan("movie.mkv", 1, output_path="movie.SF.mp4", subtitle_ids=[2])
        profile = PreferenceProfile(1, "test", subtitle_languages=["en"])
        image = Track(2, "subtitles", "HDMV PGS", "en", "", False, False, False, True, False)
        self.assertTrue(batch.mp4_compatibility_reasons(plan, profile, [image]))
        plan.subtitle_ids.clear()
        self.assertFalse(batch.mp4_compatibility_reasons(plan, profile, [image]))

    def test_failed_mp4_validation_keeps_previous_published_file(self):
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory) / "stage.mkv"
            output = Path(directory) / "movie.SF.mp4"
            stage.write_bytes(b"stage")
            output.write_bytes(b"previous good output")
            media = {"tracks": [], "container": {"type": "Matroska"}}
            def remux(_source, target, *_args):
                Path(target).write_bytes(b"unverified mp4")
            with patch.object(batch.core, "inspect_media", return_value=media), \
                 patch.object(batch.core, "remux_mkv_to_mp4", side_effect=remux), \
                 patch.object(batch.core, "validate_media_output", side_effect=RuntimeError("bad media")):
                with self.assertRaisesRegex(RuntimeError, "bad media"):
                    batch._publish_mp4(stage, output, lambda text: None, None)
            self.assertEqual(output.read_bytes(), b"previous good output")
            self.assertFalse(batch.core.partial_output_path(str(output)).exists())
            self.assertTrue(stage.exists())

    def test_mp4_input_has_its_own_mux_progress_state_until_publication(self):
        from qt_workers import MuxProgressTracker
        tracker = MuxProgressTracker()
        tracker.update("Movie.mp4：转换为 MP4 输出：partial.mp4")
        self.assertTrue(tracker.visible)
        self.assertTrue(tracker.busy)
        tracker.update("Movie.mp4：MP4 封装完成，正在验证成品。")
        self.assertTrue(tracker.selected[1]["active"])
        tracker.update("Movie.mp4：正式输出已就绪：Movie.SF.mp4")
        self.assertFalse(tracker.selected[1]["active"])


if __name__ == "__main__":
    unittest.main()
