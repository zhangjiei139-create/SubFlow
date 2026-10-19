from __future__ import annotations

import threading
import time
from pathlib import Path

from PySide6.QtCore import QItemSelectionModel, QObject, Property, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication,QDialog

import batch_core
import manual_review_state
import online_subtitles
import subdl_subtitles
import system_resources
from qt_online_dialog import PROVIDERS


class BackendBridge(QObject):
    toolStateChanged = Signal()
    batchItemsChanged = Signal()
    batchActionAvailabilityResolved = Signal(object)
    hardwareChanged = Signal()
    hardwareResolved = Signal(object)
    subtitleServiceChanged = Signal()
    subtitleServiceValidationResolved = Signal(str, bool, str, str, int)

    def __init__(self) -> None:
        super().__init__()
        service_settings = online_subtitles.load_settings()
        self._service_keys = {
            "opensubtitles": str(service_settings.get("opensubtitles_api_key", "")),
            "subdl": str(service_settings.get("subdl_api_key", "")),
        }
        self._service_status = {
            code: ("已配置" if key else "未配置")
            for code, key in self._service_keys.items()
        }
        self._service_validation_versions = {code: 0 for code in self._service_keys}
        self._tool_controller = None
        self._batch_items = None
        self._batch_availability = {}
        self._batch_probe_running = False
        self._batch_probe_next_at = 0.0
        self._batch_closed = False
        self._hardware_profile: system_resources.HardwareProfile | None = None

        self.toolStateChanged.connect(self._sync_batch_items)
        self.batchActionAvailabilityResolved.connect(self._finish_batch_availability)
        self.subtitleServiceValidationResolved.connect(self._finish_service_validation)
        self.hardwareResolved.connect(self._apply_hardware_profile)
        self._tool_sync_timer = QTimer(self)
        self._tool_sync_timer.setInterval(400)
        self._tool_sync_timer.timeout.connect(self._sync_tool_state)
        self._tool_sync_timer.start()
        threading.Thread(target=self._detect_hardware, daemon=True).start()

    def _sync_tool_state(self):
        # The native translucent listening window owns the interaction. Do not
        # rebuild the QML movie delegates behind it every 400ms; resume on the
        # first timer tick after closing. Remux dialogs still receive live state.
        application=QApplication.instance()
        modal=application.activeModalWidget() if application is not None else None
        while modal is not None:
            if modal.metaObject().className()=='ManualSubtitleReviewDialog': return
            modal=modal.parentWidget()
        self.toolStateChanged.emit()

    @Property(str, notify=hardwareChanged)
    def performanceSummary(self) -> str:
        profile = self._hardware_profile
        if profile is None:
            return "正在检测硬件…"
        return f"{profile.work_mode_label}工作模式"

    def _detect_hardware(self) -> None:
        try:
            profile = system_resources.detect_hardware()
        except Exception:
            profile = None
        self.hardwareResolved.emit(profile)

    @Slot(object)
    def _apply_hardware_profile(self, profile: object) -> None:
        self._hardware_profile = profile if isinstance(profile, system_resources.HardwareProfile) else None
        if self._tool_controller is not None and self._hardware_profile is not None:
            self._tool_controller.batch_hardware = self._hardware_profile
            self._tool_controller.batch_mode.setCurrentText(self._hardware_profile.recommended_mode)
        self.hardwareChanged.emit()

    @Slot(int)
    def activateToolPage(self, index: int) -> None:
        if index not in (0, 1, 2, 3):
            return
        if index == 2:
            self.subtitleServiceChanged.emit()
            return
        controller = self._ensure_tool_controller()
        controller_index = 2 if index == 3 else index
        controller.tabs.setCurrentIndex(controller_index)
        controller.nav_buttons[controller_index].setChecked(True)
        self.toolStateChanged.emit()

    def _ensure_tool_controller(self):
        if self._tool_controller is None:
            from qt_app import ProMaxQt, STYLE

            application = QApplication.instance()
            if application is not None:
                application.setStyle("Fusion")
                application.setStyleSheet(STYLE)
            self._tool_controller = ProMaxQt()
        return self._tool_controller

    @Property("QVariantList", notify=batchItemsChanged)
    def batchItems(self) -> list[dict]:
        # QML can read this several times for one signal (selection, menus,
        # model). Reading the model must never touch a movie/NAS filesystem.
        if self._batch_items is None:
            self._refresh_batch_items(notify=False)
        return self._batch_items

    def _sync_batch_items(self) -> None:
        # Ignore notifications until the controller's batch view is ready.
        if self._tool_controller is None or all(hasattr(self._tool_controller, name) for name in (
            "batch_paths", "batch_plans", "batch_item_cancel_events",
            "_batch_task_label", "_batch_row_color",
        )):
            self._refresh_batch_items()

    def _refresh_batch_items(self, notify: bool = True) -> None:
        controller = self._tool_controller
        if controller is None:
            result = []
            if result != self._batch_items:
                self._batch_items = result
                if notify:
                    self.batchItemsChanged.emit()
            return
        result = []
        requests = set()
        for path in controller.batch_paths:
            plan = controller.batch_plans[path]
            editable = manual_review_state.subtitle_editable(controller, plan)
            source = str(getattr(plan, "external_subtitle", "") or "")
            output = str(getattr(plan, "output_path", "") or "")
            source_key = ("subtitle", source)
            output_key = ("translation", output)
            if editable and source:
                requests.add(source_key)
            translation_editable = (not controller.batch_running
                                    and plan.task_state != "processing"
                                    and plan.status != "processing")
            if translation_editable and output:
                requests.add(output_key)
            available = (editable and bool(source)
                         and self._batch_availability.get(source_key, False)
                         and getattr(plan, "status_label", "") != "需换字幕")
            pending = bool(available and getattr(plan, "manual_review_required", False))
            active = bool(available and not pending)
            result.append(
                {
                    "name": Path(path).name,
                    "path": path,
                    "audio": plan.main_audio or "待分析",
                    "audioDetail": plan.audio_tracks or plan.main_audio or "待分析",
                    "codecs": plan.audio_codecs or "待分析",
                    "subtitles": plan.subtitles or "待分析",
                    "profile": f"偏好 {plan.profile_slot}",
                    "profileSlot": plan.profile_slot,
                    "summary": plan.summary or "待分析",
                    "detail": plan.detail or "",
                    "detailText": batch_core.movie_detail_text(plan, controller._batch_task_label(plan)),
                    "status": controller._batch_task_label(plan),
                    "state": plan.status,
                    "burned": bool(plan.status == "burned" or plan.burned_subtitle),
                    "processing": plan.task_state == "processing" or plan.status == "processing",
                    "rowColor": controller._batch_row_color(plan).name(),
                    "subtitleEditable": editable,
                    "canPendingReview": pending,
                    "canActiveReview": active,
                    "canListenSubtitle": pending or active,
                    "canAlignSubtitle": pending or active,
                    "canReviewTranslation": bool(translation_editable
                        and self._batch_availability.get(output_key, False)),
                    "manualReviewRequired": bool(getattr(plan, "manual_review_required", False)),
                    "stoppable": (
                        path in controller.batch_item_cancel_events
                        and plan.task_state in {"waiting", "processing"}
                        and not controller.batch_item_cancel_events[path].is_set()
                    ),
                    "taskState": plan.task_state,
                    "audioWarning": plan.audio_passthrough_warning,
                    "completeEnglishText": plan.has_complete_english_text,
                    "imageSubtitles": plan.has_image_subtitles,
                }
            )
        self._queue_batch_availability(requests)
        if result != self._batch_items:
            self._batch_items = result
            if notify:
                self.batchItemsChanged.emit()

    def _queue_batch_availability(self, requests: set) -> None:
        if self._batch_closed or self._batch_probe_running or not requests:
            return
        if (time.monotonic() < self._batch_probe_next_at
                and all(key in self._batch_availability for key in requests)):
            return
        self._batch_probe_running = True

        def probe():
            import translation_remux
            results = {}
            for kind, path in sorted(requests):
                if self._batch_closed:
                    return
                try:
                    results[(kind, path)] = (Path(path).is_file() if kind == "subtitle"
                                            else translation_remux.available(path))
                except (OSError, ValueError):
                    results[(kind, path)] = False
            try:
                self.batchActionAvailabilityResolved.emit(results)
            except RuntimeError:
                # A slow/disconnected share may return after the bridge closes.
                pass

        threading.Thread(target=probe, daemon=True).start()

    @Slot(object)
    def _finish_batch_availability(self, results: dict) -> None:
        self._batch_probe_running = False
        if self._batch_closed:
            return
        self._batch_availability.update(results)
        self._batch_probe_next_at = time.monotonic() + 5.0
        self._refresh_batch_items()

    @Property(str, notify=toolStateChanged)
    def batchStatus(self) -> str:
        if self._tool_controller is None:
            return "添加影片后点击“分析全部”。"
        return self._tool_controller.batch_progress.text()

    @Property(int, notify=toolStateChanged)
    def batchProgress(self) -> int:
        return self._tool_controller.batch_progress.value() if self._tool_controller is not None else 0

    def _mux_state(self):
        return getattr(self._tool_controller,'mux_progress',None)

    @Property(bool, notify=toolStateChanged)
    def muxVisible(self):
        state=self._mux_state()
        return bool(state and state.visible)

    @Property(int, notify=toolStateChanged)
    def muxProgress(self):
        state=self._mux_state()
        return state.value if state else 0

    @Property(bool, notify=toolStateChanged)
    def muxBusy(self):
        state=self._mux_state()
        return bool(state and state.busy)

    @Property(str, notify=toolStateChanged)
    def muxStatus(self):
        state=self._mux_state()
        return state.label if state else ''

    @Property(bool, notify=toolStateChanged)
    def batchRunning(self) -> bool:
        return bool(self._tool_controller is not None and self._tool_controller.batch_running)

    @Property(str, notify=toolStateChanged)
    def batchOutputFormat(self) -> str:
        return getattr(self._tool_controller, "batch_output_format", "MKV")

    @Property(bool, notify=toolStateChanged)
    def batchOutputFormatLocked(self) -> bool:
        return bool(self._tool_controller is not None
                    and not self._tool_controller._batch_output_format_editable())

    @Slot(str)
    def batchSetOutputFormat(self, value: str) -> None:
        self._ensure_tool_controller()._batch_set_output_format(value)
        self.toolStateChanged.emit()

    @Property(str, notify=toolStateChanged)
    def batchLogText(self) -> str:
        return self._tool_controller.batch_log.toPlainText() if self._tool_controller is not None else ""

    @Property(int, notify=toolStateChanged)
    def profileSlot(self) -> int:
        return self._tool_controller.profile_slot.currentIndex() + 1 if self._tool_controller is not None else 1

    @Property(str, notify=toolStateChanged)
    def profileName(self) -> str:
        return self._tool_controller.profile_name.text() if self._tool_controller is not None else ""

    @Property(str, notify=toolStateChanged)
    def profileAudioPolicy(self) -> str:
        if self._tool_controller is None:
            return "universal"
        for code, button in self._tool_controller.profile_audio_policy.items():
            if button.isChecked():
                return code
        return "universal"

    @Property("QVariantList", notify=toolStateChanged)
    def profileSubtitleOptions(self) -> list[dict]:
        if self._tool_controller is None:
            return []
        return [
            {"code": code, "label": widget.text(), "checked": widget.isChecked()}
            for code, widget in self._tool_controller.profile_subtitles.items()
        ]

    @Property(bool, notify=toolStateChanged)
    def profileReplaceDownloadedSubtitle(self) -> bool:
        return bool(
            self._tool_controller
            and self._tool_controller.profile_replace_downloaded_subtitle.isChecked()
        )

    @Property(bool, notify=toolStateChanged)
    def profileChineseScriptEquivalent(self) -> bool:
        return bool(
            self._tool_controller
            and self._tool_controller.profile_chinese_script_equivalent.isChecked()
        )

    @Property(str, notify=toolStateChanged)
    def profileFeedback(self) -> str:
        return self._tool_controller.profile_feedback.text() if self._tool_controller is not None else ""

    @Property(bool, notify=toolStateChanged)
    def profileEditingLocked(self) -> bool:
        controller = self._tool_controller
        return bool(
            controller is not None
            and (
                getattr(controller, "batch_running", False)
                or any(
                    getattr(plan, "status", "pending") != "pending"
                    for plan in getattr(controller, "batch_plans", {}).values()
                )
            )
        )

    @Property(str, notify=toolStateChanged)
    def hardwareRequirements(self) -> str:
        import system_resources

        return system_resources.hardware_requirements_text()

    @Property(str, notify=toolStateChanged)
    def hardwareStatus(self) -> str:
        if self._hardware_profile is None:
            return "正在检测本机硬件…"
        return system_resources.hardware_status_text(self._hardware_profile)

    @Property(str, notify=toolStateChanged)
    def hardwareDetail(self) -> str:
        return system_resources.hardware_detail_text(self._hardware_profile) if self._hardware_profile is not None else ""

    @Property(str, notify=toolStateChanged)
    def hardwareReason(self) -> str:
        return system_resources.hardware_reason_text(self._hardware_profile) if self._hardware_profile is not None else ""

    @Slot()
    def batchAddFiles(self) -> None:
        self._ensure_tool_controller()._batch_add_files()
        self.toolStateChanged.emit()

    @Slot()
    def batchAddFolder(self) -> None:
        self._ensure_tool_controller()._batch_add_folder()
        self.toolStateChanged.emit()

    @Slot(int)
    def batchRemove(self, row: int) -> None:
        controller = self._ensure_tool_controller()
        if 0 <= row < controller.batch_table.rowCount():
            controller.batch_table.selectRow(row)
            controller._batch_remove()
        self.toolStateChanged.emit()

    @Slot("QVariantList", result=bool)
    def batchRemoveRows(self, rows) -> bool:
        controller = self._ensure_tool_controller()
        valid = sorted({int(row) for row in rows if 0 <= int(row) < len(controller.batch_paths)})
        if not valid or controller.batch_running:
            return False
        plans = getattr(controller, "batch_plans", {})
        if any(
            (plan := plans.get(controller.batch_paths[row])) is not None
            and (plan.task_state == "processing" or plan.status == "processing")
            for row in valid
        ):
            return False
        selection = controller.batch_table.selectionModel()
        selection.clearSelection()
        for row in valid:
            selection.select(
                controller.batch_table.model().index(row, 0),
                QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
            )
        controller._batch_remove()
        self.toolStateChanged.emit()
        return True

    @Slot()
    def batchClear(self) -> None:
        self._ensure_tool_controller()._batch_clear()
        self.toolStateChanged.emit()

    @Slot()
    def batchAnalyze(self) -> None:
        self._ensure_tool_controller()._batch_analyze()
        self.toolStateChanged.emit()

    @Slot()
    def batchStart(self) -> None:
        controller = self._ensure_tool_controller()
        if self._hardware_profile is not None:
            controller.batch_hardware = self._hardware_profile
            controller.batch_mode.setCurrentText(self._hardware_profile.recommended_mode)
        controller._batch_start()
        self.toolStateChanged.emit()

    @Slot()
    def batchStop(self) -> None:
        self._ensure_tool_controller()._batch_stop()
        self.toolStateChanged.emit()

    @Slot(int)
    def batchStartOne(self, row: int) -> None:
        controller = self._ensure_tool_controller()
        if 0 <= row < len(controller.batch_paths):
            if self._hardware_profile is not None:
                controller.batch_hardware = self._hardware_profile
                controller.batch_mode.setCurrentText(self._hardware_profile.recommended_mode)
            controller._batch_start_one(controller.batch_paths[row])
        self.toolStateChanged.emit()

    @Slot("QVariantList")
    def batchStartRows(self, rows) -> None:
        controller = self._ensure_tool_controller()
        selected_paths = [
            controller.batch_paths[row]
            for row in sorted({int(row) for row in rows if 0 <= int(row) < len(controller.batch_paths)})
        ]
        paths = [
            path for path in selected_paths
            if (plan := controller.batch_plans[path]).status in {"ready", "review"}
            and not plan.burned_subtitle
            and plan.task_state not in {"processing", "completed"}
        ]
        if paths and not controller.batch_running:
            if self._hardware_profile is not None:
                controller.batch_hardware = self._hardware_profile
                controller.batch_mode.setCurrentText(self._hardware_profile.recommended_mode)
            controller._batch_log_line(f'处理启动请求：右键“处理”；{len(paths)} 部；{time.strftime("%H:%M:%S")}。')
            controller._batch_start_paths(paths)
        self.toolStateChanged.emit()

    @Slot("QVariantList")
    def batchStopRows(self, rows) -> None:
        controller = self._ensure_tool_controller()
        for row in sorted({int(row) for row in rows if 0 <= int(row) < len(controller.batch_paths)}):
            path = controller.batch_paths[row]
            if path in controller.batch_item_cancel_events:
                controller._batch_stop_one(path)
        self.toolStateChanged.emit()

    @Slot(int)
    def batchStopOne(self, row: int) -> None:
        controller = self._ensure_tool_controller()
        if 0 <= row < len(controller.batch_paths):
            controller._batch_stop_one(controller.batch_paths[row])
        self.toolStateChanged.emit()

    @Slot(int)
    def batchManualSubtitles(self, row: int) -> None:
        self._defer_movie_dialog(row, '_open_batch_online_search')

    @Slot(int)
    def batchAlignSubtitle(self, row: int) -> None:
        self._defer_movie_dialog(row, '_force_batch_manual_review')

    @Slot(int)
    def batchPendingReview(self, row: int) -> None:
        self._defer_movie_dialog(row, '_open_batch_pending_review')

    @Slot(int)
    def batchListenCurrentSubtitle(self, row: int) -> None:
        self._defer_movie_dialog(row, '_listen_current_subtitle')

    @Slot(int)
    def batchReviewTranslation(self, row: int) -> None:
        self._defer_movie_dialog(row, '_batch_edit_translation')

    def _defer_movie_dialog(self, row: int, method: str) -> None:
        controller = self._ensure_tool_controller()
        if 0 <= row < len(controller.batch_paths):
            path = controller.batch_paths[row]
            plan = controller.batch_plans.get(path)

            def open_dialog():
                # A modal QWidget runs a nested event loop. Calling it inside
                # MenuItem.onTriggered lets the 400ms model refresh destroy a
                # delegate that is still executing JavaScript, which is fatal
                # in Qt. Also capture the movie, not its transient row index.
                if (self._tool_controller is not controller or plan is None
                        or path not in controller.batch_paths
                        or controller.batch_plans.get(path) is not plan):
                    return
                try:
                    getattr(controller, method)(path)
                finally:
                    self.toolStateChanged.emit()

            QTimer.singleShot(0, self, open_dialog)

    @Slot(QUrl)
    def reviewCompletedMovie(self, url: QUrl) -> None:
        if not url.isLocalFile(): return
        output=url.toLocalFile()
        controller=self._ensure_tool_controller()
        def open_dialog():
            if self._tool_controller is not controller: return
            try:
                controller._edit_translation_output(output)
            finally:
                self.toolStateChanged.emit()
        QTimer.singleShot(0,self,open_dialog)

    @Slot(int)
    def batchImportSubtitle(self, row: int) -> None:
        controller = self._ensure_tool_controller()
        if 0 <= row < len(controller.batch_paths):
            controller._import_batch_subtitle(controller.batch_paths[row])
        self.toolStateChanged.emit()

    @Slot(int, result=str)
    def batchMovieFolderUrl(self, row: int) -> str:
        controller = self._ensure_tool_controller()
        if 0 <= row < len(controller.batch_paths):
            return QUrl.fromLocalFile(str(Path(controller.batch_paths[row]).parent)).toString()
        return ""

    @Slot(int, QUrl)
    def batchImportSubtitleFile(self, row: int, subtitle_url: QUrl) -> None:
        controller = self._ensure_tool_controller()
        if 0 <= row < len(controller.batch_paths) and subtitle_url.isLocalFile():
            controller._import_batch_subtitle(
                controller.batch_paths[row], subtitle_url.toLocalFile()
            )
        self.toolStateChanged.emit()

    @Slot(int)
    def batchSmartSubtitles(self, row: int) -> None:
        controller = self._ensure_tool_controller()
        if 0 <= row < len(controller.batch_paths):
            controller._start_batch_smart_subtitle(controller.batch_paths[row])
        self.toolStateChanged.emit()

    @Slot(int)
    def batchPlayVideo(self, row: int) -> None:
        controller = self._ensure_tool_controller()
        if 0 <= row < len(controller.batch_paths):
            QDesktopServices.openUrl(QUrl.fromLocalFile(controller.batch_paths[row]))

    @Slot(int)
    def batchSearchSubtitles(self, row: int) -> None:
        self.batchManualSubtitles(row)

    @Slot(int, int)
    def batchSetProfile(self, row: int, slot: int) -> None:
        controller = self._ensure_tool_controller()
        if 0 <= row < len(controller.batch_paths) and 1 <= slot <= 4:
            controller._batch_profile_changed(controller.batch_paths[row], slot)
        self.toolStateChanged.emit()

    @Slot("QVariantList", int, result=int)
    def handleDroppedUrls(self, urls, current_page: int) -> int:
        paths: list[str] = []
        for value in urls:
            url = value if isinstance(value, QUrl) else QUrl(str(value))
            path = url.toLocalFile() if url.isLocalFile() else ""
            if path:
                paths.append(path)
        if not paths:
            return current_page

        video_extensions = {".mkv", ".mp4", ".mov", ".avi", ".m4v"}
        expanded: list[str] = []
        for path in paths:
            candidate = Path(path)
            if candidate.is_dir():
                expanded.extend(batch_core.videos_in_folder(path))
            elif candidate.suffix.lower() in video_extensions and not batch_core.is_output_path(candidate):
                expanded.append(path)
        expanded = list(dict.fromkeys(expanded))
        if not expanded:
            return current_page

        controller = self._ensure_tool_controller()
        controller._batch_add_paths(expanded)
        self.toolStateChanged.emit()
        return 0

    @Property(str, notify=subtitleServiceChanged)
    def openSubtitlesKey(self) -> str:
        return self._service_keys.get("opensubtitles", "")

    @Property(str, notify=subtitleServiceChanged)
    def subdlKey(self) -> str:
        return self._service_keys.get("subdl", "")

    @Property(str, notify=subtitleServiceChanged)
    def openSubtitlesStatus(self) -> str:
        return self._service_status.get("opensubtitles", "未配置")

    @Property(str, notify=subtitleServiceChanged)
    def subdlStatus(self) -> str:
        return self._service_status.get("subdl", "未配置")

    @Slot(str)
    def openSubtitleKeyPage(self, provider: str) -> None:
        info = PROVIDERS.get(provider)
        if info:
            QDesktopServices.openUrl(QUrl(str(info["url"])))

    @Slot(str, str)
    def saveSubtitleService(self, provider: str, key: str) -> None:
        value = key.strip()
        if provider not in PROVIDERS:
            return
        request_id = self._service_validation_versions[provider] + 1
        self._service_validation_versions[provider] = request_id
        if not value:
            self._service_status[provider] = "请先填写 API Key"
            self.subtitleServiceChanged.emit()
            return
        self._service_keys[provider] = value
        self._service_status[provider] = "正在验证…"
        self.subtitleServiceChanged.emit()

        def validate() -> None:
            try:
                quota = (online_subtitles.validate_api_key(value)
                         if provider == "opensubtitles"
                         else subdl_subtitles.validate_token(value))
            except Exception as exc:
                message = str(exc).replace(value, "[已隐藏]")
                self.subtitleServiceValidationResolved.emit(provider, False, message, value, request_id)
            else:
                suffix = f"；可用额度 {quota}" if quota is not None else ""
                self.subtitleServiceValidationResolved.emit(provider, True, f"已验证并保存{suffix}", value, request_id)

        threading.Thread(target=validate, daemon=True).start()

    @Slot(str, bool, str, str, int)
    def _finish_service_validation(self, provider: str, ok: bool, message: str, key: str, request_id: int) -> None:
        if request_id != self._service_validation_versions.get(provider):
            return
        if ok:
            try:
                PROVIDERS[provider]["service"].save_settings(key)
            except Exception as exc:
                self._service_status[provider] = f"保存失败：{str(exc).replace(key, '[已隐藏]')}"
                self.subtitleServiceChanged.emit()
                return
        self._service_status[provider] = message if ok else f"验证失败：{message}"
        self.subtitleServiceChanged.emit()

    @Slot(int)
    def profileSelect(self, slot: int) -> None:
        if self.profileEditingLocked:
            self.toolStateChanged.emit()
            return
        controller = self._ensure_tool_controller()
        index = max(0, min(3, slot - 1))
        controller.profile_slot.setCurrentIndex(index)
        controller.batch_default.setCurrentIndex(index)
        if not getattr(controller, "batch_running", False):
            for plan in controller.batch_plans.values():
                if getattr(plan, "status", "pending") == "pending":
                    plan.profile_slot = index + 1
        self.toolStateChanged.emit()

    @Slot()
    def profileEditFonts(self):
        if self.profileEditingLocked: return
        controller=self._ensure_tool_controller()
        slot=controller.profile_slot.currentIndex()
        def open_dialog():
            if self.profileEditingLocked or controller.profile_slot.currentIndex()!=slot: return
            from subtitle_font_dialog import SubtitleFontDialog
            from profile_model import save_profiles
            dialog=SubtitleFontDialog(controller.profiles[slot],controller)
            try:
                if dialog.exec()==QDialog.DialogCode.Accepted and not self.profileEditingLocked:
                    controller.profiles[slot].subtitle_styles=dialog.result_styles
                    save_profiles(controller.profiles)
            finally:
                dialog.deleteLater()
            self.toolStateChanged.emit()
        QTimer.singleShot(0,self,open_dialog)

    @Slot(str)
    def profileSetName(self, value: str) -> None:
        if self.profileEditingLocked:
            self.toolStateChanged.emit()
            return
        controller = self._ensure_tool_controller()
        if controller.profile_name.text() != value:
            controller.profile_name.setText(value)
            controller._save_profile()
        self.toolStateChanged.emit()

    @Slot(str)
    def profileSetAudioPolicy(self, policy: str) -> None:
        if self.profileEditingLocked:
            self.toolStateChanged.emit()
            return
        widget = self._ensure_tool_controller().profile_audio_policy.get(policy)
        if widget is not None:
            widget.setChecked(True)
        self.toolStateChanged.emit()

    @Slot(str, bool)
    def profileToggleSubtitle(self, code: str, checked: bool) -> None:
        if self.profileEditingLocked:
            self.toolStateChanged.emit()
            return
        widget = self._ensure_tool_controller().profile_subtitles.get(code)
        if widget is not None:
            widget.setChecked(checked)
        self.toolStateChanged.emit()

    @Slot(str, bool)
    def profileSetFlag(self, name: str, checked: bool) -> None:
        if self.profileEditingLocked:
            self.toolStateChanged.emit()
            return
        controller = self._ensure_tool_controller()
        targets = {
            "replaceDownloaded": controller.profile_replace_downloaded_subtitle,
            "chineseScriptEquivalent": controller.profile_chinese_script_equivalent,
        }
        widget = targets.get(name)
        if widget is not None and widget.isChecked() != checked:
            widget.setChecked(checked)
            # The underlying checkbox persists changes through its toggled signal.
        self.toolStateChanged.emit()

    @Slot()
    def hardwareRefresh(self) -> None:
        self._hardware_profile = None
        self.hardwareChanged.emit()
        self.toolStateChanged.emit()
        threading.Thread(target=self._detect_hardware, daemon=True).start()

    @Slot()
    def shutdown(self) -> None:
        """Stop polling; slow probes may finish without updating the closed UI."""
        self._batch_closed = True
        self._tool_sync_timer.stop()
