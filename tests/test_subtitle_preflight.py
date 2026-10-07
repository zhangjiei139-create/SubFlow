# -*- coding: utf-8 -*-
from __future__ import annotations

import subprocess
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import batch_core
import media_title_resolver
import online_subtitles
import pro_core
import subtitle_tool_core as core


def write_timed_srt(path: Path, offset_seconds: float = 0.0, count: int = 40) -> None:
    lines = []
    for index in range(count):
        start_ms = int((30 + index * 10 + offset_seconds) * 1000)
        end_ms = start_ms + 2000
        lines.extend(
            [
                str(index + 1),
                f"{core.srt_time_from_milliseconds(start_ms)} --> {core.srt_time_from_milliseconds(end_ms)}",
                f"Dialogue line {index + 1}",
                "",
            ]
        )
    path.write_text("\n".join(lines), encoding="utf-8")
from profile_model import BatchPlan, PreferenceProfile


def write_movie_srt(path: Path, count: int = 20, final_hour: int = 1) -> None:
    blocks: list[str] = []
    for index in range(1, count + 1):
        hour = final_hour if index == count else 0
        minute = 50 if index == count else index
        blocks.append(
            f"{index}\n"
            f"{hour:02d}:{minute:02d}:00,000 --> "
            f"{hour:02d}:{minute:02d}:03,000\n"
            f"Line {index}\n"
        )
    path.write_text("\n".join(blocks), encoding="utf-8")


class SubtitleCandidateIdentityTest(unittest.TestCase):
    def test_request_uses_ipv4_opener_and_canonical_query(self) -> None:
        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self) -> bytes:
                return b'{"data": []}'

        opener = Mock()
        opener.open.return_value = FakeResponse()
        with patch.object(online_subtitles, "_IPV4_OPENER", opener):
            response = online_subtitles._request(
                "api-key",
                "/subtitles",
                params={"query": "How To Train Your Dragon", "page": 1, "languages": "en"},
            )

        request = opener.open.call_args.args[0]
        self.assertEqual(response, {"data": []})
        self.assertEqual(
            request.full_url,
            f"{online_subtitles.API_BASE}/subtitles?languages=en&query=how+to+train+your+dragon",
        )
        self.assertEqual(opener.open.call_args.kwargs["timeout"], online_subtitles.OPEN_TIMEOUT_SECONDS)

    def test_ipv4_connection_resolves_only_ipv4_addresses(self) -> None:
        connection = Mock()
        address = ("203.0.113.8", 443)
        with patch.object(
            online_subtitles.socket,
            "getaddrinfo",
            return_value=[
                (
                    online_subtitles.socket.AF_INET,
                    online_subtitles.socket.SOCK_STREAM,
                    online_subtitles.socket.IPPROTO_TCP,
                    "",
                    address,
                )
            ],
        ) as getaddrinfo, patch.object(
            online_subtitles.socket,
            "socket",
            return_value=connection,
        ):
            result = online_subtitles._create_ipv4_connection(("api.opensubtitles.com", 443), timeout=5)

        self.assertIs(result, connection)
        self.assertEqual(getaddrinfo.call_args.args[2], online_subtitles.socket.AF_INET)
        connection.settimeout.assert_called_once_with(5)
        connection.connect.assert_called_once_with(address)
    def test_title_connector_word_matches_ampersand(self) -> None:
        self.assertTrue(
            online_subtitles._titles_equivalent(
                "Peter Pan and Wendy",
                "Peter Pan & Wendy",
            )
        )
        self.assertTrue(
            online_subtitles._titles_equivalent(
                "Peter Pan and Wendy",
                "2023 - Peter Pan &amp; Wendy",
            )
        )

    def test_feature_identity_retries_ampersand_alias(self) -> None:
        identity = online_subtitles.MediaIdentity(
            "Peter Pan and Wendy",
            "Peter Pan and Wendy",
            "2023",
        )
        response = {
            "data": [
                {
                    "id": "peter-pan-wendy",
                    "type": "feature",
                    "attributes": {
                        "title": "Peter Pan & Wendy",
                        "original_title": "Peter Pan & Wendy",
                        "year": 2023,
                        "feature_type": "Movie",
                        "imdb_id": 5635026,
                    },
                }
            ]
        }
        with patch.object(
            online_subtitles,
            "_request",
            side_effect=[{"data": []}, response],
        ) as request:
            resolved = online_subtitles._resolve_feature_identity("key", identity)

        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.original_title, "Peter Pan & Wendy")
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args.kwargs["params"]["query"], "Peter Pan & Wendy")

    def test_feature_identity_falls_back_to_strictly_filtered_broad_search(self) -> None:
        identity = online_subtitles.MediaIdentity(
            "Peter Pan and Wendy",
            "Peter Pan and Wendy",
            "2023",
        )
        unrelated = {
            "data": [
                {
                    "id": "unrelated",
                    "type": "feature",
                    "attributes": {
                        "title": "Disneycember Peter Pan and Wendy",
                        "year": 2023,
                        "feature_type": "Movie",
                    },
                }
            ]
        }
        correct = {
            "data": [
                {
                    "id": "correct",
                    "type": "feature",
                    "attributes": {
                        "title": "Peter Pan & Wendy",
                        "original_title": "Peter Pan & Wendy",
                        "year": 2023,
                        "feature_type": "Movie",
                        "imdb_id": 5635026,
                        "tmdb_id": 420808,
                    },
                }
            ]
        }
        with patch.object(
            online_subtitles,
            "_request",
            side_effect=[{"data": []}, {"data": []}, unrelated, correct],
        ) as request:
            resolved = online_subtitles._resolve_feature_identity("key", identity)

        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.imdb_id, "5635026")
        self.assertEqual(request.call_count, 4)
        self.assertNotIn("query_match", request.call_args.kwargs["params"])

    def test_localized_title_is_resolved_before_title_year_search(self) -> None:
        identity = online_subtitles.MediaIdentity("魔境仙踪", "魔境仙踪", "2013")
        candidate = online_subtitles.SubtitleCandidate(
            1,
            "Oz.The.Great.and.Powerful.2013.srt",
            "Oz.The.Great.and.Powerful.2013.1080p.BluRay",
            "en",
            1000,
            9.0,
            True,
            False,
            feature_title="Oz the Great and Powerful",
            feature_year="2013",
            feature_imdb_id="1623205",
        )
        with patch.object(online_subtitles, "identify_media", return_value=identity), patch.object(
            online_subtitles, "opensubtitles_movie_hash", return_value=""
        ), patch.object(
            online_subtitles, "resolve_canonical_title", return_value="Oz the Great and Powerful"
        ), patch.object(
            online_subtitles, "remember_canonical_title"
        ), patch.object(
            online_subtitles, "_resolve_feature_identity", return_value=None
        ), patch.object(
            online_subtitles, "_search_pages", return_value=([candidate], 1, False)
        ) as search_pages:
            resolved, results, meta = online_subtitles.search("key", "movie.mkv", "en")

        self.assertEqual(resolved.original_title, "Oz the Great and Powerful")
        self.assertEqual(len(results), 1)
        self.assertEqual(meta.query_mode, "title-year")
        self.assertEqual(search_pages.call_args.args[1]["query"], "Oz the Great and Powerful")
        self.assertTrue(results[0].identity_verified)
        self.assertEqual(results[0].identity_key, "title-year:oz-the-great-powerful:2013")

    def test_unresolved_localized_title_retries_original_title_and_year(self) -> None:
        identity = online_subtitles.MediaIdentity("魔境仙踪", "魔境仙踪", "2013")
        with patch.object(online_subtitles, "identify_media", return_value=identity), patch.object(
            online_subtitles, "opensubtitles_movie_hash", return_value=""
        ), patch.object(
            online_subtitles, "resolve_canonical_title", return_value=""
        ), patch.object(
            online_subtitles, "_resolve_feature_identity", return_value=None
        ), patch.object(online_subtitles, "_search_pages", return_value=([], 0, False)) as search_pages:
            _resolved, results, meta = online_subtitles.search("key", "movie.mkv", "en")

        self.assertEqual(results, [])
        self.assertEqual(meta.query_mode, "unresolved-title")
        self.assertEqual(search_pages.call_args.args[1]["query"], "魔境仙踪")
        self.assertEqual(search_pages.call_args.args[1]["year"], "2013")

    def test_latin_title_does_not_need_local_model(self) -> None:
        self.assertFalse(media_title_resolver.needs_canonical_title("The Witch"))
        self.assertTrue(media_title_resolver.needs_canonical_title("魔境仙踪"))

    def test_non_latin_title_does_not_create_title_year_identity_lock(self) -> None:
        candidate = online_subtitles.SubtitleCandidate(
            1,
            "subtitle.srt",
            "720p.BluRay.x264-SPARKS",
            "en",
            1000,
            10.0,
            True,
            False,
            feature_title="Oz the Great and Powerful",
            feature_year="2013",
        )
        identity = online_subtitles.MediaIdentity(
            "\u9b54\u5883\u4ed9\u8e2a",
            "\u9b54\u5883\u4ed9\u8e2a",
            "2013",
        )

        self.assertEqual(online_subtitles._title_year_identity_key(candidate, identity), "")

    def test_feature_year_mismatch_is_rejected(self) -> None:
        candidate = online_subtitles.SubtitleCandidate(
            1,
            "subtitle.srt",
            "The.Witch.2015.1080p",
            "en",
            100,
            8.0,
            True,
            False,
            feature_title="The Witch",
            feature_year="2019",
        )
        identity = online_subtitles.MediaIdentity("The Witch", "The Witch", "2015")

        self.assertEqual(online_subtitles._title_year_identity_key(candidate, identity), "")

    def test_short_sequel_title_rejects_partial_title_match(self) -> None:
        candidate = online_subtitles.SubtitleCandidate(
            1,
            "Misty.Creed.2023.WEBRip-QY.srt",
            "2023 - Misty Creed (2023) · Misty.Creed.2023.WEBRip-QY",
            "en",
            61,
            0.0,
            False,
            False,
            feature_title="Misty Creed",
            feature_year="2023",
        )
        identity = online_subtitles.MediaIdentity("Creed III", "Creed III", "2023")

        self.assertFalse(online_subtitles._candidate_matches_identity(candidate, identity))

    def test_feature_identity_search_uses_imdb_id(self) -> None:
        identity = online_subtitles.MediaIdentity("Creed III", "Creed III", "2023")
        feature = online_subtitles.MediaIdentity(
            "Creed III",
            "Creed III",
            "2023",
            feature_id="123",
            imdb_id="11145118",
            tmdb_id="677179",
        )
        candidate = online_subtitles.SubtitleCandidate(
            2,
            "Creed.III.2023.BluRay.srt",
            "Creed.III.2023.BluRay",
            "en",
            100,
            8.0,
            True,
            False,
            feature_title="Creed III",
            feature_year="2023",
        )
        with patch.object(online_subtitles, "identify_media", return_value=identity), patch.object(
            online_subtitles, "opensubtitles_movie_hash", return_value=""
        ), patch.object(
            online_subtitles, "_resolve_feature_identity", return_value=feature
        ), patch.object(
            online_subtitles, "_search_pages", return_value=([candidate], 1, False)
        ) as search_pages:
            resolved, results, meta = online_subtitles.search(
                "key",
                "Creed.III.2023.UHD.BluRay.2160p.mkv",
                "en",
            )

        self.assertEqual(resolved.imdb_id, "11145118")
        self.assertEqual(meta.query_mode, "feature")
        self.assertEqual(len(results), 1)
        self.assertEqual(search_pages.call_args.args[1]["imdb_id"], "11145118")

    def test_feature_response_requires_exact_title_and_year(self) -> None:
        identity = online_subtitles.MediaIdentity("Creed III", "Creed III", "2023")
        response = {
            "data": [
                {
                    "id": "wrong",
                    "type": "feature",
                    "attributes": {
                        "title": "Misty Creed",
                        "original_title": "Misty Creed",
                        "year": 2023,
                        "feature_type": "Movie",
                        "imdb_id": 1,
                    },
                },
                {
                    "id": "correct",
                    "type": "feature",
                    "attributes": {
                        "title": "Creed III",
                        "original_title": "Creed III",
                        "year": 2022,
                        "feature_type": "Movie",
                        "imdb_id": 11145118,
                        "tmdb_id": 677179,
                    },
                },
            ],
        }

        resolved = online_subtitles._feature_identity_from_response(response, identity)

        self.assertIsNotNone(resolved)
        self.assertEqual(resolved.feature_id, "correct")
        self.assertEqual(resolved.imdb_id, "11145118")


class BatchSubtitleVerificationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = PreferenceProfile(
            1,
            "test",
            ["AAC"],
            True,
            False,
            ["zh-CN", "en"],
            "online",
        )
        self.tracks = [
            core.Track(0, "audio", "AAC", "eng", "", True, False, False, False, False),
        ]

    def test_unverified_external_subtitle_stays_in_review(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            subtitle = Path(folder) / "downloaded.srt"
            write_movie_srt(subtitle)
            with patch.object(batch_core, "_inspect_media_cached", return_value=({}, False)), patch.object(
                batch_core.core, "tracks_from_media", return_value=self.tracks
            ):
                plan = batch_core.analyze_video(
                    "movie.mkv", self.profile, str(subtitle), "en", False, ""
                )

        self.assertEqual(plan.status, "review")
        self.assertFalse(plan.external_subtitle_verified)

    def test_verified_external_subtitle_can_become_ready(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            subtitle = Path(folder) / "downloaded.srt"
            write_movie_srt(subtitle)
            with patch.object(batch_core, "_inspect_media_cached", return_value=({}, False)), patch.object(
                batch_core.core, "tracks_from_media", return_value=self.tracks
            ):
                plan = batch_core.analyze_video(
                    "movie.mkv",
                    self.profile,
                    str(subtitle),
                    "en",
                    True,
                    "verified",
                )

        self.assertEqual(plan.status, "ready")
        self.assertEqual(plan.source_mode, "online")
        self.assertTrue(plan.external_subtitle_verified)

    def test_smart_subtitle_cancellation_is_not_swallowed_by_image_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            plan = BatchPlan(
                "movie.mkv",
                1,
                status="review",
                output_path=str(Path(folder) / "output.mkv"),
                missing_subtitle_languages=["zh-CN"],
                has_image_subtitles=True,
            )
            with patch.object(batch_core.core, "inspect_tracks", return_value=self.tracks), patch(
                "smart_subtitles.find_verified_english",
                side_effect=core.CancelledError("用户已停止处理。"),
            ):
                with self.assertRaises(core.CancelledError):
                    batch_core.process_plan(
                        plan,
                        self.profile,
                        lambda _message: None,
                        None,
                    )
    def test_processing_guard_rejects_unverified_online_source(self) -> None:
        plan = BatchPlan("movie.mkv", 1, source_mode="online", status="ready")

        with self.assertRaises(RuntimeError):
            batch_core.process_plan(plan, self.profile, lambda _message: None, None)


class ExternalSubtitlePreflightTest(unittest.TestCase):
    def test_targeted_audio_passes_exact_windows_to_shared_cache(self) -> None:
        cue = core.SubtitleEvent("00:10:00,000", "00:10:02,000", "First line")
        other_cue = core.SubtitleEvent("00:10:04,000", "00:10:06,000", "Other line")
        with tempfile.TemporaryDirectory() as folder, patch.object(
            pro_core.audio_offset_verifier, "transcribe_exact_windows", return_value=({}, {}, {})
        ) as evidence, patch.object(pro_core, "_tool", return_value="test-tool"):
            def fingerprint(current_cue):
                return pro_core._extract_targeted_dialogue_fingerprint(
                    "movie.mkv", (current_cue,), work_dir=Path(folder) / "audio",
                    audio_stream_index=1, audio_language="en",
                    provisional_offset=0.0, start_index=0,
                    log=lambda _message: None, cancel=None,
                )

            fingerprint(cue)
            fingerprint(cue)
            fingerprint(other_cue)
        self.assertEqual(evidence.call_count, 3)
        self.assertEqual(evidence.call_args_list[0].args[1], evidence.call_args_list[1].args[1])
        self.assertNotEqual(evidence.call_args_list[0].args[1], evidence.call_args_list[2].args[1])

    def test_audio_alignment_reapplies_container_start_delta(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.srt"
            destination = root / "aligned.srt"
            write_timed_srt(source, count=40)

            captured_args = []

            def fake_run(_args, _log, _cancel, _cwd=None):
                captured_args.extend(_args)
                pro_core._shifted_srt(source, destination, 50)
                return pro_core.subprocess.CompletedProcess(
                    _args,
                    0,
                    stdout=b"score: 278714\noffset seconds: 0.05\n",
                    stderr=b"",
                )

            diagnostics = {}
            with patch.object(pro_core, "_tool", return_value="ffsubsync.exe"), patch.object(
                pro_core, "_run", side_effect=fake_run
            ), patch.object(
                pro_core, "_audio_container_start_delta", return_value=1.008
            ), patch.object(
                pro_core, "_audio_stream_index", return_value=0
            ):
                result = pro_core._alignment_candidate(
                    "movie.mkv",
                    source,
                    destination,
                    1,
                    "audio-full",
                    lambda _message: None,
                    diagnostics=diagnostics,
                )

            self.assertEqual(pro_core._subtitle_offset_milliseconds(source, result), 1058)
            self.assertAlmostEqual(diagnostics["raw_offset_seconds"], 0.05)
            self.assertAlmostEqual(diagnostics["container_start_delta_seconds"], 1.008)
            self.assertAlmostEqual(diagnostics["offset_seconds"], 1.058)
            max_offset_index = captured_args.index("--max-offset-seconds")
            self.assertEqual(captured_args[max_offset_index + 1], "10")

    def test_public_preflight_reports_hard_time_budget(self) -> None:
        with patch.object(pro_core.time, "monotonic", side_effect=[0.0, 6.0]), patch.object(
            pro_core,
            "_preflight_external_subtitle_impl",
            side_effect=core.CancelledError("用户已停止处理。"),
        ):
            with self.assertRaisesRegex(RuntimeError, "5 秒硬上限"):
                pro_core.preflight_external_subtitle(
                    "movie.mkv",
                    "subtitle.srt",
                    "work",
                    0,
                    lambda _message: None,
                    time_budget_seconds=5.0,
                )

    def test_affine_timeline_detects_fixed_offset_and_framerate_drift(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            candidate = root / "candidate.srt"
            reference = root / "reference.srt"
            candidate_events = []
            reference_events = []
            for index in range(40):
                candidate_time = 30.0 + index * 125.0
                reference_time = 7.75 + candidate_time * 1.001
                text = f"Unique dialogue sentence number {index}"
                candidate_events.append(core.SubtitleEvent(
                    core.srt_time_from_milliseconds(round(candidate_time * 1000)),
                    core.srt_time_from_milliseconds(round((candidate_time + 2) * 1000)),
                    text,
                ))
                reference_events.append(core.SubtitleEvent(
                    core.srt_time_from_milliseconds(round(reference_time * 1000)),
                    core.srt_time_from_milliseconds(round((reference_time + 2.002) * 1000)),
                    text,
                ))
            core.write_srt(candidate, candidate_events, {
                index: event.text for index, event in enumerate(candidate_events, 1)
            })
            core.write_srt(reference, reference_events, {
                index: event.text for index, event in enumerate(reference_events, 1)
            })

            match = pro_core._estimate_affine_timeline(candidate, reference, 5_000.0)

            self.assertIsNotNone(match)
            self.assertAlmostEqual(match.offset_seconds, 7.75, delta=0.02)
            self.assertAlmostEqual(match.scale, 1.001, delta=0.00001)
            self.assertLess(match.p90_residual, 0.02)

    def test_media_timeline_health_rejects_large_audio_video_start_gap(self) -> None:
        media = {
            "tracks": [
                {"type": "video", "properties": {"minimum_timestamp": 0, "tag_duration": "01:30:00.000"}},
                {"type": "audio", "properties": {"minimum_timestamp": 3_500_000_000, "tag_duration": "01:30:00.000"}},
            ]
        }
        with self.assertRaisesRegex(RuntimeError, "起点相差"):
            pro_core._log_media_timeline_health(media, lambda _message: None)

    def test_affine_timeline_rejects_nonlinear_edit_differences(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            candidate = root / "candidate.srt"
            reference = root / "reference.srt"
            candidate_events = []
            reference_events = []
            for index in range(40):
                candidate_time = 30.0 + index * 125.0
                edit_jump = (index % 5) * 3.0
                reference_time = 5.0 + candidate_time + edit_jump
                text = f"Nonlinear unique dialogue number {index}"
                candidate_events.append(core.SubtitleEvent(
                    core.srt_time_from_milliseconds(round(candidate_time * 1000)),
                    core.srt_time_from_milliseconds(round((candidate_time + 2) * 1000)),
                    text,
                ))
                reference_events.append(core.SubtitleEvent(
                    core.srt_time_from_milliseconds(round(reference_time * 1000)),
                    core.srt_time_from_milliseconds(round((reference_time + 2) * 1000)),
                    text,
                ))
            core.write_srt(candidate, candidate_events, {
                index: event.text for index, event in enumerate(candidate_events, 1)
            })
            core.write_srt(reference, reference_events, {
                index: event.text for index, event in enumerate(reference_events, 1)
            })

            match = pro_core._estimate_affine_timeline(candidate, reference, 5_000.0)

            self.assertIsNone(match)

    def test_embedded_multilanguage_text_tracks_are_verified_independently(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            english = root / "english.srt"
            chinese = root / "chinese.srt"
            corrected = root / "corrected.srt"
            write_timed_srt(english)
            write_timed_srt(chinese)
            write_timed_srt(corrected, 30.31)
            tracks = [
                core.Track(1, "audio", "E-AC-3", "eng", "", True, False, False, False, False),
                core.Track(2, "subtitles", "SubRip/SRT", "eng", "", True, False, True, False, False),
                core.Track(3, "subtitles", "SubRip/SRT", "chi", "", False, False, True, False, False),
            ]

            def extract_many(args, **_kwargs):
                for spec in args[3:]:
                    track_id, destination = spec.split(":", 1)
                    source = english if track_id == "2" else chinese
                    Path(destination).write_bytes(source.read_bytes())
                return subprocess.CompletedProcess(args, 0, b"", b"")

            with patch.object(pro_core.legacy, "inspect_tracks", return_value=tracks), patch.object(
                pro_core.legacy, "run_command", side_effect=extract_many
            ), patch.object(
                pro_core.legacy,
                "inspect_media",
                return_value={"container": {"properties": {"duration": 7_200_000_000_000}}},
            ), patch.object(
                pro_core,
                "align_embedded_text_track",
                return_value=(corrected, "逐词音频证据确认固定偏移 +30.31 秒；测试证据"),
            ) as align_track:
                offsets, corrected_sources, resolved_source = pro_core.prepare_embedded_text_corrections(
                    "movie.mkv",
                    [2, 3],
                    2,
                    1,
                    str(root / "work"),
                    lambda _message: None,
                )

        self.assertEqual(offsets, {2: 30310, 3: 30310})
        self.assertEqual(resolved_source, 2)
        self.assertEqual(align_track.call_count, 2)
        self.assertTrue(corrected_sources[3].name.endswith(".srt"))

    def test_unverified_embedded_track_cannot_shift_another_track(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            english = root / "english.srt"
            chinese = root / "chinese.srt"
            write_timed_srt(english, 0.0, count=40)
            write_timed_srt(chinese, 1.0, count=40)
            original_english = english.read_text(encoding="utf-8-sig")
            original_chinese = chinese.read_text(encoding="utf-8-sig")
            tracks = [
                core.Track(1, "audio", "E-AC-3", "eng", "", True, False, False, False, False),
                core.Track(2, "subtitles", "SubRip/SRT", "eng", "", True, False, True, False, False),
                core.Track(3, "subtitles", "SubRip/SRT", "chi", "", False, False, True, False, False),
            ]

            def extract_many(args, **_kwargs):
                for spec in args[3:]:
                    track_id, destination = spec.split(":", 1)
                    source = english if track_id == "2" else chinese
                    Path(destination).write_bytes(source.read_bytes())
                return subprocess.CompletedProcess(args, 0, b"", b"")

            def insufficient(_input, normalized, *_args, **_kwargs):
                return Path(normalized), "建议偏移未获逐词证据支持，保留原时间轴；证据不足"

            status: dict[str, object] = {}
            with patch.object(pro_core.legacy, "inspect_tracks", return_value=tracks), patch.object(
                pro_core.legacy, "run_command", side_effect=extract_many
            ), patch.object(
                pro_core.legacy,
                "inspect_media",
                return_value={"container": {"properties": {"duration": 7_200_000_000_000}}},
            ), patch.object(
                pro_core, "align_embedded_text_track", side_effect=insufficient
            ) as align_track:
                offsets, corrected, resolved = pro_core.prepare_embedded_text_corrections(
                    "movie.mkv",
                    [2, 3],
                    2,
                    1,
                    str(root / "work"),
                    lambda _message: None,
                    verification_status=status,
                )
                corrected_english = corrected[2].read_text(encoding="utf-8-sig")
                corrected_chinese = corrected[3].read_text(encoding="utf-8-sig")

        self.assertEqual(offsets, {2: 0, 3: 0})
        self.assertEqual(resolved, 2)
        self.assertEqual(align_track.call_count, 2)
        self.assertFalse(status["verified_anchor"])
        self.assertEqual(corrected_english, original_english)
        self.assertEqual(corrected_chinese, original_chinese)

    def test_embedded_text_material_alignment_preserves_verified_original_timeline(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            original = root / "original.srt"
            write_timed_srt(original)
            media = {"container": {"properties": {"duration": 400_000_000_000}}}
            content_audio = Mock(audio_language="en", fingerprint=Mock())
            proposed = Mock(accepted=True, offset_seconds=-3.07, affine=False, reason="")
            guard = pro_core.subtitle_offset_guard.OffsetGuardDecision(
                True,
                "KEEP_ORIGINAL",
                0.0,
                -3.07,
                (-0.45, -0.04, -0.62, 0.34),
                0.40,
                2.66,
                "原时间轴逐词残差更小",
            )

            with patch.object(pro_core.legacy, "inspect_media", return_value=media), patch.object(
                pro_core, "_preferred_validation_audio_id", return_value=15
            ), patch.object(
                pro_core.audio_offset_verifier, "match_subtitle_to_fingerprint", return_value=proposed
            ), patch.object(
                pro_core.subtitle_offset_guard, "evaluate_proposed_offset", return_value=guard
            ) as exact_guard, patch.object(
                pro_core, "semantic_spot_check"
            ) as old_spot_check:
                selected, report = pro_core.align_embedded_text_track(
                    "movie.mkv",
                    original,
                    root,
                    15,
                    "en",
                    1,
                    lambda _message: None,
                    shared_content_audio=content_audio,
                )

        self.assertEqual(selected, original)
        self.assertIn("原时间轴更可靠", report)
        exact_guard.assert_called_once()
        old_spot_check.assert_not_called()

    def test_embedded_text_tiny_offset_does_not_scan_audio_twice(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            original = root / "original.srt"
            write_timed_srt(original)
            media = {"container": {"properties": {"duration": 400_000_000_000}}}
            content_audio = Mock(audio_language="en", fingerprint=Mock())
            proposed = Mock(accepted=True, offset_seconds=0.03, affine=False, reason="")
            guard = pro_core.subtitle_offset_guard.OffsetGuardDecision(
                True,
                "KEEP_ORIGINAL",
                0.0,
                0.03,
                (0.01, 0.04, -0.02),
                0.02,
                0.03,
                "偏移不足以证明需要修改",
            )

            with patch.object(pro_core.legacy, "inspect_media", return_value=media), patch.object(
                pro_core, "_preferred_validation_audio_id", return_value=1
            ), patch.object(
                pro_core.audio_offset_verifier, "match_subtitle_to_fingerprint", return_value=proposed
            ), patch.object(
                pro_core.subtitle_offset_guard, "evaluate_proposed_offset", return_value=guard
            ), patch.object(
                pro_core, "prepare_shared_subtitle_content_audio"
            ) as repeated_audio, patch.object(
                pro_core, "preflight_external_subtitle"
            ) as repeated_preflight:
                selected, report = pro_core.align_embedded_text_track(
                    "movie.mkv",
                    original,
                    root,
                    1,
                    "en",
                    2,
                    lambda _message: None,
                    shared_content_audio=content_audio,
                )

        self.assertEqual(selected, original)
        self.assertIn("原时间轴更可靠", report)
        repeated_audio.assert_not_called()
        repeated_preflight.assert_not_called()

    def test_large_external_timeline_disagreement_keeps_embedded_original(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            external = root / "verified-english.srt"
            english = root / "english.srt"
            chinese = root / "chinese.srt"
            write_timed_srt(external, 20.0, count=40)
            write_timed_srt(english, 0.03, count=40)
            write_timed_srt(chinese, 0.03, count=40)
            tracks = [
                core.Track(1, "audio", "DTS-HD", "eng", "", True, False, False, False, False),
                core.Track(2, "subtitles", "SubRip/SRT", "eng", "", True, False, True, False, False),
                core.Track(3, "subtitles", "SubRip/SRT", "chi", "", False, False, True, False, False),
            ]

            def extract_many(args, **_kwargs):
                for spec in args[3:]:
                    track_id, destination = spec.split(":", 1)
                    source = english if track_id == "2" else chinese
                    Path(destination).write_bytes(source.read_bytes())
                return subprocess.CompletedProcess(args, 0, b"", b"")

            def independently_align(_input, normalized, *_args, **_kwargs):
                return Path(normalized), "逐词证据确认原时间轴更可靠；保留原时间轴"

            with patch.object(pro_core.legacy, "inspect_tracks", return_value=tracks), patch.object(
                pro_core.legacy, "run_command", side_effect=extract_many
            ), patch.object(
                pro_core.legacy,
                "inspect_media",
                return_value={"container": {"properties": {"duration": 7_200_000_000_000}}},
            ), patch.object(
                pro_core, "align_embedded_text_track", side_effect=independently_align
            ) as audio_alignment:
                offsets, corrected, resolved = pro_core.prepare_embedded_text_corrections(
                    "movie.mkv",
                    [2, 3],
                    2,
                    1,
                    str(root / "work"),
                    lambda _message: None,
                    verified_external_subtitle=str(external),
                )

        self.assertEqual(offsets, {2: 0, 3: 0})
        self.assertEqual(set(corrected), {2, 3})
        self.assertEqual(resolved, 2)
        audio_alignment.assert_not_called()

    def test_embedded_text_rejects_large_offset_without_word_support(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            original = root / "original.srt"
            write_timed_srt(original, count=40)
            media = {"container": {"properties": {"duration": 400_000_000_000}}}
            content_audio = Mock(audio_language="en", fingerprint=Mock())
            proposed = Mock(accepted=True, offset_seconds=-94.62, affine=False, reason="")
            guard = pro_core.subtitle_offset_guard.OffsetGuardDecision(
                False,
                "REJECT",
                None,
                -94.62,
                (-0.4, 0.2, -0.1),
                0.23,
                94.5,
                "建议偏移会显著破坏逐词时间关系",
            )

            with patch.object(pro_core.legacy, "inspect_media", return_value=media), patch.object(
                pro_core, "_preferred_validation_audio_id", return_value=1
            ), patch.object(
                pro_core.audio_offset_verifier, "match_subtitle_to_fingerprint", return_value=proposed
            ), patch.object(
                pro_core.subtitle_offset_guard, "evaluate_proposed_offset", return_value=guard
            ), patch.object(
                pro_core, "semantic_spot_check"
            ) as old_spot_check:
                selected, report = pro_core.align_embedded_text_track(
                    "movie.mkv",
                    original,
                    root,
                    1,
                    "en",
                    3,
                    lambda _message: None,
                    shared_content_audio=content_audio,
                )

        self.assertEqual(selected, original)
        self.assertIn("保留原时间轴", report)
        old_spot_check.assert_not_called()

    def test_validation_audio_prefers_subtitle_language_over_default_audio(self) -> None:
        tracks = [
            core.Track(1, "audio", "AC-3", "tur", "", True, False, False, False, False),
            core.Track(2, "audio", "AC-3", "eng", "", False, False, False, False, False),
        ]

        with patch.object(pro_core.legacy, "inspect_tracks", return_value=tracks):
            selected = pro_core._preferred_validation_audio_id("movie.mkv", 1, "en")

        self.assertEqual(selected, 2)

    def test_validation_audio_falls_back_to_selected_when_language_is_missing(self) -> None:
        tracks = [
            core.Track(1, "audio", "AC-3", "tur", "", True, False, False, False, False),
            core.Track(2, "audio", "AC-3", "deu", "", False, False, False, False, False),
        ]

        with patch.object(pro_core.legacy, "inspect_tracks", return_value=tracks):
            selected = pro_core._preferred_validation_audio_id("movie.mkv", 1, "en")

        self.assertEqual(selected, 1)

    def test_embedded_reference_ignores_forced_and_prefers_same_language(self) -> None:
        tracks = [
            core.Track(1, "audio", "AAC", "eng", "", True, False, False, False, False),
            core.Track(2, "subtitles", "SubRip/SRT", "eng", "Forced", True, True, True, False, False),
            core.Track(3, "subtitles", "SubRip/SRT", "chi", "完整中文字幕", False, False, True, False, False),
            core.Track(4, "subtitles", "SubRip/SRT", "eng", "English", False, False, True, False, False),
        ]

        with patch.object(pro_core.legacy, "inspect_tracks", return_value=tracks):
            mode, stream_index, selected = pro_core._embedded_subtitle_reference("movie.mkv", "en")

        self.assertEqual(mode, "subtitle")
        self.assertEqual(stream_index, 2)
        self.assertEqual(selected.id, 4)

    def test_semantic_score_separates_matching_and_unrelated_dialogue(self) -> None:
        expected = "I know what you mean. We have to leave this place before sunrise."
        matching = "I know what you mean, we have to leave this place before the sun rises."
        unrelated = "The weather report says heavy rain will arrive tomorrow afternoon."

        matching_score = pro_core.semantic_text_score(expected, matching)
        unrelated_score = pro_core.semantic_text_score(expected, unrelated)

        self.assertGreater(matching_score, 0.55)
        self.assertLess(unrelated_score, 0.25)
        self.assertGreater(matching_score, unrelated_score)

    def test_completeness_rejects_sparse_narrative_track(self) -> None:
        duration = 7638.923
        partial = [
            core.SubtitleEvent(
                core.srt_time_from_milliseconds(int((1986 + index * 68) * 1000)),
                core.srt_time_from_milliseconds(int((1989 + index * 68) * 1000)),
                f"Narrative line {index}",
            )
            for index in range(68)
        ]
        complete = [
            core.SubtitleEvent(
                core.srt_time_from_milliseconds(int((6 + index * 3.62) * 1000)),
                core.srt_time_from_milliseconds(int((9 + index * 3.62) * 1000)),
                f"Dialogue line {index}",
            )
            for index in range(2099)
        ]

        partial_result = pro_core.subtitle_completeness(partial, duration)
        complete_result = pro_core.subtitle_completeness(complete, duration)

        self.assertFalse(partial_result.accepted)
        self.assertTrue(complete_result.accepted)
        self.assertGreater(complete_result.score, partial_result.score)

    def test_source_selection_replaces_incomplete_first_english_track(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            partial_path = root / "partial.srt"
            complete_path = root / "complete.srt"
            partial_events = [
                core.SubtitleEvent(
                    core.srt_time_from_milliseconds(int((1986 + index * 68) * 1000)),
                    core.srt_time_from_milliseconds(int((1989 + index * 68) * 1000)),
                    f"Narrative line {index}",
                )
                for index in range(68)
            ]
            complete_events = [
                core.SubtitleEvent(
                    core.srt_time_from_milliseconds(int((6 + index * 3.62) * 1000)),
                    core.srt_time_from_milliseconds(int((9 + index * 3.62) * 1000)),
                    f"Dialogue line {index}",
                )
                for index in range(2099)
            ]
            core.write_srt(
                partial_path,
                partial_events,
                {index: event.text for index, event in enumerate(partial_events, 1)},
            )
            core.write_srt(
                complete_path,
                complete_events,
                {index: event.text for index, event in enumerate(complete_events, 1)},
            )
            tracks = [
                core.Track(2, "subtitles", "SubRip/SRT", "eng", "English", False, False, True, False, False),
                core.Track(3, "subtitles", "SubRip/SRT", "eng", "English", False, False, True, False, False),
            ]
            messages: list[str] = []

            selected = pro_core.choose_complete_subtitle_source(
                tracks,
                {2: partial_path, 3: complete_path},
                2,
                7638.923,
                messages.append,
            )

        self.assertEqual(selected, 3)
        self.assertTrue(any("已自动改用完整字幕轨 3" in message for message in messages))

    def test_pgs_intervals_use_clear_packets_when_duration_is_missing(self) -> None:
        packets = [
            {"pts_time": "10.0", "size": "6416"},
            {"pts_time": "13.5", "size": "30"},
            {"pts_time": "20.0", "size": "7200"},
            {"pts_time": "24.0", "size": "29"},
        ]

        intervals = pro_core._infer_pgs_intervals(packets)

        self.assertEqual(intervals, [(10.0, 13.5), (20.0, 24.0)])

    def test_sparse_pgs_only_fast_paths_three_consistent_near_zero_regions(self) -> None:
        reference = [
            (
                float(center + index * 2.3 + index * index * 0.04),
                float(center + index * 2.3 + index * index * 0.04 + 1),
            )
            for center in (450, 1650, 2850)
            for index in range(30)
        ]
        windows = pro_core._pgs_sample_windows(reference, 3600.0)
        self.assertEqual(len(windows), 3)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            movie = root / "movie.mkv"
            movie.write_bytes(b"video")
            for label, shifts, expected in (
                ("unchanged", (0.1, 0.1, 0.1), True),
                ("fixed-offset", (1.5, 1.5, 1.5), False),
                ("conflicting-region", (0.1, 0.1, 1.1), False),
            ):
                packets = [
                    {
                        "stream_index": 7,
                        "pts_time": str(start + shifts[region]),
                        "duration_time": "1.0",
                        "size": "1000",
                    }
                    for region, (window_start, window_end) in enumerate(windows)
                    for start, _end in reference
                    if window_start + 3 <= start < window_end - 3
                ]
                completed = subprocess.CompletedProcess(
                    ["ffprobe"], 0,
                    json.dumps({"packets": packets, "streams": [
                        {"index": 3, "codec_name": "subrip"},
                        {"index": 7, "codec_name": "hdmv_pgs_subtitle"},
                    ]}).encode("utf-8"), b"",
                )
                with self.subTest(label=label), patch.object(
                    pro_core.legacy, "resolve_config_path", return_value=sys.executable
                ), patch.object(
                    pro_core.legacy, "run_command", return_value=completed
                ) as probe:
                    result = pro_core._pgs_sampled_noop(
                        str(movie), 1, reference, 3600.0,
                        lambda _message: None, None, root / label,
                    )
                    self.assertEqual(result, expected)
                    self.assertIn("s", probe.call_args.args[0])
                    if label == "unchanged":
                        self.assertTrue(pro_core._pgs_sampled_noop(
                            str(movie), 1, reference, 3600.0,
                            lambda _message: None, None, root / label,
                        ))
                        probe.assert_called_once()

    def test_semantic_samples_cover_four_dense_regions(self) -> None:
        events = []
        for index in range(100):
            second = index * 60
            start = f"{second // 3600:02d}:{second % 3600 // 60:02d}:00,000"
            end = f"{second // 3600:02d}:{second % 3600 // 60:02d}:04,000"
            events.append(core.SubtitleEvent(start, end, f"Dialogue line {index}"))

        samples = pro_core._semantic_samples(events, 6000.0)

        self.assertEqual(len(samples), 4)
        starts = [sample[0] for sample in samples]
        self.assertLess(starts[0], 1600)
        self.assertGreater(starts[-1], 4400)

    def test_incomplete_ffsubsync_output_is_not_silently_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);subtitle=root/'subtitle.srt'
            write_movie_srt(subtitle)
            completed=subprocess.CompletedProcess(['ffsubsync'],0,b'',b'score: -1.000\nlow-quality alignment')
            with patch.object(pro_core,'_run',return_value=completed),patch.object(pro_core,'_audio_stream_index',return_value=0):
                with self.assertRaises(pro_core.SubtitleVerificationToolError):
                    pro_core._alignment_candidate('movie.mkv',subtitle,root/'output.srt',0,'audio-continuous',lambda _:None)
            self.assertFalse((root/'output.srt').exists())

    def test_preflight_can_apply_verified_small_offset(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            subtitle = root / "subtitle.srt"
            write_movie_srt(subtitle)
            work = root / "work"
            media = {"container": {"properties": {"duration": 7_200_000_000_000}}}
            modes: list[str] = []

            def fake_candidate(
                _input,
                _normalized,
                destination,
                _audio_id,
                mode,
                _log,
                _cancel=None,
                **_kwargs,
            ):
                modes.append(mode)
                destination.write_text(f"{mode} candidate", encoding="utf-8")
                return destination

            audio_result = pro_core.SemanticCheckResult(
                "十段音频采样校正时间轴", (0.62, 0.66, 0.70, 0.64), 4
            )
            with patch.object(pro_core.legacy, "inspect_media", return_value=media), patch.object(
                pro_core, "_embedded_subtitle_reference", return_value=None
            ), patch.object(
                pro_core, "_alignment_candidate", side_effect=fake_candidate
            ), patch.object(
                pro_core,
                "semantic_spot_check",
                return_value=audio_result,
            ):
                selected, summary = pro_core.preflight_external_subtitle(
                    "movie.mkv",
                    str(subtitle),
                    str(work),
                    0,
                    lambda _message: None,
                    source_language="en",
                )

            self.assertEqual(selected.name, "external-verified.srt")
            self.assertEqual(selected.read_text(encoding="utf-8-sig"), "audio-segmented candidate")
            self.assertEqual(modes, ["audio-segmented"])
            self.assertIn("十段音频采样", summary)

    def test_shared_alignment_audio_is_built_once_and_reused(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video = root / "movie.mkv"
            video.write_bytes(b"video")
            messages: list[str] = []

            def create_audio(args, *_args, **_kwargs):
                Path(args[1]).with_suffix(".npz").write_bytes(b"speech" * 1000)
                return subprocess.CompletedProcess(args, 0, b"", b"")

            with patch.object(core, "inspect_tracks", return_value=[core.Track(1, "audio", "AAC", "en", "", True, False, False, False, False)]), patch.object(
                pro_core, "_run", side_effect=create_audio
            ) as run:
                first = pro_core.prepare_shared_alignment_audio(
                    str(video),
                    1,
                    root / "cache",
                    messages.append,
                )
                second = pro_core.prepare_shared_alignment_audio(
                    str(video),
                    1,
                    root / "cache",
                    messages.append,
                )

            self.assertEqual(first, second)
            self.assertEqual(run.call_count, 1)
            self.assertEqual(first.suffix, ".npz")
            self.assertTrue(any("复用影片连续VAD指纹" in message for message in messages))

    def test_preflight_passes_shared_audio_reference_to_alignment(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            subtitle = root / "subtitle.srt"
            write_movie_srt(subtitle)
            media = {"container": {"properties": {"duration": 7_200_000_000_000}}}
            passed = pro_core.SemanticCheckResult(
                "十段音频采样校正时间轴",
                (0.80, 0.82),
                4,
                fast_path=True,
            )

            def align(_input, normalized, destination, *_args, **_kwargs):
                self.assertEqual(_kwargs["shared_audio_reference"], root / "shared.npz")
                destination.write_bytes(normalized.read_bytes())
                return destination

            with patch.object(pro_core.legacy, "inspect_media", return_value=media), patch.object(
                pro_core, "_embedded_subtitle_reference", return_value=None
            ), patch.object(
                pro_core, "_alignment_candidate", side_effect=align
            ) as alignment, patch.object(
                pro_core, "semantic_spot_check", return_value=passed
            ):
                selected, _summary = pro_core.preflight_external_subtitle(
                    "movie.mkv",
                    str(subtitle),
                    str(root / "work"),
                    0,
                    lambda _message: None,
                    source_language="en",
                    use_embedded_reference=False,
                    shared_audio_reference=root / "shared.npz",
                )

            alignment.assert_called_once()
            self.assertEqual(selected.name, "external-verified.srt")

    def test_moderate_fixed_offset_allows_strict_two_point_fast_accept(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            subtitle = root / "subtitle.srt"
            write_movie_srt(subtitle)
            media = {"container": {"properties": {"duration": 7_200_000_000_000}}}
            fast_flags: list[bool] = []

            def fake_candidate(
                _input,
                _normalized,
                destination,
                _audio_id,
                _mode,
                _log,
                _cancel=None,
                **kwargs,
            ):
                kwargs["diagnostics"]["offset_seconds"] = 4.26
                destination.write_text("aligned candidate", encoding="utf-8")
                return destination

            def fake_semantic(*_args, **kwargs):
                fast_flags.append(bool(kwargs.get("allow_fast_accept")))
                return pro_core.SemanticCheckResult(
                    "十段音频采样校正时间轴", (0.78, 0.87), 4, fast_path=True
                )

            with patch.object(pro_core.legacy, "inspect_media", return_value=media), patch.object(
                pro_core, "_embedded_subtitle_reference", return_value=None
            ), patch.object(
                pro_core, "_alignment_candidate", side_effect=fake_candidate
            ), patch.object(
                pro_core, "semantic_spot_check", side_effect=fake_semantic
            ):
                selected, _summary = pro_core.preflight_external_subtitle(
                    "movie.mkv",
                    str(subtitle),
                    str(root / "work"),
                    0,
                    lambda _message: None,
                    source_language="en",
                    use_embedded_reference=True,
                )

        self.assertTrue(fast_flags[0])
        self.assertEqual(selected.name, "external-verified.srt")

    def test_preflight_uses_only_audio_verified_embedded_text_reference(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video = root / "movie.mkv"
            video.write_bytes(b"video")
            subtitle = root / "subtitle.srt"
            embedded = root / "embedded-audio-verified.srt"
            write_timed_srt(subtitle, count=700)
            write_timed_srt(embedded, offset_seconds=1.5, count=700)
            media = {"container": {"properties": {"duration": 7_200_000_000_000}}}
            affine = pro_core.AffineTimelineMatch(1.5, 1.0, 700, 0.9, 0.05, 0.1)

            def apply_timeline(source, destination, _match):
                destination.write_bytes(Path(source).read_bytes())
                return destination

            with patch.object(pro_core.legacy, "inspect_media", return_value=media), patch.object(
                pro_core,
                "_prepare_aligned_embedded_text_reference",
                return_value=(embedded, "内嵌文本已独立通过音频核验"),
            ) as prepare_reference, patch.object(
                pro_core, "_estimate_affine_timeline", return_value=affine
            ), patch.object(
                pro_core, "_apply_affine_timeline", side_effect=apply_timeline
            ), patch.object(
                pro_core, "_alignment_candidate"
            ) as audio_alignment:
                selected, summary = pro_core.preflight_external_subtitle(
                    str(video),
                    str(subtitle),
                    str(root / "work"),
                    0,
                    lambda _message: None,
                    source_language="en",
                )

            prepare_reference.assert_called_once()
            audio_alignment.assert_not_called()
            self.assertEqual(selected.name, "external-verified.srt")
            self.assertIn("精确参照校正", summary)

    def test_semantic_check_stops_only_when_acceptance_is_impossible(self) -> None:
        self.assertFalse(
            pro_core._semantic_acceptance_still_possible([0.10, 0.12], 2, 4)
        )
        self.assertTrue(
            pro_core._semantic_acceptance_still_possible([0.10, 0.70], 2, 4)
        )
        self.assertFalse(
            pro_core._semantic_acceptance_still_possible([], 2, 4)
        )
    def test_strict_two_point_result_can_fast_accept(self) -> None:
        result = pro_core.SemanticCheckResult("small offset", (0.72, 0.81), 4, True)
        self.assertTrue(result.accepted)

    def test_verifier_tool_failure_retries_once_and_is_not_content_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            subtitle = root / "subtitle.srt"
            write_movie_srt(subtitle)
            media = {"container": {"properties": {"duration": 7_200_000_000_000}}}

            def fake_candidate(
                _input, _normalized, destination, _audio_id, _mode, _log,
                _cancel=None, diagnostics=None, **_kwargs,
            ):
                destination.write_text("candidate", encoding="utf-8")
                if diagnostics is not None:
                    diagnostics["offset_seconds"] = 1.24
                return destination

            with patch.object(pro_core.legacy, "inspect_media", return_value=media), patch.object(
                pro_core, "_embedded_subtitle_reference", return_value=None
            ), patch.object(
                pro_core, "_alignment_candidate", side_effect=fake_candidate
            ), patch.object(
                pro_core, "semantic_spot_check", side_effect=OSError("whisper unavailable")
            ) as semantic:
                with self.assertRaises(pro_core.SubtitleVerificationToolError):
                    pro_core.preflight_external_subtitle(
                        "movie.mkv",
                        str(subtitle),
                        str(root / "work"),
                        0,
                        lambda _message: None,
                        source_language="en",
                    )

            self.assertEqual(semantic.call_count, 2)

    def test_preflight_rejects_large_offset_when_corrected_timeline_fails(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            subtitle = root / "subtitle.srt"
            write_movie_srt(subtitle)
            media = {"container": {"properties": {"duration": 7_200_000_000_000}}}

            def fake_candidate(
                _input, _normalized, destination, _audio_id, _mode, _log,
                _cancel=None, diagnostics=None, **_kwargs,
            ):
                destination.write_text("large offset candidate", encoding="utf-8")
                diagnostics["offset_seconds"] = 118.04
                return destination

            failed = pro_core.SemanticCheckResult(
                "large offset candidate", (0.15, 0.16, 0.17), 4
            )
            with patch.object(pro_core.legacy, "inspect_media", return_value=media), patch.object(
                pro_core, "_alignment_candidate", side_effect=fake_candidate
            ), patch.object(
                pro_core, "semantic_spot_check", return_value=failed
            ) as semantic:
                with self.assertRaisesRegex(RuntimeError, "118.04"):
                    pro_core.preflight_external_subtitle(
                        "movie.mkv",
                        str(subtitle),
                        str(root / "work"),
                        0,
                        lambda _message: None,
                        source_language="en",
                    )

            self.assertEqual(semantic.call_count, 2)

    def test_preflight_uses_original_timeline_when_large_alignment_is_false_positive(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            subtitle = root / "subtitle.srt"
            write_movie_srt(subtitle)
            media = {"container": {"properties": {"duration": 7_200_000_000_000}}}

            def fake_candidate(
                _input, _normalized, destination, _audio_id, _mode, _log,
                _cancel=None, diagnostics=None, **_kwargs,
            ):
                destination.write_text("false large offset", encoding="utf-8")
                diagnostics["offset_seconds"] = 118.04
                return destination

            failed_correction = pro_core.SemanticCheckResult(
                "十段音频采样校正时间轴", (0.15, 0.16, 0.17), 4
            )
            passed_original = pro_core.SemanticCheckResult(
                "下载字幕原时间轴", (0.62, 0.66, 0.81, 0.65), 4
            )
            logs: list[str] = []
            with patch.object(pro_core.legacy, "inspect_media", return_value=media), patch.object(
                pro_core, "_embedded_subtitle_reference", return_value=None
            ), patch.object(
                pro_core, "_alignment_candidate", side_effect=fake_candidate
            ), patch.object(
                pro_core,
                "semantic_spot_check",
                side_effect=[failed_correction, passed_original],
            ) as semantic:
                selected, summary = pro_core.preflight_external_subtitle(
                    "movie.mkv",
                    str(subtitle),
                    str(root / "work"),
                    0,
                    logs.append,
                    source_language="en",
                )

            self.assertEqual(semantic.call_count, 2)
            self.assertEqual(
                selected.read_bytes(),
                (root / "work" / "external-source.srt").read_bytes(),
            )
            self.assertIn("原时间轴", summary)
            self.assertTrue(any("拒绝该偏移" in message for message in logs))

    def test_preflight_does_not_use_unverified_embedded_reference(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            subtitle = root / "subtitle.srt"
            write_movie_srt(subtitle)
            work = root / "work"
            media = {"container": {"properties": {"duration": 7_200_000_000_000}}}
            track = core.Track(2, "subtitles", "SubRip/SRT", "eng", "", True, False, True, False, False)
            modes: list[str] = []

            def fake_candidate(
                _input,
                _normalized,
                destination,
                _audio_id,
                mode,
                _log,
                _cancel=None,
                **_kwargs,
            ):
                modes.append(mode)
                destination.write_text(f"{mode} candidate", encoding="utf-8")
                return destination

            failed_audio = pro_core.SemanticCheckResult(
                "十段音频采样校正时间轴", (0.10, 0.12, 0.14), 4
            )
            failed_original = pro_core.SemanticCheckResult(
                "下载字幕原时间轴", (0.11, 0.13, 0.15), 4
            )
            with patch.object(pro_core.legacy, "inspect_media", return_value=media), patch.object(
                pro_core, "_embedded_subtitle_reference", return_value=("subtitle", 0, track)
            ), patch.object(
                pro_core, "_prepare_aligned_embedded_text_reference", return_value=None
            ), patch.object(
                pro_core, "_alignment_candidate", side_effect=fake_candidate
            ), patch.object(
                pro_core,
                "semantic_spot_check",
                side_effect=[failed_audio, failed_original],
            ):
                with self.assertRaisesRegex(RuntimeError, "仍未找到可靠时间轴"):
                    pro_core.preflight_external_subtitle(
                        "movie.mkv",
                        str(subtitle),
                        str(work),
                        0,
                        lambda _message: None,
                        source_language="en",
                        use_embedded_reference=False,
                    )

            self.assertEqual(modes, ["audio-segmented"])

    def test_pgs_is_never_used_as_download_subtitle_time_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            subtitle = root / "subtitle.srt"
            write_movie_srt(subtitle)
            media = {"container": {"properties": {"duration": 7_200_000_000_000}}}
            track = core.Track(2, "subtitles", "HDMV PGS", "eng", "", True, False, False, True, False)
            modes: list[str] = []
            def fake_candidate(
                _input,
                _normalized,
                destination,
                _audio_id,
                mode,
                _log,
                _cancel=None,
                **_kwargs,
            ):
                modes.append(mode)
                destination.write_text(f"{mode} candidate", encoding="utf-8")
                return destination

            failed = pro_core.SemanticCheckResult(
                "failed", (0.10, 0.12, 0.14), 4
            )
            with patch.object(pro_core.legacy, "inspect_media", return_value=media), patch.object(
                pro_core, "_embedded_subtitle_reference", return_value=("pgs", 0, track)
            ) as embedded_reference, patch.object(
                pro_core, "_prepare_aligned_embedded_text_reference", return_value=None
            ), patch.object(
                pro_core, "_alignment_candidate", side_effect=fake_candidate
            ), patch.object(
                pro_core, "semantic_spot_check", return_value=failed
            ):
                with self.assertRaises(RuntimeError):
                    pro_core.preflight_external_subtitle(
                        "movie.mkv",
                        str(subtitle),
                        str(root / "work"),
                        0,
                        lambda _message: None,
                        source_language="en",
                    )

            self.assertEqual(modes, ["audio-segmented"])
            embedded_reference.assert_not_called()

    def test_semantic_samples_are_limited_to_twelve_seconds(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            subtitle = Path(folder) / "subtitle.srt"
            write_movie_srt(subtitle, count=80, final_hour=2)
            events = core.parse_subtitle(subtitle)

            samples = pro_core._semantic_samples(events, 7_200.0)

        self.assertGreaterEqual(len(samples), 3)
        self.assertTrue(all(duration <= 12.0 for _start, duration, _text in samples))

    def test_preflight_can_skip_unverified_embedded_reference(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            subtitle = root / "subtitle.srt"
            write_movie_srt(subtitle)
            media = {"container": {"properties": {"duration": 7_200_000_000_000}}}
            passed = pro_core.SemanticCheckResult(
                "十段音频采样校正时间轴", (0.70, 0.68, 0.72, 0.66), 4
            )

            def copy_candidate(_input, _normalized, destination, *_args, **_kwargs):
                destination.write_bytes(subtitle.read_bytes())
                return destination

            with patch.object(pro_core.legacy, "inspect_media", return_value=media), patch.object(
                pro_core, "_embedded_subtitle_reference"
            ) as embedded_reference, patch.object(
                pro_core, "_alignment_candidate", side_effect=copy_candidate
            ), patch.object(
                pro_core, "semantic_spot_check", return_value=passed
            ):
                pro_core.preflight_external_subtitle(
                    "movie.mkv",
                    str(subtitle),
                    str(root / "work"),
                    0,
                    lambda _message: None,
                    source_language="en",
                    use_embedded_reference=False,
                    candidate_label="原字幕轨 2",
                )

        embedded_reference.assert_not_called()

    def test_short_partial_subtitle_is_rejected_before_alignment(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            subtitle = Path(folder) / "partial.srt"
            write_movie_srt(subtitle, count=5)
            media = {"container": {"properties": {"duration": 7_200_000_000_000}}}
            with patch.object(pro_core.legacy, "inspect_media", return_value=media), patch.object(
                pro_core, "_embedded_subtitle_reference", return_value=None
            ):
                with self.assertRaises(RuntimeError):
                    pro_core.preflight_external_subtitle(
                        "movie.mkv",
                        str(subtitle),
                        str(Path(folder) / "work"),
                        0,
                        lambda _message: None,
                    )

    def test_embedded_image_track_receives_audio_verified_offset(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video = root / "movie.mkv"
            video.write_bytes(b"video")
            tracks = [
                core.Track(0, "audio", "AC-3", "eng", "", True, False, False, False, False),
                core.Track(2, "subtitles", "HDMV PGS", "chi", "", False, False, False, True, False),
            ]
            intervals = [(30.0 + index * 20.0, 32.0 + index * 20.0) for index in range(40)]

            def align_image(_input, normalized, destination, *_args, **kwargs):
                diagnostics = kwargs.get("diagnostics")
                if diagnostics is not None:
                    diagnostics["score"] = 120000.0
                return pro_core._shifted_srt(normalized, destination, 1250)

            with patch.object(pro_core.legacy, "inspect_tracks", return_value=tracks), patch.object(
                pro_core.legacy,
                "inspect_media",
                return_value={"container": {"properties": {"duration": 3_600_000_000_000}}},
            ), patch.object(
                pro_core,
                "_embedded_image_intervals",
                return_value=intervals,
            ) as image_scan, patch.object(
                pro_core,
                "_alignment_candidate",
                side_effect=align_image,
            ):
                offsets, corrected, source_id = pro_core.prepare_embedded_text_corrections(
                    str(video),
                    [2],
                    2,
                    0,
                    str(root / "work"),
                    lambda _message: None,
                )

        self.assertEqual(offsets, {2: 0})
        self.assertEqual(corrected, {})
        self.assertEqual(source_id, 2)
        image_scan.assert_not_called()

    def test_embedded_image_track_uses_verified_text_without_audio_rescan(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video = root / "movie.mkv"
            external = root / "external-verified.srt"
            video.write_bytes(b"video")
            write_timed_srt(external, count=40)
            tracks = [
                core.Track(0, "audio", "AC-3", "eng", "", True, False, False, False, False),
                core.Track(2, "subtitles", "HDMV PGS", "chi", "", False, False, False, True, False),
            ]
            intervals = [(35.0 + index * 10.0, 37.0 + index * 10.0) for index in range(40)]

            with patch.object(pro_core.legacy, "inspect_tracks", return_value=tracks), patch.object(
                pro_core.legacy,
                "inspect_media",
                return_value={"container": {"properties": {"duration": 3_600_000_000_000}}},
            ), patch.object(
                pro_core,
                "_embedded_image_intervals",
                return_value=intervals,
            ), patch.object(
                pro_core,
                "_alignment_candidate",
            ) as audio_alignment:
                offsets, _, _ = pro_core.prepare_embedded_text_corrections(
                    str(video),
                    [2],
                    None,
                    0,
                    str(root / "work"),
                    lambda _message: None,
                    verified_external_subtitle=str(external),
                )

        self.assertEqual(offsets, {2: -5000})
        audio_alignment.assert_not_called()

    def test_sparse_image_track_keeps_original_timeline_without_failing_movie(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video = root / "movie.mkv"
            external = root / "external-verified.srt"
            video.write_bytes(b"video")
            write_timed_srt(external, count=40)
            tracks = [
                core.Track(0, "audio", "AC-3", "eng", "", True, False, False, False, False),
                core.Track(2, "subtitles", "HDMV PGS", "eng", "English", False, False, False, True, False),
            ]
            messages: list[str] = []
            with patch.object(pro_core.legacy, "inspect_tracks", return_value=tracks), patch.object(
                pro_core, "_embedded_image_intervals", return_value=[(35.0, 37.0)]
            ), patch.object(pro_core, "_interval_alignment") as alignment:
                offsets, _, _ = pro_core.prepare_embedded_text_corrections(
                    str(video), [2], None, 0, str(root / "work"), messages.append,
                    verified_external_subtitle=str(external),
                )

        self.assertEqual(offsets, {2: 0})
        self.assertTrue(any("证据不足以纠偏" in message for message in messages))
        alignment.assert_not_called()

    def test_image_timing_read_failure_does_not_interrupt_translation_source(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            video = root / "movie.mkv"
            external = root / "external-verified.srt"
            video.write_bytes(b"video")
            write_timed_srt(external, count=40)
            tracks = [
                core.Track(0, "audio", "AC-3", "eng", "", True, False, False, False, False),
                core.Track(2, "subtitles", "HDMV PGS", "eng", "English", False, False, False, True, False),
            ]
            messages: list[str] = []
            with patch.object(pro_core.legacy, "inspect_tracks", return_value=tracks), patch.object(
                pro_core, "_embedded_image_intervals", side_effect=RuntimeError("packet parse failed")
            ):
                offsets, _, _ = pro_core.prepare_embedded_text_corrections(
                    str(video), [2], None, 0, str(root / "work"), messages.append,
                    verified_external_subtitle=str(external),
                )

        self.assertEqual(offsets, {2: 0})
        self.assertTrue(any("packet parse failed" in message for message in messages))

    def test_shift_subtitle_timeline_preserves_text_and_applies_offset(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.srt"
            shifted = root / "shifted.srt"
            write_timed_srt(source, count=4)

            core.shift_subtitle_timeline(source, shifted, 1500)

            original_events = core.parse_subtitle(source)
            shifted_events = core.parse_subtitle(shifted)
            self.assertEqual(original_events[0].text, shifted_events[0].text)
            self.assertEqual(
                core.subtitle_time_to_milliseconds(shifted_events[0].start)
                - core.subtitle_time_to_milliseconds(original_events[0].start),
                1500,
            )


class ManualSubtitleReferenceFirstTest(unittest.TestCase):
    def test_fixed_text_match_supports_bilingual_shared_language(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            candidate = root / "candidate.srt"
            reference = root / "reference.srt"
            candidate_events = []
            reference_events = []
            for index in range(700):
                candidate_time = 30.0 + index * 10.0
                reference_time = candidate_time + 1.25
                english = f"Unique dialogue sentence number {index}"
                candidate_events.append(core.SubtitleEvent(
                    core.srt_time_from_milliseconds(round(candidate_time * 1000)),
                    core.srt_time_from_milliseconds(round((candidate_time + 2) * 1000)),
                    f"中文字幕第 {index} 句\n{english}",
                ))
                reference_events.append(core.SubtitleEvent(
                    core.srt_time_from_milliseconds(round(reference_time * 1000)),
                    core.srt_time_from_milliseconds(round((reference_time + 2) * 1000)),
                    english,
                ))
            core.write_srt(candidate, candidate_events, {
                index: event.text for index, event in enumerate(candidate_events, 1)
            })
            core.write_srt(reference, reference_events, {
                index: event.text for index, event in enumerate(reference_events, 1)
            })

            match = pro_core._estimate_fixed_text_timeline(candidate, reference, 7_200.0)

        self.assertIsNotNone(match)
        self.assertAlmostEqual(match.offset_seconds, 1.25, delta=0.001)
        self.assertEqual(match.scale, 1.0)

    def test_manual_partial_subtitle_stops_before_heavy_verification(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            subtitle = root / "partial.srt"
            write_timed_srt(subtitle, count=400)
            media = {"container": {"properties": {"duration": 7_200_000_000_000}}}
            with patch.object(pro_core.legacy, "inspect_media", return_value=media), patch.object(
                pro_core, "_manual_reference_first_candidate"
            ) as reference_first, patch.object(
                pro_core, "_alignment_candidate"
            ) as alignment:
                with self.assertRaisesRegex(RuntimeError, "完整度检查未通过"):
                    pro_core.preflight_external_subtitle(
                        "movie.mkv",
                        str(subtitle),
                        str(root / "work"),
                        0,
                        lambda _message: None,
                        source_language="zh-CN",
                        manual_reference_first=True,
                    )

        reference_first.assert_not_called()
        alignment.assert_not_called()

    def test_manual_overlong_subtitle_stops_before_heavy_verification(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            subtitle = root / "extended.srt"
            write_timed_srt(subtitle, count=800)
            media = {"container": {"properties": {"duration": 7_200_000_000_000}}}
            with patch.object(pro_core.legacy, "inspect_media", return_value=media), patch.object(
                pro_core, "_manual_reference_first_candidate"
            ) as reference_first, patch.object(
                pro_core, "_alignment_candidate"
            ) as alignment:
                with self.assertRaisesRegex(RuntimeError, "疑似加长版"):
                    pro_core.preflight_external_subtitle(
                        "movie.mkv",
                        str(subtitle),
                        str(root / "work"),
                        0,
                        lambda _message: None,
                        source_language="en",
                        manual_reference_first=True,
                    )

        reference_first.assert_not_called()
        alignment.assert_not_called()

    def test_manual_reference_cannot_bypass_shared_english_guard(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            subtitle = root / "candidate.srt"
            corrected = root / "corrected.srt"
            write_timed_srt(subtitle, count=700)
            write_timed_srt(corrected, offset_seconds=1.25, count=700)
            media = {"container": {"properties": {"duration": 7_200_000_000_000}}}
            with patch.object(pro_core.legacy, "inspect_media", return_value=media), patch.object(
                pro_core,
                "_manual_reference_first_candidate",
                return_value=(corrected, "固定偏移 +1.25 秒"),
            ) as old_reference, patch.object(
                pro_core, "prepare_shared_subtitle_content_audio", return_value=Mock()
            ), patch.object(
                pro_core, "preflight_online_subtitle",
                return_value=(corrected, "三分区核验通过"),
            ) as shared_guard:
                selected, summary = pro_core.preflight_external_subtitle(
                    "movie.mkv",
                    str(subtitle),
                    str(root / "work"),
                    0,
                    lambda _message: None,
                    source_language="en",
                    manual_reference_first=True,
                )

        self.assertEqual(selected, corrected)
        self.assertIn("三分区", summary)
        old_reference.assert_not_called()
        shared_guard.assert_called_once()

    def test_manual_english_subtitle_uses_cross_language_whisper(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            subtitle = root / "english.srt"
            verified = root / "verified.srt"
            write_timed_srt(subtitle, count=700)
            write_timed_srt(verified, count=700)
            media = {"container": {"properties": {"duration": 7_200_000_000_000}}}
            shared = Mock()
            shared.fingerprint.usable_clip_count = 3
            with patch.object(pro_core.legacy, "inspect_media", return_value=media), patch.object(
                pro_core, "_manual_reference_first_candidate", return_value=None
            ), patch.object(
                pro_core, "_selected_audio_language", return_value="de"
            ), patch.object(
                pro_core, "prepare_shared_subtitle_content_audio", return_value=shared
            ) as prepare, patch.object(
                pro_core,
                "preflight_online_subtitle",
                return_value=(verified, "跨语言固定偏移核验通过"),
            ) as online_preflight:
                selected, summary = pro_core.preflight_external_subtitle(
                    "movie.mkv",
                    str(subtitle),
                    str(root / "work"),
                    0,
                    lambda _message: None,
                    source_language="en",
                    manual_reference_first=True,
                )

        self.assertEqual(selected, verified)
        self.assertIn("跨语言固定偏移核验通过", summary)
        prepare.assert_called_once()
        self.assertEqual(prepare.call_args.kwargs["source_language"], "en")
        online_preflight.assert_called_once()
        self.assertEqual(online_preflight.call_args.kwargs["source_language"], "en")
        self.assertLessEqual(
            online_preflight.call_args.kwargs["time_budget_seconds"],
            pro_core.MANUAL_CROSS_LANGUAGE_BUDGET_SECONDS,
        )

    def test_manual_cross_language_without_speech_cannot_bypass_shared_guard(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            subtitle = root / "english.srt"
            write_timed_srt(subtitle, count=700)
            media = {"container": {"properties": {"duration": 7_200_000_000_000}}}
            shared = Mock()
            shared.fingerprint.usable_clip_count = 0
            with patch.object(pro_core.legacy, "inspect_media", return_value=media), patch.object(
                pro_core, "_manual_reference_first_candidate", return_value=None
            ), patch.object(
                pro_core, "_selected_audio_language", return_value="de"
            ), patch.object(
                pro_core, "prepare_shared_subtitle_content_audio", return_value=shared
            ), patch.object(
                pro_core, "preflight_online_subtitle",
                side_effect=pro_core.SubtitleContentMismatchError("证据不足"),
            ) as online_preflight:
                with self.assertRaisesRegex(RuntimeError, "证据不足"):
                    pro_core.preflight_external_subtitle(
                        "movie.mkv",
                        str(subtitle),
                        str(root / "work"),
                        0,
                        lambda _message: None,
                        source_language="en",
                        manual_reference_first=True,
                    )

        online_preflight.assert_called_once()

    def test_manual_non_english_cross_language_still_requests_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            subtitle = root / "chinese.srt"
            events = []
            for index in range(700):
                start_ms = int((30 + index * 10) * 1000)
                events.append(core.SubtitleEvent(
                    core.srt_time_from_milliseconds(start_ms),
                    core.srt_time_from_milliseconds(start_ms + 2000),
                    f"这是第{index}句完整中文字幕内容",
                ))
            core.write_srt(subtitle, events, {
                index: event.text for index, event in enumerate(events, 1)
            })
            media = {"container": {"properties": {"duration": 7_200_000_000_000}}}
            with patch.object(pro_core.legacy, "inspect_media", return_value=media), patch.object(
                pro_core, "_manual_reference_first_candidate", return_value=None
            ), patch.object(
                pro_core, "_selected_audio_language", return_value="en"
            ), patch.object(
                pro_core, "prepare_shared_subtitle_content_audio"
            ) as prepare:
                with self.assertRaisesRegex(RuntimeError, "只支持将非英语语音翻译为英语"):
                    pro_core.preflight_external_subtitle(
                        "movie.mkv",
                        str(subtitle),
                        str(root / "work"),
                        0,
                        lambda _message: None,
                        source_language="zh-CN",
                        manual_reference_first=True,
                    )

        prepare.assert_not_called()


if __name__ == "__main__":
    unittest.main()




# These cases assert the retained Whisper algorithm, not the active VAD route.
# Active entry-point equivalence is covered by test_continuous_vad_route.
def setUpModule():
    global _historical_scope
    from tests.legacy_whisper_context import enter
    _historical_scope = enter()

def tearDownModule():
    _historical_scope.close()
