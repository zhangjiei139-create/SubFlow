"""An unfinished human review must not fall back into automatic processing."""
from dataclasses import asdict
import unittest
from unittest.mock import patch

import batch_core
from profile_model import BatchPlan, PreferenceProfile


class ManualReviewProcessingGuardTests(unittest.TestCase):
    def test_unfinished_review_blocks_every_processing_mode_before_io(self):
        for mode in ("none", "online", "embedded"):
            for verified in (False, True):
                for allow_search in (False, True):
                    with self.subTest(mode=mode, verified=verified, search=allow_search):
                        plan = BatchPlan("missing.mkv", 1, status="ready", source_mode=mode,
                                         external_subtitle_verified=verified,
                                         manual_review_required=True)
                        with patch.object(batch_core.core, "inspect_tracks") as inspect:
                            with self.assertRaisesRegex(RuntimeError, "人工对齐尚未完成"):
                                batch_core._process_plan_impl(plan, PreferenceProfile(1, "test"),
                                                              lambda _: None, None,
                                                              allow_online_search=allow_search)
                            inspect.assert_not_called()

    def test_review_state_survives_plan_serialization_without_shared_dict(self):
        first = BatchPlan("first.mkv", 1, manual_review_required=True,
                          manual_review_origin={"raw_path": "original.srt", "raw_sha256": "abc"})
        restored = BatchPlan(**asdict(first))
        self.assertTrue(restored.manual_review_required)
        self.assertEqual(restored.manual_review_origin, first.manual_review_origin)
        second, third = BatchPlan("second.mkv", 1), BatchPlan("third.mkv", 1)
        second.manual_review_origin["raw_path"] = "other.srt"
        self.assertEqual(third.manual_review_origin, {})


if __name__ == "__main__":
    unittest.main()
