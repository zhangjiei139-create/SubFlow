"""Bounded catalogue aliases and anonymous-row download guards (offline)."""
from dataclasses import replace
import unittest
from unittest.mock import patch

import media_title_resolver as titles
import online_subtitles as online
import smart_subtitles as smart
import subdl_subtitles as subdl
from subtitle_identity_guard import clean_video_title


LOCAL = "Godzilla VS Kong The New Empire"
CANONICAL = "Godzilla x Kong: The New Empire"


def feature(title=CANONICAL, year="2024", imdb="14539740", tmdb="823464"):
    return {"id": "1784214", "type": "movie", "attributes": {
        "title": title, "original_title": title, "year": year,
        "imdb_id": imdb, "tmdb_id": tmdb, "feature_type": "Movie",
    }}


def candidate(**kwargs):
    defaults = dict(file_id=8732768, file_name="Godzilla.X.Kong.The.New.Empire.2024.srt",
                    release="Godzilla.X.Kong.The.New.Empire.2024.BluRay.x264.AAC5.1-[YTS.MX]",
                    language="en", downloads=0, rating=0, trusted=False, hearing_impaired=False,
                    feature_title="2024 - " + CANONICAL, feature_year="2024",
                    feature_imdb_id="14539740", feature_tmdb_id="823464")
    defaults.update(kwargs)
    return online.SubtitleCandidate(**defaults)


class CatalogueAliasTests(unittest.TestCase):
    def setUp(self):
        self.identity = online.MediaIdentity(LOCAL, LOCAL, "2024")

    def test_fullwidth_filename_and_numeric_titles(self):
        self.assertEqual(clean_video_title(LOCAL + "（2024）BluRay.2160p.mkv"), (LOCAL, "2024"))
        self.assertEqual(clean_video_title("2001_A_Space_Odyssey_1968_BluRay.mkv"),
                         ("2001 A Space Odyssey", "1968"))
        for left, right in (("1917", "1918"), ("Blade Runner 2049", "Blade Runner"),
                            ("2001: A Space Odyssey", "A Space Odyssey")):
            self.assertFalse(online._titles_equivalent(left, right))
        self.assertFalse(online._titles_typo_equivalent("1917", "1918"))
        self.assertFalse(online._titles_typo_equivalent("Movie Part II", "Movie Part III"))

    def test_connector_x_is_not_episode_ten(self):
        self.assertIn("X", online._identity_title_tokens(CANONICAL))
        for left, right in (("Star Wars Episode X A New Hope", "Star Wars Episode 10 A New Hope"),
                            ("Fast X", "Fast 10"), ("Movie Part II", "Movie Part 2")):
            self.assertTrue(online._titles_equivalent(left, right))
        self.assertFalse(online._titles_equivalent("Movie Part II", "Movie Part III"))
        self.assertFalse(online._titles_equivalent(LOCAL, CANONICAL))
        self.assertFalse(titles.controlled_connector_alias("Godzilla vs Kong", "Godzilla x Kong"))

    def test_connector_alias_needs_exact_year_unique_strong_id(self):
        for strict in (False, True):
            resolved = online._feature_identity_from_response(
                {"data": [feature()]}, self.identity, strict=strict)
            self.assertEqual(resolved.imdb_id, "14539740")
            self.assertEqual(resolved.original_title, CANONICAL)
            self.assertFalse(smart._candidate_identity_conflict(candidate(), resolved))
        for rows in ([feature(year="2021")], [feature(year="2023")], [feature(year="")],
                     [feature(imdb="", tmdb="")], [feature(title="Godzilla vs. Kong", year="2021")],
                     [feature(title="Godzilla x Kong")], [feature(title="Godzilla x Kong The Old Empire")],
                     [feature(), feature(imdb="999", tmdb="998")],
                     [feature(), feature(year="2021")], [feature(), feature(tmdb="999")]):
            with self.subTest(rows=rows):
                self.assertIsNone(online._feature_identity_from_response({"data": rows}, self.identity))

    def test_lookup_is_bounded_and_uses_canonical_id_for_english_subtitles(self):
        requests = []
        def request(_key, path, *, params):
            requests.append((path, params))
            return {"data": [feature()]} if " X " in params["query"] else {"data": []}
        with patch.object(online, "filename_container_identity_conflict", return_value=""), \
             patch.object(online, "identify_media", return_value=self.identity), \
             patch.object(online, "_request", side_effect=request), \
             patch.object(online, "_search_pages", return_value=([candidate()], 1, False)) as pages:
            resolved, results, meta = online.search("key", "film.mkv", "en", skip_hash=True)
        self.assertEqual(len(requests), 2)
        self.assertNotIn("query_match", requests[1][1])
        self.assertEqual(pages.call_args.args[1], {"languages": "en", "imdb_id": "14539740"})
        self.assertTrue(results[0].identity_verified)
        self.assertEqual(meta.query_mode, "feature")
        self.assertFalse(smart._candidate_identity_conflict(results[0], resolved))

    def test_subdl_alias_also_requires_exact_year_unique_site_id(self):
        identity = subdl.MediaIdentity(LOCAL, LOCAL, "2024")
        movie = {"sd_id": 123, "name": CANONICAL, "year": "2024", "type": "movie"}
        self.assertEqual(subdl._choose_movie_without_score(identity, [movie]), movie)
        self.assertEqual(subdl._resolved_movie_name(identity, dict(movie, original_name="Unrelated Film")), CANONICAL)
        self.assertEqual(subdl._movie_queries(identity)[1], "GODZILLA X KONG THE NEW EMPIRE")
        for rows in ([dict(movie, year="2021")], [dict(movie, year="2023")],
                     [dict(movie, sd_id="unknown")], [dict(movie, type="tv")],
                     [movie, dict(movie, sd_id=456)], [movie, dict(movie, year="2021")],
                     [dict(movie, imdb_id="14539740"), dict(movie, imdb_id="5034838")]):
            self.assertIsNone(subdl._choose_movie_without_score(identity, rows))
        with patch.object(subdl, "_request_v2", return_value={"results": [movie, dict(movie, year="2021")]}) as request:
            self.assertIsNone(subdl._resolve_movie("key", identity))
        self.assertEqual(request.call_count, len(subdl._movie_queries(identity)))


class CandidateEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.identity = online.MediaIdentity(LOCAL, CANONICAL, "2024", imdb_id="14539740", tmdb_id="823464")

    def test_current_provider_placeholder_movie_does_not_admit_prince_of_tennis(self):
        # These are fields observed in the 2026-10-10 live replay, not an asserted
        # reconstruction of the earlier failed request's full JSON payload.
        prince = candidate(feature_title="2024 - Empty Movie (SubScene)", feature_id="2797937",
                           feature_type="Movie", feature_imdb_id="", feature_tmdb_id="",
                           release="The.Prince.of.Tennis.II.U17.World.Cup.Semi-final.S01E01",
                           file_name="PrinceTennis.srt", identity_verified=True)
        self.assertIn("占位", smart._candidate_identity_conflict(prince, self.identity))
        self.assertTrue(smart._candidate_identity_conflict(replace(prince, feature_title=""), self.identity))

    def test_hash_and_matching_ids_can_confirm_missing_metadata_but_flag_alone_cannot(self):
        unknown = candidate(feature_title="", feature_imdb_id="", feature_tmdb_id="",
                            release="anonymous", file_name="subtitle.srt", identity_verified=True)
        self.assertTrue(smart._candidate_identity_conflict(unknown, self.identity))
        self.assertFalse(smart._candidate_identity_conflict(replace(unknown, moviehash_match=True), self.identity))
        self.assertFalse(smart._candidate_identity_conflict(replace(unknown, feature_imdb_id="14539740"), self.identity))
        self.assertTrue(smart._candidate_identity_conflict(replace(unknown, feature_imdb_id="5034838"), self.identity))
        self.assertTrue(smart._candidate_identity_conflict(candidate(feature_tmdb_id="399566"), self.identity))

    def test_legitimate_no_id_titles_and_localized_aliases_stay_eligible(self):
        identity = online.MediaIdentity("Fury", "Fury", "2014")
        for name in ("Fury", "Fury: Extended Cut", "Corazones de acero", ""):
            item = candidate(feature_title=name, feature_year="2014", feature_imdb_id="", feature_tmdb_id="",
                             release="Fury.2014.BluRay", file_name="fury.srt")
            self.assertFalse(smart._candidate_identity_conflict(item, identity))
        self.assertFalse(smart._candidate_identity_conflict(
            candidate(feature_imdb_id="", feature_tmdb_id=""), self.identity))

    def test_confirmed_connector_reverse_alias_does_not_weaken_year_and_episode_guards(self):
        item = candidate(feature_title=LOCAL, release=LOCAL + " 2024")
        self.assertFalse(smart._candidate_identity_conflict(item, self.identity))
        self.assertTrue(smart._candidate_identity_conflict(replace(item, feature_imdb_id="", feature_tmdb_id=""), self.identity))
        wrong_year = replace(item, feature_year="2021", release=LOCAL + " 2021")
        self.assertTrue(smart._candidate_identity_conflict(wrong_year, self.identity))

    def test_broad_search_shared_tail_word_does_not_admit_the_empire(self):
        unresolved = online.MediaIdentity(LOCAL, LOCAL, "2024")
        empire = candidate(feature_title="2024 - The Empire", release="The.Empire.2024.WEB.H264",
                           file_name="empire.srt", feature_imdb_id="17375596", feature_tmdb_id="929831")
        self.assertIn("局部", smart._candidate_identity_conflict(empire, unresolved))
        self.assertIn("局部", smart._candidate_identity_conflict(
            replace(empire, release="L'Empire.2024.FRENCH", file_name="film.srt"), unresolved))
        self.assertFalse(smart._candidate_identity_conflict(
            replace(empire, release=LOCAL + " 2024"), unresolved))
        self.assertFalse(smart._candidate_identity_conflict(
            replace(empire, feature_imdb_id="14539740", feature_tmdb_id="823464"), self.identity))
        star_wars = online.MediaIdentity("Star Wars Episode IV A New Hope", "Star Wars Episode IV A New Hope", "1977")
        self.assertFalse(smart._candidate_identity_conflict(candidate(
            feature_title="Star Wars", release="Star.Wars.1977", file_name="film.srt",
            feature_year="1977", feature_imdb_id="", feature_tmdb_id=""), star_wars))

    def test_tv_metadata_survives_provider_parsing_and_blocks_even_with_matching_ids(self):
        for field, value in (("feature_type", "TvEpisode"), ("season_number", 1),
                             ("episode_number", 1), ("parent_imdb_id", 42), ("parent_tmdb_id", 43)):
            response = {"data": [{"attributes": {"release": CANONICAL, "files": [{"file_id": 1}],
                         "feature_details": {"movie_name": CANONICAL, "year": "2024", "imdb_id": "14539740",
                                             field: value}}}]}
            parsed = online._candidates_from_response(response, "en")[0]
            self.assertIn("剧集", smart._candidate_identity_conflict(parsed, self.identity))


if __name__ == "__main__":
    unittest.main()
