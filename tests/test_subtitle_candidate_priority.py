"""Search suitability, provenance and unchanged acceptance boundaries."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from pathlib import Path
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

import online_subtitles as opensubtitles
import subdl_subtitles as subdl
from subtitle_candidate_priority import prioritize_candidates, candidate_note


def candidate(name, author="", **fields):
    return NS(release=name, file_name=name + ".srt", author=author, **fields)


class CandidatePriorityTests(unittest.TestCase):
    def test_hash_precedes_version_hints_without_granting_acceptance(self):
        exact = candidate("Unknown", moviehash_match=True, recommendation="待内容验证")
        version = candidate("Example.2024.UNRATED.BluRay")
        self.assertIs(prioritize_candidates("Example.2024.UNRATED.BluRay.mkv", [version, exact])[0], exact)
        self.assertEqual(exact.recommendation, "待内容验证")

    def test_version_and_source_bring_suitable_late_candidate_forward(self):
        unknown = [candidate("Example.2024", str(i)) for i in range(8)]
        web = candidate("Example.2024.UNRATED.WEB-DL")
        disc = candidate("Example.2024.UNRATED.BluRay")
        result = prioritize_candidates("Example.2024.UNRATED.BluRay.mkv", [*unknown, web, disc])
        self.assertEqual(result[:2], [disc, web])
        self.assertEqual({id(c) for c in result}, {id(c) for c in [*unknown, web, disc]})

    def test_author_variety_only_breaks_equal_suitability_ties(self):
        first = candidate("Example.2024.BluRay", "Alice")
        other_clock = candidate("Example.2024.BluRay", "ALICE")
        other_author = candidate("Example.2024.BluRay", "Bob")
        weak = candidate("Example.2024", "Carol")
        result = prioritize_candidates("Example.2024.BluRay.mkv", [first, other_clock, weak, other_author])
        self.assertEqual(result, [first, other_author, other_clock, weak])
        self.assertIn(other_clock, result)

    def test_unknown_authors_are_kept_without_inventing_independence(self):
        rows = [candidate("Example.2024", author) for author in ["", "os-auto", "os-auto", "Anonymous"]]
        self.assertEqual(prioritize_candidates("Example.2024.mkv", rows), rows)

    def test_title_words_do_not_become_edition_or_source_tags(self):
        rows = [candidate("Final Cut.2024"), candidate("Final Cut.2024.BluRay")]
        result = prioritize_candidates("Final.Cut.2024.WEB-DL.mkv", rows)
        self.assertEqual(result, rows)
        self.assertNotIn("剪辑版本一致", rows[0].priority_reason)

    def test_release_group_with_and_without_extension(self):
        other = candidate("Example.2024.BluRay.x265-OTHER")
        same = candidate("Example.2024.BluRay.x264-GROUP")
        self.assertIs(prioritize_candidates("Example.2024.BluRay.x265-GROUP.mkv", [other, same])[0], same)
        self.assertIn("发布组一致", same.priority_reason)

    def test_provider_metadata_keeps_uploader_distinct_from_author(self):
        rows = opensubtitles._candidates_from_response({"data": [{"attributes": {
            "files": [{"file_id": 123, "file_name": "Example.srt"}],
            "uploader": {"name": "Alice", "uploader_id": 17},
        }}]}, "en")
        self.assertEqual((rows[0].author, rows[0].author_role), ("Alice", "上传者"))
        rows = subdl._candidates_from_response({"subtitles": [{
            "author": "Bob", "lang": "EN", "unpack_files": [
                {"id": "123", "name": "Example.srt", "author": "", "url": "https://example.org/subtitle"},
            ],
        }]}, "en")
        self.assertEqual((rows[0].author, rows[0].author_role), ("Bob", "作者（站点标注）"))
        self.assertIn("不代表已确认不同时间轴", candidate_note(rows[0]))

    def test_smart_search_checks_late_suitable_source_first_and_records_provenance(self):
        import smart_subtitles as smart
        import pro_core
        from tests import test_subtitle_strategy_remediation as fixtures

        fixture = fixtures.StrategyRemediationTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.movie = fixture.root / "Movie.2025.UNRATED.BluRay.mkv"
        fixture.movie.write_bytes(b"movie")
        service = fixture.service(8)
        identity, rows, meta = service.search("", "", "")
        rows[-1].release = "Movie.2025.UNRATED.BluRay"
        rows[-1].author = "Bob"
        rows[-1].author_role = "上传者"
        service.search = lambda *_args: (identity, rows, meta)
        stack, _reference = fixture.search_context(service)
        logs = []
        checked = []
        def verify(*args, **kwargs):
            checked.append(Path(args[1]).name)
            return Path(args[1]), "连续VAD时间轴核验通过：提议固定偏移 +0.00 秒"
        with stack, patch.object(pro_core, "preflight_online_subtitle", side_effect=verify):
            smart.find_verified_english(str(fixture.movie), 1, logs.append, max_candidates=1)
        self.assertEqual(checked, ["Movie-8.srt"])
        report = fixture.report()
        row = next(row for row in report["records"] if row.get("priority_rank") == 1)
        self.assertEqual(row["author"], "Bob")
        self.assertIn("剪辑版本一致", row["priority_reason"])
        self.assertTrue(any("作者/上传者=Bob" in line for line in logs))


class CandidateDialogTests(unittest.TestCase):
    def test_real_qt_dialog_displays_author_and_downloads_corresponding_sorted_row(self):
        from PySide6.QtWidgets import QApplication
        from qt_online_dialog import OnlineSubtitleDialog
        app = QApplication.instance() or QApplication([])
        identity = opensubtitles.MediaIdentity(title="Example", year="2024")
        first = opensubtitles.SubtitleCandidate(1, "Example.srt", "Example.2024", "en", 0, 0, False, False)
        second = opensubtitles.SubtitleCandidate(2, "good.srt", "Example.2024.UNRATED.BluRay", "en", 0, 0, False, False,
                                                author="Alice", author_role="上传者")
        with patch.object(opensubtitles, "identify_media", return_value=identity), \
             patch.object(opensubtitles, "load_settings", return_value={}):
            dialog = OnlineSubtitleDialog(None, "Example.2024.UNRATED.BluRay.mkv", lambda *_: None)
        try:
            dialog._show_results("opensubtitles", (identity, [first, second], NS(truncated=False, total_count=2)))
            self.assertEqual(dialog.table.item(0, 7).text(), "Alice")
            self.assertEqual(dialog.table.item(1, 7).text(), "未提供")
            self.assertIn("上传者：Alice", dialog.table.item(0, 7).toolTip())
            self.assertIn("不代表已确认不同时间轴", dialog.status.text())
            dialog.keys["opensubtitles"] = "test"
            with patch.object(dialog, "_run_worker") as worker, patch.object(opensubtitles, "download") as download:
                dialog._download()
                worker.call_args.args[0](None)
            self.assertIs(download.call_args.args[1], second)
        finally:
            dialog.close()
            app.processEvents()


if __name__ == "__main__":
    unittest.main()
