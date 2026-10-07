from __future__ import annotations

import tempfile
import unittest
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import batch_core
import online_subtitles
import smart_subtitles
import subtitle_tool_core as core
from profile_model import BatchPlan, PreferenceProfile


class SearchSummaryTest(unittest.TestCase):
    def test_raw_filtered_and_truncated_are_not_reported_as_same_count(self):
        result = smart_subtitles._search_result_summary(
            SimpleNamespace(total_count=241, truncated=True), 150, 17,
            Counter(identity=123, role=7, release=3),
        )
        for fragment in ("总计 241", "加载 150", "过滤 133", "影片身份 123", "待核验 17", "分页上限"):
            self.assertIn(fragment, result)

    def test_missing_service_metadata_does_not_invent_total_or_truncation(self):
        result = smart_subtitles._search_result_summary(None, 2, 2, Counter())
        self.assertIn("总计 未提供", result)
        self.assertIn("过滤 0", result)
        self.assertNotIn("分页上限", result)

    def test_terminal_provider_error_does_not_retry_same_site_by_title(self):
        candidate = SimpleNamespace(
            file_id=1, release="Movie.2000.BluRay", file_name="subtitle.srt",
            language="en", identity_key="hash:test",
        )
        for error in (
            online_subtitles.SubtitleQuotaError("OpenSubtitles 今日请求或下载额度已用完。"),
            online_subtitles.SubtitleAuthenticationError("OpenSubtitles API Key 无效或无权访问。"),
        ):
            with self.subTest(error=type(error).__name__), tempfile.TemporaryDirectory() as folder:
                movie = Path(folder) / "Movie.2000.mkv"
                movie.write_bytes(b"movie")
                searches = []

                class Service:
                    @staticmethod
                    def load_settings():
                        return {"key": "test"}

                    @staticmethod
                    def search(*args, **kwargs):
                        searches.append(kwargs)
                        return None, [candidate], SimpleNamespace(query_mode="hash")

                    @staticmethod
                    def download(*args):
                        raise error

                with patch.object(smart_subtitles, "PROVIDERS", (("OpenSubtitles", Service, "key"),)), patch.object(
                    smart_subtitles, "filename_container_identity_conflict", return_value="",
                ), patch.object(core, "inspect_media", return_value={}), patch.object(
                    smart_subtitles.pro_core, "prepare_shared_subtitle_content_audio", return_value=object(),
                ):
                    with self.assertRaises(RuntimeError):
                        smart_subtitles.find_verified_english(str(movie), 1, lambda _: None)
                self.assertEqual(searches, [{}])


class ImageRetentionLogTest(unittest.TestCase):
    def run_plan(self, kept):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            movie = root / "Movie.1995.mkv"
            movie.write_bytes(b"movie")
            subtitle = root / "verified.srt"
            subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n", encoding="utf-8")
            audio = core.Track(1, "audio", "FLAC", "en", "", True, False, False, False, False)
            image = core.Track(2, "subtitles", "S_HDMV/PGS", "ja", "", False, False, False, True, False)
            plan = BatchPlan(
                path=str(movie), profile_slot=1, audio_ids=[1], subtitle_ids=kept,
                missing_subtitle_languages=["en", "zh-CN"], status="ready",
                output_path=str(root / "Movie.SF.mkv"), has_image_subtitles=True,
            )
            result = smart_subtitles.SmartSubtitleResult(str(subtitle), "en", "passed", "Test", object())
            logs = []
            with patch.object(core, "inspect_tracks", return_value=[audio, image]), patch.object(
                smart_subtitles, "find_verified_english", return_value=result,
            ), patch.object(batch_core.pro_core, "process_pro") as process:
                batch_core.process_plan(plan, PreferenceProfile(1, "Test", subtitle_languages=["en", "zh-CN"]), logs.append, None)
            return logs, process.call_args.kwargs

    def test_unselected_japanese_pgs_is_not_promised_as_retained(self):
        logs, kwargs = self.run_plan([])
        self.assertTrue(any("当前偏好未保留" in line for line in logs))
        self.assertFalse(any("原图片字幕继续保留" in line for line in logs))
        self.assertEqual(kwargs["keep_subtitle_ids"], [])

    def test_retained_image_log_identifies_actual_track_and_language(self):
        logs, kwargs = self.run_plan([2])
        self.assertTrue(any("轨 2（日语）" in line for line in logs))
        self.assertEqual(kwargs["keep_subtitle_ids"], [2])


if __name__ == "__main__":
    unittest.main()
