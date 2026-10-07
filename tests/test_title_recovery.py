"""Offline recovery fixtures: wrong aliases must not disable a whole movie."""
from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import media_title_resolver as resolver
import online_subtitles as online
import subdl_subtitles as subdl

from tests.test_canonical_title_confirmation import (
    CORRECT, LOCAL, WRONG, SD_SUBTITLES, os_feature, os_subtitle, sd_movie,
)


class TitleRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Path(self.temp.name) / "settings.json"
        self.write({"custom_preference": "keep"})
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch("subtitle_tool_core.begin_ollama_lease"))
        self.stack.enter_context(patch("subtitle_tool_core.end_ollama_lease", return_value=False))
        self.stack.enter_context(patch("subtitle_tool_core.ensure_ollama_running"))
        self.model = self.stack.enter_context(patch("subtitle_tool_core.call_ollama_retry", return_value="UNKNOWN"))
        for provider in (online, subdl):
            self.stack.enter_context(patch.object(provider, "settings_path", return_value=self.config))
            self.stack.enter_context(patch.object(provider, "filename_container_identity_conflict", return_value=""))
            self.stack.enter_context(patch.object(provider, "identify_media", return_value=provider.MediaIdentity(LOCAL, LOCAL, "2014")))
        self.stack.enter_context(patch.object(online, "opensubtitles_movie_hash", return_value=""))

    def write(self, payload):
        self.config.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def payload(self):
        return json.loads(self.config.read_text(encoding="utf-8"))

    def legacy_rejection(self, *, series=True):
        payload = self.payload()
        payload["title_alias_rejections"] = {"驯龙高手2|2014": {"title": WRONG, "reason": "年份冲突"}}
        if series:
            payload["title_aliases"] = {"驯龙高手|2010": "How to Train Your Dragon"}
        self.write(payload)

    def test_legacy_single_rejection_allows_known_series_number_proposal(self):
        self.legacy_rejection()
        self.assertEqual(resolver.resolve_canonical_title(LOCAL, "2014", self.config), CORRECT)
        self.model.assert_not_called()
        self.assertNotIn("驯龙高手2|2014", self.payload()["title_aliases"])
        self.assertEqual(resolver.rejected_canonical_titles(LOCAL, "2014", self.config), (WRONG,))

    def test_series_recovery_is_generic_not_a_dragon_exception(self):
        self.write({"title_aliases": {"玩具总动员|1995": "Toy Story"}})
        self.assertEqual(resolver.resolve_canonical_title("玩具总动员3", "2010", self.config), "Toy Story 3")
        self.model.assert_not_called()

    def test_ambiguous_series_names_do_not_choose_arbitrarily(self):
        self.write({"title_aliases": {"玩具总动员|1995": "Toy Story", "玩具总动员2|1999": "Wrong Franchise 2"}})
        self.assertEqual(resolver.resolve_canonical_title("玩具总动员3", "2010", self.config), "")
        self.model.assert_called_once()

    def test_legacy_rejection_reprompts_with_year_number_and_exclusion(self):
        self.legacy_rejection(series=False)
        self.model.return_value = CORRECT
        self.assertEqual(resolver.resolve_canonical_title(LOCAL, "2014", self.config), CORRECT)
        prompt = self.model.call_args.args[0]
        self.assertIn("Release year: 2014", prompt)
        self.assertIn("Explicit installment number: 2", prompt)
        self.assertIn(WRONG, prompt)
        self.assertNotIn("title_aliases", self.payload())

    def test_model_cannot_return_disproven_title_or_wrong_installment(self):
        for response in (WRONG, "How to Train Your Dragon 3"):
            with self.subTest(response=response):
                resolver._MODEL_PROPOSALS.clear()
                self.legacy_rejection(series=False)
                self.model.return_value = response
                self.assertEqual(resolver.resolve_canonical_title(LOCAL, "2014", self.config), "")

    def test_rejection_blacklist_preserves_multiple_bad_names_and_good_alias(self):
        self.legacy_rejection()
        resolver.reject_canonical_title(LOCAL, "2014", "How to Train Your Dragon 3", "续集冲突", self.config)
        self.assertEqual(set(resolver.rejected_canonical_titles(LOCAL, "2014", self.config)), {WRONG, "How to Train Your Dragon 3"})
        resolver.remember_canonical_title(LOCAL, "2014", CORRECT, self.config,
                                         confirmed_year="2014", confirmed_identity="imdb:1646971")
        self.assertEqual(resolver.resolve_canonical_title(LOCAL, "2014", self.config), CORRECT)
        self.assertIn(WRONG, resolver.rejected_canonical_titles(LOCAL, "2014", self.config))
        self.assertEqual(self.payload()["custom_preference"], "keep")

    def test_opensubtitles_recovers_second_film_from_old_rejection(self):
        self.legacy_rejection()
        request = self.stack.enter_context(patch.object(online, "_request", return_value={"data": [os_feature()]}))
        self.stack.enter_context(patch.object(online, "_search_pages", return_value=([os_subtitle()], 1, False)))
        identity, results, meta = online.search("fixture", "film.mkv", "en")
        self.assertEqual(request.call_args.kwargs["params"]["query"], CORRECT)
        self.assertEqual(identity.original_title, CORRECT)
        self.assertTrue(results[0].identity_verified)
        self.assertEqual(meta.query_mode, "feature")
        self.assertEqual(self.payload()["title_aliases"]["驯龙高手2|2014"], CORRECT)
        self.model.assert_not_called()

    def test_opensubtitles_missing_catalogue_id_cannot_accept_or_cache_proposal(self):
        self.legacy_rejection()
        self.stack.enter_context(patch.object(online, "_request", return_value={"data": [os_feature(imdb="")]}))
        self.stack.enter_context(patch.object(online, "_search_pages", return_value=([os_subtitle(imdb="")], 1, False)))
        _, results, _ = online.search("fixture", "film.mkv", "en")
        self.assertEqual(results, [])
        self.assertNotIn("驯龙高手2|2014", self.payload()["title_aliases"])

    def test_opensubtitles_broad_metadata_requires_one_consistent_id(self):
        self.legacy_rejection()
        self.stack.enter_context(patch.object(online, "_resolve_feature_identity", return_value=None))
        self.stack.enter_context(patch.object(online, "_search_pages", return_value=(
            [os_subtitle(), os_subtitle(imdb="99999")], 2, False)))
        _, results, _ = online.search("fixture", "film.mkv", "en")
        self.assertEqual(results, [])
        self.assertNotIn("驯龙高手2|2014", self.payload()["title_aliases"])

    def test_unrelated_third_film_cannot_poison_correct_proposal_blacklist(self):
        self.legacy_rejection()
        self.stack.enter_context(patch.object(online, "_request", return_value={
            "data": [os_feature("How to Train Your Dragon 3", "2019")],
        }))
        self.stack.enter_context(patch.object(online, "_search_pages", return_value=([], 0, False)))
        _, results, _ = online.search("fixture", "film.mkv", "en")
        self.assertEqual(results, [])
        self.assertNotIn(CORRECT, resolver.rejected_canonical_titles(LOCAL, "2014", self.config))
        self.assertNotIn("驯龙高手2|2014", self.payload()["title_aliases"])

    def test_subdl_recovers_second_film_from_old_rejection(self):
        self.legacy_rejection()
        def request(_key, path, *, params):
            if path == "/movies/search":
                self.assertEqual(params["q"], CORRECT)
                return {"results": [sd_movie()]}
            self.assertEqual(params["sd_id"], "sd100")
            return SD_SUBTITLES
        self.stack.enter_context(patch.object(subdl, "_request_v2", side_effect=request))
        identity, results, _ = subdl.search("fixture", "film.mkv", "en")
        self.assertEqual(identity.original_title, CORRECT)
        self.assertEqual(len(results), 1)
        self.model.assert_not_called()

    def test_subdl_missing_provider_id_cannot_cache_proposal(self):
        self.legacy_rejection()
        self.stack.enter_context(patch.object(subdl, "_request_v2", return_value={"results": [sd_movie(film_id="")]}))
        _, results, _ = subdl.search("fixture", "film.mkv", "en")
        self.assertEqual(results, [])
        self.assertNotIn("驯龙高手2|2014", self.payload()["title_aliases"])

    def test_both_providers_share_model_recovery_and_reject_third_film(self):
        self.model.side_effect = [WRONG, CORRECT]
        queries = []
        def os_request(_key, _path, *, params):
            queries.append(params["query"])
            return {"data": [os_feature(WRONG, "2019")] if params["query"] == WRONG else []}
        self.stack.enter_context(patch.object(online, "_request", side_effect=os_request))
        self.stack.enter_context(patch.object(online, "_search_pages", return_value=([], 0, False)))
        _, results, _ = online.search("fixture", "film.mkv", "en")
        self.assertEqual(results, [])
        self.assertIn(WRONG, resolver.rejected_canonical_titles(LOCAL, "2014", self.config))
        self.assertEqual(self.model.call_count, 2)
        sd_queries = []
        def sd_request(_key, path, *, params):
            if path == "/movies/search":
                sd_queries.append(params["q"])
                return {"results": [sd_movie()]}
            return SD_SUBTITLES
        self.stack.enter_context(patch.object(subdl, "_request_v2", side_effect=sd_request))
        identity, results, _ = subdl.search("fixture", "film.mkv", "en")
        self.assertEqual(sd_queries, [CORRECT])
        self.assertEqual(identity.original_title, CORRECT)
        self.assertEqual(len(results), 1)
        self.assertEqual(self.model.call_count, 2)


if __name__ == "__main__":
    unittest.main()
