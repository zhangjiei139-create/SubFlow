import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PySide6.QtCore import QUrl

ROOT = Path(__file__).resolve().parents[1]
QML_ROOT = ROOT / "qml_frontend"
for path in (str(ROOT), str(QML_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

import system_resources
from bridge import BackendBridge


class QmlBackendBridgeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import os
        os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
        from PySide6.QtWidgets import QApplication
        cls._app=QApplication.instance() or QApplication([])

    def setUp(self):
        # Destroy each QObject's timer before later processEvents() calls.
        # Local Python fixtures otherwise leave signal/closure cycles alive,
        # and Qt can dispatch their stale callbacks in an unrelated test.
        self._bridges=[]
        original_init=BackendBridge.__init__
        def init(instance):
            original_init(instance); self._bridges.append(instance)
        bridge_init=patch.object(BackendBridge,'__init__',init)
        bridge_init.start(); self.addCleanup(bridge_init.stop)
        # Bridge tests provide their own profile and must not leave real
        # PowerShell hardware probes running across later GUI tests.
        hardware_probe = patch.object(BackendBridge, "_detect_hardware")
        hardware_probe.start()
        self.addCleanup(hardware_probe.stop)
        settings = patch("bridge.online_subtitles.load_settings", return_value={
            "opensubtitles_api_key": "saved-open-key", "subdl_api_key": "saved-subdl-key"})
        settings.start()
        self.addCleanup(settings.stop)

    def tearDown(self):
        from PySide6.QtCore import QCoreApplication,QEvent
        for bridge in self._bridges:
            bridge.shutdown(); bridge.deleteLater()
        QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)
        self._app.processEvents()

    def test_font_editor_is_deferred_and_saves_only_selected_profile(self):
        from profile_model import default_profiles
        bridge=BackendBridge(); values=default_profiles()
        controller=SimpleNamespace(batch_running=False,batch_plans={},profiles=values,
            profile_slot=Mock())
        controller.profile_slot.currentIndex.return_value=1
        bridge._tool_controller=controller
        with patch('subtitle_font_dialog.SubtitleFontDialog') as factory, patch('profile_model.save_profiles') as save:
            dialog=factory.return_value; dialog.exec.return_value=1
            dialog.result_styles={'zh-CN':dict(font_id='builtin:chill',size=38,color='#FFFFFF')}
            bridge.profileEditFonts(); factory.assert_not_called()
            self._app.processEvents()
            factory.assert_called_once_with(values[1],controller)
            self.assertEqual(values[1].subtitle_styles,dialog.result_styles)
            self.assertEqual(values[0].subtitle_styles,{})
            save.assert_called_once_with(values); dialog.deleteLater.assert_called_once()

    def test_font_editor_obeys_analysis_lock(self):
        bridge=BackendBridge(); bridge._tool_controller=SimpleNamespace(batch_running=False,
            batch_plans={'film':SimpleNamespace(status='ready')})
        with patch('subtitle_font_dialog.SubtitleFontDialog') as factory:
            bridge.profileEditFonts(); self._app.processEvents(); factory.assert_not_called()

    def test_subtitle_service_verifies_and_saves_without_video(self):
        bridge = BackendBridge()
        with patch("bridge.threading.Thread") as thread, \
             patch("bridge.online_subtitles.validate_api_key", return_value=None) as validate, \
             patch("bridge.online_subtitles.save_settings") as save:
            bridge.saveSubtitleService("opensubtitles", " key-value ")
            save.assert_not_called()
            self.assertEqual(bridge.openSubtitlesStatus, "正在验证…")
            thread.call_args.kwargs["target"]()
        validate.assert_called_once_with("key-value")
        save.assert_called_once_with("key-value")
        self.assertEqual(bridge.openSubtitlesStatus, "已验证并保存")

    def test_periodic_background_refresh_pauses_only_for_listening_window(self):
        # Real Qt modal windows must not leak their nested-window lifetimes into
        # unrelated native-dialog tests in this long-lived test process.
        import os,subprocess
        if os.environ.get('SUBFLOW_BACKGROUND_SYNC_TEST_CHILD')!='1':
            env=dict(os.environ,QT_QPA_PLATFORM='offscreen',SUBFLOW_BACKGROUND_SYNC_TEST_CHILD='1')
            result=subprocess.run([sys.executable,'-m','unittest',
                'tests.test_qml_bridge.QmlBackendBridgeTest.test_periodic_background_refresh_pauses_only_for_listening_window'],
                cwd=ROOT,env=env,capture_output=True,text=True,encoding='utf8',errors='replace',timeout=20)
            self.assertEqual(result.returncode,0,result.stdout+'\n'+result.stderr)
            return
        from PySide6.QtWidgets import QApplication,QDialog
        class ManualSubtitleReviewDialog(QDialog): pass
        app=QApplication.instance() or QApplication([])
        type(self)._progress_test_app=app
        bridge=BackendBridge()
        changes=[]
        bridge.toolStateChanged.connect(lambda:changes.append(True))
        listening=ManualSubtitleReviewDialog()
        listening.setModal(True);listening.show();app.processEvents()
        bridge._sync_tool_state()
        self.assertFalse(changes)
        child=QDialog(listening);child.setModal(True);child.show();app.processEvents()
        bridge._sync_tool_state()
        self.assertFalse(changes)
        child.close();listening.close();app.processEvents()
        normal=QDialog();normal.setModal(True);normal.show();app.processEvents()
        bridge._sync_tool_state()
        self.assertEqual(len(changes),1)
        normal.close();app.processEvents()
        bridge._sync_tool_state()
        self.assertEqual(len(changes),2)

    def test_failed_key_validation_preserves_saved_configuration(self):
        bridge = BackendBridge()
        with patch("bridge.threading.Thread") as thread, \
             patch("bridge.online_subtitles.validate_api_key", side_effect=RuntimeError("bad-key invalid")), \
             patch("bridge.online_subtitles.save_settings") as save:
            bridge.saveSubtitleService("opensubtitles", "bad-key")
            thread.call_args.kwargs["target"]()
        save.assert_not_called()
        self.assertIn("验证失败", bridge.openSubtitlesStatus)
        self.assertNotIn("bad-key", bridge.openSubtitlesStatus)

    def test_subdl_validation_still_uses_its_own_service_and_quota(self):
        bridge = BackendBridge()
        with patch("bridge.threading.Thread") as thread, \
             patch("bridge.subdl_subtitles.validate_token", return_value=17) as validate, \
             patch("bridge.subdl_subtitles.save_settings") as save, \
             patch("bridge.online_subtitles.save_settings") as other_save:
            bridge.saveSubtitleService("subdl", "subdl-key")
            save.assert_not_called()
            thread.call_args.kwargs["target"]()
        validate.assert_called_once_with("subdl-key")
        save.assert_called_once_with("subdl-key")
        other_save.assert_not_called()
        self.assertEqual(bridge.subdlStatus, "已验证并保存；可用额度 17")

    def test_old_validation_response_cannot_replace_new_key(self):
        bridge = BackendBridge()
        with patch("bridge.threading.Thread") as thread, \
             patch("bridge.online_subtitles.validate_api_key", return_value=None), \
             patch("bridge.online_subtitles.save_settings") as save:
            bridge.saveSubtitleService("opensubtitles", "old-key")
            old_response = thread.call_args.kwargs["target"]
            bridge.saveSubtitleService("opensubtitles", "new-key")
            thread.call_args.kwargs["target"]()
            old_response()
        save.assert_called_once_with("new-key")
        self.assertEqual(bridge.openSubtitlesKey, "new-key")
        self.assertEqual(bridge.openSubtitlesStatus, "已验证并保存")

    def test_blank_key_does_not_validate_or_save(self):
        bridge = BackendBridge()
        with patch("bridge.threading.Thread") as thread, \
             patch("bridge.online_subtitles.save_settings") as save:
            bridge.saveSubtitleService("opensubtitles", "  ")
        thread.assert_not_called()
        save.assert_not_called()
        self.assertEqual(bridge.openSubtitlesStatus, "请先填写 API Key")

    def test_configuration_save_error_does_not_report_success(self):
        bridge = BackendBridge()
        with patch("bridge.threading.Thread") as thread, \
             patch("bridge.online_subtitles.validate_api_key", return_value=None), \
             patch("bridge.online_subtitles.save_settings", side_effect=OSError("disk locked")):
            bridge.saveSubtitleService("opensubtitles", "valid-key")
            thread.call_args.kwargs["target"]()
        self.assertEqual(bridge.openSubtitlesStatus, "保存失败：disk locked")

    def test_qml_exposes_compact_paired_menu_and_image_subtitle_controls(self):
        qml = (QML_ROOT / "Main.qml").read_text(encoding="utf-8")
        main = (QML_ROOT / "main.py").read_text(encoding="utf-8")
        installer = (ROOT / "installer" / "pro_installer.py").read_text(encoding="utf-8")

        self.assertIn('title: "SubFlow v" + appVersion', qml)
        self.assertIn('text: "v" + appVersion', qml)
        self.assertNotIn('v2.0.33', qml)
        self.assertIn('SubFlow 你字幕处理助手', installer)
        self.assertNotIn('SubFlow 你的字幕处理专家', installer)
        self.assertIn('setContextProperty(\n        "appVersion"', main)
        self.assertEqual(installer.count("$shortcut.Arguments = ''"), 2)
        self.assertIn('component SubtitleServicePage: Item', qml)
        self.assertIn('text: "字幕服务"', qml)
        self.assertNotIn('backend.openSubtitleService()', qml)
        self.assertNotIn('text: "单片处理"', qml)
        self.assertIn('text: "影片处理"', qml)
        self.assertNotIn('text: "智能字幕"', qml)
        self.assertNotIn('text: "手动对齐当前字幕"', qml)
        self.assertIn('title: "字幕选择"', qml)
        self.assertIn('text: "在线选择"', qml)
        self.assertIn('text: "本地导入"', qml)
        self.assertIn('text: "播放视频"', qml)
        self.assertIn('firstText: "处理"', qml)
        self.assertIn('secondText: "停止"', qml)
        self.assertIn('text: "核听当前字幕"', qml)
        self.assertNotIn('firstText: "待核听"', qml)
        self.assertNotIn('secondText: "主动核听"', qml)
        self.assertNotIn('firstObjectName: "contextPendingReview"', qml)
        self.assertIn('text: "移除"', qml)
        self.assertIn('color: modelData.rowColor', qml)
        self.assertNotIn('text: "单独处理"', qml)
        self.assertNotIn('text: "单独停止"', qml)
        self.assertIn('backend.batchRemoveRows(selectedRows)', qml)
        self.assertIn('backend.batchStartRows(selectedRows)', qml)
        self.assertIn('backend.batchStopRows(selectedRows)', qml)
        self.assertIn('text: "保留下载字幕并替换"', qml)
        self.assertNotIn("图片字幕不保留", qml)
        self.assertNotIn("profileDiscardImageSubtitles", qml)
        self.assertIn('title: "原生音频"', qml)
        self.assertIn('title: "通用兼容"', qml)
        self.assertIn('title: "精简兼容"', qml)
        self.assertNotIn('text: "兼容方案"', qml)
        self.assertNotIn('text: "懒人方案"', qml)
        self.assertIn('backend.profileSetFlag("replaceDownloaded", checked)', qml)
        self.assertNotIn('text: "修改后自动保存"', qml)
        self.assertNotIn('text: "保存偏好"', qml)
        self.assertIn('onTextEdited: backend.profileSetName(text)', qml)
        self.assertNotIn('text: "影片完全无字幕时"', qml)
        self.assertNotIn("profileNoSubtitleAction", qml)
        self.assertNotIn("profileSetNoSubtitleAction", qml)
        self.assertIn("columns: 4", qml)

    def test_replacement_preference_uses_current_controller_flag(self):
        bridge = BackendBridge()
        replacement = Mock()
        replacement.isChecked.return_value = False
        script = Mock()
        controller = SimpleNamespace(
            profile_replace_downloaded_subtitle=replacement,
            profile_chinese_script_equivalent=script,
            batch_running=False,
            batch_plans={},
            _save_profile=Mock(),
        )
        bridge._tool_controller = controller

        self.assertFalse(bridge.profileReplaceDownloadedSubtitle)
        bridge.profileSetFlag("replaceDownloaded", True)
        replacement.setChecked.assert_called_once_with(True)
        # A real checkbox's toggled signal persists the profile; this mock
        # checks only that the bridge does not issue a duplicate save.
        controller._save_profile.assert_not_called()
        replacement.isChecked.return_value = True
        self.assertTrue(bridge.profileReplaceDownloadedSubtitle)

    def test_batch_smart_subtitle_delegates_to_controller(self):
        bridge = BackendBridge()
        controller = SimpleNamespace(
            batch_paths=["movie.mkv"],
            _start_batch_smart_subtitle=Mock(),
        )
        bridge._tool_controller = controller

        bridge.batchSmartSubtitles(0)

        controller._start_batch_smart_subtitle.assert_called_once_with("movie.mkv")

    def test_review_action_states_are_exposed_independently(self):
        from profile_model import BatchPlan
        from PySide6.QtGui import QColor
        bridge = BackendBridge()
        plan = BatchPlan(path="movie.mkv", profile_slot=1, status="ready")
        controller = SimpleNamespace(batch_paths=[plan.path], batch_plans={plan.path: plan},
                                     batch_item_cancel_events={}, batch_running=False,
                                     _batch_task_label=Mock(return_value="ready"),
                                     _batch_row_color=Mock(return_value=QColor("#fff3da")),
                                     _batch_translation_editable=Mock(return_value=False))
        bridge._tool_controller = controller
        plan.external_subtitle = "fixture.srt"
        for pending, active in ((True, False), (False, True), (False, False)):
            plan.manual_review_required = pending
            bridge._batch_availability[("subtitle", plan.external_subtitle)] = pending or active
            with patch.object(bridge, "_queue_batch_availability"):
                bridge._refresh_batch_items()
                item = bridge.batchItems[0]
            self.assertEqual(item["canPendingReview"], pending)
            self.assertEqual(item["canActiveReview"], active)
            self.assertEqual(item["canListenSubtitle"], pending or active)
            self.assertEqual(item["canAlignSubtitle"], pending or active)
            self.assertEqual(item["rowColor"], "#fff3da")

    def test_listen_current_is_deferred_and_keeps_movie_identity(self):
        bridge = BackendBridge()
        controller = SimpleNamespace(batch_paths=["first.mkv", "second.mkv"],
                                     batch_plans={"first.mkv": object(), "second.mkv": object()},
                                     _listen_current_subtitle=Mock())
        bridge._tool_controller = controller
        bridge.batchListenCurrentSubtitle(-1)
        bridge.batchListenCurrentSubtitle(2)
        bridge.batchListenCurrentSubtitle(0)
        controller._listen_current_subtitle.assert_not_called()
        controller.batch_paths.reverse()
        self._app.processEvents()
        controller._listen_current_subtitle.assert_called_once_with("first.mkv")

    def test_listen_current_does_not_target_removed_and_readded_plan(self):
        bridge = BackendBridge()
        controller = SimpleNamespace(batch_paths=["movie.mkv"],
                                     batch_plans={"movie.mkv": object()},
                                     _listen_current_subtitle=Mock())
        bridge._tool_controller = controller
        bridge.batchListenCurrentSubtitle(0)
        controller.batch_plans["movie.mkv"] = object()
        self._app.processEvents()
        controller._listen_current_subtitle.assert_not_called()

    def test_pending_review_is_deferred_and_keeps_movie_identity(self):
        bridge = BackendBridge()
        controller = SimpleNamespace(batch_paths=["first.mkv", "second.mkv"],
                                     batch_plans={"first.mkv": object(), "second.mkv": object()},
                                     _open_batch_pending_review=Mock())
        bridge._tool_controller = controller
        bridge.batchPendingReview(-1)
        bridge.batchPendingReview(2)
        bridge.batchPendingReview(0)
        controller._open_batch_pending_review.assert_not_called()
        controller.batch_paths.reverse()
        self._app.processEvents()
        controller._open_batch_pending_review.assert_called_once_with("first.mkv")

    def test_pending_review_does_not_target_removed_and_readded_plan(self):
        bridge = BackendBridge()
        controller = SimpleNamespace(batch_paths=["movie.mkv"],
                                     batch_plans={"movie.mkv": object()},
                                     _open_batch_pending_review=Mock())
        bridge._tool_controller = controller
        bridge.batchPendingReview(0)
        controller.batch_plans["movie.mkv"] = object()
        self._app.processEvents()
        controller._open_batch_pending_review.assert_not_called()

    def test_batch_single_item_actions_delegate_to_controller(self):
        bridge = BackendBridge()
        profile = system_resources.HardwareProfile(
            8, 32.0, 1, (8192,), False, "均衡模式", 20.0,
            "standard", "标准可用", "测试", (),
        )
        controller = SimpleNamespace(
            batch_paths=["movie.mkv"],
            batch_hardware=None,
            batch_mode=SimpleNamespace(setCurrentText=Mock()),
            _batch_start_one=Mock(),
            _batch_stop_one=Mock(),
        )
        bridge._hardware_profile = profile
        bridge._tool_controller = controller

        bridge.batchStartOne(0)
        bridge.batchStopOne(0)

        self.assertIs(controller.batch_hardware, profile)
        controller._batch_start_one.assert_called_once_with("movie.mkv")
        controller._batch_stop_one.assert_called_once_with("movie.mkv")

    def test_translation_review_menu_dispatches_only_valid_movie_row(self):
        from PySide6.QtWidgets import QApplication
        self.__class__._app = QApplication.instance() or QApplication([])
        bridge = BackendBridge()
        plan = object()
        controller = SimpleNamespace(batch_paths=["movie.mkv"], batch_plans={"movie.mkv":plan},
                                     _batch_edit_translation=Mock())
        bridge._tool_controller = controller
        bridge.batchReviewTranslation(-1)
        bridge.batchReviewTranslation(1)
        controller._batch_edit_translation.assert_not_called()
        bridge.batchReviewTranslation(0)
        controller._batch_edit_translation.assert_not_called()
        self.__class__._app.processEvents()
        controller._batch_edit_translation.assert_called_once_with("movie.mkv")

    def test_deferred_translation_review_keeps_movie_identity_after_reordering(self):
        from PySide6.QtWidgets import QApplication
        self.__class__._app = QApplication.instance() or QApplication([])
        bridge=BackendBridge()
        controller=SimpleNamespace(batch_paths=['first.mkv','second.mkv'],
            batch_plans={'first.mkv':object(),'second.mkv':object()},_batch_edit_translation=Mock())
        bridge._tool_controller=controller
        bridge.batchReviewTranslation(0)
        controller.batch_paths.reverse()
        self.__class__._app.processEvents()
        controller._batch_edit_translation.assert_called_once_with('first.mkv')

    def test_removed_and_readded_movie_does_not_receive_queued_translation_review(self):
        from PySide6.QtWidgets import QApplication
        self.__class__._app = QApplication.instance() or QApplication([])
        bridge=BackendBridge()
        controller=SimpleNamespace(batch_paths=['movie.mkv'],batch_plans={'movie.mkv':object()},
                                   _batch_edit_translation=Mock())
        bridge._tool_controller=controller
        bridge.batchReviewTranslation(0)
        controller.batch_plans['movie.mkv']=object()
        self.__class__._app.processEvents()
        controller._batch_edit_translation.assert_not_called()

    def test_completed_movie_review_is_deferred_and_does_not_add_processing_input(self):
        from PySide6.QtWidgets import QApplication
        self.__class__._app=QApplication.instance() or QApplication([])
        bridge=BackendBridge()
        controller=SimpleNamespace(_edit_translation_output=Mock())
        bridge._tool_controller=controller
        bridge.reviewCompletedMovie(QUrl.fromLocalFile('G:/movie.SF.mkv'))
        controller._edit_translation_output.assert_not_called()
        self.__class__._app.processEvents()
        controller._edit_translation_output.assert_called_once_with('G:/movie.SF.mkv')

    def test_selected_movie_actions_dispatch_one_batch_and_stop_each_selected(self):
        bridge = BackendBridge()
        controller = SimpleNamespace(
            batch_paths=["first.mkv", "second.mkv", "third.mkv"],
            batch_plans={
                "first.mkv": SimpleNamespace(status="ready", burned_subtitle=False, task_state="waiting"),
                "second.mkv": SimpleNamespace(status="ready", burned_subtitle=False, task_state="completed"),
                "third.mkv": SimpleNamespace(status="review", burned_subtitle=False, task_state="waiting"),
            },
            batch_running=False,
            batch_item_cancel_events={"first.mkv": Mock(), "third.mkv": Mock()},
            _batch_log_line=Mock(),
            _batch_start_paths=Mock(),
            _batch_stop_one=Mock(),
        )
        bridge._tool_controller = controller

        bridge.batchStartRows([2, 1, 0, 2, 99])
        controller._batch_start_paths.assert_called_once_with(["first.mkv", "third.mkv"])
        bridge.batchStopRows([2, 0, 2, 99])
        self.assertEqual(
            [call.args[0] for call in controller._batch_stop_one.call_args_list],
            ["first.mkv", "third.mkv"],
        )

    def test_batch_play_video_uses_system_player(self):
        bridge = BackendBridge()
        controller = SimpleNamespace(batch_paths=["movie.mkv"])
        bridge._tool_controller = controller

        with patch("bridge.QDesktopServices.openUrl", return_value=True) as open_url:
            bridge.batchPlayVideo(0)

        open_url.assert_called_once()
        self.assertEqual(open_url.call_args.args[0].toLocalFile(), "movie.mkv")

    def test_batch_preference_combo_is_not_covered_by_row_mouse_area(self):
        qml = (QML_ROOT / "Main.qml").read_text(encoding="utf-8")
        combo = qml.index('objectName: "batchPreferenceCombo"')
        row_mouse_area = qml.index("MouseArea {", combo)
        row_mouse_area_end = qml.index("}", row_mouse_area)

        self.assertIn("z: 1", qml[row_mouse_area:row_mouse_area_end])
        row_layout = qml.rfind("RowLayout {", 0, combo)
        self.assertIn("z: 2", qml[row_layout:combo])

    def test_batch_start_applies_detected_mode_to_qt_controller(self):
        bridge = BackendBridge()
        profile = system_resources.HardwareProfile(
            8, 32.0, 1, (8192,), False, "均衡模式", 20.0,
            "standard", "标准可用", "测试", (),
        )
        controller = SimpleNamespace(
            batch_hardware=None,
            batch_mode=SimpleNamespace(setCurrentText=Mock()),
            _batch_start=Mock(),
        )
        bridge._hardware_profile = profile
        bridge._tool_controller = controller

        bridge.batchStart()

        self.assertIs(controller.batch_hardware, profile)
        controller.batch_mode.setCurrentText.assert_called_once_with("均衡模式")
        controller._batch_start.assert_called_once_with()

    def test_batch_profile_can_be_changed_per_movie(self):
        bridge = BackendBridge()
        controller = SimpleNamespace(
            batch_paths=["movie.mkv"],
            _batch_profile_changed=Mock(),
        )
        bridge._tool_controller = controller

        bridge.batchSetProfile(0, 3)

        controller._batch_profile_changed.assert_called_once_with("movie.mkv", 3)

    def test_profile_selection_becomes_batch_default_and_updates_existing_rows(self):
        bridge = BackendBridge()
        plan = SimpleNamespace(profile_slot=1)
        controller = SimpleNamespace(
            profile_slot=SimpleNamespace(setCurrentIndex=Mock()),
            batch_default=SimpleNamespace(setCurrentIndex=Mock()),
            batch_plans={"movie.mkv": plan},
        )
        bridge._tool_controller = controller

        bridge.profileSelect(2)

        controller.profile_slot.setCurrentIndex.assert_called_once_with(1)
        controller.batch_default.setCurrentIndex.assert_called_once_with(1)
        self.assertEqual(plan.profile_slot, 2)

    def test_one_movie_drop_uses_unified_movie_list(self):
        bridge = BackendBridge()
        with tempfile.TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir)
            video = folder / "movie.mkv"
            video.write_bytes(b"video")
            controller = SimpleNamespace(_batch_add_paths=Mock())
            bridge._tool_controller = controller
            with patch("batch_core.videos_in_folder", return_value=[str(video)]):
                destination = bridge.handleDroppedUrls([QUrl.fromLocalFile(str(folder))], 0)

        self.assertEqual(destination, 0)
        controller._batch_add_paths.assert_called_once_with([str(video)])

    def test_multiple_movie_drop_uses_same_unified_page(self):
        bridge = BackendBridge()
        with tempfile.TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir)
            videos = [str(folder / "one.mkv"), str(folder / "two.mkv")]
            controller = SimpleNamespace(_batch_add_paths=Mock())
            bridge._tool_controller = controller
            with patch("batch_core.videos_in_folder", return_value=videos):
                destination = bridge.handleDroppedUrls([QUrl.fromLocalFile(str(folder))], 0)

        self.assertEqual(destination, 0)
        controller._batch_add_paths.assert_called_once_with(videos)

    def test_every_page_accepts_video_drop_and_returns_movie_page(self):
        bridge = BackendBridge()
        with tempfile.TemporaryDirectory() as temp_dir:
            video = Path(temp_dir) / "movie.mkv"
            video.write_bytes(b"video")
            controller = SimpleNamespace(_batch_add_paths=Mock())
            bridge._tool_controller = controller
            dropped_url = QUrl.fromLocalFile(str(video))
            for page in (1, 2, 3):
                controller._batch_add_paths.reset_mock()
                destination = bridge.handleDroppedUrls([dropped_url], page)
                self.assertEqual(destination, 0)
                controller._batch_add_paths.assert_called_once_with([dropped_url.toLocalFile()])

    def test_service_and_hardware_navigation_match_new_page_order(self):
        bridge = BackendBridge()
        controller = SimpleNamespace(tabs=Mock(), nav_buttons=[Mock(), Mock(), Mock()])
        bridge._tool_controller = controller
        bridge.activateToolPage(2)
        controller.tabs.setCurrentIndex.assert_not_called()
        bridge.activateToolPage(3)
        controller.tabs.setCurrentIndex.assert_called_once_with(2)
        controller.nav_buttons[2].setChecked.assert_called_once_with(True)

    def test_folder_drop_from_service_page_and_nonvideo_drop_keeps_page(self):
        bridge = BackendBridge()
        with tempfile.TemporaryDirectory() as temp_dir:
            folder = Path(temp_dir)
            video = folder / 'movie.mkv'
            video.write_bytes(b'video')
            controller = SimpleNamespace(_batch_add_paths=Mock())
            bridge._tool_controller = controller
            with patch('batch_core.videos_in_folder', return_value=[str(video)]):
                self.assertEqual(bridge.handleDroppedUrls([QUrl.fromLocalFile(str(folder))], 2), 0)
            controller._batch_add_paths.assert_called_once_with([str(video)])
            controller._batch_add_paths.reset_mock()
            self.assertEqual(bridge.handleDroppedUrls([QUrl.fromLocalFile(str(folder/'note.txt'))], 3), 3)
            controller._batch_add_paths.assert_not_called()








if __name__ == "__main__":
    unittest.main()
