# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import QApplication

import online_subtitles as opensubtitles
import subdl_subtitles as subdl
from subtitle_identity_guard import filename_container_identity_conflict
from qt_online_dialog import (
    OnlineSubtitleDialog,
)


class OnlineSubtitleDialogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        # This is a Python fixture file, not media; container-title behavior is
        # covered by identity tests rather than launching mkvmerge here.
        container_probe = patch("subtitle_identity_guard.container_title", return_value="")
        container_probe.start()
        self.addCleanup(container_probe.stop)
        self.dialog = OnlineSubtitleDialog(None, __file__, lambda *_args: None)

    def tearDown(self) -> None:
        self.dialog.close()
        self.app.processEvents()

    def test_saved_keys_survive_initialization_and_provider_switching(self) -> None:
        self.dialog.close()
        settings = {
            "opensubtitles_api_key": "open-key",
            "subdl_api_key": "subdl-key",
        }
        with patch.object(opensubtitles, "load_settings", return_value=settings):
            self.dialog = OnlineSubtitleDialog(None, __file__, lambda *_args: None)

        self.assertEqual(self.dialog.key_edit.text(), "open-key")
        self.dialog.provider_buttons["subdl"].setChecked(True)
        self.assertEqual(self.dialog.key_edit.text(), "subdl-key")
        self.dialog.provider_buttons["opensubtitles"].setChecked(True)
        self.assertEqual(self.dialog.key_edit.text(), "open-key")

    def test_search_button_is_restored_after_empty_result(self) -> None:
        for provider, service in (("opensubtitles", opensubtitles), ("subdl", subdl)):
            with self.subTest(provider=provider):
                self.dialog.provider_buttons[provider].setChecked(True)
                self.dialog.key_edit.setText(f"{provider}-key")
                original_search = service.search
                original_save = service.save_settings
                try:
                    service.save_settings = lambda _key: None
                    service.search = lambda _key, _path, _language: (
                        self.dialog.identity,
                        [],
                        SimpleNamespace(query_mode="", truncated=False, total_count=0),
                    )
                    with patch.object(self.dialog, "_run_worker") as run_worker:
                        self.dialog._search()
                        payload = run_worker.call_args.args[0](None)
                        run_worker.call_args.kwargs["result"](payload)
                        run_worker.call_args.kwargs["finished"]()
                    self.assertTrue(self.dialog.search_button.isEnabled())
                finally:
                    service.search = original_search
                    service.save_settings = original_save

    def test_both_services_verify_before_saving(self) -> None:
        for provider, service, validator_name, quota in (
            ("opensubtitles", opensubtitles, "validate_api_key", None),
            ("subdl", subdl, "validate_token", 12),
        ):
            with self.subTest(provider=provider), \
                 patch.object(service, validator_name, return_value=quota) as validate, \
                 patch.object(service, "save_settings") as save, \
                 patch.object(self.dialog, "_run_worker") as worker:
                self.dialog.provider_buttons[provider].setChecked(True)
                self.dialog.key_edit.setText(" test-key ")
                self.assertEqual(self.dialog.verify_button.text(), "验证并保存")
                self.dialog._save_or_verify()
                self.assertFalse(self.dialog.verify_button.isEnabled())
                save.assert_not_called()
                value = worker.call_args.args[0](None)
                worker.call_args.kwargs["result"](value)
                worker.call_args.kwargs["finished"]()
                validate.assert_called_once_with("test-key")
                save.assert_called_once_with("test-key")
                self.assertTrue(self.dialog.verify_button.isEnabled())
                self.assertIn("有效并已保存", self.dialog.status.text())

    def test_invalid_key_does_not_save(self) -> None:
        self.dialog.key_edit.setText("invalid-key")
        with patch.object(opensubtitles, "validate_api_key", side_effect=RuntimeError("invalid key")), \
             patch.object(opensubtitles, "save_settings") as save, \
             patch.object(self.dialog, "_run_worker") as worker:
            self.dialog._save_or_verify()
            with self.assertRaisesRegex(RuntimeError, "invalid key"):
                worker.call_args.args[0](None)
            worker.call_args.kwargs["finished"]()
        save.assert_not_called()
        self.assertTrue(self.dialog.verify_button.isEnabled())

    def test_provider_switch_cannot_save_key_into_wrong_service(self) -> None:
        self.dialog.key_edit.setText("open-key")
        with patch.object(opensubtitles, "validate_api_key", return_value=None), \
             patch.object(opensubtitles, "save_settings") as save_open, \
             patch.object(subdl, "save_settings") as save_subdl, \
             patch.object(self.dialog, "_run_worker") as worker:
            self.dialog._save_or_verify()
            value = worker.call_args.args[0](None)
            self.dialog.provider_buttons["subdl"].setChecked(True)
            previous_status = self.dialog.status.text()
            worker.call_args.kwargs["result"](value)
            worker.call_args.kwargs["finished"]()
        save_open.assert_called_once_with("open-key")
        save_subdl.assert_not_called()
        self.assertEqual(self.dialog.status.text(), previous_status)

    def test_validation_error_never_displays_subdl_key(self) -> None:
        self.dialog.provider_buttons["subdl"].setChecked(True)
        self.dialog.key_edit.setText("secret-subdl-key")
        with patch.object(subdl, "validate_token", side_effect=RuntimeError("rejected secret-subdl-key")), \
             patch.object(self.dialog, "_run_worker") as worker:
            self.dialog._save_or_verify()
            with self.assertRaises(RuntimeError) as raised:
                worker.call_args.args[0](None)
        self.assertNotIn("secret-subdl-key", str(raised.exception))

    def test_failed_search_does_not_replace_saved_key(self) -> None:
        self.dialog.key_edit.setText("invalid-open-key")
        with patch.object(opensubtitles, "search", side_effect=RuntimeError("API Key invalid")), \
             patch.object(opensubtitles, "save_settings") as save, \
             patch.object(self.dialog, "_run_worker") as worker:
            self.dialog._search()
            save.assert_not_called()
            with self.assertRaisesRegex(RuntimeError, "invalid"):
                worker.call_args.args[0](None)
            worker.call_args.kwargs["finished"]()
        save.assert_not_called()

    def test_successful_search_saves_key_after_response(self) -> None:
        self.dialog.key_edit.setText("valid-open-key")
        payload = (self.dialog.identity, [], SimpleNamespace(query_mode="", truncated=False, total_count=0))
        with patch.object(opensubtitles, "search", return_value=payload), \
             patch.object(opensubtitles, "save_settings") as save, \
             patch.object(self.dialog, "_run_worker") as worker:
            self.dialog._search()
            save.assert_not_called()
            result = worker.call_args.args[0](None)
            worker.call_args.kwargs["result"](result)
        save.assert_called_once_with("valid-open-key")

    def test_old_validated_key_does_not_overwrite_new_key(self) -> None:
        self.dialog.key_edit.setText("old-key")
        with patch.object(opensubtitles, "validate_api_key", return_value=None), \
             patch.object(opensubtitles, "save_settings") as save, \
             patch.object(self.dialog, "_run_worker") as worker:
            self.dialog._save_or_verify()
            callback = worker.call_args.kwargs["result"]
            self.dialog.key_edit.setText("new-key")
            self.dialog._save_or_verify()
            worker.call_args.kwargs["result"](None)
            callback(None)
        save.assert_called_once_with("new-key")

    def test_identity_conflict_shows_actionable_warning(self) -> None:
        with patch("subtitle_identity_guard.container_title", return_value="Star Wars: Episode IV - A New Hope (1977)"):
            notice = filename_container_identity_conflict("Return of the Jedi (1983).mkv")
        with patch("qt_online_dialog.QMessageBox.warning") as warning, \
             patch("qt_online_dialog.QMessageBox.critical") as error:
            self.dialog._show_error("RuntimeError: " + notice)
        warning.assert_called_once()
        error.assert_not_called()
        self.assertIn("影片名称不一致", warning.call_args.args[1])
        self.assertNotIn("RuntimeError", warning.call_args.args[2])
        self.assertIn("重新添加并分析", warning.call_args.args[2])

    def test_other_search_errors_remain_errors(self) -> None:
        with patch("qt_online_dialog.QMessageBox.warning") as warning, \
             patch("qt_online_dialog.QMessageBox.critical") as error:
            self.dialog._show_error("RuntimeError: API Key 无效")
        warning.assert_not_called()
        error.assert_called_once()


if __name__ == "__main__":
    unittest.main()
