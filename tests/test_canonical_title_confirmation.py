"""Offline sequel fixtures: English guesses do not establish film identity."""
from __future__ import annotations

import concurrent.futures
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import media_title_resolver as resolver
import online_subtitles as online
import subdl_subtitles as subdl


CORRECT = "How to Train Your Dragon 2"
WRONG = "How to Train Your Dragon: The Hidden World"
LOCAL = "驯龙高手2"


def os_feature(title=CORRECT, year="2014", imdb="1646971"):
    return {"id": "film", "type": "movie", "attributes": {
        "title": title, "original_title": title, "year": year,
        "imdb_id": imdb, "feature_type": "Movie",
    }}


def os_subtitle(title=CORRECT, year="2014", imdb="1646971"):
    return online.SubtitleCandidate(
        11, "film.srt", title, "en", 0, 0, False, False,
        feature_title=title, feature_year=year, feature_imdb_id=imdb,
    )


def sd_movie(title=CORRECT, year=2014, film_id="sd100"):
    return {"sd_id": film_id, "name": title, "original_name": title,
            "year": year, "imdb_id": "1646971"}


SD_SUBTITLES = {"subtitles": [{"id": "s1", "name": "film.srt",
                              "url": "/film.zip", "language": "EN"}]}


class CanonicalTitleConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name) / "settings.json"
        self.config.write_text(json.dumps({"opensubtitles_api_key": "old-os",
                                          "subdl_api_key": "old-sd",
                                          "custom_preference": "keep"}), encoding="utf-8")

    def payload(self):
        return json.loads(self.config.read_text(encoding="utf-8"))

    def common(self, provider):
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(provider, "settings_path", return_value=self.config))
        stack.enter_context(patch.object(provider, "filename_container_identity_conflict", return_value=""))
        cls = provider.MediaIdentity
        identity = cls(LOCAL, LOCAL, "2014")
        stack.enter_context(patch.object(provider, "identify_media", return_value=identity))
        if provider is online:
            stack.enter_context(patch.object(online, "opensubtitles_movie_hash", return_value=""))
        return stack, identity

    def test_model_hidden_world_wrong_year_falls_back_to_native_query(self):
        stack, base = self.common(online)
        queries = []
        def request(_key, _path, *, params):
            queries.append(params["query"])
            return {"data": [os_feature(WRONG, "2018") if params["query"] == WRONG else os_feature()]}
        stack.enter_context(patch.object(online, "resolve_canonical_title", return_value=WRONG))
        stack.enter_context(patch.object(online, "_request", side_effect=request))
        pages = stack.enter_context(patch.object(online, "_search_pages", return_value=([os_subtitle()], 1, False)))
        identity, results, meta = online.search("fixture-key", "film.mkv", "en")
        self.assertEqual(queries, [WRONG, LOCAL])
        self.assertEqual(pages.call_args.args[1]["imdb_id"], "1646971")
        self.assertEqual(identity.original_title, CORRECT)
        self.assertEqual(base.original_title, LOCAL)
        self.assertTrue(results[0].identity_verified)
        self.assertIn("年份冲突", meta.fallback_reason)
        self.assertIn("模型", meta.feature_lookup_attempts[0])
        self.assertEqual(self.payload()["title_aliases"]["驯龙高手2|2014"], CORRECT)
        self.assertEqual(self.payload()["title_alias_rejections"]["驯龙高手2|2014"]["title"], WRONG)
        self.assertEqual(self.payload()["custom_preference"], "keep")

    def test_old_bad_alias_is_a_proposal_and_gets_replaced_after_confirmation(self):
        payload = self.payload()
        payload["title_aliases"] = {"驯龙高手2|2014": WRONG, "驯龙高手|2010": "How to Train Your Dragon"}
        self.config.write_text(json.dumps(payload), encoding="utf-8")
        stack, _base = self.common(online)
        stack.enter_context(patch.object(online, "_request", side_effect=lambda _key, _path, *, params:
                                        {"data": [os_feature(WRONG, "2018") if params["query"] == WRONG else os_feature()]}))
        stack.enter_context(patch.object(online, "_search_pages", return_value=([os_subtitle()], 1, False)))
        with patch("subtitle_tool_core.call_ollama_retry", side_effect=AssertionError("cache must avoid model")):
            identity, _, meta = online.search("fixture-key", "film.mkv", "en")
        self.assertEqual(identity.original_title, CORRECT)
        self.assertIn("缓存", meta.feature_lookup_attempts[0])
        self.assertEqual(self.payload()["title_aliases"]["驯龙高手2|2014"], CORRECT)

    def test_proven_rejection_is_shared_with_second_provider(self):
        stack, _base = self.common(online)
        stack.enter_context(patch.object(online, "resolve_canonical_title", return_value=WRONG))
        stack.enter_context(patch.object(online, "_request", side_effect=lambda _key, _path, *, params:
                                        {"data": [os_feature(WRONG, "2018")]} if params["query"] == WRONG else {"data": []}))
        stack.enter_context(patch.object(online, "_search_pages", return_value=([], 0, False)))
        _, results, _meta = online.search("fixture-key", "film.mkv", "en")
        self.assertEqual(results, [])
        self.assertEqual(self.payload()["title_alias_rejections"]["驯龙高手2|2014"]["title"], WRONG)
        payload = self.payload()
        payload["title_aliases"] = {"驯龙高手|2010": "How to Train Your Dragon"}
        self.config.write_text(json.dumps(payload), encoding="utf-8")
        sd_stack, _base = self.common(subdl)
        queries = []
        def request(_key, path, *, params):
            if path == "/movies/search":
                queries.append(params["q"])
                return {"results": [sd_movie()]}
            return SD_SUBTITLES
        sd_stack.enter_context(patch.object(subdl, "_request_v2", side_effect=request))
        with patch("subtitle_tool_core.call_ollama_retry", side_effect=AssertionError("do not repeat disproven model title")):
            identity, results, meta = subdl.search("fixture-key", "film.mkv", "en")
        self.assertEqual(queries, [CORRECT])
        self.assertEqual(identity.original_title, CORRECT)
        self.assertEqual(len(results), 1)
        self.assertIn("已确认系列别名", meta.feature_lookup_attempts[0])

    def test_explicit_sequel_number_conflict_skips_bad_english_query(self):
        stack, _base = self.common(online)
        stack.enter_context(patch.object(online, "resolve_canonical_title", return_value="How to Train Your Dragon 3"))
        request = stack.enter_context(patch.object(online, "_request", return_value={"data": [os_feature()]}))
        stack.enter_context(patch.object(online, "_search_pages", return_value=([os_subtitle()], 1, False)))
        identity, _results, meta = online.search("fixture-key", "film.mkv", "en")
        request.assert_called_once()
        self.assertEqual(request.call_args.kwargs["params"]["query"], LOCAL)
        self.assertEqual(identity.original_title, CORRECT)
        self.assertIn("续集编号冲突", meta.fallback_reason)

    def test_model_failure_still_queries_original_native_name(self):
        stack, _base = self.common(online)
        stack.enter_context(patch.object(online, "resolve_canonical_title", return_value=""))
        request = stack.enter_context(patch.object(online, "_request", return_value={"data": [os_feature()]}))
        stack.enter_context(patch.object(online, "_search_pages", return_value=([os_subtitle()], 1, False)))
        identity, _results, meta = online.search("fixture-key", "film.mkv", "en")
        self.assertEqual(request.call_args.kwargs["params"]["query"], LOCAL)
        self.assertEqual(identity.original_title, CORRECT)
        self.assertEqual(meta.query_mode, "feature")

    def test_native_response_requires_unique_original_year_and_installment(self):
        identity = online.MediaIdentity(LOCAL, LOCAL, "2014")
        for rows in ([os_feature(year="2015")],
                     [os_feature("How to Train Your Dragon 3")],
                     [os_feature(), os_feature("Unrelated 2", imdb="999")]):
            with self.subTest(rows=rows):
                self.assertIsNone(online._feature_identity_from_response({"data": rows}, identity))

    def test_native_catalogue_title_uses_its_confirmed_english_original_name(self):
        row = os_feature()
        row["attributes"]["title"] = LOCAL
        identity = online.MediaIdentity(LOCAL, LOCAL, "2014")
        resolved = online._feature_identity_from_response({"data": [row]}, identity)
        self.assertEqual(resolved.original_title, CORRECT)

    def test_conflicting_duplicate_installments_do_not_merge_by_shared_id(self):
        identity = online.MediaIdentity(LOCAL, CORRECT, "2014")
        self.assertIsNone(online._feature_identity_from_response({"data": [
            os_feature(), os_feature("How to Train Your Dragon 3"),
        ]}, identity))

    def test_strong_id_cannot_override_clear_year_conflict(self):
        identity = online.MediaIdentity(WRONG, WRONG, "2014")
        self.assertIsNone(online._feature_identity_from_response(
            {"data": [os_feature(WRONG, "2018", "2386490")]}, identity))

    def test_normal_english_adjacent_release_year_is_still_accepted(self):
        identity = online.MediaIdentity("V for Vendetta", "V for Vendetta", "2005")
        result = online._feature_identity_from_response({"data": [os_feature("V for Vendetta", "2006")]}, identity)
        self.assertIsNotNone(result)
        self.assertEqual(result.year, "2005")

    def test_empty_subtitles_do_not_write_confirmed_alias(self):
        stack, _base = self.common(online)
        stack.enter_context(patch.object(online, "resolve_canonical_title", return_value=CORRECT))
        stack.enter_context(patch.object(online, "_request", return_value={"data": [os_feature()]}))
        stack.enter_context(patch.object(online, "_search_pages", return_value=([], 0, False)))
        online.search("fixture-key", "film.mkv", "en")
        self.assertNotIn("title_aliases", self.payload())
        self.assertNotIn("title_alias_rejections", self.payload())

    def test_unconfirmed_or_missing_year_does_not_write_alias(self):
        stack, _base = self.common(online)
        stack.enter_context(patch.object(online, "resolve_canonical_title", return_value=CORRECT))
        stack.enter_context(patch.object(online, "_request", return_value={"data": [os_feature(year="")]}))
        stack.enter_context(patch.object(online, "_search_pages", return_value=([os_subtitle(year="")], 1, False)))
        _identity, results, _meta = online.search("fixture-key", "film.mkv", "en")
        self.assertEqual(results, [])
        self.assertNotIn("title_aliases", self.payload())
        self.assertNotIn("title_alias_rejections", self.payload())

    def test_network_failures_do_not_permanently_reject_a_title(self):
        stack, _base = self.common(online)
        stack.enter_context(patch.object(online, "resolve_canonical_title", return_value=CORRECT))
        stack.enter_context(patch.object(online, "_request", side_effect=RuntimeError("fixture network unavailable")))
        stack.enter_context(patch.object(online, "_search_pages", return_value=([], 0, False)))
        online.search("fixture-key", "film.mkv", "en")
        self.assertNotIn("title_alias_rejections", self.payload())

    def test_subdl_wrong_year_falls_back_to_native_and_returns_confirmed_identity(self):
        stack, base = self.common(subdl)
        stack.enter_context(patch.object(subdl, "resolve_canonical_title", return_value=WRONG))
        queries = []
        def request(_key, path, *, params):
            if path == "/movies/search":
                queries.append(params["q"])
                return {"results": [sd_movie(WRONG, 2018) if params["q"] == subdl._safe_search_text(WRONG) else sd_movie()]}
            self.assertEqual(params["sd_id"], "sd100")
            return SD_SUBTITLES
        stack.enter_context(patch.object(subdl, "_request_v2", side_effect=request))
        identity, results, meta = subdl.search("fixture-key", "film.mkv", "en")
        self.assertEqual(queries, [subdl._safe_search_text(WRONG), LOCAL])
        self.assertEqual(identity.original_title, CORRECT)
        self.assertEqual(base.original_title, LOCAL)
        self.assertEqual(meta.query_mode, "movie-id")
        self.assertEqual(results[0].feature_year, "2014")
        self.assertIn("年份", meta.fallback_reason)

    def test_subdl_missing_year_cannot_lock_or_cache_a_localized_proposal(self):
        stack, _base = self.common(subdl)
        stack.enter_context(patch.object(subdl, "resolve_canonical_title", return_value=CORRECT))
        stack.enter_context(patch.object(subdl, "_request_v2", return_value={"results": [sd_movie(year="")]}))
        _identity, results, meta = subdl.search("fixture-key", "film.mkv", "en")
        self.assertEqual(results, [])
        self.assertEqual(meta.query_mode, "unresolved-title")
        self.assertNotIn("title_aliases", self.payload())
        self.assertNotIn("title_alias_rejections", self.payload())

    def test_subdl_known_id_without_subtitles_does_not_cache_alias(self):
        stack, _base = self.common(subdl)
        stack.enter_context(patch.object(subdl, "resolve_canonical_title", return_value=CORRECT))
        stack.enter_context(patch.object(subdl, "_request_v2", side_effect=lambda _key, path, *, params:
                                        {"results": [sd_movie()]} if path == "/movies/search" else {"subtitles": []}))
        _identity, results, meta = subdl.search("fixture-key", "film.mkv", "en")
        self.assertEqual(results, [])
        self.assertEqual(meta.query_mode, "movie-id")
        self.assertNotIn("title_aliases", self.payload())

    def test_subdl_unlocked_search_does_not_claim_movie_id_mode(self):
        stack, _base = self.common(subdl)
        stack.enter_context(patch.object(subdl, "resolve_canonical_title", return_value=""))
        stack.enter_context(patch.object(subdl, "_request_v2", return_value={"results": []}))
        _identity, results, meta = subdl.search("fixture-key", "film.mkv", "en")
        self.assertEqual(results, [])
        self.assertEqual(meta.query_mode, "unresolved-title")

    def test_subdl_english_adjacent_year_and_typo_alias_remain_supported(self):
        identity = subdl.MediaIdentity("Transcendence", "Transcendence", "2014")
        movie = sd_movie("Transcendance", 2015)
        self.assertIs(subdl._choose_movie_without_score(identity, [movie]), movie)

    def test_subdl_keeps_the_name_that_matched_instead_of_provider_suffix(self):
        identity = subdl.MediaIdentity("V for Vendetta", "V for Vendetta", "2005")
        movie = sd_movie("V for Vendetta", 2006)
        movie["original_name"] = "V for Vendetta At the IMAX"
        self.assertIs(subdl._choose_movie_without_score(identity, [movie]), movie)
        self.assertEqual(subdl._resolved_movie_name(identity, movie), "V for Vendetta")

    def test_alias_writer_requires_independent_confirmed_year(self):
        resolver.remember_canonical_title(LOCAL, "2014", WRONG, self.config)
        resolver.remember_canonical_title(LOCAL, "2014", WRONG, self.config, confirmed_year="2018")
        self.assertNotIn("title_aliases", self.payload())
        resolver.remember_canonical_title(LOCAL, "2014", CORRECT, self.config, confirmed_year="2014")
        self.assertNotIn("title_aliases", self.payload())
        resolver.remember_canonical_title(LOCAL, "2014", CORRECT, self.config,
                                         confirmed_year="2014", confirmed_identity="imdb:1646971")
        self.assertEqual(self.payload()["title_aliases"]["驯龙高手2|2014"], CORRECT)

    def test_key_saves_and_alias_saves_share_lock_and_keep_other_settings(self):
        with patch.object(online, "settings_path", return_value=self.config), \
             patch.object(subdl, "settings_path", return_value=self.config), \
             concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
            tasks = [pool.submit(online.save_settings, "new-os"),
                     pool.submit(subdl.save_settings, "new-sd"),
                     pool.submit(resolver.remember_canonical_title, LOCAL, "2014", CORRECT,
                                 self.config, confirmed_year="2014", confirmed_identity="imdb:1646971")]
            for task in tasks:
                task.result(timeout=3)
        payload = self.payload()
        self.assertEqual(payload["opensubtitles_api_key"], "new-os")
        self.assertEqual(payload["subdl_api_key"], "new-sd")
        self.assertEqual(payload["title_aliases"]["驯龙高手2|2014"], CORRECT)
        self.assertEqual(payload["custom_preference"], "keep")


if __name__ == "__main__":
    unittest.main()
