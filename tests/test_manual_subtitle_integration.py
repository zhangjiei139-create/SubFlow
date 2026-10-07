# -*- coding: utf-8 -*-
import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QDialog

import pro_core
import qt_app


class ManualSubtitleIntegrationTest(unittest.TestCase):
    def test_confirmed_out_of_auto_range_is_shifted_and_sealed(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            selected = work / "selected.srt"
            text = (
                "1\n00:00:30,000 --> 00:00:32,000\nA full dialogue line.\n\n"
                "2\n00:10:30,000 --> 00:10:32,000\nAnother dialogue line.\n"
            )
            selected.write_text(text, encoding="utf-8")
            (work / "manual-source.srt").write_text(text, encoding="utf-8")
            evidence = {
                "source_sha256": hashlib.sha256(selected.read_bytes()).hexdigest(),
                "review_dir": str(work),
                "cue_count": 876,
                "clips": [
                    {"region": i, "subtitle_time": point}
                    for i, point in enumerate((100.0, 1000.0, 2000.0), 1)
                ],
            }
            fake = SimpleNamespace(
                batch_plans={"movie.mkv": object()},
                _batch_log_line=Mock(),
                _batch_manual_review_failed=Mock(),
                _batch_manual_review_cancelled=Mock(),
                _batch_online_verified=Mock(),
            )
            candidate = SimpleNamespace(language="en")
            with patch("qt_app.ManualSubtitleReviewDialog") as dialog_class, patch(
                "qt_app.pro_core.append_subtitle_diagnostic_log"
            ):
                dialog = dialog_class.return_value
                dialog.exec.return_value = QDialog.DialogCode.Accepted
                dialog.confirmed_offsets = [-13.52] * 3
                qt_app.ProMaxQt._batch_manual_review_ready(
                    fake, "movie.mkv", str(selected), candidate, evidence
                )
            fake._batch_manual_review_failed.assert_not_called()
            fake._batch_online_verified.assert_called_once()
            passed_path, passed_candidate, result = fake._batch_online_verified.call_args.args
            self.assertEqual((passed_path, passed_candidate), ("movie.mkv", candidate))
            confirmed = Path(result[0])
            self.assertTrue(confirmed.is_file())
            shifted = pro_core.legacy.parse_subtitle(confirmed)
            self.assertEqual(shifted[0].start, "00:00:16,480")
            sealed = hashlib.sha256(confirmed.read_bytes()).hexdigest()
            self.assertEqual(
                fake._batch_online_verified.call_args.kwargs["manual_hash"], sealed
            )


if __name__ == "__main__":
    unittest.main()
