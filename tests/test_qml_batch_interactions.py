from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QMetaObject, QObject, QPoint, QPointF, Qt, QUrl
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuick import QQuickItem
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QTableWidget, QTableWidgetItem

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "qml_frontend"))

from bridge import BackendBridge
from main import LicenseBridge


class QmlBatchInteractionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        hardware_patch = patch.object(BackendBridge, "_detect_hardware")
        hardware_patch.start()
        self.addCleanup(hardware_patch.stop)

    def test_replacement_toggle_persists_before_analysis(self) -> None:
        import profile_model

        with tempfile.TemporaryDirectory() as folder:
            profile_path = Path(folder) / "preference_profiles.json"
            with patch.object(profile_model, "profile_file", return_value=profile_path), patch(
                "qt_app.ProMaxQt._detect_hardware"
            ):
                backend = BackendBridge()
                backend.activateToolPage(1)
                self.assertFalse(backend.profileReplaceDownloadedSubtitle)
                backend.profileSetFlag("replaceDownloaded", True)
                self.assertTrue(backend.profileReplaceDownloadedSubtitle)
                self.assertTrue(backend._tool_controller.profiles[0].replace_downloaded_subtitle)
                saved = json.loads(profile_path.read_text(encoding="utf-8"))
                self.assertTrue(saved[0]["replace_downloaded_subtitle"])
                backend.shutdown()

    def test_all_preferences_auto_save_and_survive_slot_switch_and_reload(self) -> None:
        import profile_model

        with tempfile.TemporaryDirectory() as folder:
            profile_path = Path(folder) / "preference_profiles.json"
            with patch.object(profile_model, "profile_file", return_value=profile_path), patch(
                "qt_app.ProMaxQt._detect_hardware"
            ):
                backend = BackendBridge()
                backend.activateToolPage(1)
                backend.profileSetName("自动保存测试")
                backend.profileSetAudioPolicy("native")
                backend.profileToggleSubtitle("ja", True)
                backend.profileSetFlag("replaceDownloaded", True)
                backend.profileSetFlag("chineseScriptEquivalent", True)
                saved = json.loads(profile_path.read_text(encoding="utf-8"))
                self.assertEqual(saved[0]["name"], "自动保存测试")
                self.assertEqual(saved[0]["audio_policy"], "native")
                self.assertIn("ja", saved[0]["subtitle_languages"])
                self.assertTrue(saved[0]["replace_downloaded_subtitle"])
                self.assertTrue(saved[0]["chinese_script_equivalent"])

                backend.profileSelect(2)
                backend.profileSetName("第二套名称")
                backend.profileSelect(1)  # Switching slots must preserve both saved names.
                saved = json.loads(profile_path.read_text(encoding="utf-8"))
                self.assertEqual(saved[0]["name"], "自动保存测试")
                self.assertEqual(saved[1]["name"], "第二套名称")
                backend.shutdown()

                reopened = BackendBridge()
                reopened.activateToolPage(1)
                self.assertEqual(reopened.profileName, "自动保存测试")
                self.assertEqual(reopened.profileAudioPolicy, "native")
                self.assertTrue(reopened.profileReplaceDownloadedSubtitle)
                self.assertTrue(reopened.profileChineseScriptEquivalent)
                self.assertTrue(next(option for option in reopened.profileSubtitleOptions if option["code"] == "ja")["checked"])
                reopened.shutdown()

    def test_ctrl_and_shift_click_select_multiple_movie_rows(self) -> None:
        app = QApplication.instance() or QApplication([])
        backend = BackendBridge()
        license_bridge = LicenseBridge()
        license_bridge._valid = True
        with tempfile.TemporaryDirectory() as folder:
            movies = [Path(folder) / f"movie_{index}.mkv" for index in range(4)]
            for movie in movies:
                movie.write_bytes(b"movie")
            with patch("qt_app.ProMaxQt._detect_hardware"):
                backend._ensure_tool_controller()._batch_add_paths([str(path) for path in movies])
            engine = QQmlApplicationEngine()
            context = engine.rootContext()
            context.setContextProperty("backend", backend)
            context.setContextProperty("licenseBridge", license_bridge)
            context.setContextProperty("uiFontFamily", "Arial")
            context.setContextProperty("appVersion", "test")
            engine.load(QUrl.fromLocalFile(str(ROOT / "qml_frontend" / "Main.qml")))
            self.assertTrue(engine.rootObjects())
            window = engine.rootObjects()[0]
            backend.toolStateChanged.emit()
            app.processEvents()
            page = window.findChild(QQuickItem, "batchToolPage")
            batch_list = window.findChild(QQuickItem, "batchList")
            rows = sorted(
                [item for item in batch_list.childItems()[0].childItems()
                 if item.objectName() == "movieRow"],
                key=lambda row: row.property("index"),
            )
            self.assertEqual(len(rows), 4)

            def click(row: QQuickItem, modifier=Qt.KeyboardModifier.NoModifier) -> None:
                center = row.mapToScene(QPointF(100, row.height() / 2))
                QTest.mouseClick(window, Qt.MouseButton.LeftButton, modifier,
                                 QPoint(round(center.x()), round(center.y())))
                app.processEvents()

            click(rows[0])
            click(rows[2], Qt.KeyboardModifier.ControlModifier)
            self.assertEqual(set(page.property("selectedRows").toVariant()), {0, 2})
            click(rows[3], Qt.KeyboardModifier.ShiftModifier)
            self.assertEqual(set(page.property("selectedRows").toVariant()), {2, 3})
            remove_button = window.findChild(QQuickItem, "removeSelectedButton")
            center = remove_button.mapToScene(
                QPointF(remove_button.width() / 2, remove_button.height() / 2)
            )
            QTest.mouseClick(window, Qt.MouseButton.LeftButton,
                             pos=QPoint(round(center.x()), round(center.y())))
            app.processEvents()
            self.assertEqual(backend._tool_controller.batch_paths,
                             [str(movies[0]), str(movies[1])])
            window.close()
        backend.shutdown()

    def test_right_click_target_is_independent_from_selected_movies(self) -> None:
        app = QApplication.instance() or QApplication([])
        backend = BackendBridge()
        license_bridge = LicenseBridge()
        license_bridge._valid = True
        with tempfile.TemporaryDirectory() as folder, patch("qt_app.ProMaxQt._detect_hardware"):
            movies = [Path(folder) / f"movie_{index}.mkv" for index in range(3)]
            for movie in movies:
                movie.write_bytes(b"movie")
            backend._ensure_tool_controller()._batch_add_paths([str(path) for path in movies])
            engine = QQmlApplicationEngine()
            context = engine.rootContext()
            context.setContextProperty("backend", backend)
            context.setContextProperty("licenseBridge", license_bridge)
            context.setContextProperty("uiFontFamily", "Arial")
            context.setContextProperty("appVersion", "test")
            engine.load(QUrl.fromLocalFile(str(ROOT / "qml_frontend" / "Main.qml")))
            self.assertTrue(engine.rootObjects())
            window = engine.rootObjects()[0]
            backend.toolStateChanged.emit()
            app.processEvents()
            page = window.findChild(QQuickItem, "batchToolPage")
            batch_list = window.findChild(QQuickItem, "batchList")
            rows = sorted(
                [item for item in batch_list.childItems()[0].childItems()
                 if item.objectName() == "movieRow"],
                key=lambda row: row.property("index"),
            )
            self.assertEqual(len(rows), 3)

            def click(row, button, modifier=Qt.KeyboardModifier.NoModifier):
                point = row.mapToScene(QPointF(100, row.height() / 2))
                QTest.mouseClick(window, button, modifier,
                                 QPoint(round(point.x()), round(point.y())))
                app.processEvents()

            click(rows[0], Qt.MouseButton.LeftButton)
            click(rows[2], Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ControlModifier)
            self.assertEqual(set(page.property("selectedRows").toVariant()), {0, 2})
            click(rows[1], Qt.MouseButton.RightButton)
            self.assertEqual(set(page.property("selectedRows").toVariant()), {0, 2})
            menu = rows[1].findChild(QObject, "movieContextMenu")
            self.assertIsNotNone(menu)
            play = menu.findChild(QObject, "contextPlayVideo")
            self.assertIsNotNone(play)
            with patch("bridge.QDesktopServices.openUrl", return_value=True) as open_url:
                self.assertTrue(QMetaObject.invokeMethod(play, "triggered"))
                app.processEvents()
                open_url.assert_called_once()
                self.assertEqual(Path(open_url.call_args.args[0].toLocalFile()), movies[1])
            self.assertEqual(set(page.property("selectedRows").toVariant()), {0, 2})
            details = menu.findChild(QObject, "contextShowDetails")
            self.assertIsNotNone(details)
            self.assertTrue(QMetaObject.invokeMethod(details, "triggered"))
            app.processEvents()
            detail_item = page.property("detailItem")
            if hasattr(detail_item, "toVariant"):
                detail_item = detail_item.toVariant()
            self.assertEqual(detail_item["path"], str(movies[1]))
            self.assertEqual(set(page.property("selectedRows").toVariant()), {0, 2})
            QTest.keyClick(window, Qt.Key.Key_Escape)
            app.processEvents()

            remove = menu.findChild(QObject, "contextRemoveSelected")
            self.assertIsNotNone(remove)
            self.assertTrue(QMetaObject.invokeMethod(remove, "triggered"))
            app.processEvents()
            self.assertEqual(backend._tool_controller.batch_paths, [str(movies[1])])
            window.close()
        backend.shutdown()

    def test_add_movie_button_opens_visible_qml_owned_file_picker(self) -> None:
        app = QApplication.instance() or QApplication([])
        backend = BackendBridge()
        license_bridge = LicenseBridge()
        license_bridge._valid = True
        engine = QQmlApplicationEngine()
        context = engine.rootContext()
        context.setContextProperty("backend", backend)
        context.setContextProperty("licenseBridge", license_bridge)
        context.setContextProperty("uiFontFamily", "Arial")
        context.setContextProperty("appVersion", "test")
        engine.load(QUrl.fromLocalFile(str(ROOT / "qml_frontend" / "Main.qml")))
        self.assertTrue(engine.rootObjects())
        window = engine.rootObjects()[0]
        button = window.findChild(QQuickItem, "addMovieButton")
        dialog = window.findChild(QObject, "addMovieDialog")
        self.assertIsNotNone(button)
        self.assertIsNotNone(dialog)
        center = button.mapToScene(QPointF(button.width() / 2, button.height() / 2))
        QTest.mouseClick(window, Qt.MouseButton.LeftButton,
                         pos=QPoint(round(center.x()), round(center.y())))
        app.processEvents()
        self.assertTrue(dialog.property("visible"))
        dialog.close()
        folder_button = window.findChild(QQuickItem, "addFolderButton")
        folder_dialog = window.findChild(QObject, "addFolderDialog")
        center = folder_button.mapToScene(QPointF(folder_button.width() / 2,
                                                   folder_button.height() / 2))
        QTest.mouseClick(window, Qt.MouseButton.LeftButton,
                         pos=QPoint(round(center.x()), round(center.y())))
        app.processEvents()
        self.assertTrue(folder_dialog.property("visible"))
        folder_dialog.close()
        window.close()
        backend.shutdown()

    def test_qml_backend_adds_multiple_files_and_removes_multiple_rows(self) -> None:
        app = QApplication.instance() or QApplication([])
        backend = BackendBridge()
        with tempfile.TemporaryDirectory() as folder:
            first = Path(folder) / "first.mkv"
            second = Path(folder) / "second.mp4"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            table = QTableWidget(3, 1)
            for row in range(3):
                table.setItem(row, 0, QTableWidgetItem(str(row)))
            controller = SimpleNamespace(
                batch_paths=[str(first), str(second), str(Path(folder) / "third.mkv")],
                batch_table=table, batch_running=False,
                _batch_add_paths=Mock(), _batch_remove=Mock(),
            )
            backend._tool_controller = controller
            self.assertEqual(
                backend.handleDroppedUrls([QUrl.fromLocalFile(str(first)),
                                           QUrl.fromLocalFile(str(second))], 0),
                0,
            )
            self.assertEqual(
                [Path(path) for path in controller._batch_add_paths.call_args.args[0]],
                [first, second],
            )
            backend.batchRemoveRows([0, 2])
            self.assertEqual(
                {index.row() for index in table.selectionModel().selectedRows()}, {0, 2}
            )
            controller._batch_remove.assert_called_once()
            controller._import_batch_subtitle = Mock()
            subtitle = Path(folder) / "English.srt"
            subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
            self.assertEqual(
                Path(QUrl(backend.batchMovieFolderUrl(1)).toLocalFile()), second.parent
            )
            backend.batchImportSubtitleFile(1, QUrl.fromLocalFile(str(subtitle)))
            args = controller._import_batch_subtitle.call_args.args
            self.assertEqual(Path(args[0]), second)
            self.assertEqual(Path(args[1]), subtitle)
        backend.shutdown()


if __name__ == "__main__":
    unittest.main()
