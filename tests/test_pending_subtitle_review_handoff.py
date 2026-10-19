"""Only regional uncertainty may retain originals for an unfinished review."""
from contextlib import ExitStack
from dataclasses import FrozenInstanceError
import hashlib
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import batch_core
import pro_core
import smart_subtitles
import subtitle_tool_core as core
from profile_model import BatchPlan, PreferenceProfile


class PendingSubtitleReviewHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.movie = self.folder / "Movie.2025.mkv"
        self.movie.write_bytes(b"movie")
        self.reference = self.folder / "speech-75.npz"
        self.reference.write_bytes(b"existing VAD")
        self.reference.with_suffix(".json").write_text("{}", encoding="utf-8")
        self.shared = SimpleNamespace(duration_seconds=600.0, validation_audio_id=1,
            audio_stream_index=0, audio_language="en", vad_reference=self.reference,
            vad_preparation_state={"references": {.75: self.reference}, "limit": 180, "spent": 12})
        self.messages = []
        self.checked = []

    def candidate(self, number):
        return SimpleNamespace(file_id=str(number), release=f"Movie.2025.BluRay.{number}",
            file_name=f"Movie-{number}.srt", feature_title="Movie", feature_year="2025",
            language="en", identity_verified=True, identity_key="imdb:1")

    def service(self, candidates):
        def download(_key, candidate, destination):
            path = Path(destination) / candidate.file_name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"1\n00:00:0{candidate.file_id},000 --> 00:00:0{candidate.file_id},800\n"
                            f"This is the movie dialogue {candidate.file_id}.\n", encoding="utf-8")
            return path
        return SimpleNamespace(load_settings=lambda: {"key": "test-token"},
            search=lambda *_args: (None, candidates, None), download=download)

    def rejection(self, reasons=None):
        return pro_core.SubtitleInsufficientRegionalSupportError(
            {"sufficient": False, "reasons": reasons or ["insufficient_independent_regional_support"],
             "outside_range_peak": False, "regional_offset_conflict": False,
             "tool_coordinate_invalid": False, "range_limited": False},
            self.reference, self.shared.vad_preparation_state)

    def search(self, outcomes, *, providers=None, cancel=None):
        candidates = [self.candidate(i + 1) for i in range(len(outcomes))]
        service = self.service(candidates)
        def preflight(*args, **kwargs):
            self.checked.append(Path(args[1]).name)
            value = outcomes[len(self.checked) - 1]
            if callable(value):
                return value(*args, **kwargs)
            if isinstance(value, Exception):
                raise value
            return value
        with ExitStack() as stack:
            stack.enter_context(patch.object(smart_subtitles, "PROVIDERS", providers or (("Test", service, "key"),)))
            stack.enter_context(patch.object(smart_subtitles, "filename_container_identity_conflict", return_value=""))
            stack.enter_context(patch.object(pro_core, "prepare_shared_subtitle_content_audio", return_value=self.shared))
            stack.enter_context(patch.object(pro_core, "preflight_online_subtitle", side_effect=preflight))
            return smart_subtitles.find_verified_english(str(self.movie), 1, self.messages.append, cancel)

    def pending(self, count=1):
        with self.assertRaises(smart_subtitles.PendingSubtitleReviewError) as caught:
            self.search([self.rejection() for _ in range(count)])
        return caught.exception

    def test_all_eligible_originals_remain_in_site_order_with_context(self):
        pending = self.pending(3)
        self.assertEqual(self.checked, ["Movie-1.srt", "Movie-2.srt", "Movie-3.srt"])
        self.assertEqual([Path(x.subtitle_path).name for x in pending.candidates], self.checked)
        manifest = json.loads(Path(pending.manifest_path).read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "awaiting-manual-review")
        self.assertEqual(len(manifest["candidates"]), 3)
        self.assertEqual(pending.context["validation_audio_id"], 1)
        self.assertEqual(pending.context["reference_paths"], [str(self.reference.resolve())])
        for candidate in pending.candidates:
            original = Path(candidate.subtitle_path)
            self.assertEqual(hashlib.sha256(original.read_bytes()).hexdigest(), candidate.raw_sha256)
            self.assertNotIn("match", original.name)
            self.assertEqual(candidate.reference_sha256, (hashlib.sha256(self.reference.read_bytes()).hexdigest(),))
        with self.assertRaises(FrozenInstanceError):
            pending.candidates[0].raw_sha256 = "changed"
        self.assertEqual(self.reference.read_bytes(), b"existing VAD")

    def test_later_passing_candidate_takes_priority(self):
        accepted = self.folder / "accepted.srt"
        accepted.write_text("accepted", encoding="utf-8")
        result = self.search([self.rejection(), (accepted, "passed")])
        self.assertEqual(result.subtitle_path, str(accepted))
        self.assertEqual(self.checked, ["Movie-1.srt", "Movie-2.srt"])
        self.assertFalse((self.movie.parent / (self.movie.stem + "_pro_work") / "smart-subtitles/pending-review.json").exists())

    def test_next_provider_pass_is_preferred_to_first_provider_pending(self):
        accepted = self.folder / "accepted.srt"
        accepted.write_text("accepted", encoding="utf-8")
        providers = (("First", self.service([self.candidate(1)]), "key"),
                     ("Second", self.service([self.candidate(2)]), "key"))
        result = self.search([self.rejection(), (accepted, "passed")], providers=providers)
        self.assertEqual(result.provider, "Second")
        self.assertEqual(result.subtitle_path, str(accepted))

    def test_outside_range_and_generic_error_text_never_create_pending(self):
        for failure in (pro_core.SubtitleContentMismatchError("自动范围外存在明显更强的时间匹配"),
                        RuntimeError("独立取样区域对同一固定偏移的支持不足")):
            self.checked.clear()
            with self.subTest(failure=failure), self.assertRaises(RuntimeError) as caught:
                self.search([failure])
            self.assertNotIsInstance(caught.exception, smart_subtitles.PendingSubtitleReviewError)

    def test_cancel_after_rejection_does_not_turn_into_pending(self):
        cancel = threading.Event()
        def reject_and_cancel(*_args, **_kwargs):
            cancel.set()
            raise self.rejection()
        with self.assertRaises(core.CancelledError):
            self.search([reject_and_cancel], cancel=cancel)

    def test_raw_mutation_during_preflight_is_blocked(self):
        def mutate(*args, **_kwargs):
            Path(args[1]).write_text("mutated", encoding="utf-8")
            raise self.rejection()
        with self.assertRaisesRegex(RuntimeError, "核验期间发生变化"):
            self.search([mutate])

    def test_payload_revalidation_blocks_missing_or_changed_source_and_movie(self):
        pending = self.pending()
        candidate = pending.candidates[0]
        Path(candidate.subtitle_path).write_text("changed", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "原始字幕已变化"):
            smart_subtitles.validate_pending_candidate(self.movie, candidate)
        self.movie.write_bytes(b"changed movie")
        with self.assertRaisesRegex(RuntimeError, "影片在自动核验后发生变化"):
            smart_subtitles.validate_pending_candidate(self.movie, candidate)

    def plan(self):
        return BatchPlan(str(self.movie), 1, status="review", status_label="需要确认",
            output_path=str(self.folder / "Movie.SF.mkv"), audio_ids=[1],
            missing_subtitle_languages=["zh-CN"], source_mode="none")

    def test_batch_typed_handoff_has_origin_and_never_starts_processing(self):
        pending = self.pending(2)
        plan = self.plan()
        tracks = [core.Track(1, "audio", "AAC", "eng", "", True, False, False, False, False)]
        with patch.object(core, "inspect_tracks", return_value=tracks), \
                patch.object(smart_subtitles, "find_verified_english", side_effect=pending), \
                patch.object(pro_core, "process_pro") as process:
            with self.assertRaises(batch_core.PendingSubtitleReviewError) as caught:
                batch_core._process_plan_impl(plan, PreferenceProfile(1, "test", audio_policy="native", subtitle_languages=["zh-CN"]), self.messages.append, None)
        process.assert_not_called()
        self.assertIs(caught.exception.plan, plan)
        self.assertEqual(len(caught.exception.candidates), 2)
        self.assertTrue(plan.manual_review_required)
        self.assertTrue(plan.manual_review_resume_processing)
        self.assertFalse(plan.external_subtitle_verified)
        self.assertEqual(plan.external_subtitle_seal, "")
        self.assertEqual(plan.status_label, "需要确认")
        self.assertEqual(Path(plan.external_subtitle).read_bytes(), Path(pending.candidates[0].subtitle_path).read_bytes())
        self.assertEqual(plan.manual_review_origin["raw_path"], pending.candidates[0].subtitle_path)

    def test_ordinary_manual_review_does_not_authorize_processing_resume(self):
        plan = BatchPlan(str(self.movie), 1, manual_review_required=True)
        self.assertFalse(plan.manual_review_resume_processing)

    def test_unfinished_pending_blocks_second_process_without_search(self):
        pending = self.pending()
        plan = self.plan()
        batch_core._retain_pending_subtitle_review(plan, pending)
        with patch.object(core, "inspect_tracks") as inspect, \
                patch.object(smart_subtitles, "find_verified_english") as search:
            with self.assertRaisesRegex(RuntimeError, "人工对齐尚未完成"):
                batch_core._process_plan_impl(plan, PreferenceProfile(1, "test"), self.messages.append, None)
        inspect.assert_not_called()
        search.assert_not_called()

    def test_manual_confirmed_text_resumes_existing_path_without_download_or_match(self):
        pending = self.pending()
        plan = self.plan()
        batch_core._retain_pending_subtitle_review(plan, pending)
        plan.manual_review_required = False
        plan.external_subtitle_verified = True
        plan.external_subtitle_media_signature = batch_core.media_signature(plan.path)
        digest = hashlib.sha256(Path(plan.external_subtitle).read_bytes()).hexdigest()
        plan.external_subtitle_content_hash = plan.manual_confirmation_hash = digest
        tracks = [core.Track(1, "audio", "AAC", "eng", "", True, False, False, False, False)]
        with patch.object(core, "inspect_tracks", return_value=tracks), \
                patch.object(smart_subtitles, "find_verified_english") as search, \
                patch.object(pro_core, "preflight_online_subtitle") as preflight, \
                patch.object(pro_core, "process_pro") as process:
            batch_core._process_plan_impl(plan, PreferenceProfile(1, "test", audio_policy="native", subtitle_languages=["zh-CN"]), self.messages.append, None)
        search.assert_not_called()
        preflight.assert_not_called()
        self.assertEqual(process.call_args.kwargs["external_subtitle"], plan.external_subtitle)
        self.assertTrue(process.call_args.kwargs["trust_external_original_timeline"])


if __name__ == "__main__":
    unittest.main()
