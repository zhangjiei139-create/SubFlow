"""Precise post-hash film lookup and explicit recut candidate guards."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import online_subtitles as online
import smart_subtitles as smart


class PreciseRetryTest(unittest.TestCase):
    def setUp(self):
        self.identity = online.MediaIdentity(
            "Star Wars: Episode IV - A New Hope", "Star Wars: Episode IV - A New Hope", "1977",
        )

    @staticmethod
    def feature(title="Star Wars", year="1977", imdb_id="0076759", original_title=None):
        return {"id": "film", "type": "movie", "attributes": {
            "title": title, "original_title": original_title or title,
            "year": year, "imdb_id": imdb_id,
        }}

    def test_strict_lookup_accepts_original_title_with_exact_year_and_keeps_episode(self):
        result = online._feature_identity_from_response(
            {"data": [self.feature()]}, self.identity, strict=True,
        )
        self.assertEqual(result.imdb_id, "0076759")
        self.assertEqual(result.original_title, self.identity.original_title)
        self.assertEqual(self.identity.imdb_id, "")

    def test_strict_lookup_does_not_override_wrong_year_with_matching_id(self):
        result = online._feature_identity_from_response(
            {"data": [self.feature(self.identity.title, "1999")]}, self.identity, strict=True,
        )
        self.assertIsNone(result)

    def test_strict_lookup_rejects_conflicting_episode_even_with_matching_alias(self):
        result = online._feature_identity_from_response(
            {"data": [self.feature("Star Wars: Episode I - A New Hope", original_title="A New Hope")]},
            self.identity, strict=True,
        )
        self.assertIsNone(result)

    def test_strict_lookup_requires_unique_movie_with_strong_id(self):
        for response in (
            {"data": [self.feature(imdb_id="")]},
            {"data": [self.feature(), self.feature(imdb_id="9999999")]},
        ):
            with self.subTest(response=response):
                self.assertIsNone(online._feature_identity_from_response(
                    response, self.identity, strict=True,
                ))

    def test_failed_hash_identity_is_not_used_by_id_retry(self):
        wrong = online.SubtitleCandidate(
            1, "Episode.I.srt", "Star Wars Episode I A New Hope 1977", "en", 0, 0, False, False,
            moviehash_match=True, feature_title="The Phantom Menace", feature_year="1999",
            feature_imdb_id="0120915",
        )
        correct = online.SubtitleCandidate(
            2, "Star.Wars.1977.Proper.srt", "Star Wars 1977 Proper", "en", 0, 0, False, False,
            feature_title="Star Wars", feature_year="1977", feature_imdb_id="0076759",
        )
        calls = []
        def search_pages(_key, params, _language, **_kwargs):
            calls.append(dict(params))
            return ([wrong] if "moviehash" in params else [correct]), 1, False

        with patch.object(online, "filename_container_identity_conflict", return_value=""), \
             patch.object(online, "identify_media", return_value=self.identity), \
             patch.object(online, "opensubtitles_movie_hash", return_value="hash") as moviehash, \
             patch.object(online, "_request", return_value={"data": [self.feature()]}) as request, \
             patch.object(online, "_search_pages", side_effect=search_pages):
            _identity, hash_candidates, hash_meta = online.search("key", "movie.mkv", "en")
            identity, candidates, meta = online.search("key", "movie.mkv", "en", skip_hash=True)

        self.assertEqual(hash_meta.query_mode, "hash")
        self.assertEqual(hash_candidates, [wrong])
        moviehash.assert_called_once()
        self.assertEqual(meta.query_mode, "feature")
        self.assertEqual(candidates, [correct])
        self.assertEqual(calls[-1], {"languages": "en", "imdb_id": "0076759"})
        self.assertEqual(request.call_args.kwargs["params"], {
            "query": self.identity.original_title, "type": "movie", "query_match": "exact",
        })
        self.assertEqual(identity.original_title, self.identity.original_title)
        self.assertTrue(correct.identity_verified)
        self.assertIn("第 1 部", smart._candidate_identity_conflict(wrong, identity))

    def test_missing_precise_entry_still_uses_original_title_year(self):
        with patch.object(online, "filename_container_identity_conflict", return_value=""), \
             patch.object(online, "identify_media", return_value=self.identity), \
             patch.object(online, "opensubtitles_movie_hash") as moviehash, \
             patch.object(online, "_request", return_value={"data": []}), \
             patch.object(online, "_search_pages", return_value=([], 0, False)) as search_pages:
            identity, candidates, meta = online.search("key", "movie.mkv", "en", skip_hash=True)
        moviehash.assert_not_called()
        self.assertEqual(meta.query_mode, "title-year")
        self.assertEqual(identity, self.identity)
        self.assertEqual(search_pages.call_args.args[1]["query"], self.identity.original_title)
        self.assertEqual(search_pages.call_args.args[1]["year"], "1977")


class RecutGuardTest(unittest.TestCase):
    def setUp(self):
        self.identity = SimpleNamespace(title="A New Hope", original_title="A New Hope")

    @staticmethod
    def candidate(release):
        return SimpleNamespace(release=release, file_name=release + ".srt")

    def test_explicit_recuts_are_rejected_before_timeline_work(self):
        for release in (
            "star-wars-4-1977-a-new-hope-revisited", "A.New.Hope.1977.Fan.Edit",
            "A.New.Hope.1977.Recut", "A.New.Hope.1977.Re-Edited",
            "A.New.Hope.1977.Despecialized",
        ):
            with self.subTest(release=release):
                self.assertIn("重剪/粉丝改版", smart._candidate_recut_conflict(
                    "A New Hope (1977).mkv", self.candidate(release), self.identity,
                ))

    def test_matching_explicit_recut_is_allowed_but_different_tag_is_not(self):
        movie = "A.New.Hope.1977.Revisited.mkv"
        self.assertEqual(smart._candidate_recut_conflict(
            movie, self.candidate("A.New.Hope.1977.Revisited"), self.identity,
        ), "")
        self.assertEqual(smart._candidate_recut_conflict(
            movie, self.candidate("A.New.Hope.1977.Revisited.Fan.Edit"), self.identity,
        ), "")
        self.assertIn("重剪/粉丝改版", smart._candidate_recut_conflict(
            movie, self.candidate("A.New.Hope.1977.Fan.Edit"), self.identity,
        ))

    def test_revisited_in_official_film_title_is_not_an_edition(self):
        identity = SimpleNamespace(title="Brideshead Revisited", original_title="Brideshead Revisited")
        self.assertEqual(smart._candidate_recut_conflict(
            "Brideshead Revisited (2008).mkv",
            self.candidate("Brideshead.Revisited.2008.BluRay"), identity,
        ), "")

    def test_recut_filter_runs_before_download_and_keeps_normal_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            movie = Path(directory) / "A New Hope (1977).mkv"
            movie.write_bytes(b"movie")
            recut = self.candidate("A.New.Hope.1977.Revisited")
            proper = self.candidate("A.New.Hope.1977.Proper")
            for index, candidate in enumerate((recut, proper), 1):
                candidate.file_id = index
                candidate.language = "en"
                candidate.feature_title = "A New Hope"
                candidate.feature_year = "1977"
            downloaded = []
            class Provider:
                @staticmethod
                def load_settings():
                    return {"key": "token"}
                @staticmethod
                def search(*_args, **_kwargs):
                    return self.identity, [recut, proper], SimpleNamespace(query_mode="feature")
                @staticmethod
                def download(_key, candidate, destination):
                    downloaded.append(candidate.file_id)
                    path = Path(destination) / "subtitle.srt"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello there.\n", encoding="utf-8")
                    return path

            with patch.object(smart, "PROVIDERS", (("OpenSubtitles", Provider, "key"),)), \
                 patch.object(smart, "filename_container_identity_conflict", return_value=""), \
                 patch.object(smart.pro_core, "prepare_shared_subtitle_content_audio",
                              return_value=SimpleNamespace(vad_reference="cached")), \
                 patch.object(smart.pro_core, "preflight_online_subtitle",
                              side_effect=lambda _movie, path, *_args, **_kwargs: (Path(path), "verified")) as preflight:
                result = smart.find_verified_english(str(movie), None, lambda _message: None)
            self.assertEqual(downloaded, [2])
            preflight.assert_called_once()
            self.assertEqual(result.candidate.file_id, 2)


class FinalAdoptionReportTest(unittest.TestCase):
    def _exercise(self, report, *, reject_acceptance=False):
        with tempfile.TemporaryDirectory() as directory:
            movie = Path(directory) / "Example (2000).mkv"
            movie.write_bytes(b"movie")
            candidate = SimpleNamespace(
                file_id=1, release="Example.2000.Proper", file_name="Example.2000.srt",
                language="en", feature_title="Example", feature_year="2000",
            )
            events = []
            class Provider:
                @staticmethod
                def load_settings():
                    return {"key": "token"}
                @staticmethod
                def search(*_args, **_kwargs):
                    return (SimpleNamespace(title="Example", original_title="Example", year="2000"),
                            [candidate], SimpleNamespace(query_mode="feature"))
                @staticmethod
                def download(_key, _candidate, destination):
                    path = Path(destination) / "subtitle.srt"
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello there.\n", encoding="utf-8")
                    return path

            def preflight(_movie, path, _work, _audio, _shared, log, *_args, **_kwargs):
                log(report)
                return Path(path), report

            def acceptance(_path, _cancel):
                events.append("candidate acceptance")
                if reject_acceptance:
                    raise RuntimeError("PGS rejected the candidate")

            with patch.object(smart, "PROVIDERS", (("OpenSubtitles", Provider, "key"),)), \
                 patch.object(smart, "filename_container_identity_conflict", return_value=""), \
                 patch.object(smart.pro_core, "prepare_shared_subtitle_content_audio",
                              return_value=SimpleNamespace(vad_reference="cached")), \
                 patch.object(smart.pro_core, "preflight_online_subtitle", side_effect=preflight):
                if reject_acceptance:
                    with self.assertRaises(RuntimeError):
                        smart.find_verified_english(
                            str(movie), None, events.append, candidate_acceptance=acceptance,
                        )
                    result = None
                else:
                    result = smart.find_verified_english(
                        str(movie), None, events.append, candidate_acceptance=acceptance,
                    )
            return result, events

    def test_success_returns_adopted_report_after_remaining_acceptance(self):
        for prefix in ("连续VAD时间轴核验通过：提议固定偏移", "连续VAD确认固定偏移"):
            with self.subTest(prefix=prefix):
                report = prefix + " +0.07 秒；音轨 1（en）；待候选其余验收；未运行Whisper正文核验"
                result, events = self._exercise(report)
                self.assertTrue(result.report.startswith("已采用连续VAD固定偏移"))
                self.assertNotIn("待候选其余验收", result.report)
                self.assertIn(report, events)
                adoption = next(event for event in events if "已采用连续VAD固定偏移" in event)
                self.assertGreater(events.index(adoption), events.index("candidate acceptance"))

    def test_acceptance_rejection_never_publishes_adopted_report(self):
        report = "连续VAD时间轴核验通过：提议固定偏移 -8.54 秒；待候选其余验收"
        result, events = self._exercise(report, reject_acceptance=True)
        self.assertIsNone(result)
        self.assertIn(report, events)
        self.assertFalse(any("已采用连续VAD固定偏移" in event for event in events))


if __name__ == "__main__":
    unittest.main()
