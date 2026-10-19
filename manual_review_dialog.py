# -*- coding: utf-8 -*-
"""Native three-window manual timeline, replacing the old slider-only review."""
from __future__ import annotations
import ctypes, html, os, shutil, tempfile, time
from pathlib import Path
from PySide6.QtCore import Qt, QTimer, QThreadPool, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QApplication, QDialog, QDoubleSpinBox, QFrame,
    QHBoxLayout, QLabel, QMessageBox, QPushButton, QComboBox,
    QScrollArea, QTabWidget, QVBoxLayout, QWidget)
import manual_review_prepare
from manual_timeline_widget import ManualTimelineWidget
from qt_workers import FunctionWorker

class WindowsWavPlayer:
    """Play the review WAV through the Windows system audio path."""
    def __init__(self, path: str, alias: str) -> None:
        self.alias = alias
        self.winmm = ctypes.windll.winmm
        self.closed = False
        self._call(f'open "{path}" type waveaudio alias {alias}')
        try:
            self._call(f"set {alias} time format milliseconds")
        except Exception:
            self.close()
            raise

    def _call(self, command: str) -> str:
        result = ctypes.create_unicode_buffer(256)
        code = self.winmm.mciSendStringW(command, result, 256, 0)
        if code:
            error = ctypes.create_unicode_buffer(256)
            self.winmm.mciGetErrorStringW(code, error, 256)
            raise RuntimeError(f"{command}: {error.value} ({code})")
        return result.value

    def position(self) -> int:
        return int(self._call(f"status {self.alias} position"))

    def duration(self) -> int:
        return int(self._call(f"status {self.alias} length"))

    def is_playing(self) -> bool:
        return self._call(f"status {self.alias} mode") == "playing"

    def play(self) -> None:
        if self.position() >= self.duration() - 100:
            self.set_position(0)
        self._call(f"play {self.alias}")

    def pause(self) -> None:
        if self.is_playing():
            self._call(f"pause {self.alias}")

    def set_position(self, position: int) -> None:
        resume = self.is_playing()
        bounded = max(0, min(int(position), self.duration()))
        self._call(f"seek {self.alias} to {bounded}")
        if resume:
            self.play()

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._call(f"close {self.alias}")



class TitleBar(QWidget):
    def __init__(self, dialog):
        super().__init__()
        self.dialog = dialog
        self.drag_origin = None
        layout = QHBoxLayout(self)
        layout.setContentsMargins(3,0,3,0)
        label = QLabel("手动字幕对齐")
        label.setStyleSheet("font-size:17px;font-weight:600;color:#163c3b")
        layout.addWidget(label,1)
        close = QPushButton("×")
        close.setFixedSize(32,30)
        close.setStyleSheet("QPushButton{border:0;border-radius:7px;font-size:21px;color:#526865} QPushButton:hover{background:#e9b8b4;color:#7d2424}")
        close.clicked.connect(dialog.close)
        layout.addWidget(close)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.drag_origin = event.globalPosition().toPoint() - self.dialog.pos()

    def mouseMoveEvent(self, event):
        if self.drag_origin is not None and event.buttons() & Qt.MouseButton.LeftButton:
            self.dialog.move(event.globalPosition().toPoint() - self.drag_origin)

    def mouseReleaseEvent(self, event):
        self.drag_origin = None


class ManualRegionPage(QWidget):
    confirmedChanged = Signal(bool)

    def __init__(self, clip: dict, index: int, temp_dir: str):
        super().__init__()
        self.clip = clip
        self.offset = float(clip.get('initial_offset', 0.0))
        self.selected = -1
        self.manual_selection = False
        self.locked = False
        self.speed = 1.0
        self.index = index
        self.temp_dir = temp_dir
        self.players = {}
        try:
            for speed, suffix in ((1.0, ''), (0.5, '.half')):
                source = Path(clip['audio'] if speed == 1.0 else clip['slow_audio'])
                if not source.is_file(): raise FileNotFoundError(f'核听音频不存在：{source}')
                target = Path(temp_dir) / f'r{index}{suffix}.wav'
                shutil.copyfile(source, target)
                self.players[speed] = WindowsWavPlayer(
                    str(target), f'sfmt_{os.getpid()}_{time.time_ns()}_{index}_{int(speed*10)}')
        except Exception:
            self.close_audio()
            raise
        root = QVBoxLayout(self)
        root.setContentsMargins(4,3,4,3)
        root.setSpacing(3)
        name = ('前段','中段','后段')[index]
        self.heading = QLabel(f'{name} · 影片 {clip["movie_start"]:.0f} 秒起 · 60 秒音频')
        self.heading.setStyleSheet('font-weight:600;color:#17433f')
        root.addWidget(self.heading)
        controls = QHBoxLayout()
        self.play_button = QPushButton('播放 / 暂停')
        self.play_button.setFixedWidth(76)
        self.play_button.clicked.connect(self.toggle)
        controls.addWidget(self.play_button)
        for text,delta in (('后退 5 秒',-5),('前进 5 秒',5)):
            button = QPushButton(text)
            button.clicked.connect(lambda _=False,d=delta:self.seek(self.current()+d))
            controls.addWidget(button)
        self.speed_button = QPushButton('1× / 0.5× 慢放')
        self.speed_button.setFixedWidth(144)
        self.speed_button.clicked.connect(self.switch_speed)
        controls.addWidget(self.speed_button)
        self.clock = QLabel('0.0 / 60 秒')
        # The clock must not resize its row when digits change during playback.
        self.clock.setFixedWidth(96)
        controls.addWidget(self.clock)
        self.prev_page = QPushButton('上一页')
        self.next_page = QPushButton('下一页')
        self.prev_page.clicked.connect(lambda:self.change_half(0))
        self.next_page.clicked.connect(lambda:self.change_half(30))
        controls.addWidget(self.prev_page)
        controls.addWidget(self.next_page)
        controls.addStretch()
        root.addLayout(controls)
        adjust = QHBoxLayout()
        adjust.addWidget(QLabel('字幕偏移'))
        self.spin = QDoubleSpinBox()
        self.spin.setRange(-20,20)
        self.spin.setDecimals(2)
        self.spin.setSingleStep(.1)
        self.spin.setSuffix(' 秒')
        self.spin.setFixedWidth(96)
        self.spin.setValue(self.offset)
        self.spin.valueChanged.connect(self.changed)
        adjust.addWidget(self.spin)
        self.edit_buttons = []
        for amount in (-1,-.5,-.1,.1,.5,1):
            button = QPushButton(f'{amount:+g}')
            button.setFixedWidth(47)
            button.clicked.connect(lambda _=False,a=amount:self.spin.setValue(self.spin.value()+a))
            adjust.addWidget(button)
            self.edit_buttons.append(button)
        reset = QPushButton('回到初值')
        reset.setFixedWidth(74)
        reset.clicked.connect(lambda:self.spin.setValue(float(self.clip.get('initial_offset',0))))
        adjust.addWidget(reset)
        self.edit_buttons.append(reset)
        adjust.addStretch()
        root.addLayout(adjust)
        self.timeline = ManualTimelineWidget(clip)
        self.timeline.offset = self.offset
        self.timeline.offsetDragged.connect(self.spin.setValue)
        self.timeline.seekRequested.connect(self.seek)
        self.timeline.cueClicked.connect(self.clicked_cue)
        root.addWidget(self.timeline,1)
        self.cue_card = QLabel('<b>点击字幕块查看全文</b><br>播放时自动跟随播放头。')
        self.cue_card.setTextFormat(Qt.TextFormat.RichText)
        self.cue_card.setWordWrap(True)
        self.cue_card.setMinimumHeight(62)
        self.cue_card.setStyleSheet('background:#e9f4f1;border-radius:9px;padding:6px;font-size:13px;')
        confirmation = QHBoxLayout()
        confirmation.addStretch()
        self.confirm_button = QPushButton('确认本段偏移')
        self.confirm_button.clicked.connect(self.toggle_confirmation)
        confirmation.addWidget(self.confirm_button)
        root.addLayout(confirmation)
        self.update_page_buttons()

    def replace_clip(self, clip):
        """Open the replacement first so failed audio never discards this page."""
        players = {}
        stamp = time.time_ns()
        try:
            for speed, suffix in ((1.0, ''), (0.5, '.half')):
                source = Path(clip['audio'] if speed == 1.0 else clip['slow_audio'])
                target = Path(self.temp_dir) / f'r{self.index}-{stamp}{suffix}.wav'
                shutil.copyfile(source, target)
                players[speed] = WindowsWavPlayer(
                    str(target), f'sfmt_{os.getpid()}_{stamp}_{self.index}_{int(speed*10)}')
        except Exception:
            for player in players.values():
                try: player.close()
                except RuntimeError: pass
            raise
        self.close_audio()
        self.players = players
        self.clip = clip
        self.offset = float(clip.get('initial_offset', 0.0))
        self.selected = -1
        self.manual_selection = False
        self.locked = False
        self.speed = 1.0
        self.spin.blockSignals(True)
        self.spin.setValue(self.offset)
        self.spin.blockSignals(False)
        self.spin.setEnabled(True)
        for button in self.edit_buttons: button.setEnabled(True)
        self.confirm_button.setText('确认本段偏移')
        self.speed_button.setText('1× / 0.5× 慢放')
        self.timeline.evidence = clip
        self.timeline.offset = self.offset
        self.timeline.position = 0.0
        self.timeline.selected = -1
        self.timeline.drag = None
        self.timeline.cue_rects = []
        self.timeline.set_view_start(0)
        self.timeline.setEnabled(True)
        name = ('前段', '中段', '后段')[self.index]
        self.heading.setText(f'{name} · 影片 {clip["movie_start"]:.0f} 秒起 · 60 秒音频')
        self.show_selected(-1)
        self.refresh()
        self.update_page_buttons()
        self.confirmedChanged.emit(False)

    def current(self):
        return self.players[self.speed].position()/1000*self.speed

    def is_playing(self):
        return self.players[self.speed].is_playing()

    def pause(self):
        for player in self.players.values(): player.pause()

    def seek(self, second):
        second=max(0.0,min(60.0,float(second)))
        self.players[self.speed].set_position(second/self.speed*1000)
        self.manual_selection=False
        self.follow_position(second)
        self.refresh()

    def toggle(self):
        player=self.players[self.speed]
        if player.is_playing():
            player.pause()
            self.refresh()
            return
        if self.current()>=59.9: self.seek(0)
        self.manual_selection=False
        # Show the playhead's subtitle now, before the first audio timer tick.
        self.follow_position(self.current())
        player.play()
        self.refresh()

    def switch_speed(self):
        current=self.current()
        playing=self.is_playing()
        self.players[self.speed].pause()
        self.speed=.5 if self.speed==1 else 1.0
        self.players[self.speed].set_position(current/self.speed*1000)
        if playing: self.players[self.speed].play()
        self.speed_button.setText(f'当前 {self.speed:g}×｜切换速度')
        self.refresh()

    def change_half(self, start):
        self.timeline.set_view_start(start)
        self.update_page_buttons()
        self.seek(start)

    def update_page_buttons(self):
        self.prev_page.setEnabled(self.timeline.view_start>=30)
        self.next_page.setEnabled(self.timeline.view_start<30)

    def changed(self, value):
        self.offset=float(value)
        self.timeline.offset=self.offset
        self.timeline.update()
        if not self.manual_selection: self.follow_position(self.current())

    def active_index(self, second):
        movie_time=self.clip['movie_start']+second
        active=[(row['start'],index) for index,row in enumerate(self.clip['rows'])
                if row['start']+self.offset<=movie_time<row['end']+self.offset]
        return max(active)[1] if active else -1

    def show_selected(self, index):
        self.selected=index
        self.timeline.selected=index
        if index<0:
            self.cue_card.setText('<b>这一刻没有字幕</b>')
        else:
            cue=self.clip['rows'][index]
            original=html.escape(cue['text']).replace('\n','<br>')
            zh=html.escape(cue.get('zh') or '未提供')
            self.cue_card.setText(f'<b>下载字幕</b><br><span style="font-size:17px">{original}</span>'
                                  f'<br><span style="color:#526d6b">中文辅助：{zh}</span>')
        self.timeline.update()

    def follow_position(self, second):
        if not (self.timeline.view_start<=second<self.timeline.view_start+30):
            self.timeline.set_view_start(second)
            self.update_page_buttons()
        index=self.active_index(second)
        if index!=self.selected or self.manual_selection:
            self.manual_selection=False
            self.show_selected(index)

    def clicked_cue(self, index):
        # During playback the card belongs to the audio clock. Manual inspection
        # is available only after the user pauses.
        if self.is_playing(): return
        self.manual_selection=True
        self.show_selected(index)

    def refresh(self):
        second=min(60.0,self.current())
        playing=self.is_playing()
        clock=f'{second:.1f} / 60 秒'
        if self.clock.text()!=clock: self.clock.setText(clock)
        button='暂停' if playing else '播放'
        if self.play_button.text()!=button: self.play_button.setText(button)
        moved=self.timeline.position!=second
        self.timeline.position=second
        if playing or not self.manual_selection:
            self.follow_position(second)
        if moved: self.timeline.update()

    def toggle_confirmation(self):
        if not self.locked:
            count=sum(row['end']+self.offset>self.clip['movie_start'] and
                      row['start']+self.offset<self.clip['movie_start']+60
                      for row in self.clip['rows'])
            if count<3:
                QMessageBox.information(self,'证据不足','本窗少于 3 条可见字幕，不能确认此段。')
                return
            self.pause()
            self.locked=True
        else:
            self.locked=False
        self.spin.setEnabled(not self.locked)
        for button in self.edit_buttons: button.setEnabled(not self.locked)
        self.timeline.setEnabled(not self.locked)
        self.confirm_button.setText(f'已确认 {self.offset:+.2f} 秒｜重新调整' if self.locked else '确认本段偏移')
        self.confirmedChanged.emit(self.locked)

    def close_audio(self):
        for player in self.players.values():
            try: player.close()
            except RuntimeError: pass
        self.players.clear()


class ManualSubtitleReviewDialog(QDialog):
    def __init__(self, parent: QWidget, evidence: dict):
        super().__init__(parent)
        self.evidence=evidence
        self.confirmed_offsets=None
        self.confirmed_offset=None
        self.requested_audio_id=None
        self.pages=[]
        self.region_statuses=[]
        self.region_buttons=[]
        self.replace_buttons=[]
        self._window_worker=None
        self._evidence_save_failed=False
        self._audio_released=False
        self._audio_stage=tempfile.TemporaryDirectory(prefix='sf_manual_timeline_')
        self.setModal(True)
        self.setWindowTitle('SubFlow 手动字幕对齐')
        self.setWindowFlags(Qt.WindowType.Dialog|Qt.WindowType.FramelessWindowHint)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setStyleSheet('QWidget{font-size:12px}'
            ' QPushButton{font-size:12px;padding:4px 9px;min-height:18px}'
            ' QPushButton:pressed{padding:4px 9px;border-width:1px}')
        visible_main=next((window for window in QApplication.topLevelWindows()
                           if window.isVisible() and window.title().startswith('SubFlow v')),None)
        reference=visible_main.geometry() if visible_main else parent.geometry()
        self.resize(round(reference.width()*.85),round(reference.height()*.85))
        self.move(reference.center()-self.rect().center())
        root=QVBoxLayout(self)
        root.setContentsMargins(9,9,9,9)
        shell=QFrame()
        shell.setObjectName('shell')
        shell.setStyleSheet('QFrame#shell{background:#f7faf9;border:1px solid #d3e3df;border-radius:15px}')
        # A graphics effect on the whole translucent shell recomposites every
        # timeline tick, including controls and text. Keep the rounded border
        # without the effect so playback only repaints the timeline itself.
        root.addWidget(shell)
        content=QVBoxLayout(shell)
        content.setContentsMargins(10,5,10,6)
        content.setSpacing(3)
        content.addWidget(TitleBar(self))
        note=QLabel(f"{Path(evidence['source']).name}｜前中后三段各 60 秒；播放时字幕跟随，暂停后点击字幕块看全文。声音块仅供参考，请听原声确认。")
        note.setWordWrap(True)
        note.setStyleSheet('font-size:11px;color:#526d6b')
        content.addWidget(note)
        audio_row=QHBoxLayout()
        audio_row.addWidget(QLabel('参照音轨'))
        self.audio_combo=QComboBox()
        self.audio_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.audio_combo.setMinimumContentsLength(20)
        self.audio_combo.setMinimumWidth(0)
        choices=evidence.get('audio_choices') or [dict(id=evidence.get('validation_audio_id'),
            label=f"轨{evidence.get('validation_audio_id','?')} · {evidence.get('audio_codec','')}（当前）",enabled=True)]
        for choice in choices:
            self.audio_combo.addItem(choice['label'],choice['id'])
            self.audio_combo.setItemData(self.audio_combo.count()-1,choice['label'],Qt.ItemDataRole.ToolTipRole)
            item=self.audio_combo.model().item(self.audio_combo.count()-1)
            if item is not None: item.setEnabled(choice.get('enabled',True))
        current=self.audio_combo.findData(evidence.get('validation_audio_id'))
        self.audio_combo.setCurrentIndex(current)
        audio_row.addWidget(self.audio_combo,1)
        self.audio_switch=QPushButton('切换音轨')
        self.audio_switch.setToolTip('切换后重新准备三段音频，三段偏移全部需要重新确认。')
        self.audio_switch.clicked.connect(self.request_audio_switch)
        audio_row.addWidget(self.audio_switch)
        self.audio_combo.currentIndexChanged.connect(self._audio_selection_changed)
        content.addLayout(audio_row)
        self.tabs=QTabWidget()
        self.tabs.setStyleSheet('QTabWidget::pane{border:1px solid #d3e3df;border-radius:9px;background:#f7faf9}'
            ' QTabBar::tab{padding:7px 14px;border-top-left-radius:7px;border-top-right-radius:7px;background:#e8f1ef}'
            ' QTabBar::tab:selected{background:#d2e9e5}')
        content.addWidget(self.tabs,1)
        try:
            for index,clip in enumerate(evidence['clips']):
                page=ManualRegionPage(clip,index,self._audio_stage.name)
                page.confirmedChanged.connect(self.update_confirm_button)
                page.confirm_button.hide()
                wrapper=QWidget()
                tab_layout=QVBoxLayout(wrapper)
                tab_layout.setContentsMargins(0,0,0,0)
                tab_layout.setSpacing(3)
                # Playback controls and both tracks must be visible together.
                # A scrolling page hid the subtitle lane in shorter dialogs.
                tab_layout.addWidget(page,1)
                # Only lengthy subtitle text may scroll, independently of the tracks.
                card_scroll=QScrollArea()
                card_scroll.setWidgetResizable(True)
                card_scroll.setFrameShape(QFrame.Shape.NoFrame)
                card_scroll.setFixedHeight(68)
                card_scroll.setWidget(page.cue_card)
                tab_layout.addWidget(card_scroll)
                region_actions=QHBoxLayout()
                region_status=QLabel(self._region_status_text(page))
                region_status.setWordWrap(True)
                region_button=QPushButton('确认本段偏移')
                region_button.clicked.connect(page.toggle_confirmation)
                page.confirmedChanged.connect(
                    lambda locked,p=page,b=region_button,label=region_status:
                    (b.setText('重新调整本段' if locked else '确认本段偏移'),
                     label.setText(self._region_status_text(p))))
                region_actions.addWidget(region_status,1)
                replace_button=QPushButton('换同区片段')
                replace_button.clicked.connect(lambda _checked=False,i=index:self.replace_region(i))
                replace_button.setVisible(bool(evidence.get('region_options')))
                region_actions.addWidget(replace_button)
                region_actions.addWidget(region_button)
                tab_layout.addLayout(region_actions)
                self.pages.append(page)
                self.region_statuses.append(region_status)
                self.region_buttons.append(region_button)
                self.replace_buttons.append(replace_button)
                self.tabs.addTab(wrapper,('前段','中段','后段')[index])
        except Exception:
            self._release_audio()
            raise
        self.tabs.currentChanged.connect(self.on_tab_changed)
        actions=QHBoxLayout()
        actions.addStretch()
        self.confirm_button=QPushButton('确认三点修正并继续')
        self.confirm_button.setEnabled(False)
        self.confirm_button.clicked.connect(self.confirm)
        cancel=QPushButton('取消，换字幕')
        cancel.clicked.connect(self.reject)
        actions.addWidget(self.confirm_button)
        actions.addWidget(cancel)
        content.addLayout(actions)
        self.timer=QTimer(self)
        self.timer.timeout.connect(lambda:self.pages[self.tabs.currentIndex()].refresh())
        self.timer.start(80)
        self._audio_selection_changed()
        self.ensurePolished()
        for page in self.pages:
            page.ensurePolished()
            page.setMinimumHeight(page.layout().minimumSize().height())
        root.activate()
        # Account for wrapped instructions as well as the controls. Keep the
        # dialog inside the main window's size and the current screen's bounds.
        minimum=self.minimumSizeHint()
        screen=QApplication.screenAt(reference.center()) or self.screen()
        bounds=screen.availableGeometry()
        max_width=min(reference.width(),bounds.width())
        max_height=min(reference.height(),bounds.height())
        width=min(max_width,max(self.width(),minimum.width()))
        height=max(minimum.height(),root.totalHeightForWidth(width))
        self.setMinimumSize(minimum.width(),height)
        self.setMaximumSize(max_width,max_height)
        self.resize(width,min(max_height,max(self.height(),height)))
        center=reference.center()-self.rect().center()
        self.move(max(bounds.left(),min(center.x(),bounds.right()-self.width()+1)),
                  max(bounds.top(),min(center.y(),bounds.bottom()-self.height()+1)))

    def _audio_selection_changed(self,*_):
        self.audio_switch.setEnabled(self._window_worker is None and
            self.audio_combo.currentData()!=self.evidence.get('validation_audio_id'))
        self.update_confirm_button()

    def request_audio_switch(self):
        if self._window_worker is not None or self._audio_released: return
        selected=self.audio_combo.currentData()
        if selected is None or selected==self.evidence.get('validation_audio_id'): return
        self.requested_audio_id=selected
        self.confirmed_offsets=None
        self.done(2)

    def on_tab_changed(self,index):
        for i,page in enumerate(self.pages):
            if i!=index: page.pause()
        if 0<=index<len(self.pages): self.pages[index].refresh()

    def update_confirm_button(self,_confirmed=None):
        self.confirm_button.setEnabled(self._window_worker is None and not self._evidence_save_failed and
            self.audio_combo.currentData()==self.evidence.get('validation_audio_id') and
            len(self.pages)==3 and all(p.locked for p in self.pages))

    @staticmethod
    def _region_status_text(page):
        if page.locked: return f'已确认 {page.offset:+.2f} 秒'
        warning = page.clip.get('selection_warning', '')
        return str(warning) if warning else '本段尚未确认'

    def replace_region(self,index):
        if self._window_worker is not None or self._audio_released: return
        for page in self.pages: page.pause()
        previous = self.evidence['clips'][index]
        self.region_statuses[index].setText('正在准备同区片段…')
        self.tabs.setEnabled(False)
        for button in self.replace_buttons: button.setEnabled(False)
        worker=FunctionWorker(lambda signals:manual_review_prepare.replace_review_window(
            self.evidence,index,log=signals.message.emit))
        self._window_worker=worker
        self._audio_selection_changed()
        self.update_confirm_button()
        worker.signals.result.connect(lambda clip:self._replacement_ready(index,previous,clip))
        worker.signals.error.connect(lambda message:self._replacement_failed(index,message))
        worker.signals.finished.connect(self._replacement_finished)
        QThreadPool.globalInstance().start(worker)

    def _replacement_ready(self,index,previous,clip):
        if self._audio_released: return
        try:
            self.pages[index].replace_clip(clip)
        except Exception as exc:
            # The preparation may have committed a new evidence point. Keep it
            # paired with the page's old audio if opening that new audio failed.
            self.evidence['clips'][index]=previous
            if self.evidence.get('review_dir'):
                try: manual_review_prepare._save_evidence(self.evidence)
                except OSError:
                    self._evidence_save_failed=True
                    self._replacement_failed(index,'原片段已恢复，但核听记录无法保存；请关闭后重新打开人工核听。')
                    return
            self._replacement_failed(index,f'无法播放新片段，已保留原片段：{exc}')

    def _replacement_failed(self,index,message):
        if self._audio_released: return
        self.region_statuses[index].setText(self._region_status_text(self.pages[index]))
        QMessageBox.information(self,'无法更换片段',message)

    def _replacement_finished(self):
        self._window_worker=None
        self._audio_selection_changed()
        if self._audio_released: return
        self.tabs.setEnabled(True)
        for button in self.replace_buttons: button.setEnabled(True)
        self.update_confirm_button()

    def confirm(self):
        if self.audio_combo.currentData()!=self.evidence.get('validation_audio_id'):
            QMessageBox.information(self,'尚未切换参照','请先点击切换音轨，重新准备并确认三段偏移。')
            return
        if not all(page.locked for page in self.pages):
            QMessageBox.information(self,'尚未完成','请分别确认前、中、后三段。')
            return
        offsets=[page.offset for page in self.pages]
        reason=manual_review_prepare.manual_offsets_rejection(self.evidence,offsets)
        if reason:
            QMessageBox.warning(self,'不能确认该偏移',reason)
            return
        self.confirmed_offsets=offsets
        self.confirmed_offset=offsets[0] if len(set(offsets))==1 else None
        self.accept()

    def _release_audio(self):
        if self._audio_released: return
        self._audio_released=True
        timer=getattr(self,'timer',None)
        if timer is not None: timer.stop()
        try:
            for page in self.pages: page.close_audio()
        finally:
            self._audio_stage.cleanup()

    def closeEvent(self,event):
        self._release_audio()
        super().closeEvent(event)

    def done(self,result):
        self._release_audio()
        super().done(result)
