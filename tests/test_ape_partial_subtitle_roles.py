"""Explicit partial-dialogue labels must not consume automatic candidates."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import online_subtitles
import pro_core
import smart_subtitles as smart
import subdl_subtitles
import subtitle_audit
import subtitle_roles


def candidate(candidate_class, *, release="Movie.2025.BluRay", file_name="movie.srt", sdh=False):
    return candidate_class(file_id="partial-test", file_name=file_name, release=release,
                           language="en", downloads=0, rating=0.0, trusted=False,
                           hearing_impaired=sdh)


class ApePartialSubtitleRolesTests(unittest.TestCase):
    def test_uses_business_module_beside_this_test_tree(self):
        self.assertEqual(Path(subtitle_roles.__file__).resolve().parent,
                         Path(__file__).resolve().parents[1])

    def test_explicit_ape_parts_only_spelling_variants(self):
        for label in ("Ape Parts Only", "APES PARTS ONLY", "Ape.Parts.Only.srt",
                      "ape-parts-only", "English [Ape_Parts_Only]",
                      "War.for.the.Planet.of.the.Apes.2017.BluRay.Ape.Parts.Only"):
            with self.subTest(label=label):
                role = subtitle_roles.classify(label)
                self.assertEqual(role.code, "partial")
                self.assertFalse(role.main_dialogue)
                self.assertIn("不是完整字幕", role.reason)

    def test_general_explicit_restricted_dialogue_labels(self):
        for label in ("Foreign Parts Only", "Foreign Language Dialogue Only",
                      "Non-English Parts Only", "Alien Only", "Aliens Dialogue Only",
                      "Ape Lines Only", "Sign Language Only", "Sign Language Scenes Only",
                      "Signed Language Sections Only", "Signing Parts Only"):
            with self.subTest(label=label):
                self.assertEqual(subtitle_roles.classify(label).code, "partial")

    def test_short_ape_tag_alone_does_not_claim_partial_subtitle(self):
        for label in ("[APE]", "Movie.2025.[APE].BluRay", "Ape", "Apes",
                      "English APE SDH", "Ape Parts", "Contains Ape Dialogue"):
            with self.subTest(label=label):
                self.assertTrue(subtitle_roles.classify(label).main_dialogue)

    def test_regular_parts_or_only_words_are_not_partial_evidence(self):
        for label in ("Private Parts", "Body Parts", "Only Lovers Left Alive",
                      "Only Angels Have Wings", "The Sum of All Parts",
                      "English Full Movie", "Movie.2025.BluRay.Part.1.and.Part.2"):
            with self.subTest(label=label):
                self.assertTrue(subtitle_roles.classify(label).main_dialogue)

    def test_full_sdh_with_ape_or_sign_language_dialogue_remains_complete(self):
        for label in ("English SDH", "Full SDH including Ape Parts",
                      "Full English with Sign Language Dialogue", "English CC Apes"):
            with self.subTest(label=label):
                role = subtitle_roles.classify(label, hearing_impaired=True)
                self.assertEqual(role.code, "sdh")
                self.assertTrue(role.main_dialogue)

    def test_known_film_title_prefix_is_not_an_extra_partial_label(self):
        for title in ("Ape Parts Only", "Foreign Parts Only", "Sign Language Only"):
            identity = SimpleNamespace(title=title, original_title=title)
            for cls in (online_subtitles.SubtitleCandidate, subdl_subtitles.SubtitleCandidate):
                with self.subTest(title=title, provider=cls.__module__):
                    item = candidate(cls, release=f"{title}.2025.BluRay", file_name=f"{title}.srt")
                    self.assertTrue(subtitle_roles.candidate_role(item, identity).main_dialogue)
                    self.assertEqual(smart._candidate_role_conflict(item, identity), "")

    def test_real_trailing_label_survives_exact_title_prefix_removal(self):
        identity = SimpleNamespace(title="Ape Parts Only", original_title="Ape Parts Only")
        item = candidate(online_subtitles.SubtitleCandidate,
                         release="Ape Parts Only.2025.BluRay.Ape Parts Only")
        self.assertEqual(subtitle_roles.candidate_role(item, identity).code, "partial")

    def test_both_provider_types_use_release_and_filename_independently(self):
        identity = SimpleNamespace(title="Movie", original_title="Movie")
        for cls in (online_subtitles.SubtitleCandidate, subdl_subtitles.SubtitleCandidate):
            for field in ("release", "file_name"):
                with self.subTest(provider=cls.__module__, field=field):
                    item = candidate(cls, **{field: "Movie.2025.Ape.Parts.Only.srt"})
                    before = vars(item).copy()
                    self.assertEqual(subtitle_roles.candidate_role(item, identity).code, "partial")
                    self.assertIn("片段", smart._candidate_role_conflict(item, identity))
                    # Classification does not delete manual results or hide their original label.
                    self.assertEqual(vars(item), before)

    def test_forced_and_existing_partial_rules_are_preserved(self):
        self.assertEqual(subtitle_roles.classify("English", forced=True).code, "partial")
        self.assertTrue(subtitle_roles.classify("English Full SDH", forced=True).main_dialogue)
        for label in ("Forced Only", "Foreign Only", "Alien Only", "Signs & Songs",
                      "片段字幕", "強制字幕"):
            with self.subTest(label=label):
                self.assertEqual(subtitle_roles.classify(label).code, "partial")

    def test_sdh_does_not_override_an_explicit_partial_label(self):
        self.assertEqual(subtitle_roles.classify("SDH Ape Parts Only",
                                                hearing_impaired=True).code, "partial")

    def test_existing_commentary_and_extra_priority_is_preserved(self):
        self.assertEqual(subtitle_roles.classify("Ape Parts Only Commentary").code, "commentary")
        self.assertEqual(subtitle_roles.classify("Ape Parts Only Behind The Scenes").code, "extra")

    def test_embedded_track_and_output_header_use_same_partial_rule(self):
        self.assertFalse(subtitle_roles.track_role(SimpleNamespace(
            name="English [Ape Parts Only]", forced=False)).main_dialogue)
        with tempfile.TemporaryDirectory() as directory:
            audit = subtitle_audit.FilmAudit(Path(directory) / "movie.mkv", directory)
            with self.assertRaisesRegex(RuntimeError, "成品字幕用途检查失败"):
                audit.check_output_purposes({"tracks": [{"id": 4, "type": "subtitles",
                    "properties": {"language": "eng", "track_name": "Ape Parts Only"}}]})
            self.assertEqual(audit.output_subtitles[0]["role"], "partial")
            self.assertFalse(audit.output_subtitles[0]["purpose_accepted"])

    def test_explicit_partial_candidates_are_filtered_before_download_for_both_providers(self):
        for provider_name, cls in (("OpenSubtitles", online_subtitles.SubtitleCandidate),
                                   ("SubDL", subdl_subtitles.SubtitleCandidate)):
            with self.subTest(provider=provider_name), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                movie = root / "Movie.2025.mkv"
                movie.write_bytes(b"isolated role test")
                item = candidate(cls, release="Movie.2025.BluRay.Ape.Parts.Only")
                identity = SimpleNamespace(title="Movie", original_title="Movie", year="2025")
                service = SimpleNamespace(load_settings=lambda: {"test_key": "fixture"},
                    search=lambda *_args, **_kwargs: (identity, [item], None), download=Mock())
                with patch.object(smart, "PROVIDERS", ((provider_name, service, "test_key"),)), \
                        patch.object(smart, "filename_container_identity_conflict", return_value=""), \
                        patch.object(pro_core, "prepare_shared_subtitle_content_audio", return_value=None), \
                        patch.object(pro_core, "preflight_online_subtitle") as preflight:
                    with self.assertRaises(RuntimeError):
                        smart.find_verified_english(str(movie), 1, lambda _message: None)
                service.download.assert_not_called()
                preflight.assert_not_called()
                reports = list(root.rglob("subtitle-report-*.json"))
                self.assertEqual(len(reports), 1)
                report = json.loads(reports[0].read_text(encoding="utf-8"))
                self.assertEqual(report["attempted_candidates"], 0)
                row = report["records"][0]
                self.assertEqual(row["stage"], "purpose")
                self.assertEqual(row["state"], "filtered")
                self.assertEqual(row["stages"]["purpose"]["role"], "partial")


if __name__ == "__main__":
    unittest.main()
