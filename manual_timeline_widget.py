"""Manual audio/subtitle timeline with one 30-second shared viewport."""
from PySide6.QtCore import Qt, QRectF, Signal
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QSizePolicy, QWidget

class _TimelineBase(QWidget):
    offsetDragged = Signal(float)
    seekRequested = Signal(float)

    def __init__(self, evidence, parent=None):
        super().__init__(parent)
        self.evidence = evidence
        self.offset = float(evidence.get("initial_offset", 0.0))
        self.position = 0.0
        self.selected = -1
        self.drag = None
        self.setMouseTracking(True)

    def dimensions(self):
        return 28.0, max(100.0, self.width() - 56.0)


class ManualTimelineWidget(_TimelineBase):
    cueClicked = Signal(int)

    def __init__(self, evidence, parent=None):
        super().__init__(evidence, parent)
        self.view_start = 0.0
        self.setMinimumHeight(190)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.cue_rects = []

    def set_view_start(self, second):
        self.view_start = 30.0 if second >= 30 else 0.0
        self.update()

    def x_at(self, second):
        left, width = self.dimensions()
        return left + (second - self.view_start) / 30.0 * width

    def second_at(self, x):
        left, width = self.dimensions()
        return min(self.view_start + 30, max(self.view_start,
                   self.view_start + (x-left) / width * 30.0))

    @staticmethod
    def merged(intervals):
        output = []
        for start, end in sorted(intervals):
            if end <= start:
                continue
            if output and start <= output[-1][1] + .01:
                output[-1][1] = max(output[-1][1], end)
            else:
                output.append([start, end])
        return output

    def visible_cues(self):
        base = self.evidence["movie_start"]
        lo, hi = self.view_start, self.view_start + 30.0
        result = []
        for i, cue in enumerate(self.evidence["rows"]):
            start = cue["start"] + self.offset - base
            end = cue["end"] + self.offset - base
            if end > lo and start < hi:
                result.append((i, max(lo, start), min(hi, end), cue))
        return sorted(result, key=lambda row: (row[1], row[2]))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        shell = QPainterPath()
        shell.addRoundedRect(QRectF(self.rect()), 12, 12)
        painter.fillPath(shell, QColor("#10212c"))
        painter.setClipPath(shell)
        left, width = self.dimensions()
        cue_bottom = self.height() - 6
        painter.setFont(QFont("Microsoft YaHei", 10))
        painter.setPen(QColor("#d8e8ed"))
        painter.drawText(28, 18, "同一条时间轴：上面听声音停顿，下面拖动整排字幕")
        for second in range(int(self.view_start), int(self.view_start + 31), 5):
            x = self.x_at(second)
            painter.setPen(QPen(QColor("#38515d"), 1))
            painter.drawLine(round(x), 34, round(x), cue_bottom)
            painter.setPen(QColor("#c5d9df"))
            painter.drawText(round(x)-8, 32, str(second))
        voice_top, voice_bottom = 51, 79
        cue_top = 102
        painter.fillRect(QRectF(left, voice_top, width, voice_bottom-voice_top), QColor("#1a3540"))
        painter.fillRect(QRectF(left, cue_top, width, cue_bottom-cue_top), QColor("#142d37"))
        painter.setPen(QColor("#d7ebee"))
        painter.drawText(30, 48, "影片声音位置")
        painter.drawText(30, 98, "下载字幕：上下错层显示，整排一起拖动")
        lo, hi = self.view_start, self.view_start + 30.0
        voice = self.merged((max(lo,a), min(hi,b)) for a,b in self.evidence["vad"]
                            if b > lo and a < hi)
        cursor = lo
        for a,b in voice + [[hi,hi]]:
            if a - cursor >= 2:
                x0, x1 = self.x_at(cursor), self.x_at(a)
                painter.fillRect(QRectF(x0, voice_top+4, x1-x0, voice_bottom-voice_top-8), QColor("#405764"))
                if x1-x0 >= 80:
                    painter.setPen(QColor("#d8e6e7"))
                    painter.drawText(QRectF(x0+2, voice_top+8, x1-x0-4, 27),
                                     Qt.AlignmentFlag.AlignCenter, f"安静 {a-cursor:.1f}s")
            cursor = max(cursor,b)
        for a,b in voice:
            painter.fillRect(QRectF(self.x_at(a), voice_top+7,
                             max(2,self.x_at(b)-self.x_at(a)), voice_bottom-voice_top-14), QColor("#43bdc9"))
        visible = self.visible_cues()
        occupied = self.merged((a,b) for _,a,b,_ in visible)
        for (_,end),(begin,_) in zip(occupied, occupied[1:]):
            if begin-end < 3:
                continue
            x0,x1 = self.x_at(end), self.x_at(begin)
            painter.fillRect(QRectF(x0,cue_top+3,x1-x0,cue_bottom-cue_top-6), QColor(237,179,70,37))
            painter.setPen(QPen(QColor("#b48a45"), 1, Qt.PenStyle.DashLine))
            painter.drawRect(QRectF(x0,cue_top+3,x1-x0,cue_bottom-cue_top-6))
            if x1-x0 >= 85:
                painter.setPen(QColor("#f2cb79"))
                painter.drawText(QRectF(x0+2,cue_top+4,x1-x0-4,23),
                                 Qt.AlignmentFlag.AlignCenter, f"字幕空白 {begin-end:.1f}s")
        self.cue_rects = []
        font = QFont("Microsoft YaHei", 10)
        metrics = QFontMetrics(font)
        painter.setFont(font)
        for i,a,b,cue in visible:
            x0,x1 = self.x_at(a), self.x_at(b)
            lane_spacing = max(25, min(40, (cue_bottom-cue_top-8)/3))
            box = QRectF(x0, 107+(i%3)*lane_spacing, max(4,x1-x0), 22)
            self.cue_rects.append((i,box))
            selected = i == self.selected
            painter.setBrush(QColor("#e0ac54" if selected else "#ad8751"))
            painter.setPen(QPen(QColor("#f0c97f"), 1))
            painter.drawRoundedRect(box, 6, 6)
            painter.setPen(QColor("#10202c"))
            short = cue["text"].replace("\n", " ").strip().lstrip("-– ").strip()
            available = max(0, int(box.width())-8)
            label = metrics.elidedText(short, Qt.TextElideMode.ElideRight, available) if available >= 70 else short
            painter.drawText(QRectF(x0+4,box.top()+1,max(2,box.width()-8),20),
                             Qt.AlignmentFlag.AlignVCenter | Qt.TextFlag.TextSingleLine, label)
        if lo <= self.position <= hi:
            x = self.x_at(self.position)
            painter.setPen(QPen(QColor("#ff6674"), 3))
            painter.drawLine(round(x), 36, round(x), self.height()-3)
            painter.setBrush(QColor("#ff6674"))
            painter.drawEllipse(QRectF(x-8,34,16,16))
        painter.end()

    def mousePressEvent(self, event):
        x,y = event.position().x(), event.position().y()
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if y >= 102:
            index = next((i for i,rect in reversed(self.cue_rects)
                          if rect.contains(event.position())), -1)
            if index >= 0:
                self.cueClicked.emit(index)
            self.drag = ("subtitle", x, self.offset)
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
        else:
            self.drag = ("playhead", x, self.position)
            self.seekRequested.emit(self.second_at(x))
        event.accept()

    def mouseMoveEvent(self, event):
        x = event.position().x()
        if self.drag:
            kind,x0,value = self.drag
            if kind == "playhead":
                self.seekRequested.emit(self.second_at(x))
            else:
                _,width = self.dimensions()
                self.offsetDragged.emit(max(-20,min(20,round(value+(x-x0)/width*30,2))))
        else:
            self.setCursor(Qt.CursorShape.OpenHandCursor if event.position().y() >= 102
                           else Qt.CursorShape.ArrowCursor)

    def mouseReleaseEvent(self, event):
        self.drag = None
        self.setCursor(Qt.CursorShape.ArrowCursor)
