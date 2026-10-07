from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import smart_subtitles


class SmartSearchFailureDiagnosticsTests(TestCase):
    def run_empty_search(self, status="unresolved-title", candidates=(), download_error=None):
        messages = []
        meta = SimpleNamespace(query_mode="title-year", feature_lookup_status=status,
                               feature_lookup_reason="两个影片身份，未锁定", total_count=len(candidates),
                               loaded_count=len(candidates), truncated=False)
        service = SimpleNamespace(load_settings=lambda: {"key": "test-key"},
                                  search=lambda *_: (None, list(candidates), meta))
        def download(*_):
            raise RuntimeError(download_error or "不应下载")
        service.download = download
        with TemporaryDirectory() as folder:
            movie = Path(folder) / "Movie.2025.mkv"
            movie.write_bytes(b"movie")
            with patch.object(smart_subtitles, "PROVIDERS", (("Test", service, "key"),)), \
                    patch.object(smart_subtitles, "filename_container_identity_conflict", return_value=""), \
                    patch.object(smart_subtitles.pro_core, "prepare_shared_subtitle_content_audio", return_value=None):
                with self.assertRaises(RuntimeError) as caught:
                    smart_subtitles.find_verified_english(str(movie), 0, messages.append)
        return str(caught.exception), messages

    def test_empty_results_report_no_candidates_and_no_verification(self):
        error, _ = self.run_empty_search()
        self.assertIn("没有找到可下载的英文字幕候选", error)
        self.assertIn("没有字幕进入时间轴核验", error)
        self.assertNotIn("均未通过安全核验", error)
        self.assertNotIn("所有候选均未通过内容筛选", error)

    def test_ambiguous_identity_is_not_logged_as_locked(self):
        _, messages = self.run_empty_search(status="ambiguous")
        self.assertTrue(any("影片身份查询结果" in message and "未锁定" in message for message in messages))
        self.assertFalse(any("影片身份锁定" in message for message in messages))

    def test_filtered_results_do_not_claim_timeline_rejection(self):
        candidate = SimpleNamespace(file_id="1", release="Movie.2025.commentary", file_name="commentary.srt",
                                    feature_title="Movie", feature_year="2025", language="en",
                                    identity_verified=True, identity_key="imdb:1")
        error, _ = self.run_empty_search(candidates=(candidate,))
        self.assertIn("均在下载前被排除", error)
        self.assertIn("没有字幕进入时间轴核验", error)

    def test_download_failure_is_distinguished_from_empty_site_results(self):
        candidate = SimpleNamespace(file_id="1", release="Movie.2025.BluRay", file_name="Movie.srt",
                                    feature_title="Movie", feature_year="2025", language="en",
                                    identity_verified=True, identity_key="imdb:1")
        error, _ = self.run_empty_search(candidates=(candidate,), download_error="下载失败")
        self.assertIn("候选下载或预检未通过", error)
        self.assertIn("下载失败", error)
        self.assertNotIn("没有找到可下载", error)
