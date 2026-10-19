"""Hash evidence and bounded searches for films without a confirmed title."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import online_subtitles as online
import smart_subtitles as smart
import subdl_subtitles as subdl


def candidate(**kwargs):
    return online.SubtitleCandidate(
        123, "Actual.Film.1995.BluRay.srt", "Actual.Film.1995.BluRay", "en",
        0, 0.0, False, False,
        **({"moviehash_match": True, "feature_title": "Actual Film", "feature_year": "1995"} | kwargs),
    )


class HashEvidenceTests(unittest.TestCase):
    def test_hash_is_stronger_than_an_undated_unconfirmed_filename(self):
        for name in ("release-code", "unrecognized", "0123456789abcdef"):
            with self.subTest(name=name):
                self.assertEqual(smart._candidate_identity_conflict(
                    candidate(), online.MediaIdentity(name, name)), "")

    def test_absent_or_false_hash_does_not_override_the_filename(self):
        for match in (False, None):
            with self.subTest(match=match):
                self.assertTrue(smart._candidate_identity_conflict(
                    candidate(moviehash_match=match), online.MediaIdentity("Other Film", "Other Film")))

    def test_known_year_or_id_still_blocks_wrong_film_even_with_hash(self):
        for identity, item in (
            (online.MediaIdentity("Other Film", "Other Film", "1995"), candidate()),
            (online.MediaIdentity("Other Film", "Other Film", imdb_id="123"),
             candidate(feature_imdb_id="456")),
            (online.MediaIdentity("Other Film", "Other Film", tmdb_id="123"),
             candidate(feature_tmdb_id="456")),
            (online.MediaIdentity("Other Film", "Other Film", feature_id="known"), candidate()),
        ):
            with self.subTest(identity=identity):
                self.assertTrue(smart._candidate_identity_conflict(item, identity))

    def test_matching_strong_id_can_confirm_hash_alias(self):
        self.assertEqual(smart._candidate_identity_conflict(
            candidate(feature_imdb_id="123"),
            online.MediaIdentity("Local Alias", "Local Alias", imdb_id="tt123")), "")

    def test_hash_cannot_override_episode_or_purpose(self):
        identity = online.MediaIdentity("Star Wars Episode IV", "Star Wars Episode IV")
        item = candidate()
        item.release = "Star Wars Episode I"
        self.assertIn("第 1 部", smart._candidate_identity_conflict(item, identity))
        item.feature_type = "tv_episode"
        item.release = "Actual Film"
        self.assertIn("剧集", smart._candidate_identity_conflict(item, identity))
        item = candidate()
        item.release += ".Commentary"
        self.assertTrue(smart._candidate_role_conflict(item, online.MediaIdentity("release-code")))

    def test_only_a_json_true_flag_confirms_hash(self):
        for value in (True, False, None, "false", "true", 1, 0):
            with self.subTest(value=value):
                rows = online._candidates_from_response({"data": [{"attributes": {
                    "files": [{"file_id": 1}], "moviehash_match": value,
                }}]}, "en")
                self.assertEqual(rows[0].moviehash_match, value is True)

    def test_unknown_filename_hash_candidate_reaches_real_search_download_and_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            movie = Path(directory) / "release-code.mkv"
            movie.write_bytes(b"test media")
            item = candidate()
            downloaded, logs = [], []

            class Service:
                load_settings = staticmethod(lambda: {"key": "fixture"})
                search = staticmethod(online.search)

                @staticmethod
                def download(_key, row, destination):
                    downloaded.append(row.file_id)
                    path = Path(destination) / row.file_name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello there.\n", encoding="utf-8")
                    return path

            with patch.object(smart, "PROVIDERS", (("OpenSubtitles", Service, "key"),)), \
                    patch.object(smart, "filename_container_identity_conflict", return_value=""), \
                    patch.object(online, "filename_container_identity_conflict", return_value=""), \
                    patch.object(online, "identify_media", return_value=online.MediaIdentity("release-code", "release-code")), \
                    patch.object(online, "opensubtitles_movie_hash", return_value="test-hash"), \
                    patch.object(online, "_search_pages", return_value=([item], 1, False)) as search, \
                    patch.object(smart.pro_core, "persistent_subtitle_logger", side_effect=lambda _movie, log: log), \
                    patch.object(smart.pro_core, "prepare_shared_subtitle_content_audio", return_value=object()), \
                    patch.object(smart.pro_core, "preflight_online_subtitle",
                                 side_effect=lambda _movie, path, *_a, **_kw: (Path(path), "verified")) as preflight:
                result = smart.find_verified_english(str(movie), 1, logs.append)
            self.assertEqual(downloaded, [123])
            search.assert_called_once()
            preflight.assert_called_once()
            self.assertTrue(Path(result.subtitle_path).exists())
            self.assertTrue(any("不否决候选" in text for text in logs))


class UnresolvedSearchTests(unittest.TestCase):
    def test_hash_miss_without_catalogue_identity_or_year_cannot_search_subtitles_by_abbreviation(self):
        identity = online.MediaIdentity("release-code", "release-code")
        with patch.object(online, "filename_container_identity_conflict", return_value=""), \
                patch.object(online, "identify_media", return_value=identity), \
                patch.object(online, "opensubtitles_movie_hash", return_value="test-hash"), \
                patch.object(online, "_resolve_feature_identity", return_value=None), \
                patch.object(online, "_search_pages", return_value=([], 0, False)) as pages:
            _identity, rows, meta = online.search("fixture", "release-code.mkv", "en")
        self.assertEqual(rows, [])
        self.assertEqual(pages.call_count, 1)
        self.assertIn("moviehash", pages.call_args.args[1])
        self.assertEqual(meta.query_mode, "unresolved-title")
        self.assertIn("补全正确片名和年份", meta.fallback_reason)

    def test_rejected_hash_has_no_abbreviation_download_fallback(self):
        with patch.object(online, "filename_container_identity_conflict", return_value=""), \
                patch.object(online, "identify_media", return_value=online.MediaIdentity("release-code")), \
                patch.object(online, "_resolve_feature_identity", return_value=None), \
                patch.object(online, "_search_pages") as pages:
            _identity, rows, meta = online.search("fixture", "release-code.mkv", "en", skip_hash=True)
        pages.assert_not_called()
        self.assertEqual(rows, [])
        self.assertEqual(meta.query_mode, "unresolved-title")

    def test_unique_catalogue_id_still_works_for_a_real_undated_title(self):
        identity = online.MediaIdentity("Actual Film", "Actual Film")
        resolved = online.MediaIdentity("Actual Film", "Actual Film", "1995", imdb_id="123")
        with patch.object(online, "filename_container_identity_conflict", return_value=""), \
                patch.object(online, "identify_media", return_value=identity), \
                patch.object(online, "_resolve_feature_identity", return_value=resolved), \
                patch.object(online, "_search_pages", return_value=([candidate()], 1, False)) as pages:
            _identity, rows, _meta = online.search("fixture", "film.mkv", "en", skip_hash=True)
        self.assertEqual(len(rows), 1)
        self.assertEqual(pages.call_args.args[1], {"languages": "en", "imdb_id": "123"})

    def test_full_title_and_year_search_is_unchanged(self):
        identity = online.MediaIdentity("Actual Film", "Actual Film", "1995")
        with patch.object(online, "filename_container_identity_conflict", return_value=""), \
                patch.object(online, "identify_media", return_value=identity), \
                patch.object(online, "_resolve_feature_identity", return_value=None), \
                patch.object(online, "_search_pages", return_value=([], 0, False)) as pages:
            online.search("fixture", "film.mkv", "en", skip_hash=True)
        self.assertEqual(pages.call_args.args[1], {
            "languages": "en", "query": "Actual Film", "year": "1995", "type": "movie"})

    def test_subdl_does_not_drop_tokens_without_year(self):
        identity = subdl.MediaIdentity("release-code", "release-code")
        self.assertEqual(subdl._movie_queries(identity), ["release code"])
        with patch.object(subdl, "filename_container_identity_conflict", return_value=""), \
                patch.object(subdl, "identify_media", return_value=identity), \
                patch.object(subdl, "_request_v2", return_value={"results": []}) as request:
            _identity, rows, meta = subdl.search("fixture", "release-code.mkv", "en")
        self.assertEqual(rows, [])
        self.assertEqual(request.call_count, 1)
        self.assertIn("补全正确片名和年份", meta.fallback_reason)

    def test_subdl_unique_full_title_works_but_remakes_and_typos_need_year(self):
        identity = subdl.MediaIdentity("Actual Film", "Actual Film")
        exact = {"sd_id": "1", "name": "Actual Film", "year": 1995}
        self.assertIs(subdl._choose_movie_without_score(identity, [exact]), exact)
        self.assertIsNone(subdl._choose_movie_without_score(identity, [exact, dict(exact, sd_id="2", year=2020)]))
        self.assertIsNone(subdl._choose_movie_without_score(identity, [exact, dict(exact, year=2020)]))
        self.assertIsNone(subdl._choose_movie_without_score(identity, [dict(exact, name="Actua Film")]))


if __name__ == "__main__":
    unittest.main()
