"""Compact per-profile text styling: independent font, automatic/manual size and color."""
from pathlib import Path
import os
import re
from PySide6.QtCore import Qt, Signal, QEvent, QPoint
from PySide6.QtGui import QColor, QFont, QFontDatabase, QRawFont, QPainter, QPen
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QComboBox, QLineEdit, QToolButton, QPushButton, QFileDialog,
    QColorDialog, QMessageBox, QScrollArea, QWidget, QMenu, QFrame, QListView,
    QAbstractItemView)
from profile_model import language_label
import subtitle_fonts as fonts

_SYSTEM_CHOICES = None
MIN_SIZE, MAX_SIZE = 1, 200
PREVIEW_WIDTH, PREVIEW_HEIGHT = 1920, 1080


def main_window_geometry(fallback):
    from PySide6.QtGui import QGuiApplication
    windows = [window for window in QGuiApplication.topLevelWindows()
               if window.isVisible() and window.title().startswith('SubFlow')]
    return windows[0].geometry() if windows else fallback.window().geometry()


class FontChoiceCombo(QComboBox):
    """A bounded font list with a real, draggable scrollbar on every platform."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.popup = QFrame(self, Qt.Popup | Qt.FramelessWindowHint)
        self.popup.setObjectName('fontChoicePopup')
        self.popup.setAttribute(Qt.WA_TranslucentBackground)
        self.popup.setStyleSheet('''
            QFrame#fontChoicePopup { background: transparent; }
            QFrame#fontChoiceCard { background: white; border: 1px solid #cddeda; border-radius: 12px; }
            QListView { background: white; color: #173f3d; border: 0; outline: 0; font-size: 13px; }
            QListView::item { padding: 6px 8px; min-height: 20px; border-radius: 6px; }
            QListView::item:hover { background: #eef7f5; }
            QListView::item:selected { background: #d9eee9; color: #075f56; }
            QScrollBar:vertical { background: #edf4f1; width: 14px; margin: 0; border-radius: 7px; }
            QScrollBar::handle:vertical { background: #83b4aa; min-height: 36px; border-radius: 7px; }
            QScrollBar::handle:vertical:hover { background: #419d8d; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
        ''')
        outer = QVBoxLayout(self.popup); outer.setContentsMargins(0, 0, 0, 0)
        card = QFrame(); card.setObjectName('fontChoiceCard'); outer.addWidget(card)
        layout = QVBoxLayout(card); layout.setContentsMargins(7, 8, 7, 8)
        self.font_list = QListView()
        self.font_list.setModel(self.model())
        self.font_list.setUniformItemSizes(True)
        self.font_list.setTextElideMode(Qt.ElideRight)
        self.font_list.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.font_list.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOn)
        self.font_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.font_list.clicked.connect(self.choose_font)
        self.font_list.installEventFilter(self)
        layout.addWidget(self.font_list)

    def showPopup(self):
        if self.popup.isVisible():
            self.hidePopup(); return
        box = main_window_geometry(self)
        screen = self.screen().availableGeometry()
        bounds = box.intersected(screen)
        if bounds.isEmpty(): bounds = screen
        width = min(max(1, round(box.width() / 3)), bounds.width())
        row = max(30, self.font_list.sizeHintForRow(0))
        height = min(max(1, bounds.height() - 16), 480, max(90, row * self.count() + 18))
        anchor = self.mapToGlobal(QPoint(0, self.height()))
        x = max(bounds.left(), min(anchor.x(), bounds.right() - width + 1))
        y = anchor.y()
        if y + height > bounds.bottom() + 1:
            y = self.mapToGlobal(QPoint()).y() - height
        y = max(bounds.top(), min(y, bounds.bottom() - height + 1))
        self.popup.setGeometry(x, y, width, height)
        index = self.model().index(self.currentIndex(), self.modelColumn())
        self.font_list.setCurrentIndex(index)
        self.font_list.scrollTo(index, QAbstractItemView.PositionAtCenter)
        self.popup.show()
        self.font_list.setFocus(Qt.PopupFocusReason)

    def hidePopup(self):
        self.popup.hide()
        super().hidePopup()

    def choose_font(self, index):
        if index.isValid():
            self.setCurrentIndex(index.row())
            self.activated.emit(index.row())
        self.hidePopup()

    def eventFilter(self, watched, event):
        if watched is self.font_list and event.type() == QEvent.KeyPress:
            if event.key() == Qt.Key_Escape:
                self.hidePopup(); return True
            if event.key() in (Qt.Key_Return, Qt.Key_Enter):
                self.choose_font(self.font_list.currentIndex()); return True
        return super().eventFilter(watched, event)

    def paintEvent(self, event):
        super().paintEvent(event)
        painter=QPainter(self); painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(QColor('#557c71'),1.6))
        x,y=self.width()-15,self.height()//2
        painter.drawLine(x-4,y-2,x,y+2);painter.drawLine(x,y+2,x+4,y-2)


class FontDialogTitleBar(QWidget):
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.window().windowHandle():
            self.window().windowHandle().startSystemMove()
            event.accept()
        else:
            super().mousePressEvent(event)


FONT_DIALOG_STYLE = '''
QDialog#subtitleFontDialog { background: transparent; }
QFrame#fontDialogCard { background: #f7fbfa; border: 1px solid #d2e1dd; border-radius: 16px; }
QWidget { background: transparent; color: #173f3d; font-family: "Microsoft YaHei UI", "Microsoft YaHei"; font-size: 13px; }
QLabel, QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; border: 0; }
QLabel#fontWindowTitle { font-size: 14px; font-weight: 600; }
QLineEdit, QComboBox { background: white; border: 1px solid #bdd3cd; border-radius: 9px; padding: 6px 9px; min-height: 20px; }
QComboBox { padding-right: 26px; }
QComboBox::drop-down { border: 0; width: 24px; }
QComboBox::down-arrow { image: none; }
QPushButton { background: #edf5f2; border: 1px solid #b9d2ca; border-radius: 10px; padding: 7px 12px; min-height: 20px; }
QPushButton:hover { background: #e0efea; border-color: #7fb3a4; }
QPushButton:pressed { background: #cce4db; padding: 7px 12px; }
QPushButton#fontSaveButton { background: #009b8c; color: white; border: 0; border-radius: 12px; padding: 8px 20px; min-width: 72px; min-height: 24px; font-weight: 600; }
QPushButton#fontSaveButton:hover { background: #008679; }
QPushButton#fontSaveButton:pressed { background: #006d63; }
QToolButton#fontDialogClose { background: transparent; border: 0; border-radius: 8px; font-size: 20px; padding: 0; }
QToolButton#fontDialogClose:hover { background: #e4eeeb; }
QToolButton#spinArrowButton { background: #e3efea; border: 1px solid #b9d2ca; border-radius: 5px; padding: 0; min-width: 22px; min-height: 15px; font-size: 9px; }
QScrollBar:vertical { background: #edf4f1; width: 12px; border-radius: 6px; }
QScrollBar::handle:vertical { background: #8eb5a9; min-height: 30px; border-radius: 6px; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
'''


def system_choices():
    global _SYSTEM_CHOICES
    if _SYSTEM_CHOICES is None:
        roots = [Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts',
                 Path(os.environ.get('LOCALAPPDATA', '')) / 'Microsoft/Windows/Fonts']
        found = []
        for root in roots:
            if not root.is_dir(): continue
            for path in sorted(root.iterdir()):
                if path.suffix.lower() not in {'.ttf', '.otf'}: continue
                try:
                    info = fonts.font_info(path)
                    found.append(dict(info, id='system:' + info['sha256'],
                                      label=info.get('ui_family', info['family']) + ' · ' + path.stem))
                except (OSError, ValueError): pass
        _SYSTEM_CHOICES = found
    return _SYSTEM_CHOICES


class AdaptiveSizeInput(QWidget):
    """No native spin arrows or silent clamping of a typed number."""
    valueChanged = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.estimate = None
        self.estimate_callback = None
        # A loaded value is not a user action.  In particular, old releases
        # automatically persisted 38, so only real edits may make 38 manual.
        self.size_origin = None
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0); layout.setSpacing(3)
        self.edit = QLineEdit('默认')
        self.edit.setMinimumWidth(48); self.edit.setMaximumWidth(72)
        self.edit.setToolTip('默认自动计算；手动字号请输入 1～200。')
        self.edit.setAccessibleName('字幕字号；默认或手动数值')
        self.edit.textEdited.connect(self._edited)
        self.edit.textChanged.connect(lambda _: self.valueChanged.emit())
        arrows = QWidget(); arrow_layout = QVBoxLayout(arrows)
        arrow_layout.setContentsMargins(0, 0, 0, 0); arrow_layout.setSpacing(0)
        self.up = QToolButton(); self.down = QToolButton()
        for button, text, tip in ((self.up, '▲', '增加字号'), (self.down, '▼', '减少字号')):
            button.setObjectName('spinArrowButton'); button.setText(text)
            button.setToolTip(tip); button.setAccessibleName(tip)
            button.setMinimumWidth(22); arrow_layout.addWidget(button)
        self.up.clicked.connect(lambda: self.step(1))
        self.down.clicked.connect(lambda: self.step(-1))
        self.reset = QPushButton('恢复默认')
        self.reset.setToolTip('恢复自动字号'); self.reset.setAccessibleName('恢复默认自适应字号')
        self.reset.setMaximumWidth(76); self.reset.clicked.connect(lambda: self.setValue(0))
        layout.addWidget(self.edit); layout.addWidget(arrows); layout.addWidget(self.reset)

    def value(self):
        text = self.edit.text().strip()
        if text == '默认': return 0
        if not re.fullmatch(r'[0-9]+', text):
            raise ValueError('字号请输入 1～200 的整数，或点击“恢复默认”恢复自适应。')
        value = int(text)
        if not MIN_SIZE <= value <= MAX_SIZE:
            raise ValueError('手动字号必须在 1～200 之间；输入的数值未被修改。')
        return value

    def _edited(self, text):
        self.size_origin = None if text.strip() == '默认' else 'user'

    def setValue(self, value, *, size_origin=None):
        self.size_origin = size_origin if value != 0 else None
        self.edit.setText('默认' if value == 0 else str(value))

    def step(self, direction):
        try:
            value = self.value()
            if value == 0:
                if self.estimate is None and self.estimate_callback is not None:
                    self.estimate_callback()
                if self.estimate is None:
                    raise ValueError('还无法估算字号，请先选择可用字体，或直接输入手动字号。')
                value = self.estimate
            proposed = value + direction
            if MIN_SIZE <= proposed <= MAX_SIZE:
                self.setValue(proposed, size_origin='user')
        except ValueError as exc:
            self.setToolTip(str(exc)); self.edit.setFocus()


class VideoSubtitlePreview(QLabel):
    """A cropped area of a 16:9 sample frame, using movie-equivalent size units."""
    def __init__(self):
        super().__init__()
        self.reference_font = QFont('Microsoft YaHei'); self.reference_size = 0
        self.ass_scale = 1.0
        self.reference_color = '#FFFFFF'
        self.setWordWrap(True); self.setAlignment(Qt.AlignCenter)
        self.setMinimumHeight(110); self.setMaximumHeight(200)
        self.setStyleSheet('background:#17252a;color:white;border-radius:10px;padding:12px;')

    def set_reference(self, font, size, color, ass_scale=1.0):
        self.reference_font = QFont(font); self.reference_size = size
        self.ass_scale = ass_scale
        self.reference_color = color
        self.update_scale()

    def update_scale(self):
        font = QFont(self.reference_font)
        virtual_height = max(1, self.width() - 24) * PREVIEW_HEIGHT / PREVIEW_WIDTH
        font.setPixelSize(max(1, round(self.reference_size * virtual_height / (720 * self.ass_scale))))
        self.setFont(font)
        # The application's QWidget stylesheet sets both family and pixel
        # size.  Override them explicitly so it cannot mask the actual preview.
        family = font.family().replace('\\', '\\\\').replace('"', '\\"')
        sheet = (f'background:#17252a;color:{self.reference_color};border-radius:10px;padding:12px;'
                 f'font-family:"{family}";font-size:{font.pixelSize()}px;'
                 f'font-weight:{700 if font.bold() else 400};'
                 f'font-style:{"italic" if font.italic() else "normal"};')
        if self.styleSheet() != sheet: self.setStyleSheet(sheet)

    def resizeEvent(self, event):
        super().resizeEvent(event); self.update_scale()


def preview_original_font(code):
    """The actual retained subtitle font is unknown until a film is analyzed."""
    root = Path(os.environ.get('WINDIR', 'C:/Windows')) / 'Fonts'
    names = {'zh-CN': ['msyh.ttc', 'simhei.ttf'], 'zh-TW': ['msjh.ttc', 'msyh.ttc'],
             'ja': ['YuGothM.ttc', 'msgothic.ttc'], 'ko': ['malgun.ttf']}
    for name in names.get(code, []) + ['segoeui.ttf', 'arial.ttf']:
        path = root / name
        if path.is_file():
            raw = QRawFont(str(path), 100)
            if raw.isValid(): return str(path), QFont(raw.familyName())
    raise ValueError('没有找到可用的示例字体；保留原字体时可手动输入字号。')


class SubtitleFontDialog(QDialog):
    def __init__(self, profile, parent=None):
        super().__init__(parent)
        self.setObjectName('subtitleFontDialog')
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet(FONT_DIALOG_STYLE)
        self.setWindowTitle(f'修改字体 · 偏好 {profile.slot}')
        self.setWindowModality(Qt.ApplicationModal)
        self.profile = profile; self.result_styles = None; self.rows = {}; self._font_ids = []
        self._preview_ready = False
        self.choices = [dict(id='', label='保留原字体')]
        for ident, label, file in fonts.BUILTINS:
            try:
                info = fonts.font_info(fonts.assets_root() / ident / file)
                self.choices.append(dict(info, id='builtin:' + ident, label=label))
            except (OSError, ValueError):
                self.choices.append(dict(id='builtin:' + ident, label=label + '（不可用）'))
        self.choices.extend(system_choices())
        styles = fonts.normalize_styles(profile.subtitle_styles)
        for style in styles.values():
            ident = style.get('font_id', '')
            if not any(c['id'] == ident for c in self.choices):
                self.choices.append(dict(id=ident, label=style.get('family', '已选字体'),
                                         path=style.get('path', ''), sha256=style.get('sha256', '')))
        outer = QVBoxLayout(self); outer.setContentsMargins(8, 8, 8, 8)
        card = QFrame(); card.setObjectName('fontDialogCard'); outer.addWidget(card)
        layout = QVBoxLayout(card); layout.setContentsMargins(18, 14, 18, 18); layout.setSpacing(10)
        bar = FontDialogTitleBar(); bar_layout = QHBoxLayout(bar); bar_layout.setContentsMargins(0,0,0,0)
        caption = QLabel(self.windowTitle()); caption.setObjectName('fontWindowTitle')
        caption.setAttribute(Qt.WA_TransparentForMouseEvents)
        bar_layout.addWidget(caption); bar_layout.addStretch()
        close = QToolButton(); close.setObjectName('fontDialogClose'); close.setText('×')
        close.setFixedSize(28,28); close.setAccessibleName('关闭字体设置'); close.clicked.connect(self.reject)
        bar_layout.addWidget(close); layout.addWidget(bar)
        title = QLabel(f'偏好 {profile.slot} · {profile.name}')
        title.setObjectName('dialogTitle'); layout.addWidget(title)
        description = QLabel('默认自动适配字号；有特殊需求时输入数值或点击 ▲▼。字体、字号、颜色独立设置。')
        description.setWordWrap(True); layout.addWidget(description)
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QScrollArea.NoFrame)
        content = QWidget(); grid = QGridLayout(content); grid.setHorizontalSpacing(10)
        for i, label in enumerate(('字幕语言', '字体', '字号', '颜色')): grid.addWidget(QLabel(label), 0, i)
        for row, code in enumerate(profile.subtitle_languages, 1):
            existing = styles.get(code, {})
            combo = FontChoiceCombo(); combo.setMinimumWidth(230)
            for choice in self.choices: combo.addItem(choice['label'], choice)
            spin = AdaptiveSizeInput()
            spin.estimate_callback = lambda c=code: self.preview_language(c)
            mode = existing.get('size_mode', 'manual' if existing.get('size') else 'auto')
            spin.setValue(existing.get('size', 0) if mode == 'manual' else 0,
                          size_origin=existing.get('size_origin'))
            requested_color = existing.get('color', 'keep')
            color = QPushButton('保持原色' if requested_color == 'keep' else requested_color)
            color.setMinimumWidth(95); color.setToolTip('点击选择颜色；右键可恢复原色。')
            color.setContextMenuPolicy(Qt.CustomContextMenu)
            color.customContextMenuRequested.connect(lambda pos, c=code: self.color_menu(c, pos))
            self.rows[code] = (combo, spin, color)
            grid.addWidget(QLabel(language_label(code)), row, 0); grid.addWidget(combo, row, 1)
            grid.addWidget(spin, row, 2); grid.addWidget(color, row, 3)
            selected = existing.get('font_id', '')
            combo.setCurrentIndex(next((i for i, c in enumerate(self.choices) if c['id'] == selected), 0))
            combo.currentIndexChanged.connect(lambda _, c=code: self.preview_language(c))
            spin.valueChanged.connect(lambda c=code: self.preview_language(c))
            color.clicked.connect(lambda _, c=code: self.choose_color(c))
        grid.setColumnStretch(1, 1); scroll.setWidget(content); layout.addWidget(scroll, 1)
        scroll.setMinimumHeight(min(240,content.sizeHint().height()+4))
        self.preview_title = QLabel('字体预览'); layout.addWidget(self.preview_title)
        self.preview = VideoSubtitlePreview(); layout.addWidget(self.preview)
        self.notice = QLabel(); self.notice.setWordWrap(True); self.notice.setMinimumHeight(44); layout.addWidget(self.notice)
        hint = QLabel('所有保留的文本字幕和新增译文按设置应用；定位文字保留位置和时间。图片字幕不能修改字体。')
        hint.setWordWrap(True); layout.addWidget(hint)
        bottom = QHBoxLayout(); import_button = QPushButton('导入字体（TTF / OTF）')
        import_button.clicked.connect(self.import_file); bottom.addWidget(import_button); bottom.addStretch()
        cancel = QPushButton('取消'); cancel.clicked.connect(self.reject); bottom.addWidget(cancel)
        self.save_button = QPushButton('保存'); self.save_button.setObjectName('fontSaveButton')
        self.save_button.setFixedHeight(40); self.save_button.setMaximumWidth(132)
        self.save_button.clicked.connect(self.save); bottom.addWidget(self.save_button); layout.addLayout(bottom)
        self.resize(900, min(740, 490 + len(self.rows) * 43))
        self._preview_ready = True
        if self.rows: self.preview_language(next(iter(self.rows)))
        else:
            self.preview.setText('当前偏好未勾选字幕语言。'); self.save_button.setEnabled(False)
        self.fit_main_window()

    def fit_main_window(self):
        box = main_window_geometry(self)
        self.resize(min(self.width(), box.width()), min(self.height(), box.height()))
        self.move(box.center().x() - self.width() // 2, box.center().y() - self.height() // 2)

    def selection(self, code):
        combo, spin, color = self.rows[code]; choice = combo.currentData() or {}; size = spin.value()
        result = dict(font_id=choice.get('id', ''), path=choice.get('path', ''),
                    sha256=choice.get('sha256', ''), family=choice.get('family', ''),
                    size_mode='auto' if size == 0 else 'manual', size=size,
                    color='keep' if color.text() == '保持原色' else color.text())
        if size != 0 and spin.size_origin == 'user': result['size_origin'] = 'user'
        return result

    def preview_language(self, code):
        if not self._preview_ready: return
        self._preview_code = code; combo, spin, color = self.rows[code]
        self.preview_title.setText(language_label(code) + ' · 影片字幕大小示例')
        sample = fonts.SAMPLES.get(code, fonts.SAMPLES['en']); self.preview.setText(sample)
        self.notice.setStyleSheet('color:#49625c')
        try:
            style = self.selection(code)
            if style['font_id']:
                info = fonts.resolve_font(style)
                if info['path'] not in [p for p, i in self._font_ids]:
                    ident = QFontDatabase.addApplicationFont(info['path'])
                    if ident < 0: raise ValueError('字体无法加载到预览，请重新选择字体。')
                    self._font_ids.append((info['path'], ident))
                font = QFont(info.get('ui_family', info['family']))
                font.setBold(info['bold']); font.setItalic(info['italic']); path = info['path']
                missing = fonts.missing_characters(path, sample)
                if missing: raise ValueError('这款字体缺少示例字符：' + ''.join(missing[:16]) + '。请选择其他字体。')
                original = ''
            else:
                path, font = preview_original_font(code)
                original = '保留原字体时，此处使用示例字体；成品沿用原字幕字体。'
            from subtitle_style_engine import automatic_size
            automatic, metrics = automatic_size(path, [sample], PREVIEW_WIDTH, PREVIEW_HEIGHT)
            spin.estimate = int(automatic)
            actual = automatic if style['size_mode'] == 'auto' else style['size']
            self.preview.set_reference(font, actual, '#FFFFFF' if style['color'] == 'keep' else style['color'],
                                       metrics['ass_font_scale'])
            if style['size_mode'] == 'auto':
                message = f'默认自适应；示例计算字号 {automatic}，实际处理会按影片和完整字幕计算。'
            else: message = f'手动字号 {style["size"]}：严格执行，不再自动调整。'
            self.notice.setText(message + original + '处理前会检查实际字幕是否缺字；无法应用会明确报错。')
        except (OSError, ValueError) as exc:
            spin.estimate = None; self.notice.setText(str(exc)); self.notice.setStyleSheet('color:#b45309')

    def choose_color(self, code):
        button = self.rows[code][2]; initial = '#FFFFFF' if button.text() == '保持原色' else button.text()
        color = QColorDialog.getColor(QColor(initial), self, '字幕颜色')
        if color.isValid(): button.setText(color.name().upper()); self.preview_language(code)

    def color_menu(self, code, pos):
        button = self.rows[code][2]; menu = QMenu(button)
        choose = menu.addAction('选择颜色'); keep = menu.addAction('保持原色')
        action = menu.exec(button.mapToGlobal(pos))
        if action == choose: self.choose_color(code)
        elif action == keep: button.setText('保持原色'); self.preview_language(code)

    def import_file(self):
        path, _ = QFileDialog.getOpenFileName(self, '导入字幕字体', '', '字体文件 (*.ttf *.otf)')
        if not path: return
        try:
            info = fonts.import_font(path)
            if not any(c['id'] == info['id'] for c in self.choices):
                self.choices.append(info)
                for combo, _, _ in self.rows.values(): combo.addItem(info['label'], info)
            if self.rows:
                code = getattr(self, '_preview_code', next(iter(self.rows)))
                self.rows[code][0].setCurrentIndex(next(i for i, c in enumerate(self.choices) if c['id'] == info['id']))
        except (OSError, ValueError) as exc: QMessageBox.warning(self, '字体无法导入', str(exc))

    def save(self):
        result = dict(fonts.normalize_styles(self.profile.subtitle_styles))
        try:
            for code in self.rows:
                style = self.selection(code)
                if style['font_id']:
                    info = fonts.resolve_font(style)
                    missing = fonts.missing_characters(info['path'], fonts.SAMPLES.get(code, fonts.SAMPLES['en']))
                    if missing: raise ValueError(language_label(code) + '字体缺字：' + ''.join(missing[:16]) + '。请换一款字体。')
                    if not style['font_id'].startswith('builtin:'):
                        imported = fonts.import_font(info['path'])
                        style.update(font_id=imported['id'], path=imported['path'], sha256=imported['sha256'])
                    style['family'] = info['family']; style['ui_family'] = info.get('ui_family', info['family'])
                else:
                    for key in ('path', 'sha256', 'family'): style.pop(key, None)
                result[code] = style
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, '字体设置未保存', str(exc)); return
        self.result_styles = result; self.accept()

    def done(self, result):
        for combo, _spin, _color in self.rows.values(): combo.hidePopup()
        super().done(result)
        for _, ident in self._font_ids:
            if ident >= 0: QFontDatabase.removeApplicationFont(ident)
        self._font_ids = []
