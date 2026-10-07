# -*- coding: utf-8 -*-
from __future__ import annotations

import inspect
import ctypes
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QItemSelectionModel, QObject, Qt
from PySide6.QtWidgets import QApplication, QTableWidget, QTableWidgetItem

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qt_app import ProMaxQt
from profile_model import BatchPlan


class QtFileDialogTest(unittest.TestCase):
    def test_batch_multiselection_survives_table_refresh(self) -> None:
        app = QApplication.instance() or QApplication([])
        table = QTableWidget(3, 7)
        paths = [f"movie-{index}.mkv" for index in range(3)]
        for row, path in enumerate(paths):
            item = QTableWidgetItem(path)
            item.setData(Qt.ItemDataRole.UserRole, path)
            table.setItem(row, 0, item)
        selection = table.selectionModel()
        for row in (0, 2):
            selection.select(
                table.model().index(row, 0),
                QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
            )
        controller = QObject()
        controller.batch_table = table
        controller.batch_paths = paths
        controller.batch_plans = {path: BatchPlan(path, 1) for path in paths}
        controller.batch_running = False
        controller._batch_task_label = ProMaxQt._batch_task_label
        controller._batch_row_color = ProMaxQt._batch_row_color
        controller._batch_row_foreground = ProMaxQt._batch_row_foreground
        controller._batch_profile_changed = Mock()
        ProMaxQt._render_batch_table(controller)
        self.assertEqual(
            {index.row() for index in table.selectionModel().selectedRows()},
            {0, 2},
        )
        table.deleteLater()
        del app

    def test_english_manual_subtitle_uses_automatic_strict_verifier(self) -> None:
        plan = BatchPlan("movie.mkv", 1)
        controller = SimpleNamespace(
            batch_plans={plan.path: plan},
            _render_batch_table=Mock(),
            _batch_log_line=Mock(),
            _run_worker=Mock(),
        )
        candidate = SimpleNamespace(
            language="en", release="same release", feature_title="movie",
            recommendation="用户指定", match_reason="手动字幕",
        )
        ProMaxQt._batch_subtitle_selected(
            controller, plan.path, "picked.srt", candidate, "手动下载字幕"
        )
        task = controller._run_worker.call_args.args[0]
        with patch("qt_app.pro_core.prepare_shared_subtitle_content_audio", return_value=object()), patch(
            "qt_app.pro_core.preflight_online_subtitle", return_value=(Path("verified.srt"), "通过")
        ) as strict, patch("qt_app.pro_core.preflight_external_subtitle") as old:
            task(SimpleNamespace(message=SimpleNamespace(emit=Mock())))
        strict.assert_called_once()
        old.assert_not_called()

    def test_open_dialog_is_an_instance_method(self) -> None:
        self.assertNotIsInstance(
            ProMaxQt.__dict__["_qt_open_dialog"],
            staticmethod,
        )

    def test_dialog_uses_qt_renderer_with_explicit_drive_sidebar(self) -> None:
        source = inspect.getsource(ProMaxQt._create_file_dialog)

        self.assertIn("QFileDialog(None", source)
        self.assertNotIn("QFileDialog(self", source)
        self.assertIn("ApplicationModal", source)
        self.assertIn("DontUseNativeDialog, True", source)
        self.assertIn("setSidebarUrls", source)

    def test_local_subtitle_dialog_starts_in_movie_folder(self) -> None:
        source = inspect.getsource(ProMaxQt._import_batch_subtitle)

        self.assertIn("initial=str(Path(path).parent)", source)
        self.assertIn('"本地导入字幕"', source)

    def test_local_subtitle_selection_enters_shared_preflight_with_complete_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            movie = root / "movies" / "movie.mkv"
            subtitle = root / "picked.srt"
            movie.parent.mkdir()
            movie.write_bytes(b"movie")
            subtitle.write_text(
                "1\n00:00:01,000 --> 00:00:02,000\nHello\n",
                encoding="utf-8",
            )
            plan = SimpleNamespace(status="pending", burned_subtitle=False, task_state="waiting")
            controller = SimpleNamespace(
                batch_plans={str(movie): plan},
                _qt_open_dialog=Mock(return_value=[str(subtitle)]),
                _batch_subtitle_selected=Mock(),
            )
            with patch("qt_app.core.parse_subtitle", return_value=[object()]), patch(
                "qt_app.pro_core.detect_subtitle_language", return_value="en"
            ):
                ProMaxQt._import_batch_subtitle(controller, str(movie))

            self.assertEqual(
                controller._qt_open_dialog.call_args.kwargs["initial"],
                str(movie.parent),
            )
            _path, copied, candidate, source_label = (
                controller._batch_subtitle_selected.call_args.args
            )
            self.assertTrue(Path(copied).is_file())
            self.assertEqual(candidate.recommendation, "用户指定")
            self.assertEqual(candidate.match_reason, "本地字幕文件")
            self.assertEqual(source_label, "本地导入字幕")

    def test_sidebar_contains_every_windows_drive(self) -> None:
        urls = ProMaxQt._file_dialog_sidebar_urls()
        paths = {url.toLocalFile().replace("\\", "/").rstrip("/") for url in urls}

        self.assertIn(str(Path.home()).replace("\\", "/"), paths)
        if sys.platform == "win32":
            drive_mask = ctypes.windll.kernel32.GetLogicalDrives()
            expected = {f"{chr(65 + index)}:" for index in range(26) if drive_mask & (1 << index)}
            self.assertTrue(expected.issubset(paths))


if __name__ == "__main__":
    unittest.main()
