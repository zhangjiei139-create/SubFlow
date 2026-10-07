# -*- coding: utf-8 -*-
from __future__ import annotations

import concurrent.futures
import json
import hashlib
import ctypes
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

# The frozen executable is also used as the isolated PGS/VobSub OCR worker.
# Dispatch before importing Qt so worker launches cannot accidentally create a
# second GUI and leave the parent task waiting forever.
if "--pgs-ocr-worker" in sys.argv:
    import subtitle_tool_core as worker_core

    raise SystemExit(worker_core.pgs_ocr_worker_main(sys.argv[1:]))

from PySide6.QtCore import QEvent, QItemSelection, QItemSelectionModel, QObject, QPoint, QSize, Qt, QThreadPool, QUrl, Signal
from PySide6.QtGui import QAction, QColor, QDesktopServices, QDragEnterEvent, QDropEvent, QIcon, QPalette, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QAbstractButton,
    QAbstractSpinBox,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QFileDialog,
    QDialog,
    QFrame,
    QGraphicsDropShadowEffect,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStackedWidget,
    QStyle,
    QStyleOptionViewItem,
    QStyledItemDelegate,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

import batch_core
import burned_subtitle_detector
import license_manager
import manual_review_prepare
from manual_review_dialog import ManualSubtitleReviewDialog
import pro_core
import smart_subtitles
import subtitle_tool_core as core
import system_resources
from profile_model import (
    AUDIO_FORMATS,
    LANGUAGES,
    BatchPlan,
    PreferenceProfile,
    load_profiles,
    preset_compatible,
    preset_lazy,
    save_profiles,
)
from qt_online_dialog import OnlineSubtitleDialog
from qt_workers import FunctionWorker, WorkerSignals


VIDEO_EXTENSIONS = {".mkv", ".mp4", ".mov", ".avi", ".m4v"}
SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".vtt"}

class _CombinedCancelEvent:
    def __init__(self, global_event: threading.Event, item_event: threading.Event) -> None:
        self.global_event = global_event
        self.item_event = item_event

    def is_set(self) -> bool:
        return self.global_event.is_set() or self.item_event.is_set()


def resource_path(*parts: str) -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)).joinpath(*parts)


def card(title: str) -> tuple[QGroupBox, QVBoxLayout]:
    box = QGroupBox(title)
    box.setObjectName("contentCard")
    shadow = QGraphicsDropShadowEffect(box)
    shadow.setBlurRadius(24)
    shadow.setOffset(0, 6)
    shadow.setColor(QColor(25, 63, 59, 32))
    box.setGraphicsEffect(shadow)
    layout = QVBoxLayout(box)
    layout.setContentsMargins(12, 15, 12, 12)
    layout.setSpacing(8)
    return box, layout


class TitleBar(QFrame):
    def __init__(self, window: QMainWindow, version: str) -> None:
        super().__init__(window)
        self.window = window
        self.drag_origin: QPoint | None = None
        self.setObjectName("titleBar")
        self.setFixedHeight(66)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(22, 10, 14, 10)
        layout.setSpacing(10)
        mark = QLabel("▶")
        mark.setObjectName("brandMark")
        layout.addWidget(mark)
        brand = QLabel(f"SubFlow  <span>v{version}</span>")
        brand.setObjectName("brandName")
        layout.addWidget(brand)
        layout.addStretch(1)
        for text, action, name in (("—", self.window.showMinimized, "windowControl"), ("□", self._toggle_maximize, "windowControl"), ("×", self.window.close, "windowClose")):
            button = QToolButton()
            button.setText(text)
            button.setObjectName(name)
            button.setFixedSize(36, 32)
            button.clicked.connect(action)
            layout.addWidget(button)

    def _toggle_maximize(self) -> None:
        if self.window.isMaximized():
            self.window.showNormal()
        else:
            self.window.showMaximized()

    def mousePressEvent(self, event) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.drag_origin = event.globalPosition().toPoint() - self.window.frameGeometry().topLeft()
            event.accept()

    def mouseMoveEvent(self, event) -> None:
        if self.drag_origin and event.buttons() & Qt.MouseButton.LeftButton and not self.window.isMaximized():
            self.window.move(event.globalPosition().toPoint() - self.drag_origin)
            event.accept()

    def mouseReleaseEvent(self, event) -> None:
        self.drag_origin = None
        super().mouseReleaseEvent(event)




class BatchSelectionFrameDelegate(QStyledItemDelegate):
    """Keep each row's status color visible while showing its selected outline."""

    def paint(self, painter, option, index) -> None:
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        if not selected:
            super().paint(painter, option, index)
            return

        clean_option = QStyleOptionViewItem(option)
        clean_option.state &= ~QStyle.StateFlag.State_Selected
        super().paint(painter, clean_option, index)

        painter.save()
        painter.fillRect(option.rect, QColor(0, 151, 140, 48))
        painter.setPen(QColor("#487b76"))
        rect = option.rect
        painter.drawLine(rect.topLeft(), rect.topRight())
        painter.drawLine(rect.bottomLeft(), rect.bottomRight())
        if index.column() == 0:
            painter.drawLine(rect.topLeft(), rect.bottomLeft())
        if index.column() == index.model().columnCount() - 1:
            painter.drawLine(rect.topRight(), rect.bottomRight())
        painter.restore()




class ArrowSpinBox(QWidget):
    """Compact numeric input with explicit, always-visible arrow controls."""

    def __init__(self) -> None:
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.spin = QSpinBox()
        self.spin.setButtonSymbols(QAbstractSpinBox.ButtonSymbols.NoButtons)
        self.spin.setMinimumWidth(54)
        arrows = QWidget()
        arrow_layout = QVBoxLayout(arrows)
        arrow_layout.setContentsMargins(0, 0, 0, 0)
        arrow_layout.setSpacing(0)
        up = QToolButton()
        up.setObjectName("spinArrowButton")
        up.setText("▲")
        up.setToolTip("增加")
        up.clicked.connect(self.spin.stepUp)
        down = QToolButton()
        down.setObjectName("spinArrowButton")
        down.setText("▼")
        down.setToolTip("减少")
        down.clicked.connect(self.spin.stepDown)
        arrow_layout.addWidget(up)
        arrow_layout.addWidget(down)
        layout.addWidget(self.spin)
        layout.addWidget(arrows)

    def setRange(self, minimum: int, maximum: int) -> None:
        self.spin.setRange(minimum, maximum)

    def setValue(self, value: int) -> None:
        self.spin.setValue(value)

    def value(self) -> int:
        return self.spin.value()


def empty_text_for(value: str) -> str:
    return value or "未发现轨道"


class ProMaxQt(QMainWindow):
    def eventFilter(self, obj, event) -> bool:
        if (
            hasattr(self, "batch_table")
            and obj is self.batch_table.viewport()
            and event.type() == QEvent.Type.MouseButtonPress
            and event.button() == Qt.MouseButton.RightButton
        ):
            self._context_selection_paths = tuple(
                self.batch_table.item(index.row(), 0).data(Qt.ItemDataRole.UserRole)
                for index in self.batch_table.selectionModel().selectedRows()
                if self.batch_table.item(index.row(), 0) is not None
            )
        # A preference combo occupies its whole table cell.  Modifier-clicks
        # should select movie rows just as they do in every other column.
        if (
            isinstance(obj, QComboBox)
            and obj.property("batchRowPath")
            and event.type() == QEvent.Type.MouseButtonPress
            and event.button() == Qt.MouseButton.LeftButton
            and event.modifiers() & (
                Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.ShiftModifier
            )
        ):
            path = obj.property("batchRowPath")
            if path in self.batch_paths:
                row = self.batch_paths.index(path)
                model = self.batch_table.model()
                selection = self.batch_table.selectionModel()
                index = model.index(row, 0)
                if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                    anchor = self.batch_table.currentRow()
                    if anchor < 0:
                        anchor = row
                    selected_range = QItemSelection(
                        model.index(min(anchor, row), 0),
                        model.index(max(anchor, row), model.columnCount() - 1),
                    )
                    selection.select(
                        selected_range,
                        QItemSelectionModel.SelectionFlag.ClearAndSelect
                        | QItemSelectionModel.SelectionFlag.Rows,
                    )
                else:
                    selection.select(
                        index,
                        QItemSelectionModel.SelectionFlag.Toggle
                        | QItemSelectionModel.SelectionFlag.Rows,
                    )
                selection.setCurrentIndex(index, QItemSelectionModel.SelectionFlag.NoUpdate)
                event.accept()
                return True
        return super().eventFilter(obj, event)

    def __init__(self) -> None:
        super().__init__()
        self.version = license_manager.app_version(license_manager.load_config())
        self.setWindowTitle(f"SubFlow v{self.version}")
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Window)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        icon = resource_path("assets", "subtitle-track-tool-pro-icon.ico")
        if icon.exists():
            self.setWindowIcon(QIcon(str(icon)))
        self.resize(1380, 860)
        self.setMinimumSize(1080, 720)
        self.setAcceptDrops(True)
        self.pool = QThreadPool.globalInstance()
        self.pool.setMaxThreadCount(max(4, min(12, (os.cpu_count() or 4))))
        self._workers: set[FunctionWorker] = set()
        self.batch_cancel_event = threading.Event()
        self.batch_item_cancel_events: dict[str, threading.Event] = {}
        self.batch_running = False
        self.batch_paths: list[str] = []
        self.batch_plans: dict[str, BatchPlan] = {}
        self.batch_hardware: system_resources.HardwareProfile | None = None
        self.profiles = load_profiles()
        self._build_ui()
        self._detect_hardware()

    def _run_worker(
        self,
        function,
        *,
        result=None,
        error=None,
        message=None,
        progress=None,
        state=None,
        finished=None,
    ) -> None:
        worker = FunctionWorker(function)
        self._workers.add(worker)
        if result:
            worker.signals.result.connect(result)
        if error:
            worker.signals.error.connect(error)
        if message:
            worker.signals.message.connect(message)
        if progress:
            worker.signals.progress.connect(progress)
        if state:
            worker.signals.state.connect(state)

        def cleanup() -> None:
            self._workers.discard(worker)
            if finished:
                finished()

        worker.signals.finished.connect(cleanup)
        self.pool.start(worker)

    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("windowSurface")
        outer = QVBoxLayout(root)
        outer.setContentsMargins(1, 1, 1, 1)
        outer.setSpacing(0)
        outer.addWidget(TitleBar(self, self.version))

        body = QWidget()
        body.setObjectName("appBody")
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(28, 8, 28, 24)
        body_layout.setSpacing(14)
        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        nav_group = QButtonGroup(self)
        self.nav_buttons: list[QToolButton] = []
        nav_items = (
            ("影片处理", "batch.svg"),
            ("偏好", "preferences.svg"),
            ("硬件", "hardware.svg"),
        )
        for index, (text, icon_name) in enumerate(nav_items):
            button = QToolButton()
            button.setText(text)
            button.setIcon(QIcon(str(resource_path("assets", icon_name))))
            button.setIconSize(QSize(20, 20))
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            button.setObjectName("navButton")
            button.setCheckable(True)
            button.clicked.connect(lambda _checked, tab_index=index: self.tabs.setCurrentIndex(tab_index))
            nav_group.addButton(button, index)
            self.nav_buttons.append(button)
            header.addWidget(button)
        self.nav_buttons[0].setChecked(True)
        header.addStretch(1)
        self.header_hardware = QLabel("正在检测硬件…")
        self.header_hardware.setObjectName("hardwareBadge")
        header.addWidget(self.header_hardware)
        body_layout.addLayout(header)

        self.tabs = QTabWidget()
        self.tabs.tabBar().hide()
        self.batch_page = self._batch_page()
        self.profile_page = self._profile_page()
        self.hardware_page = self._hardware_page()
        self.tabs.addTab(self.batch_page, "影片处理")
        self.tabs.addTab(self.profile_page, "偏好设置")
        self.tabs.addTab(self.hardware_page, "硬件状态")
        self.tabs.currentChanged.connect(lambda index: self.nav_buttons[index].setChecked(True))
        body_layout.addWidget(self.tabs, 1)
        outer.addWidget(body, 1)
        self.setCentralWidget(root)


    def _batch_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(2, 4, 2, 2)
        toolbar = QHBoxLayout()
        add_files = QPushButton("添加影片")
        add_files.clicked.connect(self._batch_add_files)
        add_folder = QPushButton("添加文件夹")
        add_folder.clicked.connect(self._batch_add_folder)
        remove = QPushButton("移除所选")
        remove.clicked.connect(self._batch_remove)
        clear = QPushButton("清空")
        clear.clicked.connect(self._batch_clear)
        for button in (add_files, add_folder, remove, clear):
            toolbar.addWidget(button)
        toolbar.addSpacing(10)
        toolbar.addWidget(QLabel("本批默认"))
        self.batch_default = QComboBox()
        self.batch_default.addItems([f"偏好 {i}" for i in range(1, 5)])
        toolbar.addWidget(self.batch_default)
        toolbar.addWidget(QLabel("工作模式"))
        self.batch_mode = QComboBox()
        self.batch_mode.addItems(["稳定模式", "均衡模式", "高性能模式", "自定义模式"])
        self.batch_mode.currentTextChanged.connect(self._batch_mode_changed)
        toolbar.addWidget(self.batch_mode)
        self.batch_parallel = ArrowSpinBox()
        self.batch_parallel.setRange(1, 8)
        self.batch_parallel.setValue(2)
        self.batch_parallel.setVisible(False)
        toolbar.addWidget(self.batch_parallel)
        self.batch_recommendation = QLabel("正在检测硬件…")
        self.batch_recommendation.setObjectName("muted")
        toolbar.addWidget(self.batch_recommendation)
        toolbar.addStretch()
        analyze = QPushButton("分析全部")
        analyze.clicked.connect(self._batch_analyze)
        self.batch_stop = QPushButton("停止")
        self.batch_stop.setEnabled(False)
        self.batch_stop.clicked.connect(self._batch_stop)
        self.batch_start = QPushButton("开始处理")
        self.batch_start.setObjectName("primaryButton")
        self.batch_start.clicked.connect(self._batch_start)
        toolbar.addWidget(analyze)
        toolbar.addWidget(self.batch_stop)
        toolbar.addWidget(self.batch_start)
        layout.addLayout(toolbar)

        table_box, table_layout = card("影片与处理方案")
        self.batch_table = QTableWidget(0, 7)
        self.batch_table.setHorizontalHeaderLabels(["影片", "主音轨", "音频编码", "现有字幕", "套用偏好", "最终处理方案", "状态"])
        self.batch_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.batch_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.batch_table.setItemDelegate(BatchSelectionFrameDelegate(self.batch_table))
        self.batch_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.batch_table.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.batch_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.batch_table.viewport().installEventFilter(self)
        self.batch_table.customContextMenuRequested.connect(self._batch_context_menu)
        self.batch_table.verticalHeader().setVisible(False)
        header = self.batch_table.horizontalHeader()
        for column in range(7):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
        for column, width in enumerate((300, 180, 170, 280, 105, 280, 110)):
            self.batch_table.setColumnWidth(column, width)
        header.setMinimumSectionSize(80)
        self.batch_table.cellDoubleClicked.connect(self._batch_double_click)
        table_layout.addWidget(self.batch_table)
        layout.addWidget(table_box, 1)

        self.batch_progress = QProgressBar()
        self.batch_progress.setRange(0, 100)
        self.batch_progress.setFormat("%p% · 等待分析")
        layout.addWidget(self.batch_progress)
        self.batch_log = QPlainTextEdit()
        self.batch_log.setReadOnly(True)
        self.batch_log.setMaximumBlockCount(8000)
        self.batch_log.setMinimumHeight(135)
        self.batch_log.setObjectName("logView")
        layout.addWidget(self.batch_log)
        return page

    def _profile_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 8)
        top = QHBoxLayout()
        top.addWidget(QLabel("编辑偏好"))
        self.profile_slot = QComboBox()
        self.profile_slot.addItems([f"偏好 {i}" for i in range(1, 5)])
        self.profile_slot.currentIndexChanged.connect(self._load_profile_editor)
        # activated also fires when the user re-selects the current slot. This
        # restores the saved profile after previewing a one-click preset.
        self.profile_slot.activated.connect(self._load_profile_editor)
        top.addWidget(self.profile_slot)
        top.addStretch()
        layout.addLayout(top)

        split = QSplitter(Qt.Orientation.Horizontal)
        audio_box, audio_layout = card("音频偏好")
        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("偏好名称"))
        self.profile_name = QLineEdit()
        name_row.addWidget(self.profile_name, 1)
        audio_layout.addLayout(name_row)
        self.profile_audio_policy_group = QButtonGroup(self)
        self.profile_audio_policy = {}
        for index, (code, title, description) in enumerate([
            ("native", "原生音频", "不修改影片原有音轨，不删除、不转码、不新增。"),
            ("universal", "通用兼容", "保留高品质原音轨，并确保成品包含一条通用音轨。"),
            ("compact", "精简兼容", "成品只保留一条通用音轨，减少音轨数量和文件体积。"),
        ]):
            button = QRadioButton(f"{title}　{description}")
            self.profile_audio_policy_group.addButton(button, index)
            self.profile_audio_policy[code] = button
            audio_layout.addWidget(button)
        self.profile_compact_warning = QLabel(
            "该模式最终仅保留一条通用音轨，TrueHD Atmos、DTS-HD、DTS:X "
            "等高品质音轨将在输出验证成功后删除。"
        )
        self.profile_compact_warning.setWordWrap(True)
        self.profile_compact_warning.setStyleSheet(
            "background:#fff3d6;color:#8a5a00;border:1px solid #e5c06b;"
            "border-radius:8px;padding:9px;"
        )
        self.profile_audio_policy["compact"].toggled.connect(self.profile_compact_warning.setVisible)
        audio_layout.addWidget(self.profile_compact_warning)
        audio_layout.addStretch()

        subtitle_box, subtitle_layout = card("最终需要的字幕")
        subtitle_note = QLabel("勾选即表示最终文件需要该字幕；下载字幕的替换规则见下方选项。")
        subtitle_note.setObjectName("muted")
        subtitle_layout.addWidget(subtitle_note)
        sub_grid = QGridLayout()
        self.profile_subtitles = {}
        for index, (code, label) in enumerate(LANGUAGES):
            check = QCheckBox(label)
            self.profile_subtitles[code] = check
            sub_grid.addWidget(check, index // 3, index % 3)
        subtitle_layout.addLayout(sub_grid)
        self.profile_replace_downloaded_subtitle = QCheckBox("保留下载字幕并替换")
        subtitle_layout.addWidget(self.profile_replace_downloaded_subtitle)
        replacement_note = QLabel("勾选后，已核验的下载字幕写入成品，并替换同语言内嵌字幕。")
        replacement_note.setObjectName("muted")
        subtitle_layout.addWidget(replacement_note)
        self.profile_chinese_script_equivalent = QCheckBox("简繁同源")
        subtitle_layout.addWidget(self.profile_chinese_script_equivalent)
        script_note = QLabel("勾选后，简中或繁中任意一种完整字幕即可满足中文需求；默认严格区分。")
        script_note.setObjectName("muted")
        subtitle_layout.addWidget(script_note)
        subtitle_layout.addStretch()
        split.addWidget(audio_box)
        split.addWidget(subtitle_box)
        split.setStretchFactor(0, 1)
        split.setStretchFactor(1, 1)
        layout.addWidget(split, 1)
        self.profile_feedback = QLabel("")
        self.profile_feedback.setObjectName("muted")
        layout.addWidget(self.profile_feedback)
        self._load_profile_editor()
        self.profile_name.editingFinished.connect(self._auto_save_profile)
        for button in self.profile_audio_policy.values():
            button.toggled.connect(
                lambda checked: self._auto_save_profile() if checked else None
            )
        for check in self.profile_subtitles.values():
            check.toggled.connect(self._auto_save_profile)
        self.profile_replace_downloaded_subtitle.toggled.connect(self._auto_save_profile)
        self.profile_chinese_script_equivalent.toggled.connect(self._auto_save_profile)
        return page

    def _hardware_page(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(8, 8, 8, 8)
        split = QSplitter(Qt.Orientation.Horizontal)
        req_box, req_layout = card("软件配置建议")
        req = QLabel(system_resources.hardware_requirements_text())
        req.setWordWrap(True)
        req.setAlignment(Qt.AlignmentFlag.AlignTop)
        req_layout.addWidget(req)
        current_box, current_layout = card("本机检测结果")
        self.hardware_status = QLabel("正在检测本机硬件…")
        self.hardware_status.setObjectName("sectionTitle")
        self.hardware_status.setWordWrap(True)
        self.hardware_detail = QLabel("")
        self.hardware_detail.setWordWrap(True)
        self.hardware_reason = QLabel("")
        self.hardware_reason.setWordWrap(True)
        self.hardware_reason.setObjectName("muted")
        detect = QPushButton("重新检测")
        detect.clicked.connect(self._detect_hardware)
        current_layout.addWidget(self.hardware_status)
        current_layout.addWidget(self.hardware_detail)
        current_layout.addWidget(self.hardware_reason)
        current_layout.addStretch()
        current_layout.addWidget(detect, alignment=Qt.AlignmentFlag.AlignLeft)
        split.addWidget(req_box)
        split.addWidget(current_box)
        layout.addWidget(split, 1)
        return page


    def _qt_open_dialog(
        self,
        title: str,
        name_filter: str = "",
        multiple: bool = False,
        directory: bool = False,
        initial: str = "",
    ) -> list[str]:
        dialog = self._create_file_dialog(title, initial)
        if directory:
            dialog.setFileMode(QFileDialog.FileMode.Directory)
            dialog.setOption(QFileDialog.Option.ShowDirsOnly, True)
        else:
            mode = QFileDialog.FileMode.ExistingFiles if multiple else QFileDialog.FileMode.ExistingFile
            dialog.setFileMode(mode)
            if name_filter:
                dialog.setNameFilter(name_filter)
        return dialog.selectedFiles() if dialog.exec() else []


    def _create_file_dialog(self, title: str, initial: str = "") -> QFileDialog:
        # The visible product window is QML.  This QWidget controller stays
        # hidden, so parenting the picker to ``self`` creates an invisible
        # child dialog even though the QML button signal fires correctly.
        dialog = QFileDialog(None, title, initial)
        dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
        dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
        dialog.setViewMode(QFileDialog.ViewMode.Detail)
        dialog.resize(900, 580)
        dialog.setSidebarUrls(self._file_dialog_sidebar_urls())
        return dialog

    @staticmethod
    def _file_dialog_sidebar_urls() -> list[QUrl]:
        paths = [str(Path.home())]
        if os.name == "nt":
            drive_mask = ctypes.windll.kernel32.GetLogicalDrives()
            paths.extend(f"{chr(65 + index)}:/" for index in range(26) if drive_mask & (1 << index))
        else:
            paths.append("/")
        return [QUrl.fromLocalFile(path) for path in dict.fromkeys(paths)]




    @staticmethod






    @staticmethod





















    def _batch_add_files(self) -> None:
        paths = self._qt_open_dialog("添加批量影片", "视频文件 (*.mkv *.mp4 *.mov *.avi *.m4v)", multiple=True)
        self._batch_add_paths(paths)

    def _batch_add_folder(self) -> None:
        folders = self._qt_open_dialog("添加影片文件夹（包含所有下级文件夹）", directory=True)
        if folders:
            self._batch_add_paths(batch_core.videos_in_folder(folders[0]))

    def _batch_add_paths(self, paths) -> None:
        existing = {os.path.normcase(os.path.abspath(path)) for path in self.batch_paths}
        slot = self.batch_default.currentIndex() + 1
        added = 0
        for raw in paths:
            path = str(Path(raw))
            normalized = os.path.normcase(os.path.abspath(path))
            if Path(path).suffix.lower() not in VIDEO_EXTENSIONS or normalized in existing:
                continue
            if batch_core.is_output_path(path):
                continue
            plan = BatchPlan(path=path, profile_slot=slot, output_path=str(Path(path).with_suffix("")) + batch_core.OUTPUT_SUFFIX)
            self.batch_paths.append(path)
            self.batch_plans[path] = plan
            existing.add(normalized)
            added += 1
        self._render_batch_table()
        self._batch_log_line(f"已添加 {added} 部影片，共 {len(self.batch_paths)} 部。")

    def _batch_remove(self) -> None:
        rows = sorted({index.row() for index in self.batch_table.selectionModel().selectedRows()}, reverse=True)
        if not rows or self.batch_running:
            return
        if any(
            (plan := self.batch_plans.get(self.batch_paths[row])) is not None
            and (plan.task_state == "processing" or plan.status == "processing")
            for row in rows
        ):
            return
        next_row = min(rows)
        scroll_value = self.batch_table.verticalScrollBar().value()
        for row in rows:
            path = self.batch_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
            if path in self.batch_paths:
                self.batch_paths.remove(path)
                self.batch_plans.pop(path, None)
        self._render_batch_table()
        if self.batch_paths:
            next_row = min(next_row, len(self.batch_paths) - 1)
            self.batch_table.selectRow(next_row)
            self.batch_table.setCurrentCell(next_row, 0)
            self.batch_table.verticalScrollBar().setValue(scroll_value)

    def _batch_clear(self) -> None:
        if self.batch_running:
            return
        self.batch_paths.clear()
        self.batch_plans.clear()
        self.batch_table.setRowCount(0)
        self.batch_log.clear()
        self.batch_progress.setValue(0)
        self.batch_progress.setFormat("%p% · 等待分析")

    def _render_batch_table(self) -> None:
        selected_paths = {
            item.data(Qt.ItemDataRole.UserRole)
            for index in self.batch_table.selectionModel().selectedRows()
            if (item := self.batch_table.item(index.row(), 0)) is not None
        }
        current_item = self.batch_table.item(self.batch_table.currentRow(), 0)
        current_path = current_item.data(Qt.ItemDataRole.UserRole) if current_item else None
        scroll_value = self.batch_table.verticalScrollBar().value()
        self.batch_table.setRowCount(len(self.batch_paths))
        for row, path in enumerate(self.batch_paths):
            plan = self.batch_plans[path]
            status = self._batch_task_label(plan)
            values = [Path(path).name, plan.main_audio or "待分析", plan.audio_codecs or "待分析",
                      plan.subtitles or "待分析", f"偏好 {plan.profile_slot}", plan.summary or "待分析", status]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setData(Qt.ItemDataRole.UserRole, path)
                item.setToolTip(plan.detail or plan.summary)
                item.setBackground(self._batch_row_color(plan))
                item.setForeground(self._batch_row_foreground(plan))
                self.batch_table.setItem(row, column, item)
            combo = QComboBox()
            combo.setProperty("batchRowPath", path)
            combo.installEventFilter(self)
            combo.addItems([f"偏好 {i}" for i in range(1, 5)])
            combo.setCurrentIndex(plan.profile_slot - 1)
            combo.currentIndexChanged.connect(lambda index, movie=path: self._batch_profile_changed(movie, index + 1))
            combo.setEnabled(
                plan.status == "pending"
                and not self.batch_running
                and not plan.burned_subtitle
            )
            if plan.status == "burned" or plan.burned_subtitle:
                combo.setStyleSheet("background: #173f7a; color: white;")
            self.batch_table.setCellWidget(row, 4, combo)
        selection = self.batch_table.selectionModel()
        selection.clearSelection()
        for row, path in enumerate(self.batch_paths):
            if path in selected_paths:
                selection.select(
                    self.batch_table.model().index(row, 0),
                    QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
                )
            if path == current_path:
                selection.setCurrentIndex(
                    self.batch_table.model().index(row, 0),
                    QItemSelectionModel.SelectionFlag.NoUpdate,
                )
        self.batch_table.verticalScrollBar().setValue(scroll_value)

    @staticmethod
    def _batch_task_label(plan: BatchPlan) -> str:
        if plan.status == "burned" or plan.burned_subtitle:
            return "不可操作"
        if plan.task_state == "waiting" and plan.audio_passthrough_warning:
            return "音频原样"
        if plan.task_state == "waiting" and plan.external_subtitle_verified:
            return "字幕匹配成功"
        return {
            "processing": "正在处理",
            "completed": "已经处理",
            "failed": "处理失败",
            "blocked": "不可操作",
            "stopped": "已停止",
            "skipped": "已跳过",
        }.get(plan.task_state, "等待处理")

    @staticmethod
    def _batch_row_color(plan: BatchPlan) -> QColor:
        if plan.status == "burned" or plan.burned_subtitle:
            return QColor("#173f7a")
        if plan.task_state == "processing":
            return QColor("#fff0cf")
        if plan.task_state == "completed":
            return QColor("#e2f4ed")
        if plan.task_state in {"failed", "blocked"}:
            return QColor("#fde9e7")
        if plan.task_state in {"stopped", "skipped"}:
            return QColor("#fff3da")
        if plan.audio_passthrough_warning:
            return QColor("#fff3da")
        return QColor({
            "ready": "#e8f5f1",
            "review": "#fff3da",
            "blocked": "#fde9e7",
            "pending": "#f7f9f8",
        }.get(plan.status, "#ffffff"))

    @staticmethod
    def _batch_row_foreground(plan: BatchPlan) -> QColor:
        if plan.status == "burned" or plan.burned_subtitle:
            return QColor("#ffffff")
        return QColor("#183432")

    def _batch_item_state_update(self, path: str, state: str, label: str) -> None:
        plan = self.batch_plans.get(path)
        if not plan:
            return
        plan.task_state = state
        plan.task_status_label = label
        try:
            row = self.batch_paths.index(path)
        except ValueError:
            return
        color = self._batch_row_color(plan)
        for column in range(self.batch_table.columnCount()):
            item = self.batch_table.item(row, column)
            if item:
                item.setBackground(color)
                item.setForeground(self._batch_row_foreground(plan))
        status_item = self.batch_table.item(row, 6)
        if status_item:
            status_item.setText(self._batch_task_label(plan))

    def _batch_profile_changed(self, path: str, slot: int) -> None:
        plan = self.batch_plans.get(path)
        if (
            not plan
            or plan.profile_slot == slot
            or self.batch_running
            or plan.status != "pending"
        ):
            return
        plan.profile_slot = slot

    def _batch_analyze(self) -> None:
        self._analyze_paths(list(self.batch_paths))

    def _analyze_paths(self, paths: list[str]) -> None:
        if self.batch_running or not paths:
            return
        self.batch_running = True
        self.batch_cancel_event.clear()
        self.batch_start.setEnabled(False)
        self.batch_stop.setEnabled(True)
        self.batch_progress.setValue(0)
        self.batch_progress.setFormat("%p% · 正在分析…")

        def task(signals: WorkerSignals):
            plans: list[BatchPlan | None] = [None] * len(paths)
            # Match the detector's two-reader gate. This prevents a third movie
            # from accumulating visible analysis time while it waits for a slot.
            workers = max(1, min(2, len(paths)))

            def analyze_one(index: int, path: str) -> tuple[int, BatchPlan]:
                old = self.batch_plans[path]
                if self.batch_cancel_event.is_set():
                    return index, old
                profile = self.profiles[old.profile_slot - 1]
                try:
                    plan = batch_core.analyze_video(
                        path,
                        profile,
                        old.external_subtitle,
                        old.external_language,
                        old.external_subtitle_verified,
                        old.external_subtitle_verification,
                        old.external_subtitle_provider,
                        old.external_subtitle_release,
                        old.external_subtitle_identity_key,
                        old.external_subtitle_seal,
                        log=signals.message.emit,
                        external_subtitle_origin=old.external_subtitle_origin,
                        manual_confirmation_hash=old.manual_confirmation_hash,
                        cancel_event=self.batch_cancel_event,
                    )
                except core.CancelledError:
                    return index, old
                except Exception as exc:
                    plan = BatchPlan(path=path, profile_slot=profile.slot, status="blocked", status_label="分析失败",
                                     summary=f"分析失败：{exc}", detail=str(exc), output_path=old.output_path)
                return index, plan

            completed = 0
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=workers,
                thread_name_prefix="qt-movie-analysis",
            ) as executor:
                futures = {
                    executor.submit(analyze_one, index, path): index
                    for index, path in enumerate(paths)
                }
                for future in concurrent.futures.as_completed(futures):
                    index, plan = future.result()
                    plans[index] = plan
                    completed += 1
                    signals.progress.emit(
                        int(completed / len(paths) * 100),
                        f"正在分析 {completed}/{len(paths)} · {workers} 路并行",
                    )
            return [plan for plan in plans if plan is not None]

        self._run_worker(task, result=self._analysis_done, error=self._batch_error,
                         message=self._batch_log_line, progress=self._batch_progress_update,
                         finished=self._batch_idle)

    def _analysis_done(self, plans) -> None:
        for plan in plans:
            self.batch_plans[plan.path] = plan
        self._render_batch_table()
        ready = sum(plan.status == "ready" for plan in self.batch_plans.values())
        review = sum(plan.status == "review" for plan in self.batch_plans.values())
        blocked = sum(plan.status == "blocked" for plan in self.batch_plans.values())
        burned = sum(plan.status == "burned" for plan in self.batch_plans.values())
        verified = sum(plan.external_subtitle_verified for plan in self.batch_plans.values())
        message = f"分析完成：可处理 {ready}，需确认 {review}，无法处理 {blocked}，烧录字幕 {burned}。"
        if verified:
            message += f" 智能字幕已匹配 {verified}。"
        self.batch_progress.setValue(100)
        self.batch_progress.setFormat(f"%p% · {message}")
        self._batch_log_line(message)

    def _batch_double_click(self, row: int, _column: int) -> None:
        path = self.batch_table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        plan = self.batch_plans.get(path)
        if not plan or plan.status != "review" or "字幕" not in (plan.summary + plan.detail):
            return
        dialog = OnlineSubtitleDialog(
            self,
            path,
            lambda subtitle, candidate: self._batch_subtitle_selected(
                path,
                subtitle,
                candidate,
                "手动下载字幕",
            ),
        )
        dialog.exec()

    def _batch_context_menu(self, position: QPoint) -> None:
        index = self.batch_table.indexAt(position)
        preserved = getattr(self, "_context_selection_paths", None)
        self._context_selection_paths = None
        if not index.isValid():
            return
        # Restore the selection captured before Qt handles the right press.
        # The menu's row target and selected set are independent.
        if preserved is not None:
            selection = self.batch_table.selectionModel()
            selection.clearSelection()
            for selected_path in preserved:
                if selected_path in self.batch_paths:
                    row = self.batch_paths.index(selected_path)
                    selection.select(
                        self.batch_table.model().index(row, 0),
                        QItemSelectionModel.SelectionFlag.Select | QItemSelectionModel.SelectionFlag.Rows,
                    )
        selected_paths = [
            self.batch_table.item(selected.row(), 0).data(Qt.ItemDataRole.UserRole)
            for selected in self.batch_table.selectionModel().selectedRows()
            if self.batch_table.item(selected.row(), 0) is not None
        ]
        item = self.batch_table.item(index.row(), 0)
        if not item:
            return
        path = item.data(Qt.ItemDataRole.UserRole)
        plan = self.batch_plans.get(path)
        if not plan:
            return
        menu = QMenu(self.batch_table)
        play_action = menu.addAction("播放视频")
        remove_action = menu.addAction("移除选中")
        menu.addSeparator()
        process_action = menu.addAction("处理选中")
        stop_action = menu.addAction("停止选中")
        menu.addSeparator()
        smart_action = menu.addAction("智能字幕")
        manual_action = menu.addAction("在线手动字幕")
        import_action = menu.addAction("导入本地字幕")
        menu.addSeparator()
        detail_action = menu.addAction("查看详情")
        if plan.status == "burned" or plan.burned_subtitle or plan.task_state == "processing":
            smart_action.setEnabled(False)
            manual_action.setEnabled(False)
            import_action.setEnabled(False)
        remove_action.setEnabled(
            bool(selected_paths) and not self.batch_running
            and all(
                self.batch_plans[selected_path].task_state != "processing"
                and self.batch_plans[selected_path].status != "processing"
                for selected_path in selected_paths
            )
        )
        process_paths = [
            selected_path for selected_path in selected_paths
            if self.batch_plans[selected_path].status in {"ready", "review"}
            and not self.batch_plans[selected_path].burned_subtitle
            and self.batch_plans[selected_path].task_state not in {"processing", "completed"}
        ]
        process_action.setEnabled(not self.batch_running and bool(process_paths))
        stop_action.setEnabled(any(
            selected_path in self.batch_item_cancel_events
            and self.batch_plans[selected_path].task_state in {"waiting", "processing"}
            and not self.batch_item_cancel_events[selected_path].is_set()
            for selected_path in selected_paths
        ))
        chosen = menu.exec(self.batch_table.viewport().mapToGlobal(position))
        if chosen is play_action:
            if not QDesktopServices.openUrl(QUrl.fromLocalFile(path)):
                QMessageBox.warning(self, "无法播放", "未找到可用于播放该影片的系统播放器。")
        elif chosen is smart_action and smart_action.isEnabled():
            self._start_batch_smart_subtitle(path)
        elif chosen is manual_action and manual_action.isEnabled():
            self._open_batch_online_search(path)
        elif chosen is import_action and import_action.isEnabled():
            self._import_batch_subtitle(path)
        elif chosen is remove_action and remove_action.isEnabled():
            self._batch_remove()
        elif chosen is process_action and process_action.isEnabled():
            self._batch_start_paths(process_paths)
        elif chosen is stop_action and stop_action.isEnabled():
            for selected_path in selected_paths:
                if selected_path in self.batch_item_cancel_events:
                    self._batch_stop_one(selected_path)
        elif chosen is detail_action:
            QMessageBox.information(
                self,
                f"{Path(path).name} · 处理详情",
                plan.detail or plan.summary or "尚未分析该影片。",
            )

    def _start_batch_smart_subtitle(self, path: str) -> None:
        plan = self.batch_plans.get(path)
        if not plan or plan.status == "burned" or plan.burned_subtitle or plan.task_state == "processing":
            return
        if plan.has_complete_text:
            self._batch_log_line(f"{Path(path).name}：已有可用内嵌文本，处理时将优先纠偏该文本，无需下载英文字幕。")
            return
        if self.batch_running:
            self._batch_log_line(f"{Path(path).name}：当前有批量任务正在运行，请稍后再试。")
            return
        self.batch_running = True
        self.batch_start.setEnabled(False)
        plan.status = "processing"
        plan.status_label = "智能字幕处理中"
        plan.summary = "正在自动搜索、下载并检验英文文本字幕"
        self._render_batch_table()
        self.batch_progress.setValue(2)
        self.batch_progress.setFormat("%p% · 智能字幕处理中")
        self._batch_log_line(f"{Path(path).name}：开始智能字幕流程。")
        selected_audio_id = plan.audio_ids[0] if plan.audio_ids else None

        def task(signals: WorkerSignals):
            return smart_subtitles.find_verified_english(
                path,
                selected_audio_id,
                lambda message: signals.message.emit(f"{Path(path).name}：{message}"),
                None,
            )

        self._run_worker(
            task,
            result=lambda result: self._batch_smart_subtitle_verified(path, result),
            error=lambda message: self._batch_smart_subtitle_failed(path, message),
            message=self._batch_log_line,
        )

    def _batch_smart_subtitle_verified(self, path: str, result: smart_subtitles.SmartSubtitleResult) -> None:
        plan = self.batch_plans[path]
        plan.external_subtitle = result.subtitle_path
        plan.external_language = result.language
        plan.external_subtitle_verified = True
        plan.external_subtitle_verification = result.report
        plan.external_subtitle_provider = result.provider
        plan.external_subtitle_release = result.release
        plan.external_subtitle_identity_key = result.identity_key
        plan.external_subtitle_seal = result.verification_seal
        plan.external_subtitle_origin = "download"
        plan.manual_confirmation_hash = ""
        self.batch_running = False
        self.batch_start.setEnabled(True)
        self._batch_log_line(
            f"{Path(path).name}：智能字幕已通过，来源 {result.provider}；{result.report}。"
        )
        self._analyze_paths([path])

    def _batch_smart_subtitle_failed(self, path: str, message: str) -> None:
        plan = self.batch_plans[path]
        plan.external_subtitle_verified = False
        plan.external_subtitle_verification = message
        plan.status = "review"
        plan.status_label = "需要确认"
        plan.summary = "智能字幕未找到合格英文文本字幕"
        plan.detail = f"{message}\n请使用“手动字幕”选择字幕版本。"
        self.batch_running = False
        self.batch_start.setEnabled(True)
        self._render_batch_table()
        self.batch_progress.setValue(100)
        self.batch_progress.setFormat("%p% · 智能字幕未通过")
        self._batch_log_line(f"{Path(path).name}：智能字幕失败：{message}")
        QMessageBox.warning(self, "智能字幕未通过", f"{Path(path).name}\n\n{message}")

    def _open_batch_online_search(self, path: str) -> None:
        plan = self.batch_plans.get(path)
        if not plan or plan.status == "burned" or plan.burned_subtitle or plan.task_state == "processing":
            return
        dialog = OnlineSubtitleDialog(
            self,
            path,
            lambda subtitle, candidate: self._batch_subtitle_selected(
                path,
                subtitle,
                candidate,
                "手动下载字幕",
            ),
        )
        dialog.exec()

    def _import_batch_subtitle(self, path: str, selected_path: str | None = None) -> None:
        plan = self.batch_plans.get(path)
        if (
            not plan
            or plan.status == "burned"
            or plan.burned_subtitle
            or plan.task_state == "processing"
        ):
            return
        selected = [selected_path] if selected_path else self._qt_open_dialog(
            "导入本地字幕",
            "字幕文件 (*.srt *.ass *.ssa *.vtt);;所有文件 (*.*)",
            initial=str(Path(path).parent),
        )
        if not selected:
            return
        source = Path(selected[0])
        try:
            events = core.parse_subtitle(source)
        except Exception as exc:
            QMessageBox.warning(self, "字幕无法读取", str(exc))
            return
        language = pro_core.detect_subtitle_language(events)
        if language == "und":
            labels = [label for _code, label in LANGUAGES]
            label, accepted = QInputDialog.getItem(
                self,
                "选择字幕语言",
                "无法自动判断字幕语言，请选择：",
                labels,
                0,
                False,
            )
            if not accepted:
                return
            language = next(code for code, name in LANGUAGES if name == label)
        destination = Path(path).with_suffix("").with_name(
            Path(path).stem + "_pro_work"
        ) / "manual-import" / source.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        candidate = SimpleNamespace(
            language=language,
            release=f"本地导入 · {source.name}",
            feature_title=Path(path).stem,
            recommendation="用户指定",
            match_reason="本地字幕文件",
        )
        self._batch_subtitle_selected(
            path,
            str(destination),
            candidate,
            "本地导入字幕",
        )

    def _batch_subtitle_selected(
        self,
        path: str,
        subtitle: str,
        candidate,
        source_label: str,
    ) -> None:
        plan = self.batch_plans[path]
        plan.external_subtitle = subtitle
        plan.external_language = candidate.language
        plan.external_subtitle_origin = (
            "download" if source_label == "手动下载字幕" else "local"
        )
        plan.manual_confirmation_hash = ""
        plan.external_subtitle_verified = False
        plan.external_subtitle_verification = ""
        plan.status = "processing"
        plan.status_label = "字幕预检中"
        plan.summary = "正在检查字幕完整性及与音轨的时间轴匹配"
        plan.detail = (
            f"候选：{candidate.feature_title or candidate.release}\n"
            f"筛选状态：{candidate.recommendation} · {candidate.match_reason}"
        )
        self._render_batch_table()
        self._batch_log_line(f"{Path(path).name}：{source_label}已选择，开始快速预检。")
        selected_audio_id = plan.audio_ids[0] if plan.audio_ids else None
        verify_dir = Path(subtitle).parent / "preflight"

        def task(signals: WorkerSignals):
            report = lambda message: signals.message.emit(f"{Path(path).name}：{message}")
            if pro_core.normalize_language_code(candidate.language) == "en":
                shared_audio = pro_core.prepare_shared_subtitle_content_audio(
                    path,
                    selected_audio_id,
                    verify_dir / "shared-content-audio",
                    report,
                    source_language="en",
                )
                return pro_core.preflight_online_subtitle(
                    path,
                    subtitle,
                    str(verify_dir),
                    selected_audio_id,
                    shared_audio,
                    report,
                    source_language="en",
                    candidate_label=f"{source_label} {candidate.release}",
                )
            return pro_core.preflight_external_subtitle(
                path,
                subtitle,
                str(verify_dir),
                selected_audio_id,
                report,
                None,
                candidate.language,
                use_embedded_reference=True,
                candidate_label=f"{source_label} {candidate.release}",
                timeline_cache_dir=str(Path(path).with_suffix("").with_name(
                    Path(path).stem + "_pro_work"
                ) / "timeline-health"),
                manual_reference_first=True,
            )

        self._run_worker(
            task,
            result=lambda result: self._batch_online_verified(path, candidate, result),
            error=lambda message: self._batch_online_verify_failed(path, subtitle, candidate, message),
            message=self._batch_log_line,
        )

    def _batch_online_verified(
        self, path: str, candidate, result, manual_hash: str = "",
    ) -> None:
        aligned, report = result
        plan = self.batch_plans[path]
        plan.external_subtitle = str(aligned)
        plan.external_language = candidate.language
        plan.external_subtitle_verified = True
        plan.external_subtitle_verification = report
        plan.manual_confirmation_hash = manual_hash
        plan.external_subtitle_provider = ""
        plan.external_subtitle_release = ""
        plan.external_subtitle_identity_key = ""
        plan.external_subtitle_seal = ""
        self._batch_log_line(f"{Path(path).name}：{report}。")
        self._analyze_paths([path])

    def _batch_online_verify_failed(self, path: str, subtitle: str, candidate, message: str) -> None:
        plan = self.batch_plans[path]
        pro_core.append_subtitle_diagnostic_log(path, f"手动字幕预检未通过：{message}")
        dialog = QMessageBox(self)
        dialog.setWindowTitle("字幕需要人工对齐")
        dialog.setText(
            f"{Path(path).name}\n\n自动预检未通过：{message}\n\n"
            "将准备前、中、后三段短音频与声音位置，"
            "随后由您在同一时间轴上核听并调整字幕。"
        )
        inspect_button = dialog.addButton(
            "打开人工时间轴", QMessageBox.ButtonRole.AcceptRole
        )
        dialog.addButton("换字幕", QMessageBox.ButtonRole.RejectRole)
        dialog.exec()
        if dialog.clickedButton() is inspect_button:
            self._start_batch_manual_review(path, subtitle, candidate)
            return
        plan.external_subtitle_verified = False
        plan.external_subtitle_verification = message
        plan.status = "review"
        plan.status_label = "需要确认"
        plan.summary = "字幕预检未通过"
        plan.detail = f"{message}\n请双击本片并换一个字幕版本。"
        self._render_batch_table()
        self._batch_log_line(f"{Path(path).name}：字幕预检未通过：{message}")
        QMessageBox.warning(self, "字幕预检未通过", f"{Path(path).name}\n\n{message}\n\n请换一个字幕版本。")

    def _start_batch_manual_review(self, path: str, subtitle: str, candidate) -> None:
        plan = self.batch_plans[path]
        plan.status = "processing"
        plan.status_label = "准备人工时间轴"
        plan.summary = "正在准备前、中、后三段短音频与局部声音标记"
        self._render_batch_table()
        # Every review must keep its own source and short clips. A later
        # candidate (or a repeat of the same candidate) must not rewrite an
        # already open review page.
        source_hash = hashlib.sha256(Path(subtitle).read_bytes()).hexdigest()
        work = Path(subtitle).parent / "manual-deep-review" / (
            f"{source_hash[:12]}-{time.time_ns()}"
        )
        audio_id = plan.audio_ids[0] if plan.audio_ids else None

        def task(signals: WorkerSignals):
            return manual_review_prepare.prepare_review(
                path, subtitle, work, audio_id,
                lambda message: signals.message.emit(
                    f"{Path(path).name}：{message}"
                ),
                source_language=candidate.language,
            )

        self._run_worker(
            task,
            result=lambda result: self._batch_manual_review_ready(
                path, subtitle, candidate, result
            ),
            error=lambda message: self._batch_manual_review_failed(path, message),
            message=self._batch_log_line,
        )

    def _batch_manual_review_failed(self, path: str, message: str) -> None:
        plan = self.batch_plans[path]
        outside_safe_range = "超出手动核听范围 ±20 秒" in message
        title = "所选字幕时间轴超出安全范围" if outside_safe_range else "人工时间轴准备失败"
        plan.status = "review"
        plan.status_label = "需换字幕" if outside_safe_range else "需要确认"
        plan.summary = title
        plan.detail = message
        self._render_batch_table()
        self._batch_log_line(f"{Path(path).name}：{title}：{message}")
        QMessageBox.warning(self, title, message)

    def _batch_manual_review_cancelled(self, path: str) -> None:
        plan = self.batch_plans[path]
        plan.status = "review"
        plan.status_label = "需要确认"
        plan.summary = "人工核听未确认，未使用所选字幕"
        self._render_batch_table()
        self._batch_log_line(f"{Path(path).name}：用户取消人工核听裁决，未使用所选字幕。")

    def _batch_manual_review_ready(
        self, path: str, subtitle: str, candidate, evidence: dict,
    ) -> None:
        plan = self.batch_plans[path]
        if hashlib.sha256(Path(subtitle).read_bytes()).hexdigest() != evidence["source_sha256"]:
            self._batch_manual_review_failed(path, "所选字幕在深入核验后发生变化，请重新选择。")
            return
        self._batch_log_line(f"{Path(path).name}：已准备前、中、后三段人工核听；各处可独立调整。")
        try:
            dialog = ManualSubtitleReviewDialog(self, evidence)
        except Exception as exc:
            self._batch_manual_review_failed(path, f"无法准备软件内核听窗口：{exc}")
            return
        if dialog.exec() != QDialog.DialogCode.Accepted:
            self._batch_manual_review_cancelled(path)
            return
        offsets = dialog.confirmed_offsets
        if offsets is None:
            self._batch_manual_review_failed(path, "尚未确认三点修正。")
            return
        rejection = manual_review_prepare.manual_offsets_rejection(evidence, offsets)
        if rejection:
            self._batch_manual_review_failed(path, rejection)
            return
        if hashlib.sha256(Path(subtitle).read_bytes()).hexdigest() != evidence["source_sha256"]:
            self._batch_manual_review_failed(path, "核听期间所选字幕发生变化，请重新选择。")
            return
        work = Path(evidence["review_dir"])
        source = work / "manual-source.srt"
        confirmed = work / "manual-confirmed.srt"
        if evidence.get("normalized_sha256") and hashlib.sha256(source.read_bytes()).hexdigest() != evidence["normalized_sha256"]:
            self._batch_manual_review_failed(path, "核听来源发生变化，请重新选择。")
            return
        import manual_timeline
        points = manual_review_prepare.review_points(evidence)
        try:
            manual_timeline.apply_to_srt(source, confirmed, points, offsets)
        except ValueError as exc:
            self._batch_manual_review_failed(path, str(exc))
            return
        confirmed_hash = hashlib.sha256(confirmed.read_bytes()).hexdigest()
        (work / "manual-confirmation.json").write_text(json.dumps({
            "rule": "manual-three-window-timeline-v3", "original_sha256": evidence["source_sha256"],
            "points": points, "offsets": offsets, "output_sha256": confirmed_hash,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        report_text = ("用户确认三点修正：" + "、".join(f"{x:+.2f}秒" for x in offsets)
            + "；相邻点渐变，首尾沿用端点偏移；不再自动重验；原字幕SHA-256 "
            + evidence["source_sha256"][:12])
        pro_core.append_subtitle_diagnostic_log(path, report_text)
        self._batch_online_verified(
            path, candidate, (confirmed, report_text),
            manual_hash=confirmed_hash,
        )

    def _batch_mode_changed(self, mode: str) -> None:
        self.batch_parallel.setVisible(mode == "自定义模式")

    def _batch_workers(self) -> int:
        if self.batch_hardware is not None:
            return self.batch_hardware.batch_movie_workers
        return 1

    def _batch_start(self) -> None:
        self._batch_start_paths()

    def _batch_start_one(self, path: str) -> None:
        if self.batch_running:
            QMessageBox.information(self, "批量任务正在运行", "请先停止或等待当前批量任务完成。")
            return
        self._batch_start_paths([path])

    def _batch_stop_one(self, path: str) -> None:
        plan = self.batch_plans.get(path)
        item_event = self.batch_item_cancel_events.get(path)
        if plan is None or item_event is None or plan.task_state not in {"waiting", "processing"}:
            self._batch_log_line(f"{Path(path).name}：当前未在处理队列中，无需停止。")
            return
        if item_event.is_set():
            return
        item_event.set()
        if plan.task_state == "processing":
            self._batch_log_line(f"{Path(path).name}：正在停止当前外部工具，其他影片不受影响。")
        else:
            self._batch_log_line(f"{Path(path).name}：已请求停止排队中的任务，不影响其他影片。")

    def _batch_start_paths(self, requested_paths: list[str] | None = None) -> None:
        if self.batch_running:
            return
        if any(plan.status == "processing" and "预检" in plan.status_label for plan in self.batch_plans.values()):
            QMessageBox.information(self, "字幕正在预检", "请等待下载字幕完成音轨时间轴预检后再开始批量处理。")
            return
        source_paths = requested_paths if requested_paths is not None else self.batch_paths
        ready_paths = [
            path for path in source_paths
            if path in self.batch_plans
            and self.batch_plans[path].status in {"ready", "review"}
            and not self.batch_plans[path].burned_subtitle
            and self.batch_plans[path].task_state != "processing"
        ]
        if not ready_paths:
            QMessageBox.information(self, "没有可处理影片", "请先分析全部；蓝色烧录字幕项目不会自动处理。")
            return
        allow_online_search = True
        online_service_needed = any(
            batch_core.plan_requires_online_subtitle_service(
                self.batch_plans[path],
                self.profiles[self.batch_plans[path].profile_slot - 1],
            )
            for path in ready_paths
        )
        if online_service_needed and not smart_subtitles.subtitle_service_available():
            dialog = QMessageBox(QMessageBox.Icon.Warning, "当前无法连接字幕服务", "", parent=None)
            dialog.setWindowModality(Qt.WindowModality.ApplicationModal)
            dialog.setText("当前无法连接字幕服务")
            dialog.setInformativeText(
                "是否进入“无搜索处理”模式？\n"
                "此模式无法下载字幕，也无法进行时间轴核验；"
                "无字幕或字幕不完整的影片将跳过。"
            )
            continue_button = dialog.addButton("无搜索继续", QMessageBox.ButtonRole.AcceptRole)
            cancel_button = dialog.addButton("取消处理", QMessageBox.ButtonRole.RejectRole)
            dialog.setDefaultButton(cancel_button)
            dialog.exec()
            if dialog.clickedButton() is not continue_button:
                self._batch_log_line("字幕服务不可用；用户取消本次处理，分析结果已保留。")
                return
            allow_online_search = False
            self._batch_log_line(
                "字幕服务不可用；已选择无搜索处理。不会下载字幕或核验时间轴，"
                "无完整字幕的影片将跳过。"
            )
        ok, reason = batch_core.enough_space(ready_paths)
        if not ok:
            QMessageBox.warning(self, "磁盘空间不足", reason)
            return
        self.batch_cancel_event.clear()
        self.batch_item_cancel_events = {path: threading.Event() for path in ready_paths}
        self.batch_running = True
        self.batch_start.setEnabled(False)
        self.batch_stop.setEnabled(True)
        self.batch_progress.setValue(0)
        mode = self.batch_hardware.recommended_mode if self.batch_hardware is not None else "稳定模式"
        self.batch_mode.setCurrentText(mode)
        workers = self._batch_workers()

        def task(signals: WorkerSignals):
            total = len(ready_paths)
            completed = failed = stopped = skipped = 0
            item_progress = {path: 0 for path in ready_paths}
            progress_lock = threading.Lock()
            parallel_targets = 2 if workers == 1 else 1
            ai_slots = max(1, min(workers, self.batch_hardware.ai_slots if self.batch_hardware else 1))
            ai_gate = threading.Semaphore(ai_slots)
            guard = system_resources.RuntimeGuard()
            pending = list(ready_paths)
            active: dict[concurrent.futures.Future, tuple[str, threading.Event]] = {}
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="qt-movie-batch") as executor:
                while pending or active:
                    if self.batch_cancel_event.is_set() and pending:
                        for path in pending:
                            plan = self.batch_plans[path]
                            plan.task_state, plan.task_status_label = "stopped", "已停止"
                            signals.state.emit(path, "stopped", "已停止")
                            item_progress[path] = 100
                            stopped += 1
                        pending.clear()
                    while pending and len(active) < workers and not self.batch_cancel_event.is_set():
                        allowed, reason = guard.can_start_next()
                        if not allowed:
                            signals.message.emit(reason)
                            break
                        path = pending.pop(0)
                        item_event = self.batch_item_cancel_events[path]
                        if item_event.is_set():
                            plan = self.batch_plans[path]
                            plan.task_state, plan.task_status_label = "stopped", "已停止"
                            signals.state.emit(path, "stopped", "已停止")
                            item_progress[path] = 100
                            stopped += 1
                            continue
                        plan = self.batch_plans[path]
                        profile = self.profiles[plan.profile_slot - 1]
                        plan.task_state, plan.task_status_label = "processing", "正在处理"
                        signals.state.emit(path, "processing", "正在处理")
                        signals.message.emit(f"{Path(path).name}：开始处理")

                        def report(text: str, movie: str = path) -> None:
                            signals.message.emit(f"{Path(movie).name}：{text}")
                            value = None
                            match = re.search(r"翻译进度：\s*(\d+)\s*/\s*(\d+)", text)
                            if match:
                                done = int(match.group(1))
                                count = max(1, int(match.group(2)))
                                value = 8 + int(done / count * 62)
                            match = re.search(r"兼容音频进度：\s*(\d+)%", text)
                            if match:
                                value = 70 + int(min(100, int(match.group(1))) * 0.2)
                            if "正在封装批量输出" in text:
                                value = 92
                            if value is None:
                                return
                            with progress_lock:
                                item_progress[movie] = max(item_progress[movie], value)
                                overall = int(sum(item_progress.values()) / total)
                            signals.progress.emit(overall, f"正在处理 {Path(movie).name}")

                        cancel_event = _CombinedCancelEvent(self.batch_cancel_event, item_event)
                        future = executor.submit(
                            batch_core.process_plan,
                            plan,
                            profile,
                            report,
                            cancel_event,
                            parallel_targets,
                            ai_gate,
                            allow_online_search,
                        )
                        active[future] = (path, item_event)
                    if not active:
                        if pending and not self.batch_cancel_event.is_set():
                            time.sleep(1)
                            continue
                        break
                    done, _ = concurrent.futures.wait(active, timeout=1, return_when=concurrent.futures.FIRST_COMPLETED)
                    for future in done:
                        path, item_event = active.pop(future)
                        plan = self.batch_plans[path]
                        try:
                            future.result()
                            plan.task_state, plan.task_status_label = "completed", "已经处理"
                            signals.state.emit(path, "completed", "已经处理")
                            completed += 1
                        except core.CancelledError:
                            batch_core.append_process_log(plan, "处理已停止。")
                            plan.task_state, plan.task_status_label = "stopped", "已停止"
                            signals.state.emit(path, "stopped", "已停止")
                            signals.message.emit(f"{Path(path).name}：已停止。")
                            stopped += 1
                        except batch_core.OfflineSubtitleUnavailableError as exc:
                            skip_detail = str(exc).strip() or "无搜索处理没有可用的完整字幕来源。"
                            batch_core.append_process_log(plan, skip_detail)
                            plan.task_state, plan.task_status_label = "skipped", "已跳过"
                            existing_detail = (plan.detail or "").strip()
                            plan.detail = "\n\n".join(filter(None, (
                                existing_detail,
                                skip_detail,
                            )))
                            plan.summary = "无搜索处理已跳过（请查看详情）"
                            signals.state.emit(path, "skipped", "已跳过")
                            signals.message.emit(f"{Path(path).name}：{skip_detail}")
                            skipped += 1
                        except Exception as exc:
                            failure_detail = str(exc).strip() or "外部处理未返回具体原因。"
                            failure_detail = f"{type(exc).__name__}: {failure_detail}"
                            batch_core.append_process_log(plan, f"处理失败：{failure_detail}")
                            plan.task_state, plan.task_status_label = "failed", "处理失败"
                            existing_detail = (plan.detail or "").strip()
                            plan.detail = "\n\n".join(filter(None, (
                                existing_detail,
                                f"处理失败：{failure_detail}",
                            )))
                            plan.summary = "处理失败（请查看详情）"
                            signals.state.emit(path, "failed", "处理失败")
                            signals.message.emit(f"{Path(path).name}：处理失败：{failure_detail}")
                            failed += 1
                        finished = completed + failed + stopped + skipped
                        with progress_lock:
                            item_progress[path] = 100
                            overall = int(sum(item_progress.values()) / total)
                        signals.progress.emit(overall, f"已处理 {finished}/{total}")
            return completed, failed, stopped, skipped, self.batch_cancel_event.is_set(), mode

        self._run_worker(
            task,
            result=self._batch_process_done,
            error=self._batch_error,
            message=self._batch_log_line,
            progress=self._batch_progress_update,
            state=self._batch_item_state_update,
            finished=self._batch_idle,
        )

    def _batch_stop(self) -> None:
        self.batch_cancel_event.set()
        for item_event in self.batch_item_cancel_events.values():
            item_event.set()
        self.batch_stop.setEnabled(False)
        self._batch_log_line("正在停止当前外部工具；未开始的影片不会处理。")

    def _batch_process_done(self, payload) -> None:
        completed, failed, stopped, skipped, cancelled, mode = payload
        self._render_batch_table()
        prefix = "已停止" if cancelled else "批量完成"
        message = f"{prefix}：成功 {completed}，跳过 {skipped}，失败 {failed}，停止 {stopped}。"
        self.batch_progress.setValue(100 if not cancelled else self.batch_progress.value())
        self.batch_progress.setFormat(f"%p% · {message}")
        self._batch_log_line(f"{mode} · {message}")
    def _batch_progress_update(self, value: int, label: str) -> None:
        self.batch_progress.setValue(value)
        self.batch_progress.setFormat(f"%p% · {label}")

    def _batch_idle(self) -> None:
        self.batch_running = False
        self.batch_item_cancel_events.clear()
        self.batch_start.setEnabled(True)
        self.batch_stop.setEnabled(False)

    def _batch_error(self, message: str) -> None:
        self._batch_log_line(f"失败：{message}")
        QMessageBox.critical(self, "批量任务失败", message)

    def _batch_log_line(self, message: str) -> None:
        scrollbar = self.batch_log.verticalScrollBar()
        follow = scrollbar.value() >= scrollbar.maximum() - 2
        self.batch_log.appendPlainText(message)
        if follow:
            scrollbar.setValue(scrollbar.maximum())

    def _load_profile_editor(self) -> None:
        if not hasattr(self, "profile_name"):
            return
        profile = self.profiles[self.profile_slot.currentIndex()]
        self._show_profile_in_editor(profile)

    def _show_profile_in_editor(self, profile: PreferenceProfile) -> None:
        self._loading_profile_editor = True
        try:
            self.profile_name.setText(profile.name)
            policy = profile.audio_policy if profile.audio_policy in self.profile_audio_policy else "universal"
            self.profile_audio_policy[policy].setChecked(True)
            self.profile_compact_warning.setVisible(policy == "compact")
            for code, check in self.profile_subtitles.items():
                check.setChecked(code in profile.subtitle_languages)
            self.profile_replace_downloaded_subtitle.setChecked(profile.replace_downloaded_subtitle)
            self.profile_chinese_script_equivalent.setChecked(profile.chinese_script_equivalent)
        finally:
            self._loading_profile_editor = False

    def _auto_save_profile(self, *_args) -> None:
        if not getattr(self, "_loading_profile_editor", False):
            self._save_profile()

    def _save_profile(self) -> None:
        if self.batch_running or any(
            plan.status != "pending" for plan in self.batch_plans.values()
        ):
            self.profile_feedback.setText(
                "当前批次已经开始分析，偏好已锁定。请先清空批次，再修改偏好。"
            )
            return
        index = self.profile_slot.currentIndex()
        audio_policy = next(
            (code for code, button in self.profile_audio_policy.items() if button.isChecked()),
            "universal",
        )
        profile = PreferenceProfile(
            slot=index + 1,
            name=self.profile_name.text().strip() or f"偏好 {index + 1}",
            subtitle_languages=[code for code, check in self.profile_subtitles.items() if check.isChecked()],
            replace_downloaded_subtitle=self.profile_replace_downloaded_subtitle.isChecked(),
            audio_policy=audio_policy,
            chinese_script_equivalent=self.profile_chinese_script_equivalent.isChecked(),
        )
        self.profiles[index] = profile
        save_profiles(self.profiles)
        self.profile_feedback.clear()

    def _detect_hardware(self) -> None:
        if hasattr(self, "hardware_status"):
            self.hardware_status.setText("正在检测本机硬件…")
        self._run_worker(lambda _signals: system_resources.detect_hardware(), result=self._hardware_done,
                         error=lambda message: self._hardware_failed(message))

    def _hardware_done(self, profile) -> None:
        self.batch_hardware = profile
        self.header_hardware.setText(f"{profile.tier_label} · {profile.recommended_mode}")
        self.batch_recommendation.setText(f"推荐：{profile.recommended_mode}")
        self.batch_mode.setCurrentText(profile.recommended_mode)
        self.hardware_status.setText(system_resources.hardware_status_text(profile))
        self.hardware_detail.setText(system_resources.hardware_detail_text(profile))
        self.hardware_reason.setText(system_resources.hardware_reason_text(profile))

    def _hardware_failed(self, message: str) -> None:
        self.header_hardware.setText("硬件检测失败")
        self.batch_recommendation.setText("建议：稳定模式")
        self.hardware_status.setText("硬件检测失败")
        self.hardware_detail.setText(message)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        expanded = []
        for path in paths:
            expanded.extend(batch_core.videos_in_folder(path) if Path(path).is_dir() else [path])
        self._batch_add_paths(expanded)
        if expanded:
            self.tabs.setCurrentIndex(0)
        event.acceptProposedAction()

    def closeEvent(self, event) -> None:
        if self.batch_running:
            answer = QMessageBox.question(self, "确认退出", "任务仍在运行。是否停止任务并退出？")
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.batch_cancel_event.set()
        event.accept()


STYLE = """
QWidget { background: #f4f8f7; color: #183432; font-family: "Microsoft YaHei UI", "Microsoft YaHei"; font-size: 12px; }
QMainWindow, QTabWidget::pane { background: #f4f8f7; }
QTabWidget::pane { border: 1px solid #d4e1de; border-radius: 0 12px 12px 12px; top: -1px; }
QTabBar::tab { background: #e7efed; border: 1px solid #d0ddda; border-bottom: 0; border-radius: 10px 10px 0 0; padding: 10px 22px; min-width: 105px; margin-right: 3px; }
QTabBar::tab:selected { background: #ffffff; color: #08736b; border-color: #b9d8d3; font-weight: 700; }
QGroupBox { background: #ffffff; border: 1px solid #c9dcd8; border-radius: 12px; margin-top: 11px; font-weight: 600; }
QGroupBox::title { subcontrol-origin: margin; left: 12px; padding: 0 6px; color: #0b615c; background: #ffffff; }
QLineEdit, QComboBox, QSpinBox, QListWidget, QTableWidget, QPlainTextEdit { background: #ffffff; border: 1px solid #bfd2ce; border-radius: 8px; padding: 6px; selection-background-color: #cce9e4; selection-color: #153936; }
QTableWidget { selection-background-color: transparent; }
QTableWidget::item:selected { background-color: transparent; color: inherit; }
QListWidget::item { padding: 5px; }
QListWidget::item:alternate, QTableWidget::item:alternate { background: #f7faf9; }
QHeaderView::section { background: #e8efed; color: #254744; border: 0; border-right: 1px solid #c6d3d0; border-bottom: 1px solid #c6d3d0; padding: 7px; font-weight: 600; }
QPushButton { background: #eef4f2; border: 1px solid #aec5c0; border-radius: 8px; padding: 6px 11px; min-height: 18px; font-size: 12px; }
QPushButton:hover { background: #e1ece9; border-color: #719b95; }
QPushButton:pressed { background: #91b8b2; color: #102f2c; border: 2px solid #426f69; padding: 9px 12px 5px 14px; }
QPushButton:disabled { color: #9aa7a5; background: #edf0ef; }
QPushButton#primaryButton { background: #0d7f77; color: white; border-color: #096961; font-weight: 600; min-width: 105px; }
QPushButton#primaryButton:hover { background: #096961; }
QPushButton#primaryButton:pressed { background: #064f4a; color: white; border: 2px solid #032f2c; padding: 9px 12px 5px 14px; }
QSpinBox::up-button, QSpinBox::down-button { width: 22px; background: #e1ece9; border-left: 1px solid #9eb5b1; }
QSpinBox::up-button:hover, QSpinBox::down-button:hover { background: #cce1dd; }
QSpinBox::up-button:pressed, QSpinBox::down-button:pressed { background: #83b0a9; }
QToolButton#spinArrowButton { background: #e1ece9; border: 1px solid #9eb5b1; border-left: 0; border-radius: 0; padding: 0; min-width: 24px; min-height: 13px; font-size: 9px; font-weight: 700; }
QToolButton#spinArrowButton:hover { background: #cce1dd; color: #075f59; }
QToolButton#spinArrowButton:pressed { background: #83b0a9; color: #ffffff; }
QProgressBar { background: #dbe5e3; border: 1px solid #b8c8c5; border-radius: 9px; text-align: center; min-height: 20px; }
QProgressBar::chunk { background: #f28c28; }
QLabel#appTitle { color: #075b55; font-size: 24px; font-weight: 700; }
QLabel#sectionTitle, QLabel#dialogTitle { color: #173f3d; font-size: 15px; font-weight: 600; }
QLabel#muted { color: #667b78; font-size: 12px; }
QLabel#hardwareBadge { background: #e7efed; color: #315954; border: 1px solid #c4d2cf; border-radius: 8px; padding: 7px 10px; }
QFrame#workflowCard { background: #ffffff; border: 1px solid #bcd9d4; border-radius: 14px; }
QLabel#workflowTitle { background: transparent; color: #075b55; font-size: 17px; font-weight: 600; }
QLabel#stepTitle { background: transparent; color: #0b756d; font-size: 14px; font-weight: 600; }
QRadioButton#sourceTile { background: #f5faf8; border: 1px solid #cfdfdc; border-radius: 9px; padding: 9px 10px; min-height: 20px; }
QRadioButton#sourceTile:hover { background: #edf7f5; border-color: #78aaa3; }
QRadioButton#sourceTile:checked { background: #e2f4f0; border: 2px solid #0d8379; color: #075b55; font-weight: 700; padding: 8px 9px; }
QLabel#workflowSummary { background: #eaf7f4; color: #165852; border: 1px solid #c4e3dc; border-radius: 9px; padding: 9px 10px; }
QMessageBox, QDialog { background: #f7fbfa; }
QMessageBox QLabel { background: transparent; }
QPlainTextEdit#logView { font-family: Consolas, "Microsoft YaHei UI"; font-size: 12px; }
QScrollBar:vertical { background: #edf2f1; width: 12px; margin: 0; }
QScrollBar::handle:vertical { background: #9fb5b1; min-height: 28px; border-radius: 3px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QToolTip { background: #163a37; color: white; border: 0; padding: 8px; }
QWidget#windowSurface { background: #f7f9fa; border: 1px solid #dbe5e5; border-radius: 20px; }
QWidget#appBody { background: #f7f9fa; border-radius: 0 0 20px 20px; }
QFrame#titleBar { background: #f7f9fa; border-radius: 20px 20px 0 0; }
QLabel#brandMark { background: #079c91; color: white; border-radius: 15px; min-width: 30px; max-width: 30px; min-height: 30px; max-height: 30px; qproperty-alignment: AlignCenter; font-size: 15px; font-weight: 700; }
QLabel#brandName { background: transparent; color: #1b2022; font-size: 22px; font-weight: 600; }
QLabel#brandName span { color: #616b70; font-size: 16px; font-weight: 500; }
QToolButton#windowControl, QToolButton#windowClose { background: transparent; border: 0; border-radius: 8px; color: #3d4549; font-size: 18px; }
QToolButton#windowControl:hover { background: #e8efee; }
QToolButton#windowClose:hover { background: #e94f47; color: white; }
QToolButton#navButton { background: transparent; border: 0; border-radius: 18px; color: #4e5b60; font-size: 12px; padding: 8px 15px; }
QToolButton#navButton:hover { background: #edf4f3; color: #087e74; }
QToolButton#navButton:checked { background: #ffffff; color: #008d82; font-weight: 700; }
QGroupBox#contentCard { border: 0; border-radius: 18px; margin-top: 0; padding-top: 22px; }
QGroupBox#contentCard::title { left: 17px; top: 11px; color: #1d2629; font-size: 15px; font-weight: 600; padding: 0 4px; }
QFrame#workflowCard { border: 0; border-radius: 18px; }
QToolButton#sourceTile { background: #ffffff; border: 1px solid #e0e5e7; border-radius: 15px; color: #323b40; font-size: 12px; font-weight: 600; padding: 7px; }
QToolButton#sourceTile:hover { background: #f4fbfa; border-color: #58b7ad; }
QToolButton#sourceTile:checked { background: #f0fbf9; border: 2px solid #009889; color: #008d82; padding: 9px; }
QLabel#workflowTitle { font-size: 18px; color: #1d2629; }
QLabel#stepTitle { color: #1d2629; font-size: 15px; }
QPushButton#primaryButton { border: 0; border-radius: 28px; min-width: 170px; min-height: 42px; font-size: 16px; background: #009b8c; }
QPushButton#primaryButton:hover { background: #008679; }
QProgressBar { background: #e9efee; border: 0; border-radius: 16px; min-height: 30px; }
QProgressBar::chunk { background: #15aa99; border-radius: 16px; }
QPlainTextEdit#logView { background: #ffffff; border: 0; border-radius: 16px; }
QLabel#workflowSummary { background: #f1faf8; border: 1px solid #9fded5; border-radius: 13px; padding: 13px; font-size: 15px; font-weight: 600; }
QLabel#hardwareBadge { background: #eff7f5; border: 1px solid #d0e4df; border-radius: 12px; padding: 10px 14px; }
QLabel#posterPlaceholder { background: #13292d; color: #d8f5f1; border-radius: 13px; font-size: 17px; font-weight: 700; }
QLabel#videoName { color: #20272a; font-size: 14px; font-weight: 600; }
QLabel#videoMeta { color: #657075; font-size: 12px; }
QLabel#policyTitle { color: #293236; font-size: 13px; font-weight: 500; }
QLabel#countBadge { background: #eef8f6; color: #008f83; border-radius: 11px; padding: 7px 11px; font-size: 12px; font-weight: 600; }
QPushButton#pillButton { background: #ffffff; border: 1px solid #d7dfe1; border-radius: 17px; padding: 7px 16px; }
QPushButton#pillButton:hover { background: #f2faf8; border-color: #79bdb5; }
QRadioButton#translationToggle { background: #ffffff; border: 1px solid #d5dddf; border-radius: 15px; padding: 6px 14px; font-size: 12px; }
QRadioButton#translationToggle:checked { background: #009b8c; border-color: #009b8c; color: white; font-weight: 700; }
QFrame#actionCard { background: #ffffff; border: 0; border-radius: 18px; }
QLabel#readyStatus { color: #263236; font-size: 12px; font-weight: 600; }
QLabel#outputPath { color: #4d595e; padding: 4px 8px; }
QCheckBox#languageChip { background: #ffffff; border: 1px solid #d7dfe1; border-radius: 13px; padding: 4px 8px; spacing: 4px; font-size: 11px; }
QCheckBox#languageChip:checked { background: #009b8c; border-color: #009b8c; color: white; font-weight: 700; }
QLabel#logTitle { color: #334044; font-size: 12px; font-weight: 600; padding-left: 4px; }
QSplitter::handle { background: transparent; width: 14px; }
"""


def ensure_qt_license() -> bool:
    """Activate the advanced edition without importing Tk into the Qt build."""
    config = license_manager.load_config()
    if not config.get("license", {}).get("enabled", False):
        return True
    valid, _message = license_manager.local_license_valid(config)
    if valid:
        return True
    while True:
        key, accepted = QInputDialog.getText(
            None,
            "软件激活",
            "请输入进阶版激活码：",
            QLineEdit.EchoMode.Normal,
        )
        if not accepted:
            return False
        key = key.strip()
        if not key:
            QMessageBox.warning(None, "激活码为空", "请输入有效激活码。")
            continue
        try:
            valid, message = license_manager.activate_license(key, config)
        except Exception as exc:
            QMessageBox.critical(None, "激活失败", f"无法连接授权服务器：{exc}")
            return False
        if valid:
            QMessageBox.information(None, "激活成功", "授权已绑定当前电脑。")
            return True
        QMessageBox.critical(None, "激活失败", message)


def main() -> int:
    if os.name == "nt":
        os.environ.setdefault("QT_ENABLE_HIGHDPI_SCALING", "1")
    app = QApplication(sys.argv)
    app.setApplicationName("SubFlow")
    app.setStyle("Fusion")
    app.setStyleSheet(STYLE)
    if not ensure_qt_license():
        return 0
    window = ProMaxQt()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
