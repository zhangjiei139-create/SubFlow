"""Regressions for the Star Wars subtitle identity and search fallbacks."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import sys
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import batch_core
import online_subtitles
import smart_subtitles
import subtitle_identity_guard as guard


class SearchStrategyTest(unittest.TestCase):
    def test_filename_and_container_disagree_before_search(self):
        movie = "Star Wars：Episode VI - Return of the Jedi (1983).mkv"
        with patch.object(guard, "container_title", return_value="Star Wars: Episode IV - A New Hope (1977)"):
            reason = guard.filename_container_identity_conflict(movie)
            self.assertIn("身份冲突", reason)
            self.assertIn("文件名：" + movie, reason)
            self.assertIn("影片内部标题：Star Wars: Episode IV - A New Hope (1977)", reason)
            self.assertIn("若片头与内部标题一致", reason)
            self.assertIn("Star Wars：Episode IV - A New Hope (1977).mkv", reason)
            self.assertIn("移除旧记录", reason)
            with patch.object(online_subtitles, "opensubtitles_movie_hash") as moviehash:
                with self.assertRaisesRegex(RuntimeError, "身份冲突"):
                    online_subtitles.search("key", movie, "en")
                moviehash.assert_not_called()

    def test_corrected_filename_resolves_the_conflict(self):
        movie = "Star Wars：Episode IV - A New Hope (1977).mkv"
        with patch.object(guard, "container_title", return_value="Star Wars: Episode IV - A New Hope (1977)"):
            self.assertEqual(guard.filename_container_identity_conflict(movie), "")
        self.assertEqual(guard.identity_conflict_notice("RuntimeError: API Key 无效"), "")

    def test_alien_only_is_not_a_complete_english_track(self):
        partial = SimpleNamespace(name="English - Alien Only", forced=False,
                                  text_subtitle=True, pgs_subtitle=False, vobsub_subtitle=False,
                                  language="eng", default=True, id=4, type="subtitles")
        complete = SimpleNamespace(name="English", forced=False,
                                   text_subtitle=True, pgs_subtitle=False, vobsub_subtitle=False,
                                   language="eng", default=False, id=5, type="subtitles")
        audio = SimpleNamespace(id=1, type="audio", language="eng", name="",
                                codec="AAC", default=True)
        self.assertTrue(batch_core._is_incomplete_subtitle(partial, 8000))
        self.assertFalse(batch_core._is_complete_english_text(partial, 8000))
        self.assertEqual(batch_core.ordered_text_anchors([audio, partial, complete], audio, 8000)[0].id, 5)

    def test_explicit_release_conflict_beats_provider_year_and_hash(self):
        movie = "Star Wars：Episode VI - Return of the Jedi (1983).mkv"
        reason = guard.release_year_conflict(
            movie, "Star Wars III-Revenge of the Sith [2005]-keltz", "1983", exact_hash=True,
        )
        self.assertIn("2005", reason)
        candidate = SimpleNamespace(release="Star Wars III-Revenge of the Sith [2005]",
                                    file_name="subtitle.srt", feature_title="Return of the Jedi",
                                    identity_verified=True)
        identity = SimpleNamespace(title="Star Wars Episode VI - Return of the Jedi",
                                   original_title="Star Wars Episode VI - Return of the Jedi")
        self.assertIn("第 3 部", smart_subtitles._candidate_identity_conflict(candidate, identity))

    def test_title_year_retry_skips_hash_search(self):
        identity = online_subtitles.MediaIdentity("Example", "Example", "2000")
        with patch.object(online_subtitles, "identify_media", return_value=identity), \
             patch.object(online_subtitles, "opensubtitles_movie_hash") as moviehash, \
             patch.object(online_subtitles, "_resolve_feature_identity", return_value=None) as resolve_feature, \
             patch.object(online_subtitles, "_search_pages", return_value=([], 0, False)) as search_pages:
            _identity, _candidates, meta = online_subtitles.search(
                "key", "Example (2000).mkv", "en", skip_hash=True,
            )
        moviehash.assert_not_called()
        resolve_feature.assert_called_once_with("key", identity, strict=True)
        self.assertEqual(meta.query_mode, "title-year")
        self.assertEqual(search_pages.call_args.args[1]["year"], "2000")

    def _exercise_hash_fallback(self, max_candidates, candidate_count=1, accept_title=True,
                                title_id_base=100, accepted_id=None):
        with tempfile.TemporaryDirectory() as directory:
            movie = Path(directory) / "Example (2000).mkv"
            movie.write_bytes(b"movie")
            calls = []
            class Provider:
                @staticmethod
                def load_settings():
                    return {"opensubtitles_api_key": "key"}
                @staticmethod
                def search(_key, _movie, _language, *, skip_hash=False):
                    calls.append(("search", skip_hash))
                    candidates = [SimpleNamespace(
                        file_id=(title_id_base if skip_hash else 0) + index,
                        release="Example.2000.BluRay", file_name="Example.2000.srt",
                        language="en", feature_year="2000", feature_title="Example",
                        moviehash_match=not skip_hash,
                    ) for index in range(1, candidate_count + 1)]
                    meta = SimpleNamespace(query_mode="title-year" if skip_hash else "hash")
                    return SimpleNamespace(title="Example", original_title="Example", year="2000"), candidates, meta
                @staticmethod
                def download(_key, candidate, destination):
                    output = Path(destination) / "online-subtitle.srt"
                    output.parent.mkdir(parents=True, exist_ok=True)
                    output.write_text(
                        f"1\n00:00:01,000 --> 00:00:02,000\nline {candidate.file_id}\n\n",
                        encoding="utf-8",
                    )
                    return output
            def preflight(_movie, path, *_args, **_kwargs):
                calls.append(("preflight", Path(path).read_text(encoding="utf-8")))
                line = Path(path).read_text(encoding="utf-8").splitlines()[2]
                candidate_id = int(line.split()[1])
                if (not accept_title or
                        (candidate_id != accepted_id if accepted_id is not None else candidate_id < 100)):
                    raise RuntimeError("hash candidate rejected")
                return Path(path), "verified"
            shared = SimpleNamespace(vad_reference="cached")
            with patch.object(smart_subtitles, "PROVIDERS", (("OpenSubtitles", Provider, "opensubtitles_api_key"),)), \
                 patch.object(smart_subtitles, "filename_container_identity_conflict", return_value=""), \
                 patch.object(smart_subtitles.pro_core, "prepare_shared_subtitle_content_audio", return_value=shared) as prepare, \
                 patch.object(smart_subtitles.pro_core, "preflight_online_subtitle", side_effect=preflight):
                try:
                    result = smart_subtitles.find_verified_english(
                        str(movie), None, lambda _message: None, max_candidates=max_candidates,
                    )
                except RuntimeError:
                    if accept_title:
                        raise
                    result = None
            return result, calls, prepare.call_count

    def test_failed_hash_candidates_retry_title_year_with_same_audio(self):
        result, calls, prepare_count = self._exercise_hash_fallback(2)
        self.assertEqual(result.candidate.file_id, 101)
        self.assertEqual([item for item in calls if item[0] == "search"],
                         [("search", False), ("search", True)])
        self.assertEqual(prepare_count, 1)

    def test_hash_and_title_fallback_share_provider_limit(self):
        result, calls, prepare_count = self._exercise_hash_fallback(
            5, candidate_count=10, accept_title=False,
        )
        self.assertIsNone(result)
        checked_ids = [int(item[1].splitlines()[2].split()[1])
                       for item in calls if item[0] == "preflight"]
        self.assertEqual(checked_ids, [1, 2, 101, 102, 103])
        self.assertEqual(prepare_count, 1)

    def test_untried_hash_candidate_can_be_checked_by_title_fallback(self):
        result, calls, prepare_count = self._exercise_hash_fallback(
            5, candidate_count=10, title_id_base=2, accepted_id=3,
        )
        self.assertEqual(result.candidate.file_id, 3)
        self.assertEqual(len([item for item in calls if item[0] == "preflight"]), 3)
        self.assertEqual(prepare_count, 1)

    def test_exhausted_single_candidate_budget_skips_title_search(self):
        _result, calls, _prepare_count = self._exercise_hash_fallback(1, accept_title=False)
        self.assertEqual([item for item in calls if item[0] == "search"], [("search", False)])


if __name__ == "__main__":
    unittest.main()
