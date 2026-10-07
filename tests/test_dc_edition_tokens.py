"""Dated release DC tags must not confuse comic titles or unknown cuts."""
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
import subdl_subtitles as subdl
import subtitle_identity_guard as guard


MOVIE = "Battle.Royale.2000.JAPANESE.DC.2160p.BluRay.mkv"


class DirectorCutShortTokenTest(unittest.TestCase):
    def test_dated_release_token_recognizes_real_battle_royale_name(self):
        for release in (
            MOVIE, "Battle.Royale.2000.JAPANESE.dc.2160p.BluRay",
            "Battle_Royale_2000_DC_1080p_BluRay.srt",
            "Battle Royale (2000) [DC] UHD BluRay",
            "Battle.Royale.2000.BluRay.DC", "Battle.Royale.2000.BluRay.DC.srt",
        ):
            with self.subTest(release=release):
                self.assertEqual(guard.edition_codes(release), {"director"})

    def test_dc_names_and_nonstandalone_words_are_not_release_editions(self):
        for title in (
            "DC.League.of.Super.Pets.2022.2160p.BluRay.mkv",
            "DC.Comics.2020.1080p.BluRay.mkv", "Washington.DC.2019.1080p.BluRay",
            "Movie.2000.1080p.DC.Comics", "Movie.2000.1080p.DC.League.of.Heroes",
            "Movie.2000.ACDC.1080p", "Movie.2000.dccomics.1080p",
            "DC", "DC Comics", "DC.2160p.BluRay",
        ):
            with self.subTest(title=title):
                self.assertNotIn("director", guard.edition_codes(title))

    def test_ambiguous_abbreviation_remains_unknown(self):
        self.assertEqual(guard.edition_codes("Battle Royale 2000 DC"), set())
        self.assertEqual(guard.edition_codes("English DC"), set())
        self.assertEqual(guard.edition_conflict(MOVIE, "Battle.Royale.2000.en.srt"), "")

    def test_existing_long_cut_labels_are_unchanged(self):
        for token in ("Director's Cut", "Directors.Cut", "Director_Cut"):
            with self.subTest(token=token):
                self.assertEqual(guard.edition_codes(token), {"director"})
        self.assertEqual(guard.edition_codes("Extended.Cut"), {"extended"})
        self.assertEqual(guard.edition_codes("THEATRICAL"), {"theatrical"})

    def test_explicit_conflict_is_symmetric_and_matching_cut_is_allowed(self):
        self.assertIn("影片为 导演剪辑版", guard.edition_conflict(
            MOVIE, "Battle.Royale.2000.Theatrical.Cut.en.srt",
        ))
        self.assertIn("字幕候选为 导演剪辑版", guard.edition_conflict(
            "Battle.Royale.2000.Theatrical.mkv", MOVIE.replace(".mkv", ".srt"),
        ))
        self.assertEqual(guard.edition_conflict(MOVIE, "Battle.Royale.2000.Directors.Cut.en.srt"), "")

    def test_both_providers_expose_same_director_cut_identity(self):
        for provider in (online, subdl):
            with self.subTest(provider=provider.__name__), patch.object(provider, "container_title", return_value=""):
                identity = provider.identify_media(MOVIE)
            self.assertEqual(identity.edition, "导演剪辑版")
            self.assertIn("导演剪辑版", identity.specification)
            self.assertEqual(identity.title, "Battle Royale")


class DirectorCutDownloadGuardTest(unittest.TestCase):
    def test_two_provider_routes_reject_explicit_theatrical_before_download(self):
        for provider_name in ("OpenSubtitles", "SubDL"):
            with self.subTest(provider=provider_name), tempfile.TemporaryDirectory() as directory:
                movie = Path(directory) / MOVIE
                movie.write_bytes(b"movie")
                identity = SimpleNamespace(title="Battle Royale", original_title="Battle Royale", year="2000")
                def candidate(identifier, release):
                    return SimpleNamespace(
                        file_id=identifier, release=release, file_name=release + ".srt", language="en",
                        feature_title="Battle Royale", feature_year="2000",
                    )
                theatrical = candidate(1, "Battle.Royale.2000.Theatrical.Cut")
                unknown = candidate(2, "Battle.Royale.2000.1080p.BluRay")
                downloaded = []
                messages = []
                class Provider:
                    @staticmethod
                    def load_settings():
                        return {"key": "token"}
                    @staticmethod
                    def search(*_args, **_kwargs):
                        return identity, [theatrical, unknown], SimpleNamespace(query_mode="feature")
                    @staticmethod
                    def download(_key, selected, destination):
                        downloaded.append(selected.file_id)
                        path = Path(destination) / "subtitle.srt"
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello there.\n", encoding="utf-8")
                        return path

                with patch.object(smart, "PROVIDERS", ((provider_name, Provider, "key"),)), \
                     patch.object(smart, "filename_container_identity_conflict", return_value=""), \
                     patch.object(smart.pro_core, "prepare_shared_subtitle_content_audio",
                                  return_value=SimpleNamespace(vad_reference="cached")), \
                     patch.object(smart.pro_core, "preflight_online_subtitle",
                                  side_effect=lambda _movie, path, *_args, **_kwargs: (Path(path), "verified")) as preflight:
                    result = smart.find_verified_english(str(movie), None, messages.append)
                self.assertEqual(downloaded, [2])
                preflight.assert_called_once()
                self.assertEqual(result.candidate.file_id, 2)
                self.assertTrue(any("未进入下载" in message and "剪辑版本冲突" in message for message in messages))


if __name__ == "__main__":
    unittest.main()
