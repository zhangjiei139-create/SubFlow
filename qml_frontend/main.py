from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = Path(getattr(sys, "_MEIPASS", SOURCE_ROOT.parent))
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

if "--pgs-ocr-worker" in sys.argv:
    import subtitle_tool_core as worker_core

    raise SystemExit(worker_core.pgs_ocr_worker_main(sys.argv[1:]))

from PySide6.QtCore import QEvent, QObject, Property, Qt, QTimer, QUrl, Signal, Slot
from PySide6.QtGui import QFont, QFontDatabase, QIcon
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtWidgets import QApplication

import license_manager
from bridge import BackendBridge


def load_ui_font(root: Path) -> str:
    candidates = [root / "fonts" / "NotoSansSC-VF.ttf"]
    # Read the locally installed font; never redistribute Windows font files.
    if os.name == "nt":
        candidates.append(Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "msyh.ttc")
    for font_path in candidates:
        if not font_path.is_file():
            continue
        font_id = QFontDatabase.addApplicationFont(str(font_path))
        if font_id >= 0:
            families = QFontDatabase.applicationFontFamilies(font_id)
            if families:
                return families[0]
    return "Microsoft YaHei UI"


class LicenseBridge(QObject):
    validChanged = Signal()
    busyChanged = Signal()
    messageChanged = Signal()
    activationResolved = Signal(bool, str)

    def __init__(self, force_activation: bool = False) -> None:
        super().__init__()
        self.config = license_manager.load_config()
        enabled = bool(self.config.get("license", {}).get("enabled", False))
        valid, message = license_manager.local_license_valid(self.config) if enabled else (True, "授权未启用")
        self._valid = valid and not force_activation
        self._busy = False
        self._message = "" if valid else message
        self.activationResolved.connect(self._finish_activation)

    @Property(bool, notify=validChanged)
    def valid(self) -> bool:
        return self._valid

    @Property(bool, notify=busyChanged)
    def busy(self) -> bool:
        return self._busy

    @Property(str, notify=messageChanged)
    def message(self) -> str:
        return self._message

    @Property(str, constant=True)
    def deviceCode(self) -> str:
        return license_manager.device_hash()[:16].upper()

    @Slot(str)
    def activate(self, key: str) -> None:
        normalized = key.strip()
        if self._busy:
            return
        if not normalized:
            self._set_message("请输入有效激活码")
            return
        self._busy = True
        self.busyChanged.emit()
        self._set_message("正在连接授权服务器…")

        def task() -> None:
            try:
                ok, message = license_manager.activate_license(normalized, self.config)
            except Exception as exc:
                ok, message = False, f"无法连接授权服务器：{exc}"
            self.activationResolved.emit(ok, message)

        threading.Thread(target=task, daemon=True).start()

    @Slot(bool, str)
    def _finish_activation(self, ok: bool, message: str) -> None:
        self._busy = False
        self.busyChanged.emit()
        self._set_message("激活成功，授权已绑定当前电脑" if ok else message)
        if ok and not self._valid:
            self._valid = True
            self.validChanged.emit()

    def _set_message(self, message: str) -> None:
        if self._message == message:
            return
        self._message = message
        self.messageChanged.emit()


class TitleBarEventFilter(QObject):
    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._last_release_at = 0.0
        self._last_release_position = None

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        is_title_click = (
            hasattr(event, "button")
            and event.button() == Qt.MouseButton.LeftButton
            and event.position().y() <= 54
            and event.position().x() <= watched.width() - 126
        )
        if event.type() == QEvent.Type.MouseButtonDblClick and is_title_click:
            return True
        if event.type() == QEvent.Type.MouseButtonRelease and is_title_click:
            now = time.monotonic()
            position = event.position()
            previous = self._last_release_position
            is_double_click = (
                previous is not None
                and now - self._last_release_at <= 0.5
                and abs(position.x() - previous.x()) <= 6
                and abs(position.y() - previous.y()) <= 6
            )
            self._last_release_at = 0.0 if is_double_click else now
            self._last_release_position = None if is_double_click else position
            if is_double_click:
                if watched.isMaximized():
                    watched.showNormal()
                else:
                    watched.showMaximized()
                return True
        return super().eventFilter(watched, event)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--screenshot")
    parser.add_argument("--force-activation", action="store_true")
    parser.add_argument("--video")
    parser.add_argument("--page", type=int, choices=range(4), default=0)
    parser.add_argument("--width", type=int)
    parser.add_argument("--height", type=int)
    parser.add_argument("--burned-worker-video")
    parser.add_argument("--burned-worker-output")
    parser.add_argument("--burned-worker-languages", default="")
    args = parser.parse_args()

    if args.burned_worker_video:
        import burned_subtitle_detector

        output = Path(args.burned_worker_output)
        try:
            result = burned_subtitle_detector.detect(
                args.burned_worker_video,
                None,
                [value for value in args.burned_worker_languages.split(",") if value],
            )
            payload = {"ok": True, "result": asdict(result)}
        except Exception as exc:
            payload = {"ok": False, "error": str(exc)}
        output.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        return 0 if payload["ok"] else 1

    root = PROJECT_ROOT / "qml_frontend" if getattr(sys, "frozen", False) else SOURCE_ROOT
    app = QApplication(sys.argv[:1])
    app.setApplicationName("SubFlow")
    app.setWindowIcon(QIcon(str(PROJECT_ROOT / "assets" / "subtitle-track-tool-pro-icon.ico")))
    ui_font_family = load_ui_font(root)
    app_font = QFont(ui_font_family, 10)
    app_font.setWeight(QFont.Weight.Bold)
    app_font.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
    app_font.setStyleStrategy(QFont.PreferAntialias)
    app.setFont(app_font)

    engine = QQmlApplicationEngine()
    license_bridge = LicenseBridge(args.force_activation)
    backend_bridge = BackendBridge()
    app.aboutToQuit.connect(backend_bridge.shutdown)
    engine.rootContext().setContextProperty("licenseBridge", license_bridge)
    engine.rootContext().setContextProperty("backend", backend_bridge)
    engine.rootContext().setContextProperty("uiFontFamily", ui_font_family)
    engine.rootContext().setContextProperty(
        "appVersion",
        license_manager.app_version(license_bridge.config),
    )
    engine.load(QUrl.fromLocalFile(str(root / "Main.qml")))
    if not engine.rootObjects():
        return 1

    window = engine.rootObjects()[0]
    if args.width:
        window.setProperty("width", args.width)
    if args.height:
        window.setProperty("height", args.height)
    if args.page:
        window.setProperty("currentPage", args.page)
        backend_bridge.activateToolPage(args.page)
    title_bar_filter = TitleBarEventFilter(window)
    window.installEventFilter(title_bar_filter)
    if args.video:
        backend_bridge.handleDroppedUrls([QUrl.fromLocalFile(args.video)], 0)
    if args.screenshot:
        destination = str(Path(args.screenshot).resolve())

        def capture() -> None:
            window.screen().grabWindow(int(window.winId())).save(destination)
            app.quit()

        QTimer.singleShot(5000 if args.video else 1200, capture)

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
