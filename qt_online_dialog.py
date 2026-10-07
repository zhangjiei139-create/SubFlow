# -*- coding: utf-8 -*-
from __future__ import annotations

import webbrowser
from pathlib import Path

from PySide6.QtCore import Qt, QThreadPool
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QRadioButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

import online_subtitles as opensubtitles
import subdl_subtitles as subdl
from subtitle_identity_guard import edition_label, identity_conflict_notice
from qt_workers import FunctionWorker


LANGUAGES = [
    ("en", "英文"), ("zh-cn", "简体中文"), ("zh-tw", "繁体中文"),
    ("ja", "日文"), ("ko", "韩文"), ("es", "西班牙语"),
    ("fr", "法语"), ("de", "德语"), ("ru", "俄语"),
]

PROVIDERS = {
    "opensubtitles": {
        "name": "OpenSubtitles", "service": opensubtitles,
        "setting": "opensubtitles_api_key", "label": "OpenSubtitles API Key",
        "url": "https://www.opensubtitles.com/consumers",
    },
    "subdl": {
        "name": "SubDL", "service": subdl,
        "setting": "subdl_api_key", "label": "SubDL API Key",
        "url": "https://subdl.com/panel/api",
    },
}



class OnlineSubtitleDialog(QDialog):
    def __init__(self, parent: QWidget, video_path: str, on_download) -> None:
        super().__init__(parent)
        self.setWindowTitle("在线查找字幕")
        self.setModal(True)
        self.resize(920, 560)
        self.setMinimumSize(760, 460)
        self.video_path = video_path
        self.on_download = on_download
        self.pool = QThreadPool.globalInstance()
        self._workers: set[FunctionWorker] = set()
        self.results = []
        self.result_provider = ""
        self.provider = ""
        self.identity = opensubtitles.identify_media(video_path)
        settings = opensubtitles.load_settings()
        self.keys = {code: str(settings.get(info["setting"], "")) for code, info in PROVIDERS.items()}
        self._build()
        if not self.provider:
            self._select_provider("opensubtitles")

    def _run_worker(self, function, *, result=None, finished=None) -> None:
        worker = FunctionWorker(function)
        self._workers.add(worker)
        if result:
            worker.signals.result.connect(result)
        worker.signals.error.connect(self._show_error)

        def cleanup() -> None:
            self._workers.discard(worker)
            if finished:
                finished()

        worker.signals.finished.connect(cleanup)
        self.pool.start(worker)

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)
        identity = f"{self.identity.title or '未识别片名'} {self.identity.year}".strip()
        self.identity_label = QLabel(
            f"识别结果：{identity} · 影片规格：{self.identity.specification} · 站点结果按返回顺序显示，下载后验证正文"
        )
        self.identity_label.setObjectName("dialogTitle")
        layout.addWidget(self.identity_label)

        providers = QHBoxLayout()
        providers.addWidget(QLabel("字幕站"))
        self.provider_buttons = {}
        for code, info in PROVIDERS.items():
            button = QRadioButton(info["name"])
            button.toggled.connect(lambda checked, value=code: checked and self._select_provider(value))
            providers.addWidget(button)
            self.provider_buttons[code] = button
        providers.addSpacing(10)
        note = QLabel("一次搜索一个站；无结果时切换另一站，避免重复消耗额度。")
        note.setObjectName("muted")
        providers.addWidget(note, 1)
        layout.addLayout(providers)

        controls = QGridLayout()
        controls.setHorizontalSpacing(8)
        self.key_label = QLabel()
        self.key_edit = QLineEdit()
        self.key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.key_button = QPushButton("获取 Key")
        self.key_button.clicked.connect(self._open_key_page)
        self.verify_button = QPushButton()
        self.verify_button.clicked.connect(self._save_or_verify)
        controls.addWidget(self.key_label, 0, 0)
        controls.addWidget(self.key_edit, 0, 1)
        controls.addWidget(self.key_button, 0, 2)
        controls.addWidget(self.verify_button, 0, 3)
        controls.addWidget(QLabel("字幕语言"), 1, 0)
        self.language = QComboBox()
        for code, label in LANGUAGES:
            self.language.addItem(label, code)
        self.search_button = QPushButton("搜索字幕")
        self.search_button.clicked.connect(self._search)
        controls.addWidget(self.language, 1, 1)
        controls.addWidget(self.search_button, 1, 2)
        controls.setColumnStretch(1, 1)
        layout.addLayout(controls)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["来源", "筛选状态", "字幕版本 / 片源", "语言", "下载量", "评分", "标记"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        layout.addWidget(self.table, 1)

        footer = QHBoxLayout()
        self.status = QLabel("选择字幕站和语言后开始搜索。")
        self.status.setObjectName("muted")
        footer.addWidget(self.status, 1)
        close_button = QPushButton("关闭")
        close_button.clicked.connect(self.reject)
        self.download_button = QPushButton("下载并使用")
        self.download_button.setObjectName("primaryButton")
        self.download_button.setEnabled(False)
        self.download_button.clicked.connect(self._download)
        footer.addWidget(close_button)
        footer.addWidget(self.download_button)
        layout.addLayout(footer)
        self.provider_buttons["opensubtitles"].setChecked(True)

    def _select_provider(self, code: str) -> None:
        if hasattr(self, "key_edit") and self.provider:
            self.keys[self.provider] = self.key_edit.text().strip()
        self.provider = code
        info = PROVIDERS[code]
        self.key_label.setText(info["label"])
        self.key_edit.setText(self.keys.get(code, ""))
        self.verify_button.setText("验证并保存")
        self.results = []
        self.result_provider = ""
        self.table.setRowCount(0)
        self.download_button.setEnabled(False)
        self.status.setText(f"已切换到 {info['name']}，选择语言后开始搜索。")

    def _open_key_page(self) -> None:
        webbrowser.open(PROVIDERS[self.provider]["url"])

    def _save_or_verify(self) -> None:
        key = self.key_edit.text().strip()
        if not key:
            QMessageBox.warning(self, "缺少 API Key", "请先填写 API Key。")
            return
        self.keys[self.provider] = key
        provider = self.provider
        info = PROVIDERS[provider]
        self.verify_button.setEnabled(False)
        self.status.setText(f"正在验证 {info['name']} API Key…")
        validator = opensubtitles.validate_api_key if provider == "opensubtitles" else subdl.validate_token

        def validate(_signals):
            try:
                return validator(key)
            except Exception as exc:
                raise RuntimeError(str(exc).replace(key, "[已隐藏]")) from None

        self._run_worker(
            validate,
            result=lambda quota: self._verified_service(provider, key, quota),
            finished=lambda: self.verify_button.setEnabled(True),
        )

    def _verified_service(self, provider: str, key: str, quota) -> None:
        if self.keys.get(provider) != key:
            return
        info = PROVIDERS[provider]
        try:
            info["service"].save_settings(key)
        except Exception as exc:
            self._show_error(f"保存失败：{str(exc).replace(key, '[已隐藏]')}")
            return
        suffix = f"，当前可用额度：{quota}" if quota is not None else ""
        if provider == self.provider:
            self.status.setText(f"{info['name']} API Key 有效并已保存{suffix}。")

    def _search(self) -> None:
        key = self.key_edit.text().strip()
        if not key:
            QMessageBox.warning(self, "缺少 API Key", "请先填写 API Key。")
            return
        provider = self.provider
        self.keys[provider] = key
        service = PROVIDERS[provider]["service"]
        language = str(self.language.currentData())
        self.table.setRowCount(0)
        self.results = []
        self.result_provider = ""
        self.search_button.setEnabled(False)
        self.download_button.setEnabled(False)
        self.status.setText(f"正在通过 {PROVIDERS[provider]['name']} 搜索字幕…")
        self._run_worker(
            lambda _signals: service.search(key, self.video_path, language),
            result=lambda result: self._searched_service(provider, key, result),
            finished=lambda: self.search_button.setEnabled(True),
        )

    def _searched_service(self, provider: str, key: str, payload) -> None:
        # A successful authenticated search may persist its key. A failed
        # request must never replace a previously working configuration.
        self._verified_service(provider, key, None)
        self._show_results(provider, payload)

    def _show_results(self, provider: str, payload) -> None:
        if provider != self.provider:
            return
        self.identity, self.results, meta = payload
        recognized = f"{self.identity.title or '未识别片名'} {self.identity.year}".strip()
        if self.identity.original_title and self.identity.original_title != self.identity.title:
            recognized = f"{recognized} → {self.identity.original_title}"
        self.identity_label.setText(
            f"识别结果：{recognized} · 影片规格：{self.identity.specification} · 站点结果按返回顺序显示，下载后验证正文"
        )
        self.result_provider = provider
        self.table.setRowCount(len(self.results))
        name = PROVIDERS[provider]["name"]
        for row, item in enumerate(self.results):
            flags = "可信" if item.trusted else ""
            if item.hearing_impaired:
                flags = f"{flags} SDH".strip()
            if item.moviehash_match:
                flags = f"{flags} HASH".strip()
            identity = item.feature_title
            if item.feature_year:
                identity = f"{identity} ({item.feature_year})" if identity else item.feature_year
            candidate_edition = edition_label(
                f"{item.feature_title} {item.release} {item.file_name}",
                unknown="版本未标明",
            )
            release_text = f"[{candidate_edition}] {item.release}"
            release = f"{identity} · {release_text}" if identity else release_text
            values = [name, item.recommendation, release, item.language,
                      str(item.downloads), f"{item.rating:.1f}", flags]
            for column, value in enumerate(values):
                self.table.setItem(row, column, QTableWidgetItem(value))
        if self.results:
            suffix = "，仅显示前 30 条" if meta.truncated else ""
            self.status.setText(f"{name} 获得候选 {meta.total_count} 条，显示 {len(self.results)} 条{suffix}。")
            self.table.selectRow(0)
        else:
            if meta.query_mode == "unresolved-title":
                self.status.setText(
                    "未能可靠识别中文片名的官方英文名，已停止宽泛搜索，避免显示其他影片字幕。"
                )
            else:
                other = "SubDL" if provider == "opensubtitles" else "OpenSubtitles"
                self.status.setText(f"{name} 没有搜索到所选语言字幕，可切换到 {other} 再搜索。")

    def _selection_changed(self) -> None:
        row = self.table.currentRow()
        valid = 0 <= row < len(self.results)
        self.download_button.setEnabled(valid)
        if valid:
            item = self.results[row]
            self.status.setText(
                f"{item.recommendation}：{item.match_reason}。下载后将验证字幕正文。"
            )

    def _download(self) -> None:
        row = self.table.currentRow()
        if not (0 <= row < len(self.results)) or not self.result_provider:
            return
        provider = self.result_provider
        key = self.keys[provider]
        candidate = self.results[row]
        destination = str(Path(self.video_path).with_suffix("")) + "_pro_work"
        self.download_button.setEnabled(False)
        self.status.setText(f"正在从 {PROVIDERS[provider]['name']} 下载字幕…")
        self._run_worker(
            lambda _signals: PROVIDERS[provider]["service"].download(key, candidate, destination),
            result=lambda path: self._download_done(path, candidate),
            finished=self._selection_changed,
        )

    def _download_done(self, path: Path, candidate) -> None:
        self.on_download(str(path), candidate)
        self.accept()

    def _show_error(self, message: str) -> None:
        if notice := identity_conflict_notice(message):
            self.status.setText("影片名称不一致；请确认片头，改正名称后重新添加影片。")
            QMessageBox.warning(self, "影片名称不一致，请先确认", notice)
            return
        self.status.setText(message)
        QMessageBox.critical(self, "在线字幕操作失败", message)
