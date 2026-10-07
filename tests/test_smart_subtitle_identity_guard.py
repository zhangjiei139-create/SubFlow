from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import online_subtitles
import smart_subtitles
import subdl_subtitles
import subtitle_identity_guard


class SmartSubtitleIdentityGuardTest(unittest.TestCase):
    def test_explicit_cd_parts_are_rejected_before_download(self) -> None:
        for release in (
            "Das.Boot.Directors.Cut.1981.DVDRip.DivX.CD3-MrAD",
            "Das.Boot.Directors.Cut.1981.DVDRip.DivX.CD2-MrAD",
            "Das.Boot.1981.Disc1.English",
        ):
            with self.subTest(release=release):
                candidate = SimpleNamespace(release=release, file_name=release + ".srt")
                self.assertIn("分卷字幕", smart_subtitles._candidate_role_conflict(candidate))
        complete = SimpleNamespace(
            release="Das.Boot.1981.BluRay.DTS-HD.MA.5.1.AC3",
            file_name="Das.Boot.1981.BluRay.srt",
        )
        self.assertEqual(smart_subtitles._candidate_role_conflict(complete), "")

    def test_fury_filename_cannot_be_replaced_by_unrelated_muxer_title(self) -> None:
        path = "FURY.(2014).2160.4K.mkv"
        with patch.object(online_subtitles, "container_title", return_value="RMX BY CHDMON"), \
             patch.object(subdl_subtitles, "container_title", return_value="RMX BY CHDMON"):
            opensubtitles = online_subtitles.identify_media(path)
            subdl = subdl_subtitles.identify_media(path)
        self.assertEqual((opensubtitles.title, opensubtitles.year), ("FURY", "2014"))
        self.assertEqual((subdl.title, subdl.year), ("FURY", "2014"))

    def test_hash_search_discards_unmatched_suggestions_and_retries_title(self) -> None:
        wrong = online_subtitles.SubtitleCandidate(
            1, "Stand.By.Me.Doraemon.srt", "Stand by Me Doraemon (2014)",
            "en", 0, 0.0, False, False, moviehash_match=False,
            feature_title="Stand by Me Doraemon", feature_year="2014",
        )
        correct = online_subtitles.SubtitleCandidate(
            2, "Fury.2014.srt", "Fury 2014", "en", 0, 0.0, False, False,
            feature_title="Fury", feature_year="2014",
        )
        calls = []

        def search_pages(_key, params, _language, **_kwargs):
            calls.append(dict(params))
            return ([wrong] if "moviehash" in params else [correct]), 1, False

        with patch.object(online_subtitles, "container_title", return_value="RMX BY CHDMON"), patch.object(
            online_subtitles, "opensubtitles_movie_hash", return_value="0123456789abcdef"
        ), patch.object(
            online_subtitles, "_resolve_feature_identity", return_value=None
        ), patch.object(online_subtitles, "_search_pages", side_effect=search_pages):
            _resolved, results, meta = online_subtitles.search("key", "FURY.2014.mkv", "en")

        self.assertEqual(calls[0]["moviehash_match"], "only")
        self.assertEqual(meta.query_mode, "title-year")
        self.assertEqual(calls[-1]["query"], "FURY")
        self.assertEqual(calls[-1]["year"], "2014")
        self.assertEqual(results, [correct])
        self.assertFalse(wrong.identity_verified)

    def test_hash_search_keeps_only_provider_confirmed_matches(self) -> None:
        identity = online_subtitles.MediaIdentity("FURY", "FURY", "2014")
        wrong = online_subtitles.SubtitleCandidate(
            1, "Stand.By.Me.Doraemon.srt", "Stand by Me Doraemon", "en",
            0, 0.0, False, False, moviehash_match=False,
        )
        correct = online_subtitles.SubtitleCandidate(
            2, "Fury.2014.srt", "Fury 2014", "en",
            0, 0.0, False, False, moviehash_match=True,
        )
        with patch.object(online_subtitles, "identify_media", return_value=identity), patch.object(
            online_subtitles, "opensubtitles_movie_hash", return_value="0123456789abcdef"
        ), patch.object(
            online_subtitles, "_search_pages", return_value=([wrong, correct], 2, False)
        ):
            _resolved, results, meta = online_subtitles.search("key", "FURY.2014.mkv", "en")

        self.assertEqual(meta.query_mode, "hash")
        self.assertEqual(results, [correct])
        self.assertTrue(correct.identity_verified)
        self.assertFalse(wrong.identity_verified)

    def test_explicit_other_movie_is_rejected_before_download(self) -> None:
        identity = online_subtitles.MediaIdentity("FURY", "FURY", "2014")
        wrong = online_subtitles.SubtitleCandidate(
            1, "Stand.By.Me.Doraemon.2014.srt", "Stand by Me Doraemon (2014)",
            "en", 0, 0.0, False, False,
            feature_title="Stand by Me Doraemon", feature_year="2014",
        )
        self.assertIn(
            "另一影片", smart_subtitles._candidate_identity_conflict(wrong, identity)
        )
        wrong.identity_verified = True
        self.assertIn("另一影片", smart_subtitles._candidate_identity_conflict(wrong, identity))

    def test_shared_title_word_does_not_admit_a_different_movie(self) -> None:
        identity = online_subtitles.MediaIdentity("FURY", "FURY", "2014")
        for feature_title, release in (
            ("Antboy: Revenge of the Red Fury", "Antboy: Revenge of the Red Fury"),
            ("Antboy: Revenge of the Red Fury", "Antboy 2 NTSC DVD"),
            ("Ardennes Fury", "Ardennes.Fury.2014.HDRip"),
        ):
            with self.subTest(release=release):
                candidate = SimpleNamespace(
                    feature_title=feature_title, release=release,
                    file_name=release + ".srt", identity_verified=False,
                )
                self.assertIn(
                    "另一影片",
                    smart_subtitles._candidate_identity_conflict(candidate, identity),
                )
        for feature_title, release in (
            ("Fury", "Fury.2014.1080p.BluRay"),
            ("Fury: Extended Cut", "Fury.Extended.Cut.2014"),
            ("Corazones de acero", "Fury.2014.Spanish.Release"),
        ):
            with self.subTest(release=release):
                candidate = SimpleNamespace(
                    feature_title=feature_title, release=release,
                    file_name=release + ".srt", identity_verified=False,
                )
                self.assertEqual(
                    smart_subtitles._candidate_identity_conflict(candidate, identity), ""
                )

    def test_exact_feature_title_with_strong_id_does_not_override_wrong_year(self) -> None:
        identity = online_subtitles.MediaIdentity(
            "Example Movie", "Example Movie", "2023"
        )
        response = {
            "data": [{
                "id": "feature-1",
                "type": "movie",
                "attributes": {
                    "title": "Example Movie",
                    "original_title": "Example Movie",
                    "year": "2021",
                    "imdb_id": 1234567,
                    "tmdb_id": 7654321,
                },
            }]
        }

        resolved = online_subtitles._feature_identity_from_response(response, identity)

        self.assertIsNone(resolved)

    def test_bad_feature_year_without_strong_id_remains_rejected(self) -> None:
        identity = online_subtitles.MediaIdentity(
            "Example Movie", "Example Movie", "2023"
        )
        response = {
            "data": [{
                "id": "feature-1",
                "type": "movie",
                "attributes": {
                    "title": "Example Movie",
                    "original_title": "Example Movie",
                    "year": "2021",
                },
            }]
        }

        self.assertIsNone(online_subtitles._feature_identity_from_response(response, identity))

    def test_bad_feature_year_with_only_prefix_alias_remains_rejected(self) -> None:
        identity = online_subtitles.MediaIdentity("Example", "Example", "2023")
        response = {
            "data": [{
                "id": "feature-1",
                "type": "movie",
                "attributes": {
                    "title": "Example Movie",
                    "original_title": "Example Movie",
                    "year": "2021",
                    "imdb_id": 1234567,
                },
            }]
        }

        self.assertIsNone(online_subtitles._feature_identity_from_response(response, identity))

    def test_feature_identity_rejects_meaningful_title_suffix(self) -> None:
        identity = online_subtitles.MediaIdentity(
            "V for Vendetta", "V for Vendetta", "2005"
        )
        response = {
            "data": [{
                "id": "imax",
                "type": "movie",
                "attributes": {
                    "title": "V for Vendetta: At the IMAX",
                    "original_title": "V for Vendetta: At the IMAX",
                    "year": "2006",
                    "imdb_id": 999,
                },
            }]
        }

        self.assertIsNone(
            online_subtitles._feature_identity_from_response(response, identity)
        )

    def test_feature_identity_keeps_the_matching_title_not_provider_suffix(self) -> None:
        identity = online_subtitles.MediaIdentity(
            "V for Vendetta", "V for Vendetta", "2005"
        )
        response = {
            "data": [{
                "id": "film",
                "type": "movie",
                "attributes": {
                    "title": "V for Vendetta",
                    "original_title": "V for Vendetta: At the IMAX",
                    "year": "2006",
                    "imdb_id": 434409,
                },
            }]
        }

        resolved = online_subtitles._feature_identity_from_response(response, identity)

        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.original_title, "V for Vendetta")

    def test_empty_feature_search_retries_original_filename_identity(self) -> None:
        base = online_subtitles.MediaIdentity(
            "V for Vendetta", "V for Vendetta", "2005"
        )
        wrong_feature = online_subtitles.MediaIdentity(
            "V for Vendetta",
            "V for Vendetta: At the IMAX",
            "2005",
            feature_id="imax",
            imdb_id="999",
        )
        candidate = online_subtitles.SubtitleCandidate(
            1,
            "V.for.Vendetta.2005.srt",
            "V.for.Vendetta.2005.BluRay",
            "en",
            0,
            0.0,
            False,
            False,
            feature_title="V for Vendetta",
            feature_year="2005",
        )
        calls: list[dict] = []

        def search_pages(_key, params, _language, **_kwargs):
            calls.append(dict(params))
            if "imdb_id" in params:
                return [], 0, False
            return [candidate], 1, False

        with patch.object(
            online_subtitles, "identify_media", return_value=base
        ), patch.object(
            online_subtitles, "opensubtitles_movie_hash", return_value=""
        ), patch.object(
            online_subtitles, "_resolve_feature_identity", return_value=wrong_feature
        ), patch.object(
            online_subtitles, "_search_pages", side_effect=search_pages
        ):
            resolved, results, meta = online_subtitles.search(
                "key", "V.for.Vendetta.2005.mkv", "en"
            )

        self.assertEqual(resolved.original_title, "V for Vendetta")
        self.assertEqual(results, [candidate])
        self.assertEqual(meta.query_mode, "title-year")
        self.assertEqual(calls[1]["query"], "V for Vendetta")
        self.assertEqual(calls[1]["year"], "2005")

    def test_subdl_uses_exact_title_with_adjacent_provider_year(self) -> None:
        identity = subdl_subtitles.MediaIdentity(
            "V for Vendetta", "V for Vendetta", "2005"
        )
        items = [
            {
                "sd_id": "suffix",
                "name": "V for Vendetta At the IMAX",
                "original_name": "V for Vendetta At the IMAX",
                "year": 2005,
            },
            {
                "sd_id": "film",
                "name": "V for Vendetta",
                "original_name": "V for Vendetta",
                "year": 2006,
            },
        ]

        selected = subdl_subtitles._choose_movie_without_score(identity, items)

        self.assertEqual(selected["sd_id"], "film")

    def test_matching_candidate_imdb_id_is_a_strong_identity(self) -> None:
        identity = online_subtitles.MediaIdentity(
            "Example Movie", "Example Movie", "2023", imdb_id="1234567"
        )
        candidate = online_subtitles.SubtitleCandidate(
            1,
            "Example.Movie.srt",
            "Example Movie",
            "en",
            100,
            8.0,
            True,
            False,
            feature_title="2021 - Example Movie",
            feature_year="2021",
            feature_imdb_id="1234567",
            identity_verified=True,
            identity_key="imdb:1234567",
        )

        self.assertTrue(online_subtitles._candidate_matches_strong_identity(candidate, identity))

    def test_different_candidate_imdb_id_is_not_a_strong_identity(self) -> None:
        identity = online_subtitles.MediaIdentity(
            "Example Movie", "Example Movie", "2023", imdb_id="1234567"
        )
        candidate = online_subtitles.SubtitleCandidate(
            1,
            "Example.Movie.srt",
            "Example Movie",
            "en",
            100,
            8.0,
            True,
            False,
            feature_title="2021 - Example Movie",
            feature_year="2021",
            feature_imdb_id="9999999",
            identity_verified=False,
            identity_key="",
        )

        self.assertFalse(online_subtitles._candidate_matches_strong_identity(candidate, identity))

    def test_media_specification_exposes_explicit_cut_source_and_runtime(self) -> None:
        specification = subtitle_identity_guard.media_specification(
            "Movie.2004.Directors.Cut.UHD.BluRay.IMAX.mkv",
            7_265,
            22 * 1024 ** 3 + 512 * 1024 ** 2,
        )
        self.assertIn("导演剪辑版", specification)
        self.assertIn("Blu-ray", specification)
        self.assertIn("片长 2:01:05 · 大小 22.50G", specification)
        self.assertIn("IMAX", specification)

    def test_explicit_cut_conflict_uses_human_readable_labels(self) -> None:
        conflict = subtitle_identity_guard.edition_conflict(
            "Movie.2003.THEATRICAL.mkv",
            "Movie.2003.Extended.Cut.srt",
        )
        self.assertIn("影片为 院线版", conflict)
        self.assertIn("字幕候选为 加长版", conflict)

    def test_repeated_ella_name_does_not_reject_english_subtitle(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            subtitle = Path(folder) / "cinderella.srt"
            subtitle.write_text(
                "\n\n".join(
                    f"{index}\n00:00:{index % 60:02d},000 --> 00:00:{index % 60:02d},900\n"
                    "Ella, you know that the prince will come to the house and find you here."
                    for index in range(1, 41)
                ),
                encoding="utf-8",
            )
            self.assertEqual(smart_subtitles.subtitle_content_conflict(subtitle, "en"), "")

    def test_real_spanish_subtitle_is_rejected_as_english_source(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            subtitle = Path(folder) / "spanish.srt"
            subtitle.write_text(
                "\n\n".join(
                    f"{index}\n00:00:{index % 60:02d},000 --> 00:00:{index % 60:02d},900\n"
                    "Ella esta con los amigos, pero no sabe por que una persona viene para hablar."
                    for index in range(1, 41)
                ),
                encoding="utf-8",
            )
            self.assertIn("es", smart_subtitles.subtitle_content_conflict(subtitle, "en"))
    def test_smart_search_passes_global_budget_into_candidate_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video = root / "Movie.2025.BluRay.mkv"
            subtitle = root / "Movie.2025.BluRay.srt"
            video.write_bytes(b"video")
            subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello world\n", encoding="utf-8")
            candidate = SimpleNamespace(
                release="Movie.2025.BluRay",
                feature_title="Movie",
                feature_year="2025",
                file_name=subtitle.name,
                language="en",
                moviehash_match=False,
                identity_verified=True,
                identity_key="imdb:123",
                file_id=1,
            )
            service = SimpleNamespace(
                load_settings=Mock(return_value={"api_key": "secret"}),
                search=Mock(return_value=(object(), [candidate], object())),
                download=Mock(return_value=subtitle),
            )
            user_cancel = threading.Event()
            with patch.object(
                smart_subtitles,
                "PROVIDERS",
                [("OpenSubtitles", service, "api_key")],
            ), patch.object(
                smart_subtitles,
                "subtitle_content_conflict",
                return_value="",
            ), patch.object(
                smart_subtitles.pro_core,
                "prepare_shared_subtitle_content_audio",
                return_value=SimpleNamespace(fingerprint=object()),
            ), patch.object(
                smart_subtitles.pro_core,
                "preflight_online_subtitle",
                return_value=(subtitle, "verified"),
            ) as preflight:
                smart_subtitles.find_verified_english(
                    str(video),
                    0,
                    lambda _message: None,
                    user_cancel,
                    time_budget_seconds=45.0,
                )

        propagated_cancel = preflight.call_args.args[6]
        self.assertIsInstance(propagated_cancel, smart_subtitles._DeadlineCancel)
        self.assertIs(propagated_cancel.cancel_event, user_cancel)
        self.assertLessEqual(preflight.call_args.kwargs["time_budget_seconds"], 45.0)


    def test_shared_content_is_reused_by_candidate_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video = root / "Movie.2025.BluRay.mkv"
            subtitle = root / "Movie.2025.BluRay.srt"
            video.write_bytes(b"video")
            subtitle.write_text(
                "1\n00:00:01,000 --> 00:00:02,000\nHello world\n",
                encoding="utf-8",
            )
            candidate = SimpleNamespace(
                release="Movie.2025.BluRay",
                feature_title="Movie",
                feature_year="2025",
                file_name=subtitle.name,
                language="en",
                moviehash_match=False,
                identity_verified=True,
                identity_key="imdb:123",
                file_id=1,
                trusted=True,
                downloads=100,
            )
            download_finished = threading.Event()

            def search(*_args, **_kwargs):
                return object(), [candidate], object()

            def download(*_args, **_kwargs):
                download_finished.set()
                return subtitle

            def preflight(*_args, **_kwargs):
                return subtitle, "verified"

            service = SimpleNamespace(
                load_settings=Mock(return_value={"api_key": "secret"}),
                search=Mock(side_effect=search),
                download=Mock(side_effect=download),
            )
            with patch.object(
                smart_subtitles,
                "PROVIDERS",
                [("OpenSubtitles", service, "api_key")],
            ), patch.object(
                smart_subtitles,
                "subtitle_content_conflict",
                return_value="",
            ), patch.object(
                smart_subtitles.pro_core,
                "prepare_shared_subtitle_content_audio",
                return_value=SimpleNamespace(fingerprint=object()),
            ) as content_preparation, patch.object(
                smart_subtitles.pro_core,
                "preflight_online_subtitle",
                side_effect=preflight,
            ) as preflight_mock:
                smart_subtitles.find_verified_english(
                    str(video),
                    0,
                    lambda _message: None,
                    threading.Event(),
                    time_budget_seconds=45.0,
                )

        self.assertTrue(download_finished.is_set())
        content_preparation.assert_called_once()
        self.assertLessEqual(preflight_mock.call_args.kwargs["time_budget_seconds"], 45.0)


    def test_broad_title_year_search_receives_safe_identity_lock(self) -> None:
        identity = online_subtitles.MediaIdentity(
            "How to Train Your Dragon", "How to Train Your Dragon", "2010"
        )
        candidate = online_subtitles.SubtitleCandidate(
            1,
            "How.to.Train.Your.Dragon.2010.1080p.BluRay.srt",
            "How.to.Train.Your.Dragon.2010.1080p.BluRay.x265-RARBG",
            "en",
            100,
            8.0,
            True,
            False,
            feature_title="How to Train Your Dragon",
            feature_year="2010",
        )
        with patch.object(online_subtitles, "identify_media", return_value=identity), patch.object(
            online_subtitles, "opensubtitles_movie_hash", return_value=""
        ), patch.object(
            online_subtitles, "_resolve_feature_identity", return_value=None
        ), patch.object(
            online_subtitles, "_search_pages", return_value=([candidate], 1, False)
        ):
            _identity, results, _meta = online_subtitles.search(
                "api-key", "How.to.Train.Your.Dragon.2010.mkv", "en"
            )

        self.assertTrue(results[0].identity_verified)
        self.assertEqual(
            results[0].identity_key,
            "title-year:how-to-train-your-dragon:2010",
        )

    def test_subdl_movie_year_conflict_cannot_be_outscored_by_title(self) -> None:
        identity = subdl_subtitles.MediaIdentity("Freaky", "Freaky", "2003")
        wrong_movie = {
            "sd_id": "wrong",
            "name": "Freaky",
            "original_name": "Freaky",
            "year": 2020,
        }

        self.assertIsNone(subdl_subtitles._choose_movie_without_score(identity, [wrong_movie]))

    def test_parenthesized_year_does_not_leave_open_bracket_in_title(self) -> None:
        path = "Example Movie (2016).mkv"

        self.assertEqual(online_subtitles.clean_video_title(path), ("Example Movie", "2016"))
        self.assertEqual(subdl_subtitles.clean_video_title(path), ("Example Movie", "2016"))

    def test_title_number_before_release_year_is_preserved(self) -> None:
        path = "The.Legend.of.1900.1998.BluRay.2160p.mkv"

        self.assertEqual(
            online_subtitles.clean_video_title(path),
            ("The Legend of 1900", "1998"),
        )
        self.assertEqual(
            subdl_subtitles.clean_video_title(path),
            ("The Legend of 1900", "1998"),
        )

    def test_subdl_typo_recovery_queries_drop_each_content_token_with_year(self) -> None:
        identity = subdl_subtitles.MediaIdentity(
            "Jacon Bourne", "Jacon Bourne", "2016"
        )

        queries = subdl_subtitles._movie_queries(identity)

        self.assertIn("BOURNE 2016", queries)
        self.assertIn("JACON 2016", queries)

    def test_subdl_typo_candidate_still_requires_matching_year_and_title_context(self) -> None:
        identity = subdl_subtitles.MediaIdentity(
            "Jacon Bourne", "Jacon Bourne", "2016"
        )
        corrected = {
            "sd_id": "correct",
            "name": "Jason Bourne",
            "original_name": "Jason Bourne",
            "year": 2016,
        }
        wrong_year = dict(corrected, year=2004)

        self.assertIs(subdl_subtitles._choose_movie_without_score(identity, [corrected]), corrected)
        self.assertIsNone(subdl_subtitles._choose_movie_without_score(identity, [wrong_year]))

    def test_opensubtitles_title_year_lock_accepts_one_character_typo(self) -> None:
        identity = online_subtitles.MediaIdentity(
            "Jacon Bourne", "Jacon Bourne", "2016"
        )
        candidate = online_subtitles.SubtitleCandidate(
            1,
            "Jason.Bourne.2016.srt",
            "Jason.Bourne.2016.BluRay",
            "en",
            100,
            8.0,
            True,
            False,
            feature_title="2016 - Jason Bourne",
            feature_year="2016",
        )

        key = online_subtitles._title_year_identity_key(candidate, identity)

        self.assertEqual(key, "title-year:jacon-bourne:2016")

    def test_opensubtitles_typo_lock_still_rejects_wrong_year(self) -> None:
        identity = online_subtitles.MediaIdentity(
            "Jacon Bourne", "Jacon Bourne", "2016"
        )
        candidate = online_subtitles.SubtitleCandidate(
            1,
            "Jason.Bourne.2004.srt",
            "Jason.Bourne.2004.BluRay",
            "en",
            100,
            8.0,
            True,
            False,
            feature_title="Jason Bourne",
            feature_year="2004",
        )

        self.assertEqual(online_subtitles._title_year_identity_key(candidate, identity), "")

    def test_mojibake_subtitle_content_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            subtitle = Path(folder) / "broken.srt"
            subtitle.write_text(
                "\n\n".join(
                    f"{index}\n00:00:{index:02d},000 --> 00:00:{index:02d},900\nPR脡SENTE broken dialogue"
                    for index in range(1, 31)
                ),
                encoding="utf-8",
            )

            reason = smart_subtitles.subtitle_content_conflict(subtitle, "en")

        self.assertIn("乱码", reason)

    def test_bluray_rejects_hdts_before_audio_validation(self) -> None:
        video = "Freakier.2025.Bluray.1080p.DTS-HDMA7.1.x265.10bit-DreamHD.mkv"
        online = online_subtitles.SubtitleCandidate(
            1,
            "Freakier.Friday.2025.1080p.HDTS.srt",
            "Freakier Friday 2025 1080p HDTS x264-RGB",
            "en",
            10,
            0.0,
            False,
            False,
            feature_title="Freakier Friday",
            feature_year="2025",
            identity_verified=True,
            identity_key="imdb:31956415",
        )
        subdl = subdl_subtitles.SubtitleCandidate(
            "1",
            "Freakier.Friday.2025.1080p.HDTS.srt",
            "Freakier Friday 2025 1080p HDTS x264-RGB",
            "en",
            10,
            0.0,
            False,
            False,
            feature_title="Freakier Friday",
            feature_year="2025",
            identity_verified=True,
            identity_key="imdb:31956415",
        )

        self.assertIn(
            "影院片源",
            subtitle_identity_guard.source_conflict(video, f"{online.release} {online.file_name}"),
        )
        self.assertIn(
            "影院片源",
            subtitle_identity_guard.source_conflict(video, f"{subdl.release} {subdl.file_name}"),
        )

    def test_safe_official_title_expansion_accepts_freakier_friday(self) -> None:
        self.assertTrue(online_subtitles._titles_safe_alias("Freakier", "Freakier Friday"))
        self.assertFalse(online_subtitles._titles_safe_alias("Creed", "Misty Creed"))

    def test_container_title_can_complete_truncated_filename(self) -> None:
        with patch.object(
            online_subtitles,
            "container_title",
            return_value="FREAKIER FRIDAY - BLU-RAY",
        ):
            identity = online_subtitles.identify_media(
                "Freakier.2025.Bluray.1080p.mkv"
            )

        self.assertEqual(identity.title, "FREAKIER FRIDAY")
        self.assertEqual(identity.year, "2025")

    def test_verified_subtitle_is_sealed_against_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video = root / "Movie.2025.BluRay.mkv"
            subtitle = root / "Movie.2025.BluRay.srt"
            video.write_bytes(b"video")
            subtitle.write_text("verified", encoding="utf-8")
            seal = smart_subtitles.verification_seal(
                str(video), str(subtitle), "SubDL", "Movie 2025 BluRay", "imdb:123"
            )

            smart_subtitles.verify_before_processing(
                str(video), str(subtitle), "SubDL", "Movie 2025 BluRay", "imdb:123", seal
            )
            subtitle.write_text("replaced", encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "发生变化"):
                smart_subtitles.verify_before_processing(
                    str(video), str(subtitle), "SubDL", "Movie 2025 BluRay", "imdb:123", seal
                )

    def test_processing_recheck_does_not_reject_release_label_after_content_pass(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video = root / "Movie.2025.BluRay.mkv"
            subtitle = root / "Movie.2025.HDTS.srt"
            video.write_bytes(b"video")
            subtitle.write_text("verified", encoding="utf-8")

            seal = smart_subtitles.verification_seal(
                str(video), str(subtitle), "SubDL", "Movie 2025 HDTS", "imdb:123"
            )
            smart_subtitles.verify_before_processing(
                str(video),
                str(subtitle),
                "SubDL",
                "Movie 2025 HDTS",
                "imdb:123",
                seal,
            )


if __name__ == "__main__":
    unittest.main()
