from __future__ import annotations

import unittest

from subtitle_identity_guard import subtitle_language_label_conflict


class MultilingualReleaseLabelsTests(unittest.TestCase):
    def test_transcendence_multi_audio_releases_are_not_bilingual_proof(self) -> None:
        candidates = (
            "Transcendance.2014.MULTI.VFF.1080p.Bluray.Remux.AVC-Hidi "
            "Transcendance (2014) eng",
            "Transcendance.2014.MULTI.VFF.1080p.Bluray.Remux.AVC-Hidi "
            "Transcendance (2014) eng SDH",
            "Transcendence.2014.MULTI.TRUEFRENCH.1080p.BluRay.DTS.x264-EXTREME "
            "Transcendence.2014.MULTI.TRUEFRENCH.1080p.BluRay.DTS.x264-EXTREME",
        )
        for candidate in candidates:
            with self.subTest(candidate=candidate):
                self.assertEqual(subtitle_language_label_conflict(candidate), "")

    def test_bare_multi_and_english_file_suffix_are_allowed(self) -> None:
        candidates = (
            "Film.2014.MULTI.1080p.BluRay.x264 Film.2014.eng.srt",
            "Film (2014) [MULTI] Film.en.ass",
            "Film.2014.MULTI.AUDIO.en.srt",
            "Film.2014.Multi.2160p.REMUX Film.en.sdh.srt",
        )
        for candidate in candidates:
            with self.subTest(candidate=candidate):
                self.assertEqual(subtitle_language_label_conflict(candidate), "")

    def test_explicit_mixed_subtitle_labels_remain_rejected(self) -> None:
        labels = (
            "bilingual",
            "BILINGUAL",
            "dual-subs",
            "dual_subtitles",
            "dual.sub",
            "multilang",
            "multilanguage",
            "multi-language",
            "multilingual",
            "MULTI.SUBS",
            "multi_subtitles",
        )
        for label in labels:
            with self.subTest(label=label):
                self.assertIn(
                    "多语言/双语",
                    subtitle_language_label_conflict(f"Film.2014.{label}.srt"),
                )

    def test_explicit_english_other_language_combinations_remain_rejected(self) -> None:
        labels = ("en+fr", "eng-fre", "English/French", "eng.zh", "en_de", "en&es")
        for label in labels:
            with self.subTest(label=label):
                self.assertIn(
                    "英文和其他语言",
                    subtitle_language_label_conflict(f"Film.2014.{label}.srt"),
                )

    def test_multi_release_does_not_override_a_bilingual_subtitle_filename(self) -> None:
        candidate = "Film.2014.MULTI.BluRay.x264 Film.2014.eng-fr.srt"
        self.assertIn("英文和其他语言", subtitle_language_label_conflict(candidate))

    def test_movie_words_are_not_language_labels(self) -> None:
        for candidate in ("Multiverse.2014.eng.srt", "Multiplicity.1996.eng.srt"):
            with self.subTest(candidate=candidate):
                self.assertEqual(subtitle_language_label_conflict(candidate), "")


if __name__ == "__main__":
    unittest.main()
