# -*- coding: utf-8 -*-
from __future__ import annotations

import ctypes
import os
import queue
import re
import sys
import threading
from pathlib import Path


WORKER_MODE = "--pgs-ocr-worker" in sys.argv
if WORKER_MODE and getattr(sys, "frozen", False):
    if sys.stdin is None:
        sys.stdin = open(os.devnull, "r", encoding="utf-8")
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")

try:
    import subtitle_tool_core as core
except Exception:
    if WORKER_MODE and "--output" in sys.argv:
        import traceback

        output_arg = sys.argv[sys.argv.index("--output") + 1]
        Path(output_arg + ".boot.error.log").write_text(traceback.format_exc(), encoding="utf-8")
    raise

if WORKER_MODE:
    raise SystemExit(core.pgs_ocr_worker_main(sys.argv[1:]))


def configure_windows_dpi_awareness() -> None:
    if os.name != "nt":
        return
    try:
        # Declare DPI awareness before Tk is imported, avoiding slow bitmap-scaled moves.
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass


def configure_tcl_tk_paths() -> None:
    """The movie-sub Conda environment inherits Tcl/Tk from its base install."""
    candidates = [
        Path(sys.prefix) / "Library" / "lib",
        Path(sys.prefix).parent.parent / "Library" / "lib",
    ]
    for root in candidates:
        tcl_dir, tk_dir = root / "tcl8.6", root / "tk8.6"
        if (tcl_dir / "init.tcl").exists() and tk_dir.exists():
            os.environ["TCL_LIBRARY"] = str(tcl_dir)
            os.environ["TK_LIBRARY"] = str(tk_dir)
            return


configure_windows_dpi_awareness()
configure_tcl_tk_paths()

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from online_subtitle_dialog import OnlineSubtitleDialog
from batch_ui import BatchMixin
import license_manager
import pro_core

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
except Exception:
    DND_FILES = None
    TkinterDnD = None


BaseTk = TkinterDnD.Tk if TkinterDnD else tk.Tk


def bundled_resource(*parts: str) -> Path:
    base_dir = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    return base_dir.joinpath(*parts)


LANGUAGES = [
    ("zh-CN", "简体中文"), ("zh-TW", "繁体中文"), ("en", "英文"),
    ("es", "西班牙语"), ("ja", "日语"), ("ko", "韩语"),
    ("fr", "法语"), ("de", "德语"), ("pt", "葡萄牙语"),
    ("ru", "俄语"), ("hi", "印地语"),
]


class TrackChecklist(ttk.Frame):
    """Scrollable checkbox list. A checkbox means that track will be kept."""
    def __init__(self, master, empty_text: str, height: int) -> None:
        super().__init__(master)
        self.tracks: list[core.Track] = []
        self.variables: dict[int, tk.BooleanVar] = {}
        controls = ttk.Frame(self)
        controls.pack(fill=tk.X, pady=(0, 5))
        ttk.Label(controls, text="勾选=保留；未勾选=删除", style="Note.TLabel").pack(side=tk.LEFT)
        ttk.Button(controls, text="默认", command=self.select_default).pack(side=tk.RIGHT)
        ttk.Button(controls, text="全不选", command=self.clear).pack(side=tk.RIGHT, padx=(0, 6))
        self.canvas = tk.Canvas(self, height=height, bg="#fbfdfd", highlightthickness=1, highlightbackground="#d9e5e3")
        scrollbar = ttk.Scrollbar(self, orient=tk.VERTICAL, command=self.canvas.yview)
        self.content = ttk.Frame(self.canvas)
        self.window = self.canvas.create_window((0, 0), window=self.content, anchor=tk.NW)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.content.bind("<Configure>", lambda _event: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda event: self.canvas.itemconfigure(self.window, width=event.width))
        self.canvas.bind("<Enter>", self._bind_wheel)
        self.canvas.bind("<Leave>", self._unbind_wheel)
        self.content.bind("<Enter>", self._bind_wheel)
        self.content.bind("<Leave>", self._unbind_wheel)
        self.empty = ttk.Label(self.content, text=empty_text, style="Note.TLabel")
        self.empty.pack(anchor=tk.W, padx=8, pady=8)

    def set_tracks(self, tracks: list[core.Track], labels: list[str], select_default: bool) -> None:
        for child in self.content.winfo_children():
            child.destroy()
        self.tracks = tracks
        self.variables = {}
        has_default = any(track.default for track in tracks)
        for index, (track, label) in enumerate(zip(tracks, labels)):
            checked = track.default or (select_default and not has_default and index == 0)
            variable = tk.BooleanVar(value=checked)
            self.variables[track.id] = variable
            button = tk.Checkbutton(
                self.content, indicatoron=False, anchor=tk.W, relief=tk.FLAT,
                borderwidth=0, highlightthickness=0, padx=7, pady=3,
                font=("Microsoft YaHei UI", 9), variable=variable,
            )
            button.pack(anchor=tk.W, fill=tk.X, padx=5, pady=1)

            def refresh(*_args, control=button, selected=variable, title=label):
                active = selected.get()
                control.configure(
                    text=f"{'✓' if active else '□'}  {title}",
                    bg="#d8efea" if active else "#fbfdfd",
                    activebackground="#cde7e2" if active else "#f1f8f6",
                    fg="#174440",
                )

            variable.trace_add("write", refresh)
            refresh()
            button.bind("<Enter>", self._bind_wheel)
            button.bind("<Leave>", self._unbind_wheel)
        if not tracks:
            ttk.Label(self.content, text="未发现该类轨道", style="Note.TLabel").pack(anchor=tk.W, padx=8, pady=8)

    def selected_ids(self) -> list[int]:
        return [track_id for track_id, variable in self.variables.items() if variable.get()]

    def clear(self) -> None:
        for variable in self.variables.values():
            variable.set(False)

    def select_default(self) -> None:
        self.clear()
        selected = [track for track in self.tracks if track.default]
        if not selected and self.tracks:
            selected = [self.tracks[0]]
        for track in selected:
            self.variables[track.id].set(True)

    def _bind_wheel(self, _event=None) -> None:
        self.canvas.bind_all("<MouseWheel>", self._wheel)

    def _unbind_wheel(self, _event=None) -> None:
        self.canvas.unbind_all("<MouseWheel>")

    def _wheel(self, event) -> None:
        self.canvas.yview_scroll(int(-event.delta / 120), "units")


class ProApp(BatchMixin, BaseTk):
    def __init__(self) -> None:
        super().__init__()
        self.title("字幕音轨整理工具 - Pro Max")
        try:
            self.iconbitmap(default=str(bundled_resource("assets", "subtitle-track-tool-pro-icon.ico")))
        except tk.TclError:
            pass
        self.product_config = license_manager.load_config()
        if not license_manager.ensure_licensed(self):
            self.after(50, self.destroy)
            return
        self.geometry("1280x820")
        self.minsize(1040, 720)
        self.configure(bg="#f3f7f6")
        self.video_path = tk.StringVar()
        self.subtitle_path = tk.StringVar()
        self.online_subtitle_path = ""
        self.online_subtitle_language = ""
        self.output_path = tk.StringVar()
        self.output_format = tk.StringVar(value="MKV")
        self.source_mode = tk.StringVar(value="embedded")
        self.embedded_source = tk.StringVar()
        self.online_status = tk.StringVar(value="搜索并下载后，将自动按所选音轨校正时间轴。")
        self.translate_enabled = tk.BooleanVar(value=False)
        self.parallel = tk.StringVar(value="1")
        self.speech_language = tk.StringVar(value="auto")
        self.audio_vars: dict[int, tk.BooleanVar] = {}
        self.subtitle_vars: dict[int, tk.BooleanVar] = {}
        self.target_vars = {code: tk.BooleanVar(value=False) for code, _ in LANGUAGES}
        self.tracks: list[core.Track] = []
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.cancel_event = threading.Event()
        self.running = False
        self._style()
        self._init_batch_state()
        self._ui()
        self._update_source_controls()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.after(120, self._poll)
        self._setup_drop()

    def _style(self) -> None:
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background="#f3f7f6")
        style.configure("Card.TLabelframe", background="#ffffff", bordercolor="#c9dbd7", relief="solid")
        style.configure("Card.TLabelframe.Label", background="#ffffff", foreground="#173f3d", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("TLabel", background="#f3f7f6", foreground="#183432", font=("Microsoft YaHei UI", 10))
        style.configure("Note.TLabel", foreground="#617875", font=("Microsoft YaHei UI", 9))
        style.configure("Primary.TButton", background="#0d7f77", foreground="#ffffff", padding=(16, 7), font=("Microsoft YaHei UI", 10, "bold"))
        style.map("Primary.TButton", background=[("active", "#096961")])
        style.configure("Title.TLabel", foreground="#0b5450", font=("Microsoft YaHei UI", 14, "bold"))
        style.configure(
            "Orange.Horizontal.TProgressbar",
            troughcolor="#dbe5e3",
            background="#f28c28",
            lightcolor="#f28c28",
            darkcolor="#d96f12",
            bordercolor="#b9c9c6",
        )

    def _ui(self) -> None:
        notebook = ttk.Notebook(self)
        notebook.pack(fill=tk.BOTH, expand=True)
        single_tab = ttk.Frame(notebook)
        batch_tab = ttk.Frame(notebook)
        profiles_tab = ttk.Frame(notebook)
        hardware_tab = ttk.Frame(notebook)
        notebook.add(single_tab, text="  单片处理  ")
        notebook.add(batch_tab, text="  偏好批量处理  ")
        notebook.add(profiles_tab, text="  偏好设置  ")
        notebook.add(hardware_tab, text="  硬件状态  ")
        self._single_ui(single_tab)
        self._batch_ui(batch_tab)
        self._profiles_ui(profiles_tab)
        self._hardware_ui(hardware_tab)

    def _single_ui(self, parent) -> None:
        outer = ttk.Frame(parent, padding=14)
        outer.pack(fill=tk.BOTH, expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=4, minsize=340)
        outer.rowconfigure(4, weight=2, minsize=150)
        ttk.Label(outer, text="字幕音轨整理工具 - Pro Max", style="Title.TLabel").grid(row=0, column=0, sticky=tk.W)
        top = ttk.Frame(outer)
        top.grid(row=1, column=0, sticky=tk.EW)
        top.columnconfigure(0, weight=3)
        top.columnconfigure(1, weight=2)
        files = self._section_frame(top, "影片与输出")
        files.grid(row=0, column=0, sticky=tk.NSEW, padx=(0, 8))
        files.columnconfigure(1, weight=1)
        self._file_row(files, 0, "视频文件", self.video_path, "选择", self.choose_video)
        self._file_row(files, 1, "输出文件", self.output_path, "另存为", self.choose_output)
        ttk.Combobox(files, textvariable=self.output_format, values=["MKV", "MP4"], width=8, state="readonly").grid(row=1, column=3, padx=(7, 0))
        self.output_format.trace_add("write", lambda *_: self.update_output_name())
        audio_host = ttk.Frame(top)
        audio_host.grid(row=0, column=1, sticky=tk.NSEW)
        self._audio_ui(audio_host)
        body = ttk.PanedWindow(outer, orient=tk.HORIZONTAL)
        body.grid(row=2, column=0, sticky=tk.NSEW, pady=8)
        left = ttk.Frame(body)
        right = ttk.Frame(body)
        body.add(left, weight=3)
        body.add(right, weight=2)
        self._subtitle_ui(left)
        self._source_ui(right)
        bottom = ttk.Frame(outer)
        bottom.grid(row=3, column=0, sticky=tk.EW)
        self.progress_label = ttk.Label(bottom, text="准备就绪", style="Note.TLabel")
        self.progress_label.pack(side=tk.LEFT, padx=(0, 8))
        self.progress = ttk.Progressbar(bottom, mode="determinate", maximum=100, value=0, style="Orange.Horizontal.TProgressbar")
        self.progress.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 12))
        self.start_button = ttk.Button(bottom, text="开始处理", style="Primary.TButton", command=self.start)
        self.start_button.pack(side=tk.LEFT)
        self.stop_button = ttk.Button(bottom, text="停止", command=self.stop, state=tk.DISABLED)
        self.stop_button.pack(side=tk.LEFT, padx=(8, 0))
        log_card = ttk.LabelFrame(outer, text=" 处理日志 ", style="Card.TLabelframe", padding=7)
        log_card.grid(row=4, column=0, sticky=tk.NSEW, pady=(10, 0))
        self.log_scrollbar = ttk.Scrollbar(log_card, orient=tk.VERTICAL)
        self.log_text = tk.Text(log_card, height=8, wrap=tk.WORD, relief="flat", bg="#fbfdfd", fg="#243f3d", font=("Consolas", 9), yscrollcommand=self.log_scrollbar.set)
        self.log_scrollbar.configure(command=self.log_text.yview)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.log_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

    def _section_frame(self, parent, title: str, padding: int = 10) -> ttk.LabelFrame:
        frame = ttk.LabelFrame(parent, style="Card.TLabelframe", padding=padding)
        heading = tk.Label(
            frame,
            text=title,
            bg="#0d7f77",
            fg="#ffffff",
            activebackground="#0d7f77",
            activeforeground="#ffffff",
            font=("Microsoft YaHei UI", 10, "bold"),
            padx=12,
            pady=3,
        )
        frame.configure(labelwidget=heading)
        return frame

    def _file_row(self, parent, row, label, variable, button, command) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky=tk.W, padx=(0, 8), pady=3)
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, columnspan=2, sticky=tk.EW, pady=3)
        ttk.Button(parent, text=button, command=command).grid(row=row, column=4, padx=(8, 0), pady=3)

    def _audio_ui(self, parent) -> None:
        audio = self._section_frame(parent, "选择原音轨", padding=8)
        audio.pack(fill=tk.BOTH, expand=True)
        ttk.Label(audio, text="默认保留主音轨；可按需要保留多个。", style="Note.TLabel").pack(anchor=tk.W)
        self.audio_checks = TrackChecklist(audio, "读取影片后显示音轨", height=72)
        self.audio_checks.pack(fill=tk.BOTH, expand=True, pady=(6, 0))

    def _subtitle_ui(self, parent) -> None:
        self.subtitle_split = ttk.PanedWindow(parent, orient=tk.HORIZONTAL)
        self.subtitle_split.pack(fill=tk.BOTH, expand=True)
        subs = self._section_frame(self.subtitle_split, "选择原字幕", padding=8)
        self.subtitle_split.add(subs, weight=3)
        ttk.Label(subs, text="可保留、删除或作为“使用影片内字幕”的翻译来源。", style="Note.TLabel").pack(anchor=tk.W)
        self.subtitle_checks = TrackChecklist(subs, "读取影片后显示原字幕", height=230)
        self.subtitle_checks.pack(fill=tk.BOTH, expand=True, pady=(6, 0))

    def _make_toggle(self, parent, text: str, variable: tk.BooleanVar, command=None) -> tk.Checkbutton:
        button = tk.Checkbutton(
            parent, indicatoron=False, anchor=tk.W, relief=tk.FLAT,
            borderwidth=0, highlightthickness=0, padx=6, pady=3,
            font=("Microsoft YaHei UI", 9), variable=variable, command=command,
        )

        def refresh(*_args) -> None:
            active = variable.get()
            button.configure(
                text=f"{'✓' if active else '□'}  {text}",
                bg="#d8efea" if active else "#fbfdfd",
                activebackground="#cde7e2" if active else "#f1f8f6",
                fg="#174440",
            )

        variable.trace_add("write", refresh)
        refresh()
        return button

    def _source_ui(self, parent) -> None:
        source = self._section_frame(parent, "字幕来源")
        source.pack(fill=tk.X)
        self.source_radios: list[ttk.Radiobutton] = []
        self.embedded_radio = None
        for value, title, detail in [
            ("none", "不新增字幕（仅整理音轨/原字幕）", "可删除不需要的音轨或字幕并直接输出。"),
            ("embedded", "使用影片内字幕", "已有文字或 PGS 图片字幕，可直接翻译。"),
            ("online", "在线查找字幕", "搜索、下载后按所选音轨自动校正。"),
            ("external", "导入外挂字幕（需选择 SRT / ASS）", "SRT / ASS 自动按音轨校正后导入。"),

        ]:
            radio = ttk.Radiobutton(source, text=title, value=value, variable=self.source_mode, command=self._update_source_controls)
            radio.pack(anchor=tk.W, pady=(3, 0))
            self.source_radios.append(radio)
            if value == "embedded":
                self.embedded_radio = radio
        self.embedded_combo = ttk.Combobox(source, textvariable=self.embedded_source, state="readonly")
        self.embedded_combo.pack(fill=tk.X, pady=(7, 0))
        self.online_row = ttk.Frame(source)
        self.online_row.pack(fill=tk.X, pady=(7, 0))
        self.online_label = ttk.Label(self.online_row, textvariable=self.online_status, style="Note.TLabel")
        self.online_label.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.online_button = ttk.Button(self.online_row, text="在线查找", command=self.open_online_search)
        self.online_button.pack(side=tk.RIGHT, padx=(7, 0))
        self.external_row = ttk.Frame(source)
        self.external_row.pack(fill=tk.X, pady=(7, 0))
        self.external_row.columnconfigure(0, weight=1)
        self.external_entry = ttk.Entry(self.external_row, textvariable=self.subtitle_path)
        self.external_entry.grid(row=0, column=0, sticky=tk.EW)
        self.external_button = ttk.Button(self.external_row, text="选择字幕", command=self.choose_subtitle)
        self.external_button.grid(row=0, column=1, padx=(7, 0))
        self.speech_row = ttk.Frame(source)
        self.speech_row.pack(fill=tk.X, pady=(7, 0))
        ttk.Label(self.speech_row, text="音轨语言提示").pack(side=tk.LEFT)
        self.speech_combo = ttk.Combobox(self.speech_row, textvariable=self.speech_language, values=["auto", "en", "zh", "ja", "fr", "de", "es", "ru", "ko"], width=10, state="readonly")
        self.speech_combo.pack(side=tk.LEFT, padx=(8, 0))
        self.translation_section = self._section_frame(self.subtitle_split, "新增翻译字幕（可选）", padding=8)
        self.translation_check = self._make_toggle(self.translation_section, "增加翻译字幕（输出将保留来源字幕，并增加目标语言字幕）", self.translate_enabled, self._toggle_languages)
        self.translation_check.pack(anchor=tk.W)
        self.language_box = ttk.Frame(self.translation_section)
        self.language_canvas = tk.Canvas(self.language_box, height=150, bg="#fbfdfd", highlightthickness=0)
        self.language_scroll = ttk.Scrollbar(self.language_box, orient=tk.VERTICAL, command=self.language_canvas.yview)
        self.language_area = ttk.Frame(self.language_canvas)
        self.language_window = self.language_canvas.create_window((0, 0), window=self.language_area, anchor=tk.NW)
        self.language_canvas.configure(yscrollcommand=self.language_scroll.set)
        self.language_canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        self.language_scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.language_area.bind("<Configure>", lambda _event: self.language_canvas.configure(scrollregion=self.language_canvas.bbox("all")))
        self.language_canvas.bind("<Configure>", lambda event: self.language_canvas.itemconfigure(self.language_window, width=event.width))
        self.language_canvas.bind("<MouseWheel>", lambda event: self.language_canvas.yview_scroll(int(-event.delta / 120), "units"))
        for index, (code, label) in enumerate(LANGUAGES):
            toggle = self._make_toggle(self.language_area, label, self.target_vars[code])
            toggle.grid(row=index // 2, column=index % 2, sticky=tk.EW, padx=(0, 8), pady=1)
            toggle.bind("<MouseWheel>", lambda event: self.language_canvas.yview_scroll(int(-event.delta / 120), "units"))
        for column in range(2):
            self.language_area.columnconfigure(column, weight=1)
        self.parallel_row = ttk.Frame(self.translation_section)
        ttk.Label(self.parallel_row, text="翻译并行数").pack(side=tk.LEFT)
        ttk.Combobox(self.parallel_row, textvariable=self.parallel, values=["1", "2", "3", "4"], width=5, state="readonly").pack(side=tk.LEFT, padx=(7, 0))
        ttk.Label(self.parallel_row, text="多个目标语言时可并行", style="Note.TLabel").pack(side=tk.LEFT, padx=(8, 0))

        self.ai_frame = ttk.LabelFrame(parent, text=" AI 翻译加速 ", style="Card.TLabelframe", padding=10)
        self.ai_frame.pack(fill=tk.X, pady=(8, 0))
        self.ai_status = ttk.Label(self.ai_frame, text="未检测。翻译前可确认是否使用 GPU。", style="Note.TLabel")
        self.ai_status.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.ai_button = ttk.Button(self.ai_frame, text="AI 状态检测", command=self.start_ai_check)
        self.ai_button.pack(side=tk.RIGHT)

    def _toggle_languages(self) -> None:
        if self.translate_enabled.get():
            self.language_box.pack(fill=tk.X, pady=(8, 0))
            self.parallel_row.pack(anchor=tk.W, pady=(8, 0))
        else:
            self.language_box.pack_forget()
            self.parallel_row.pack_forget()

    def _update_source_controls(self) -> None:
        mode = self.source_mode.get()
        has_embedded = bool(self.embedded_combo["values"])
        self.embedded_radio.config(
            state=tk.NORMAL if has_embedded else tk.DISABLED,
            text="使用影片内字幕" if has_embedded else "使用影片内字幕（未发现）",
        )
        if mode == "embedded" and not has_embedded:
            self.source_mode.set("none")
            mode = "none"
        embedded_state = "readonly" if mode == "embedded" and self.embedded_combo["values"] else "disabled"
        self.embedded_combo.config(state=embedded_state)
        self.online_button.config(state=tk.NORMAL if mode == "online" and Path(self.video_path.get().strip()).is_file() else tk.DISABLED)
        external_state = "normal" if mode == "external" else "disabled"
        self.external_entry.config(state=external_state)
        self.external_button.config(state=external_state)
        self.speech_combo.config(state="readonly" if mode == "audio" else "disabled")
        self.embedded_combo.pack_forget()
        self.online_row.pack_forget()
        self.external_row.pack_forget()
        self.speech_row.pack_forget()
        if mode == "embedded":
            self.embedded_combo.pack(fill=tk.X, pady=(7, 0))
        elif mode == "online":
            self.online_row.pack(fill=tk.X, pady=(7, 0))
        elif mode == "external":
            self.external_row.pack(fill=tk.X, pady=(7, 0))
        elif mode == "audio":
            self.speech_row.pack(fill=tk.X, pady=(7, 0))
        if mode == "embedded":
            if str(self.translation_section) not in self.subtitle_split.panes():
                self.subtitle_split.add(self.translation_section, weight=2)
            self.translation_check.config(state=tk.NORMAL)
        else:
            self.translate_enabled.set(False)
            if str(self.translation_section) in self.subtitle_split.panes():
                self.subtitle_split.forget(self.translation_section)
        self._toggle_languages()
        self._refresh_start_state()

    def _refresh_start_state(self) -> None:
        if not hasattr(self, "start_button"):
            return
        valid = bool(self.video_path.get().strip() and self.output_path.get().strip() and self.tracks)
        mode = self.source_mode.get()
        if mode == "embedded":
            valid = valid and self._selected_embedded_id() is not None
        elif mode == "external":
            valid = valid and Path(self.subtitle_path.get().strip()).is_file()
        elif mode == "online":
            valid = valid and Path(self.online_subtitle_path).is_file()
        elif mode == "audio":
            valid = valid and bool(self.audio_checks.selected_ids())
        self.start_button.config(state=tk.NORMAL if valid and not self.running else tk.DISABLED)

    def start_ai_check(self) -> None:
        self.ai_button.config(state=tk.DISABLED)
        self.ai_status.config(text="正在检测本地 AI 运行方式…")
        threading.Thread(target=self._ai_check_worker, daemon=True).start()

    def _ai_check_worker(self) -> None:
        try:
            status, detail = core.ollama_processor_status(cancel_event=self.cancel_event)
            label = {
                "gpu": "GPU 加速模式",
                "cpu": "CPU 模式，速度较慢",
                "missing": "AI 未就绪：未检测到 Ollama",
            }.get(status, "AI 状态未确认")
            self.log_queue.put(f"__AI_STATUS__:{label}|{detail}")
        except Exception as exc:
            self.log_queue.put(f"__AI_STATUS__:AI 状态检测失败|{exc}")

    def choose_video(self) -> None:
        path = filedialog.askopenfilename(title="选择影片", filetypes=[("视频文件", "*.mkv *.mp4 *.mov *.avi *.m4v"), ("所有文件", "*.*")])
        if path:
            self.set_video(path)

    def choose_subtitle(self) -> None:
        path = filedialog.askopenfilename(title="选择外挂字幕", filetypes=[("字幕文件", "*.srt *.ass *.ssa *.vtt"), ("所有文件", "*.*")])
        if path:
            self.subtitle_path.set(path)
            self._refresh_start_state()

    def open_online_search(self) -> None:
        video = self.video_path.get().strip()
        if not Path(video).is_file():
            messagebox.showwarning("缺少影片", "请先选择影片，再查找在线字幕。")
            return
        OnlineSubtitleDialog(self, video, self._online_subtitle_downloaded)

    def _online_subtitle_downloaded(self, path: str, candidate) -> None:
        self.online_subtitle_path = path
        self.online_subtitle_language = candidate.language
        self.online_status.set(f"已下载：{candidate.release}")
        self.log(f"在线字幕已下载：{path}")
        self.log("处理时将按当前保留的第一条音轨自动校正时间轴。")
        self._refresh_start_state()

    def choose_output(self) -> None:
        extension = ".mp4" if self.output_format.get() == "MP4" else ".mkv"
        path = filedialog.asksaveasfilename(defaultextension=extension, filetypes=[("视频文件", f"*{extension}")])
        if path:
            self.output_path.set(path)
            self._refresh_start_state()

    def set_video(self, path: str) -> None:
        if path != self.video_path.get():
            self.online_subtitle_path = ""
            self.online_subtitle_language = ""
            self.online_status.set("搜索并下载后，将自动按所选音轨校正时间轴。")
        self.video_path.set(path)
        self.update_output_name()
        self.load_tracks()

    def update_output_name(self) -> None:
        source = self.video_path.get().strip()
        if source:
            ext = ".mp4" if self.output_format.get() == "MP4" else ".mkv"
            self.output_path.set(str(Path(source).with_suffix("")) + ".pro" + ext)
        self._refresh_start_state()

    def load_tracks(self) -> None:
        try:
            self.tracks = core.inspect_tracks(self.video_path.get())
        except Exception as exc:
            messagebox.showerror("读取失败", f"无法读取影片轨道：\n{exc}")
            return
        audio = [track for track in self.tracks if track.type == "audio"]
        subs = [track for track in self.tracks if track.type == "subtitles"]
        self.audio_checks.set_tracks(audio, [self.track_label(track) for track in audio], select_default=True)
        source_values = []
        for track in subs:
            label = self.track_label(track)
            if track.text_subtitle or track.pgs_subtitle or track.vobsub_subtitle:
                source_values.append(f"{track.id} | {label}")
        self.subtitle_checks.set_tracks(subs, [self.track_label(track) for track in subs], select_default=False)
        self.embedded_combo["values"] = source_values
        self.embedded_combo.set(source_values[0] if source_values else "")
        if not source_values and self.source_mode.get() == "embedded":
            self.source_mode.set("none")
        self._update_source_controls()
        self.log(f"已读取：{len(audio)} 条音轨，{len(subs)} 条原字幕。")

    def track_label(self, track: core.Track) -> str:
        flag = " [默认]" if track.default else ""
        kind = (
            " [图片/PGS]" if track.pgs_subtitle
            else " [图片/VobSub]" if track.vobsub_subtitle
            else " [文本]" if track.text_subtitle
            else ""
        )
        return f"轨道 {track.id}: {track.language or 'und'} {track.codec}{flag}{kind}"

    def _selected_targets(self) -> list[str]:
        return [code for code, variable in self.target_vars.items() if variable.get()] if self.translate_enabled.get() else []

    def _selected_embedded_id(self) -> int | None:
        match = re.match(r"(\d+)\s+\|", self.embedded_source.get())
        return int(match.group(1)) if match else None

    def start(self) -> None:
        video, output, mode = self.video_path.get().strip(), self.output_path.get().strip(), self.source_mode.get()
        if not video or not output:
            messagebox.showwarning("缺少文件", "请先选择影片和输出位置。")
            return
        if mode == "embedded" and self._selected_embedded_id() is None:
            messagebox.showwarning("缺少来源字幕", "影片内没有可用于翻译的文本、PGS 或 VobSub 字幕。请选择“在线查找字幕”或“导入外挂字幕”。")
            return
        source_subtitle = self.online_subtitle_path if mode == "online" else self.subtitle_path.get().strip()
        source_language = self.online_subtitle_language if mode == "online" else ""
        if mode in {"external", "online"} and not Path(source_subtitle).is_file():
            messagebox.showwarning("缺少外挂字幕", "请选择有效的 SRT、ASS、SSA 或 VTT 文件。")
            return
        targets = self._selected_targets()
        audio_ids = self.audio_checks.selected_ids()
        subtitle_ids = self.subtitle_checks.selected_ids()
        if not audio_ids:
            messagebox.showwarning("未保留音轨", "请至少保留一条音轨；它也会作为自动对齐或语音识别的参考。")
            return
        try:
            parallel = int(self.parallel.get())
        except ValueError:
            parallel = 1
        self.running = True
        self.process_success = False
        self.cancel_event.clear()
        self.start_button.config(state=tk.DISABLED)
        self.stop_button.config(state=tk.NORMAL)
        self.set_progress(5, "准备处理")
        work = str(Path(output).with_suffix("")) + "_pro_work"
        kwargs = dict(input_path=video, output_path=output, keep_audio_ids=audio_ids, keep_subtitle_ids=subtitle_ids,
                      source_mode=mode, embedded_source_id=self._selected_embedded_id(), external_subtitle=source_subtitle or None,
                      audio_source_id=audio_ids[0], speech_language=self.speech_language.get(), source_language=source_language, target_codes=targets,
                      work_dir=work, log=self.log_queue.put, parallel_targets=parallel, cancel_event=self.cancel_event)
        threading.Thread(target=self._run, kwargs=kwargs, daemon=True).start()

    def _run(self, **kwargs) -> None:
        needs_model = bool(kwargs.get("target_codes"))
        if needs_model:
            core.begin_ollama_lease()
        try:
            result = pro_core.process_pro(**kwargs)
            self.log_queue.put(f"完成：{result}")
        except core.CancelledError:
            self.log_queue.put("已停止。未完成的正式输出已清理；工作缓存仍保留。")
        except Exception as exc:
            self.log_queue.put(f"失败：{exc}")
        finally:
            if needs_model and core.end_ollama_lease():
                core.unload_ollama_model(self.log_queue.put)
            self.log_queue.put("__DONE__")

    def stop(self) -> None:
        self.cancel_event.set()
        self.stop_button.config(state=tk.DISABLED)
        self.log("已请求停止，正在结束当前步骤…")

    def log(self, message: str) -> None:
        follow_tail = self.log_text.yview()[1] >= 0.995
        self.log_text.insert(tk.END, message + "\n")
        if follow_tail:
            self.log_text.see(tk.END)

    def set_progress(self, value: int, label: str) -> None:
        self.progress.configure(value=max(0, min(100, value)))
        self.progress_label.configure(text=label)

    def update_progress_from_log(self, message: str) -> None:
        stages = [
            ("正在按影片音轨校正", 18, "正在自动对齐外挂字幕"),
            ("外挂字幕已完成自动对齐", 48, "外挂字幕已对齐"),
            ("正在从所选音轨提取", 12, "正在提取音轨"),
            ("正在从音轨生成", 22, "正在识别语音"),
            ("正在从音轨识别对白字幕", 35, "正在识别对白字幕"),
            ("封装输出", 84, "正在封装影片"),
            ("转换为 MP4 输出", 88, "正在转换 MP4"),
            ("输出验证通过", 96, "正在验证输出"),
            ("正式输出已就绪", 100, "处理完成"),
        ]
        for marker, value, label in stages:
            if marker in message:
                self.set_progress(value, label)
                return
        match = re.search(r"翻译进度：\s*(\d+)\s*/\s*(\d+)", message)
        if match:
            done, total = int(match.group(1)), max(1, int(match.group(2)))
            self.set_progress(50 + int(done / total * 32), f"翻译字幕：{done}/{total}")

    def _poll(self) -> None:
        while True:
            try:
                message = self.log_queue.get_nowait()
            except queue.Empty:
                break
            if message == "__DONE__":
                self.running = False
                self.stop_button.config(state=tk.DISABLED)
                self._refresh_start_state()
                if self.process_success:
                    self.set_progress(100, "处理完成")
                elif self.cancel_event.is_set():
                    self.set_progress(0, "已停止")
                else:
                    self.set_progress(0, "处理失败")
            elif message.startswith("__AI_STATUS__:"):
                text = message.split(":", 1)[1]
                label, _, detail = text.partition("|")
                self.ai_status.config(text=label)
                self.ai_button.config(state=tk.NORMAL)
                self.log(detail)
            else:
                if message.startswith("完成："):
                    self.process_success = True
                    self.set_progress(100, "处理完成")
                self.update_progress_from_log(message)
                self.log(message)
        self.after(120, self._poll)

    def _setup_drop(self) -> None:
        if not DND_FILES or not hasattr(self, "drop_target_register"):
            return
        self.drop_target_register(DND_FILES)
        self.dnd_bind("<<Drop>>", self.drop)

    def drop(self, event) -> None:
        paths = self.tk.splitlist(event.data)
        for item in paths:
            suffix = Path(item).suffix.lower()
            if suffix in {".mkv", ".mp4", ".mov", ".avi", ".m4v"}:
                self.set_video(item)
            elif suffix in {".srt", ".ass", ".ssa", ".vtt"}:
                self.subtitle_path.set(item)

    def close(self) -> None:
        if self.running:
            if not messagebox.askyesno("确认退出", "任务仍在运行。是否停止任务并退出？"):
                return
            self.stop()
        self.destroy()


if __name__ == "__main__":
    ProApp().mainloop()
