"""Review flagged translations and remux only their already-confirmed text tracks."""
from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import Qt, QThreadPool, Signal
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QDialog, QHBoxLayout,
    QHeaderView, QLabel, QMessageBox, QPlainTextEdit, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QProgressBar)

from qt_workers import FunctionWorker, MuxProgressTracker
import subtitle_tool_core as core
import translation_remux
import translation_review


class TranslationReviewDialog(QDialog):
    message = Signal(str)

    def __init__(self, parent, output):
        super().__init__(parent)
        self.output = str(output)
        _manifest, self.reports = translation_remux.load_bound_reports(output)
        self.edits = {}
        self._shown_report = -1
        self._shown_row = -1
        self._worker = None
        self._operation_succeeded = False
        self._cancel = threading.Event()
        self.remuxed = False
        self.mux_tracker=MuxProgressTracker(Path(output).name)
        self.setWindowTitle('检查 / 修改翻译')
        self.setModal(True)
        self.resize(1020, 680)
        root = QVBoxLayout(self)
        note = QLabel('影片已经完成。这里列出需要检查的译文；修改后重新封装影片，保留原来的时间轴。')
        note.setWordWrap(True)
        root.addWidget(note)
        self.languages = QComboBox()
        for path, report in self.reports:
            language = report['target_language']
            label = core.LANGUAGES.get(language, ('', '', language))[2]
            self.languages.addItem(f'{label} · {len(report["issues"])} 条记录')
        root.addWidget(self.languages)
        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(['序号', '时间', '原文', '译文', '问题 / 状态'])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.table.setWordWrap(True)
        root.addWidget(self.table, 1)
        root.addWidget(QLabel('修改选中这一条的译文：'))
        self.editor = QPlainTextEdit()
        self.editor.setMaximumHeight(110)
        root.addWidget(self.editor)
        self.status = QLabel('原文、时间码和其他字幕不会被修改。')
        self.status.setWordWrap(True)
        root.addWidget(self.status)
        self.mux_label=QLabel('')
        self.mux_label.setStyleSheet('color:#9a570c;font-size:11px')
        root.addWidget(self.mux_label)
        self.mux_bar=QProgressBar()
        self.mux_bar.setRange(0,100)
        self.mux_bar.setValue(0)
        self.mux_bar.setFixedHeight(16)
        self.mux_bar.setStyleSheet('QProgressBar{border:0;border-radius:5px;min-height:0px;max-height:16px;padding:0px;background:#f6e8d5;text-align:center;color:#633c12}'
            'QProgressBar::chunk{border-radius:5px;background:#ee982d}')
        self.mux_label.hide()
        self.mux_bar.hide()
        root.addWidget(self.mux_bar)
        self.message.connect(self._mux_message)
        actions = QHBoxLayout()
        actions.addStretch()
        self.save_button = QPushButton('保存修改并重新封装')
        self.close_button = QPushButton('关闭')
        actions.addWidget(self.save_button)
        actions.addWidget(self.close_button)
        root.addLayout(actions)
        self.languages.currentIndexChanged.connect(self._show_report)
        self.table.currentCellChanged.connect(self._select_row)
        self.save_button.clicked.connect(self.save_and_remux)
        self.close_button.clicked.connect(self.reject)
        self._show_report(0)

    def _flush_editor(self):
        if self._shown_report < 0 or self._shown_row < 0: return
        path, report = self.reports[self._shown_report]
        issue = report['issues'][self._shown_row]
        text = self.editor.toPlainText()
        changes = self.edits.setdefault(str(path), {})
        if text != issue['translation']: changes[issue['index']] = text
        else: changes.pop(issue['index'], None)
        self.table.item(self._shown_row, 3).setText(text)

    def _show_report(self, index):
        self._flush_editor()
        self._shown_report, self._shown_row = index, -1
        path, report = self.reports[index]
        changes = self.edits.get(str(path), {})
        self.table.blockSignals(True)
        self.table.setRowCount(len(report['issues']))
        for row, issue in enumerate(report['issues']):
            status = {'resolved': '已修改', 'user_approved': '用户已确认保留'}.get(issue['status'], '')
            values = [str(issue['index']), f'{issue["start"]}\n{issue["end"]}', issue['source'],
                      changes.get(issue['index'], issue['translation']),
                      '\n'.join(filter(None, (status, issue.get('reason', ''))))]
            for column, value in enumerate(values):
                self.table.setItem(row, column, QTableWidgetItem(value))
        self.table.resizeRowsToContents()
        self.table.blockSignals(False)
        self.editor.clear()
        if report['issues']:
            self.table.setCurrentCell(0, 0)
            self._select_row(0, 0, -1, -1)

    def _select_row(self, row, _column, _previous_row, _previous_column):
        if row == self._shown_row: return
        self._flush_editor()
        self._shown_row = row
        if row >= 0:
            self.editor.setPlainText(self.table.item(row, 3).text())

    def save_and_remux(self):
        if self._worker is not None: return
        self._flush_editor()
        try:
            # Validate the output association again before saving an edit.
            translation_remux.load_bound_reports(self.output)
            for index, (path, report) in enumerate(self.reports):
                updates = self.edits.get(str(path), {})
                if not updates: continue
                try:
                    updated = translation_review.save_corrections(path, updates)
                except translation_review.QualityWarningError as exc:
                    text = '\n'.join(f'第 {item["index"]} 条：{item["reason"]}' for item in exc.issues)
                    answer = QMessageBox.question(self, '修改后仍有提醒',
                        text + '\n\n是否仍按你填写的译文保存并重新封装？',
                        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                        QMessageBox.StandardButton.No)
                    if answer != QMessageBox.StandardButton.Yes: return
                    updated = translation_review.save_corrections(path, updates, allow_quality_warning=True)
                self.reports[index] = (path, updated)
                self.edits[str(path)] = {}
        except Exception as exc:
            QMessageBox.warning(self, '未能保存修改', str(exc) + '\n原成品未改变。')
            return
        self._shown_row = -1
        self._show_report(self.languages.currentIndex())
        self._cancel.clear()
        self._operation_succeeded = False
        self.status.setText('译文已保存，正在重新封装；原成品在检查通过前保留。')
        self.message.emit('正在准备重新封装；原成品保留。')
        self.languages.setEnabled(False)
        self.table.setEnabled(False)
        self.editor.setEnabled(False)
        self.save_button.setEnabled(False)
        self.close_button.setText('停止重新封装')
        worker = FunctionWorker(lambda signals:translation_remux.remux_corrected_output(
            self.output, signals.message.emit, self._cancel))
        self._worker = worker
        worker.signals.message.connect(self.message)
        worker.signals.result.connect(self._remux_done)
        worker.signals.error.connect(self._remux_error)
        worker.signals.finished.connect(self._remux_finished)
        QThreadPool.globalInstance().start(worker)

    def _remux_done(self, _output):
        self._operation_succeeded = True
        self.remuxed = True
        self._mux_message('修改后的译文已写入成品：'+str(_output))
        self.status.setText('修改后的译文已写入成品。')

    def _remux_error(self, message):
        self.message.emit('重新封装未完成：'+message+'；原成品保留，修改已保存。')
        self.status.setText('重新封装未完成，原成品保留；修改已保存，可点击按钮重试。')
        QMessageBox.warning(self, '重新封装未完成', message)

    def _remux_finished(self):
        if self._cancel.is_set() and not self._operation_succeeded:
            self.message.emit('已停止重新封装，原成品保留；修改已保存。')
            self.status.setText('已停止重新封装，原成品保留；修改已保存，可重试。')
        self._worker = None
        self.languages.setEnabled(True)
        self.table.setEnabled(True)
        self.editor.setEnabled(True)
        self.save_button.setEnabled(True)
        self.close_button.setText('关闭')

    def _mux_message(self, message):
        self.mux_tracker.update(message)
        if not self.mux_tracker.visible: return
        self.mux_label.show()
        self.mux_bar.show()
        self.mux_label.setText(self.mux_tracker.label)
        self.mux_bar.setRange(0,0 if self.mux_tracker.busy else 100)
        if not self.mux_tracker.busy: self.mux_bar.setValue(self.mux_tracker.value)

    def reject(self):
        if self._worker is not None:
            self._cancel.set()
            self.status.setText('正在停止重新封装，请稍候；原成品保留。')
            return
        self._flush_editor()
        if any(self.edits.values()):
            answer = QMessageBox.question(self, '还有未保存的修改', '放弃这些修改并关闭？',
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes: return
        super().reject()

    def closeEvent(self, event):
        if self._worker is not None:
            self.reject()
            event.ignore()
            return
        event.ignore()
        self.reject()
