from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import continuous_vad
import pro_core
import smart_subtitles
import subtitle_identity_guard


class AlignmentRuleCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.movie = self.root / "Film.2014.mkv"
        self.movie.write_bytes(b"movie")
        self.source = self.root / "source.srt"
        self.source.write_text(
            "1\n00:00:10,000 --> 00:00:12,000\n"
            "This is a complete line of spoken English dialogue.\n",
            encoding="utf-8",
        )
        self.track = SimpleNamespace(id=2, text_subtitle=True, language="en")

    def seal(self) -> str:
        return subtitle_identity_guard.verification_seal(
            str(self.movie), str(self.source), "OpenSubtitles", "Film.2014", "feature:7"
        )

    def test_seal_uses_the_current_alignment_rule_and_is_stable(self) -> None:
        payload = {
            "video": str(self.movie.resolve()).casefold(),
            "subtitle_sha256": hashlib.sha256(self.source.read_bytes()).hexdigest(),
            "provider": "OpenSubtitles",
            "release": "Film.2014",
            "identity_key": "feature:7",
            "alignment_rule_version": continuous_vad.RULE_VERSION,
        }
        expected = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        self.assertEqual(self.seal(), expected)
        self.assertEqual(self.seal(), self.seal())

    def test_old_seal_cannot_authorize_processing_after_rule_change(self) -> None:
        old = self.seal()
        with patch.object(continuous_vad, "RULE_VERSION", continuous_vad.RULE_VERSION + "-next"), \
             patch.object(smart_subtitles, "subtitle_content_conflict", return_value=""):
            with self.assertRaises(RuntimeError):
                smart_subtitles.verify_before_processing(
                    str(self.movie), str(self.source), "OpenSubtitles", "Film.2014", "feature:7", old
                )
            smart_subtitles.verify_before_processing(
                str(self.movie), str(self.source), "OpenSubtitles", "Film.2014", "feature:7", self.seal()
            )

    def test_previous_unversioned_seal_is_rejected_without_modifying_audio(self) -> None:
        fingerprint = self.root / "speech.npz"
        fingerprint.write_bytes(b"existing acoustic fingerprint")
        before = fingerprint.read_bytes()
        payload = {
            "video": str(self.movie.resolve()).casefold(),
            "subtitle_sha256": hashlib.sha256(self.source.read_bytes()).hexdigest(),
            "provider": "OpenSubtitles",
            "release": "Film.2014",
            "identity_key": "feature:7",
        }
        old = hashlib.sha256(
            json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        with patch.object(smart_subtitles, "subtitle_content_conflict", return_value=""):
            with self.assertRaises(RuntimeError):
                smart_subtitles.verify_before_processing(
                    str(self.movie), str(self.source), "OpenSubtitles", "Film.2014", "feature:7", old
                )
        self.assertEqual(fingerprint.read_bytes(), before)

    def test_verified_filenames_do_not_skip_current_rule_preflight(self) -> None:
        corrected = self.root / "current-rule.srt"
        for name in ("online-verified.srt", "external-verified.srt", "download.srt"):
            with self.subTest(name=name):
                source = self.root / name
                source.write_bytes(self.source.read_bytes())
                with patch.object(pro_core, "align_external_subtitle", return_value=corrected) as align:
                    output = pro_core._external_timeline_for_processing(
                        str(self.movie), str(source), str(self.root), 1, lambda _: None
                    )
                self.assertEqual(output, corrected)
                align.assert_called_once_with(
                    str(self.movie), str(source), str(self.root), 1, unittest.mock.ANY, None
                )

    def test_explicit_current_or_manual_confirmation_skips_repeated_alignment(self) -> None:
        with patch.object(pro_core, "prepare_embedded_text_corrections", return_value=({}, {}, None)), \
             patch.object(pro_core, "_prepare_local_chinese_conversions", return_value=[]), \
             patch.object(pro_core.legacy, "ensure_output_disk_space"), \
             patch.object(pro_core, "subtitle_events_to_srt", return_value=self.source) as normalize, \
             patch.object(pro_core, "_external_timeline_for_processing") as repeated_align, \
             patch.object(pro_core.legacy, "parse_subtitle", side_effect=RuntimeError("stop-after-confirmed-axis")):
            with self.assertRaisesRegex(RuntimeError, "stop-after-confirmed-axis"):
                pro_core.process_pro(
                    input_path=str(self.movie), output_path=str(self.root / "output.mkv"),
                    keep_audio_ids=[], keep_subtitle_ids=[], source_mode="external", embedded_source_id=None,
                    external_subtitle=str(self.source), audio_source_id=1, speech_language="en",
                    source_language="en", target_codes=[], work_dir=str(self.root / "work"),
                    log=lambda _: None, parallel_targets=1, cancel_event=None,
                    trust_external_original_timeline=True,
                )
        normalize.assert_called_once()
        repeated_align.assert_not_called()

    def _reference_signature(self, *, manual: bool, rule: str | None) -> dict:
        stat = self.movie.stat()
        signature = {
            "video_size": stat.st_size,
            "video_mtime_ns": stat.st_mtime_ns,
            "track_id": 2,
            "audio_id": 1,
            "language": "en",
            "alignment_mode": "manual-audio-segmented-v1" if manual else "audio-full-v2-verified-only",
        }
        if rule is not None:
            signature["alignment_rule_version"] = rule
        return signature

    def _run_reference_cache(self, *, manual: bool, rule: str | None):
        label = "manual-audio-aligned" if manual else "audio-aligned"
        aligned = self.root / f"track-2-{label}.srt"
        metadata = self.root / f"track-2-{label}.json"
        aligned.write_bytes(self.source.read_bytes())
        metadata.write_text(json.dumps(self._reference_signature(manual=manual, rule=rule)), encoding="utf-8")
        reference = ("subtitle", 0, self.track)
        function = pro_core._prepare_manual_aligned_text_reference if manual else \
            pro_core._prepare_aligned_embedded_text_reference
        fresh_align = "_alignment_candidate" if manual else "align_embedded_text_track"
        result = self.source if manual else (self.source, "连续VAD时间轴核验通过：提议固定偏移 +0.00 秒")
        with patch.object(pro_core, "_embedded_subtitle_reference", return_value=reference), \
             patch.object(pro_core.legacy, "extract_subtitle", return_value=self.source) as extract, \
             patch.object(pro_core, "subtitle_events_to_srt", return_value=self.source), \
             patch.object(pro_core, fresh_align, return_value=result) as align, \
             patch.object(pro_core, "semantic_spot_check", return_value=SimpleNamespace(accepted=True, report="passed")):
            output, _ = function(str(self.movie), "en", 1, self.root, lambda _: None, None)
        self.assertEqual(output, aligned)
        return metadata, extract, align

    def test_embedded_reference_caches_reuse_current_rule(self) -> None:
        for manual in (False, True):
            with self.subTest(manual=manual):
                _, extract, align = self._run_reference_cache(manual=manual, rule=continuous_vad.RULE_VERSION)
                extract.assert_not_called()
                align.assert_not_called()

    def test_embedded_reference_caches_recheck_unversioned_or_old_rules(self) -> None:
        for manual in (False, True):
            for rule in (None, "continuous-vad-v5-relative-peak-20261003"):
                with self.subTest(manual=manual, rule=rule):
                    metadata, extract, align = self._run_reference_cache(manual=manual, rule=rule)
                    extract.assert_called_once()
                    align.assert_called_once()
                    self.assertEqual(
                        json.loads(metadata.read_text(encoding="utf-8"))["alignment_rule_version"],
                        continuous_vad.RULE_VERSION,
                    )

    def _legacy_anchor_signature(self, rule: str | None) -> dict:
        stat = self.movie.stat()
        signature = {
            "video_size": stat.st_size,
            "video_mtime_ns": stat.st_mtime_ns,
            "subtitle_sha256": hashlib.sha256(self.source.read_bytes()).hexdigest(),
            "selected_audio_id": 1,
            "source_language": "en",
            "track_id": 2,
            "max_offset_seconds": pro_core.MAX_AUTOMATIC_OFFSET_SECONDS,
            "format": "embedded-text-anchor-v5-terminal-verdict-only",
            "guard_rule_version": pro_core.subtitle_offset_guard.GUARD_RULE_VERSION,
        }
        if rule is not None:
            signature["alignment_rule_version"] = rule
        return signature

    def test_legacy_embedded_anchor_current_rule_is_reusable(self) -> None:
        anchor = self.root / "verified-anchor.srt"
        anchor.write_bytes(self.source.read_bytes())
        report = "连续VAD时间轴核验通过：提议固定偏移 +0.00 秒"
        (self.root / "verified-anchor.json").write_text(json.dumps({
            "signature": self._legacy_anchor_signature(continuous_vad.RULE_VERSION), "report": report,
        }), encoding="utf-8")
        with patch.object(pro_core.legacy, "parse_subtitle") as parse:
            output, cached_report = pro_core._legacy_whisper_align_embedded_text_track(
                str(self.movie), self.source, self.root, 1, "en", 2, lambda _: None
            )
        self.assertEqual((output, cached_report), (anchor, report))
        parse.assert_not_called()

    def test_legacy_embedded_anchor_without_current_rule_is_not_reused(self) -> None:
        anchor = self.root / "verified-anchor.srt"
        anchor.write_bytes(self.source.read_bytes())
        for rule in (None, "continuous-vad-v5-relative-peak-20261003"):
            with self.subTest(rule=rule):
                (self.root / "verified-anchor.json").write_text(json.dumps({
                    "signature": self._legacy_anchor_signature(rule),
                    "report": "连续VAD确认固定偏移 +0.00 秒",
                }), encoding="utf-8")
                with patch.object(pro_core.legacy, "parse_subtitle", return_value=[]) as parse:
                    with self.assertRaisesRegex(RuntimeError, "没有可识别"):
                        pro_core._legacy_whisper_align_embedded_text_track(
                            str(self.movie), self.source, self.root, 1, "en", 2, lambda _: None
                        )
                parse.assert_called_once()


if __name__ == "__main__":
    unittest.main()
