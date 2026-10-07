"""Offline catalogue fixtures: unique identities, safe fallback and diagnostics."""
from __future__ import annotations

import concurrent.futures
import io
import threading
import unittest
import urllib.error
from unittest.mock import patch

import online_subtitles as online


def feature(*, title="The Cure", year="1995", imdb="0112757", tmdb="", row="one"):
    return {"id": row, "type": "movie", "attributes": {
        "title": title, "original_title": title, "year": year,
        "imdb_id": imdb, "tmdb_id": tmdb, "feature_type": "Movie",
    }}


def candidate(title="The Cure", year="1995"):
    return online.SubtitleCandidate(
        10, "film.srt", title, "en", 0, 0, False, False,
        feature_title=title, feature_year=year,
    )


class FeatureDeduplicationTests(unittest.TestCase):
    def test_lives_of_others_dated_record_beats_other_undated_film(self):
        identity = online.MediaIdentity("The Lives of Others", "The Lives of Others", "2006")
        response = {"data": [
            feature(title=identity.title, year="2006", imdb="405094", tmdb="582", row="531907"),
            feature(title=identity.title, year="", imdb="976234", row="535181"),
        ]}
        details = online._FeatureLookupDiagnostics()
        resolved = online._feature_identity_from_response(response, identity, diagnostics=details)
        self.assertEqual(resolved.imdb_id, "405094")
        self.assertEqual(details.status, "resolved")

    def test_exact_year_preference_does_not_hide_shared_id_conflict(self):
        identity = online.MediaIdentity("The Lives of Others", "The Lives of Others", "2006")
        response = {"data": [
            feature(title=identity.title, year="2006", imdb="405094", tmdb="582"),
            feature(title=identity.title, year="", imdb="405094", tmdb="999"),
        ]}
        self.assertIsNone(online._feature_identity_from_response(response, identity))

    def setUp(self):
        self.identity = online.MediaIdentity("The Cure", "The Cure", "1995")

    def resolve(self, rows, identity=None):
        self.details = online._FeatureLookupDiagnostics()
        return online._feature_identity_from_response(
            {"data": rows}, identity or self.identity,
            strict=True, diagnostics=self.details,
        )

    def test_duplicate_catalogue_rows_merge_by_normalized_strong_id(self):
        resolved = self.resolve([
            feature(imdb="tt0112757", row="first"),
            feature(imdb="112757", tmdb="456", row="second"),
        ])
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.tmdb_id, "456")
        self.assertEqual(self.details.status, "resolved")
        self.assertIn("重复条目已合并", self.details.reason)

    def test_shared_tmdb_only_row_can_join_consistent_imdb_row(self):
        resolved = self.resolve([
            feature(imdb="", tmdb="456", row="first"),
            feature(tmdb="456", row="second"),
        ])
        self.assertEqual(resolved.imdb_id, "0112757")

    def test_different_films_with_equal_title_year_remain_ambiguous(self):
        self.assertIsNone(self.resolve([feature(), feature(imdb="9999999")]))
        self.assertEqual(self.details.status, "ambiguous")

    def test_same_imdb_with_conflicting_tmdb_mapping_is_rejected(self):
        self.assertIsNone(self.resolve([feature(tmdb="11"), feature(tmdb="22")]))
        self.assertIn("ID映射冲突", self.details.reason)

    def test_shared_tmdb_must_not_merge_two_imdb_films(self):
        self.assertIsNone(self.resolve([
            feature(tmdb="11"), feature(imdb="9999999", tmdb="11"),
        ]))
        self.assertIn("ID映射冲突", self.details.reason)

    def test_same_id_conflicting_year_cannot_hide_behind_one_valid_row(self):
        self.assertIsNone(self.resolve([feature(), feature(year="1997")]))
        self.assertIn("年份冲突", self.details.reason)

    def test_same_id_conflicting_episode_cannot_hide_behind_one_valid_row(self):
        title = "Star Wars: Episode IV - A New Hope"
        identity = online.MediaIdentity(title, title, "1977")
        self.assertIsNone(self.resolve([
            feature(title=title, year="1977"),
            feature(title="Star Wars: Episode I - A New Hope", year="1977"),
        ], identity))
        self.assertIn("集数冲突", self.details.reason)

    def test_unsupported_year_missing_id_and_wrong_title_keep_guards(self):
        for rows, status in (
            ([feature(year="1997")], "identity-conflict"),
            ([feature(imdb="")], "missing-id"),
            ([feature(title="The Quick and the Dead")], "no-match"),
        ):
            with self.subTest(status=status):
                self.assertIsNone(self.resolve(rows))
                self.assertEqual(self.details.status, status)

    def test_no_id_duplicates_are_not_merged_by_title(self):
        details = online._FeatureLookupDiagnostics()
        resolved = online._feature_identity_from_response(
            {"data": [feature(imdb=""), feature(imdb="", row="two")]},
            self.identity, diagnostics=details,
        )
        self.assertIsNone(resolved)
        self.assertEqual(details.status, "ambiguous")

    def test_placeholder_provider_ids_are_not_strong_identity(self):
        for value in ("unknown", "0", "tt000", "-1"):
            with self.subTest(value=value):
                self.assertIsNone(self.resolve([feature(imdb=value), feature(imdb=value, row="two")]))
                self.assertEqual(self.details.status, "missing-id")

    def test_subtitle_identity_uses_normalized_id_and_rejects_conflicting_mapping(self):
        identity = online.MediaIdentity("The Cure", "The Cure", "1995", imdb_id="0112757", tmdb_id="456")
        subtitle = candidate()
        subtitle.feature_imdb_id = "tt112757"
        subtitle.feature_tmdb_id = "0456"
        self.assertTrue(online._candidate_matches_strong_identity(subtitle, identity))
        subtitle.feature_imdb_id = "9999999"
        self.assertFalse(online._candidate_matches_strong_identity(subtitle, identity))


class FeatureLookupDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.identity = online.MediaIdentity("The Cure", "The Cure", "1995")

    def search(self, response, pages=None, *, strict=True):
        with patch.object(online, "filename_container_identity_conflict", return_value=""), \
             patch.object(online, "identify_media", return_value=self.identity), \
             patch.object(online, "opensubtitles_movie_hash", return_value=""), \
             patch.object(online, "_request", side_effect=response if isinstance(response, list) else None,
                          return_value=response if not isinstance(response, list) else None), \
             patch.object(online, "_search_pages", side_effect=pages if callable(pages) else None,
                          return_value=pages if pages is not None and not callable(pages) else ([candidate()], 150, True)):
            return online.search("test-key", "The Cure (1995).mkv", "en", skip_hash=strict)

    def test_no_matching_feature_explains_title_year_fallback_and_truncation(self):
        identity, results, meta = self.search({"data": []})
        self.assertIs(identity, self.identity)
        self.assertEqual(meta.query_mode, "title-year")
        self.assertEqual(meta.feature_lookup_status, "no-match")
        self.assertIn("无可锁定身份", meta.fallback_reason)
        self.assertEqual(len(meta.feature_lookup_attempts), 1)
        self.assertEqual(meta.total_count, 150)
        self.assertTrue(meta.truncated)

    def test_missing_id_explains_fallback_without_silently_losing_reason(self):
        _, _, meta = self.search({"data": [feature(imdb="")]})
        self.assertEqual(meta.feature_lookup_status, "missing-id")
        self.assertIn("缺少 IMDb/TMDB ID", meta.fallback_reason)

    def test_resolved_id_but_no_subtitles_has_distinct_fallback_reason(self):
        seen = []
        def pages(_key, params, _language, **_kwargs):
            seen.append(params)
            return ([], 0, False) if "imdb_id" in params else ([candidate()], 1, False)
        _, _, meta = self.search({"data": [feature()]}, pages)
        self.assertEqual(meta.feature_lookup_status, "resolved")
        self.assertIn("该身份未返回字幕", meta.fallback_reason)
        self.assertIn("imdb_id", seen[0])
        self.assertEqual(seen[1]["year"], "1995")

    def test_nonfatal_request_error_is_visible_and_does_not_expose_key(self):
        _, _, meta = self.search([RuntimeError("network rejected test-key")])
        self.assertEqual(meta.feature_lookup_status, "request-failed")
        self.assertIn("请求失败", meta.fallback_reason)
        self.assertNotIn("test-key", str(meta))

    def test_fatal_authentication_or_quota_error_stops_resolver_immediately(self):
        for error in (online.SubtitleAuthenticationError("credential invalid"),
                      online.SubtitleQuotaError("allowance exhausted")):
            with self.subTest(error=type(error).__name__), \
                 patch.object(online, "_request", side_effect=error) as request:
                with self.assertRaises(type(error)):
                    online._resolve_feature_identity("test-key", self.identity)
                request.assert_called_once()

    def test_request_maps_401_403_and_429_to_terminal_error_types(self):
        for code, expected in ((401, online.SubtitleAuthenticationError),
                               (403, online.SubtitleAuthenticationError),
                               (429, online.SubtitleQuotaError)):
            with self.subTest(code=code):
                error = urllib.error.HTTPError("https://invalid.example", code, "error", {}, io.BytesIO(b"{}"))
                with patch.object(online._IPV4_OPENER, "open", side_effect=error):
                    with self.assertRaises(expected):
                        online._request("test-key", "/features")

    def test_wrong_provider_year_cannot_lock_identity_or_create_an_alias(self):
        with patch.object(online, "settings_path", side_effect=AssertionError("no identity cache write")) as settings:
            _, _, meta = self.search({"data": [feature(year="1997")]}, strict=False)
        self.assertEqual(meta.query_mode, "title-year")
        self.assertEqual(meta.feature_lookup_status, "identity-conflict")
        settings.assert_not_called()

    def test_concurrent_searches_keep_attempt_reasons_separate(self):
        barrier = threading.Barrier(2)
        identities = {name: online.MediaIdentity(name, name, "1995") for name in ("One Film", "Two Film")}
        def request(_key, _path, *, params):
            barrier.wait(timeout=3)
            return {"data": [feature(title=params["query"])]} if params["query"] == "One Film" else {"data": []}
        def identify(path):
            return identities[path]
        with patch.object(online, "filename_container_identity_conflict", return_value=""), \
             patch.object(online, "identify_media", side_effect=identify), \
             patch.object(online, "_request", side_effect=request), \
             patch.object(online, "_search_pages", return_value=([candidate()], 1, False)):
            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(lambda name: online.search("test-key", name, "en", skip_hash=True), identities))
        first, second = (result[2] for result in results)
        self.assertEqual(first.feature_lookup_status, "resolved")
        self.assertEqual(second.feature_lookup_status, "no-match")
        self.assertIn("One Film", first.feature_lookup_attempts[0])
        self.assertNotIn("Two Film", first.feature_lookup_attempts[0])
        self.assertIn("Two Film", second.feature_lookup_attempts[0])


if __name__ == "__main__":
    unittest.main()
