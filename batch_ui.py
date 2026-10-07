# -*- coding: utf-8 -*-
from __future__ import annotations

import concurrent.futures
import os
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from online_subtitle_dialog import OnlineSubtitleDialog
import batch_core
import system_resources
from profile_model import (
    AUDIO_FORMATS,
    LANGUAGES,
    BatchPlan,
    PreferenceProfile,
    language_label,
    load_profiles,
    preset_compatible,
    preset_lazy,
    save_profiles,
)


class RowTooltip:
    def __init__(self, owner: tk.Misc, tree: ttk.Treeview, text_provider) -> None:
        self.owner = owner
        self.tree = tree
        self.text_provider = text_provider
        self.window: tk.Toplevel | None = None
        self.last_item = ""
        tree.bind("<Motion>", self._motion, add="+")
        tree.bind("<Leave>", lambda _event: self.hide(), add="+")

    def _motion(self, event) -> None:
        item = self.tree.identify_row(event.y)
        if not item:
            self.hide()
            return
        if item == self.last_item and self.window:
            return
        self.hide()
        text = self.text_provider(item)
        if not text:
            return
        self.last_item = item
        self.window = tk.Toplevel(self.owner)
        self.window.wm_overrideredirect(True)
        self.window.attributes("-topmost", True)
        label = tk.Label(
            self.window, text=text, justify=tk.LEFT, anchor=tk.W,
            bg="#153936", fg="#ffffff", padx=12, pady=9,
            font=("Microsoft YaHei UI", 9), wraplength=560,
        )
        label.pack()
        self.window.geometry(f"+{event.x_root + 18}+{event.y_root + 12}")

    def hide(self) -> None:
        self.last_item = ""
        if self.window:
            self.window.destroy()
            self.window = None


class BatchMixin:
    def _init_batch_state(self) -> None:
        self.profiles = load_profiles()
        self.batch_paths: list[str] = []
        self.batch_plans: dict[str, BatchPlan] = {}
        self.batch_queue: queue.Queue[tuple] = queue.Queue()
        self.batch_running = False
        self.batch_default_profile = tk.StringVar(value="偏好 1")
        self.batch_work_mode = tk.StringVar(value="稳定模式")
        self.batch_custom_parallel = tk.IntVar(value=2)
        self.batch_recommendation = tk.StringVar(value="推荐：正在检测")
        self.batch_hardware: system_resources.HardwareProfile | None = None
        self.batch_mode_user_selected = False
        self.batch_active_mode = "稳定模式"
        self.batch_active_workers = 1
        self.batch_process_paths: list[str] = []
        self.batch_status = tk.StringVar(value="拖入影片后，先分析全部，再处理绿色项目。")
        self.hardware_requirements = tk.StringVar(value=system_resources.hardware_requirements_text())
        self.hardware_status = tk.StringVar(value="正在检测本机硬件…")
        self.hardware_detail = tk.StringVar(value="")
        self.hardware_reason = tk.StringVar(value="")
        self.profile_slot = tk.StringVar(value="偏好 1")

    def _hardware_ui(self, parent) -> None:
        outer = ttk.Frame(parent, padding=14)
        outer.pack(fill=tk.BOTH, expand=True)
        outer.columnconfigure(0, weight=1)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(1, weight=1)
        ttk.Label(outer, text="硬件状态与运行档位", style="Title.TLabel").grid(row=0, column=0, columnspan=2, sticky=tk.W, pady=(0, 10))

        req = self._section_frame(outer, "软件配置建议", padding=12)
        req.grid(row=1, column=0, sticky=tk.NSEW, padx=(0, 7))
        ttk.Label(req, textvariable=self.hardware_requirements, style="Note.TLabel", wraplength=520, justify=tk.LEFT).pack(anchor=tk.W, fill=tk.X)

        current = self._section_frame(outer, "本机检测结果", padding=12)
        current.grid(row=1, column=1, sticky=tk.NSEW, padx=(7, 0))
        ttk.Label(current, textvariable=self.hardware_status, style="Title.TLabel", wraplength=520, justify=tk.LEFT).pack(anchor=tk.W, fill=tk.X)
        ttk.Label(current, textvariable=self.hardware_detail, style="Note.TLabel", wraplength=520, justify=tk.LEFT).pack(anchor=tk.W, fill=tk.X, pady=(12, 0))
        ttk.Label(current, textvariable=self.hardware_reason, style="Note.TLabel", wraplength=520, justify=tk.LEFT).pack(anchor=tk.W, fill=tk.X, pady=(12, 0))
        ttk.Button(current, text="重新检测", command=self._redetect_hardware).pack(anchor=tk.W, pady=(16, 0))

    def _redetect_hardware(self) -> None:
        self.hardware_status.set("正在重新检测本机硬件…")
        self.hardware_detail.set("")
        self.hardware_reason.set("")
        threading.Thread(target=self._detect_hardware_worker, daemon=True).start()

    def _batch_ui(self, parent) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)
        toolbar = ttk.Frame(parent, padding=(10, 9, 10, 6))
        toolbar.grid(row=0, column=0, sticky=tk.EW)
        ttk.Button(toolbar, text="添加影片", command=self.batch_add_files).pack(side=tk.LEFT)
        ttk.Button(toolbar, text="添加文件夹", command=self.batch_add_folder).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(toolbar, text="移除所选", command=self.batch_remove_selected).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(toolbar, text="清空", command=self.batch_clear).pack(side=tk.LEFT, padx=(6, 14))
        ttk.Label(toolbar, text="本批默认").pack(side=tk.LEFT)
        self.batch_profile_combo = ttk.Combobox(
            toolbar, textvariable=self.batch_default_profile,
            values=[f"偏好 {slot}" for slot in range(1, 5)], width=10, state="readonly",
        )
        self.batch_profile_combo.pack(side=tk.LEFT, padx=(6, 12))
        ttk.Label(toolbar, text="工作模式").pack(side=tk.LEFT)
        self.batch_mode_combo = ttk.Combobox(
            toolbar, textvariable=self.batch_work_mode,
            values=("稳定模式", "均衡模式", "高性能模式", "自定义模式"), width=10, state="readonly",
        )
        self.batch_mode_combo.pack(side=tk.LEFT, padx=(6, 5))
        self.batch_mode_combo.bind("<<ComboboxSelected>>", self._batch_mode_changed)
        self.batch_custom_frame = ttk.Frame(toolbar)
        ttk.Label(self.batch_custom_frame, text="并行数量").pack(side=tk.LEFT, padx=(0, 4))
        self.batch_parallel_spin = ttk.Spinbox(
            self.batch_custom_frame, from_=1, to=8, width=3, textvariable=self.batch_custom_parallel,
        )
        self.batch_parallel_spin.pack(side=tk.LEFT)
        self.batch_recommendation_label = ttk.Label(toolbar, textvariable=self.batch_recommendation, style="Note.TLabel")
        self.batch_recommendation_label.pack(side=tk.LEFT, padx=(7, 10))
        ttk.Button(toolbar, text="分析全部", command=self.batch_analyze_all).pack(side=tk.LEFT)
        self.batch_start_button = ttk.Button(toolbar, text="开始批量处理", style="Primary.TButton", command=self.batch_start)
        self.batch_start_button.pack(side=tk.RIGHT)
        self.batch_stop_button = ttk.Button(toolbar, text="停止", command=self.batch_stop, state=tk.DISABLED)
        self.batch_stop_button.pack(side=tk.RIGHT, padx=(0, 7))

        table_card = self._section_frame(parent, "批量影片与处理方案", padding=7)
        table_card.grid(row=1, column=0, sticky=tk.NSEW, padx=10, pady=(0, 6))
        table_card.columnconfigure(0, weight=1)
        table_card.rowconfigure(0, weight=1)
        columns = ("file", "audio", "codec", "subs", "profile", "plan", "status")
        self.batch_tree = ttk.Treeview(table_card, columns=columns, show="headings", selectmode="extended")
        headings = {
            "file": "影片", "audio": "主音轨", "codec": "音频编码", "subs": "现有字幕",
            "profile": "套用偏好", "plan": "最终处理方案", "status": "状态",
        }
        widths = {"file": 250, "audio": 130, "codec": 120, "subs": 125, "profile": 90, "plan": 330, "status": 90}
        for column in columns:
            self.batch_tree.heading(column, text=headings[column])
            self.batch_tree.column(column, width=widths[column], minwidth=70, stretch=column in {"file", "plan"})
        self.batch_tree.tag_configure("ready", background="#eaf7f2", foreground="#17473f")
        self.batch_tree.tag_configure("completed", background="#eaf7f2", foreground="#17473f")
        self.batch_tree.tag_configure("processing", background="#fff1d6", foreground="#7a4a00")
        self.batch_tree.tag_configure("review", background="#fff4dc", foreground="#795112")
        self.batch_tree.tag_configure("blocked", background="#fde9e7", foreground="#8a2d25")
        self.batch_tree.tag_configure("pending", background="#f7f9f8", foreground="#546967")
        scroll_y = ttk.Scrollbar(table_card, orient=tk.VERTICAL, command=self.batch_tree.yview)
        scroll_x = ttk.Scrollbar(table_card, orient=tk.HORIZONTAL, command=self.batch_tree.xview)
        self.batch_tree.configure(yscrollcommand=scroll_y.set, xscrollcommand=scroll_x.set)
        self.batch_tree.grid(row=0, column=0, sticky=tk.NSEW)
        scroll_y.grid(row=0, column=1, sticky=tk.NS)
        scroll_x.grid(row=1, column=0, sticky=tk.EW)
        self.batch_tree.bind("<Button-1>", self._batch_table_click, add="+")
        self.batch_tree.bind("<Double-1>", self._batch_double_click, add="+")
        RowTooltip(self, self.batch_tree, self._batch_tooltip_text)

        log_card = ttk.LabelFrame(parent, text=" 处理日志 ", style="Card.TLabelframe", padding=7)
        log_card.grid(row=2, column=0, sticky=tk.NSEW, padx=10, pady=(0, 10))
        log_card.columnconfigure(0, weight=1)
        log_card.rowconfigure(1, weight=1)
        batch_log_top = ttk.Frame(log_card)
        batch_log_top.grid(row=0, column=0, columnspan=2, sticky=tk.EW, pady=(0, 6))
        ttk.Label(batch_log_top, textvariable=self.batch_status, style="Note.TLabel").pack(side=tk.LEFT)
        self.batch_progress = ttk.Progressbar(batch_log_top, mode="determinate", maximum=100, value=0, style="Orange.Horizontal.TProgressbar")
        self.batch_progress.pack(side=tk.RIGHT, fill=tk.X, expand=True, padx=(16, 0))
        self.batch_log_scrollbar = ttk.Scrollbar(log_card, orient=tk.VERTICAL)
        self.batch_log_text = tk.Text(
            log_card, height=8, wrap=tk.WORD, relief="flat",
            bg="#fbfdfd", fg="#243f3d", font=("Consolas", 9),
            yscrollcommand=self.batch_log_scrollbar.set,
        )
        self.batch_log_scrollbar.configure(command=self.batch_log_text.yview)
        self.batch_log_text.grid(row=1, column=0, sticky=tk.NSEW)
        self.batch_log_scrollbar.grid(row=1, column=1, sticky=tk.NS)

        self.after(120, self._batch_poll)
        threading.Thread(target=self._detect_hardware_worker, daemon=True).start()

    def _batch_mode_changed(self, _event=None) -> None:
        self.batch_mode_user_selected = True
        custom = self.batch_work_mode.get() == "自定义模式"
        self._show_batch_custom_parallel(custom)
        if not self.batch_running:
            self.batch_status.set(f"已选择{self.batch_work_mode.get()}。")

    def _show_batch_custom_parallel(self, visible: bool) -> None:
        if visible:
            if not self.batch_custom_frame.winfo_manager():
                self.batch_custom_frame.pack(side=tk.LEFT, before=self.batch_recommendation_label, padx=(0, 5))
            self.batch_parallel_spin.configure(state=tk.NORMAL if not self.batch_running else tk.DISABLED)
        elif self.batch_custom_frame.winfo_manager():
            self.batch_custom_frame.pack_forget()

    def _detect_hardware_worker(self) -> None:
        try:
            profile = system_resources.detect_hardware()
            self.batch_queue.put(("hardware", profile))
        except Exception:
            self.batch_queue.put(("hardware", None))

    def _batch_movie_parallelism(self) -> int:
        if self.batch_hardware is not None:
            return self.batch_hardware.batch_movie_workers
        return 1

    def _profiles_ui(self, parent) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)
        top = ttk.Frame(parent, padding=10)
        top.grid(row=0, column=0, sticky=tk.EW)
        ttk.Label(top, text="编辑偏好").pack(side=tk.LEFT)
        combo = ttk.Combobox(top, textvariable=self.profile_slot, values=[f"偏好 {i}" for i in range(1, 5)], width=10, state="readonly")
        combo.pack(side=tk.LEFT, padx=(7, 18))
        combo.bind("<<ComboboxSelected>>", lambda _event: self._load_profile_editor())
        ttk.Label(top, text="推荐方案：", style="Note.TLabel").pack(side=tk.LEFT)
        ttk.Button(top, text="! 兼容偏好", command=lambda: self._apply_profile_preset("compatible")).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(top, text="! 懒人偏好", command=lambda: self._apply_profile_preset("lazy")).pack(side=tk.LEFT, padx=(6, 0))
        ttk.Button(top, text="保存偏好", style="Primary.TButton", command=self._save_profile_editor).pack(side=tk.RIGHT)

        body = ttk.Frame(parent, padding=(10, 0, 10, 10))
        body.grid(row=1, column=0, sticky=tk.NSEW)
        body.columnconfigure(0, weight=1)
        body.columnconfigure(1, weight=1)
        audio = self._section_frame(body, "音频兼容偏好", padding=12)
        audio.grid(row=0, column=0, sticky=tk.NSEW, padx=(0, 6))
        subtitles = self._section_frame(body, "最终需要的字幕", padding=12)
        subtitles.grid(row=0, column=1, sticky=tk.NSEW, padx=(6, 0))

        self.profile_name = tk.StringVar()
        name_row = ttk.Frame(audio)
        name_row.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(name_row, text="偏好名称").pack(side=tk.LEFT)
        ttk.Entry(name_row, textvariable=self.profile_name).pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(8, 0))
        ttk.Label(audio, text="多音轨时优先保留下列兼容编码；只有一条音轨时无条件保留。", style="Note.TLabel").pack(anchor=tk.W)
        self.profile_audio_vars = {name: tk.BooleanVar() for name in AUDIO_FORMATS}
        audio_grid = ttk.Frame(audio)
        audio_grid.pack(fill=tk.X, pady=8)
        for index, (name, variable) in enumerate(self.profile_audio_vars.items()):
            self._make_toggle(audio_grid, name, variable).grid(row=index // 2, column=index % 2, sticky=tk.EW, padx=(0, 6), pady=2)
        audio_grid.columnconfigure(0, weight=1)
        audio_grid.columnconfigure(1, weight=1)
        self.profile_keep_original = tk.BooleanVar()
        self.profile_create_audio = tk.BooleanVar()
        self.profile_prefer_compatible = tk.BooleanVar()
        self.profile_lazy_audio = tk.BooleanVar()
        self.profile_prefer_compatible.trace_add("write", lambda *_: self._sync_audio_mode("compatible"))
        self.profile_lazy_audio.trace_add("write", lambda *_: self._sync_audio_mode("lazy"))
        self._make_toggle(audio, "保留原主音轨", self.profile_keep_original).pack(fill=tk.X, pady=2)
        self._make_toggle(audio, "缺少所选兼容编码时，由主音轨生成第一种编码", self.profile_create_audio).pack(fill=tk.X, pady=2)
        self._make_toggle(audio, "兼容音轨：以最兼容轻音轨为主，但不删除重音轨", self.profile_prefer_compatible).pack(fill=tk.X, pady=2)
        self._make_toggle(audio, "懒人音轨：重音轨为主；按情况补/留一条轻音轨", self.profile_lazy_audio).pack(fill=tk.X, pady=2)
        ttk.Label(audio, text="生成 AAC/AC-3 等只转换音频，不转换视频；不会把轻音轨伪装成无损音轨。", style="Note.TLabel", wraplength=500).pack(anchor=tk.W, pady=(8, 0))

        ttk.Label(subtitles, text="勾选即表示最终文件需要该字幕；已有则保留，缺少则翻译。", style="Note.TLabel").pack(anchor=tk.W)
        self.profile_subtitle_vars = {code: tk.BooleanVar() for code, _label in LANGUAGES}
        subs_grid = ttk.Frame(subtitles)
        subs_grid.pack(fill=tk.X, pady=8)
        for index, (code, label) in enumerate(LANGUAGES):
            self._make_toggle(subs_grid, label, self.profile_subtitle_vars[code]).grid(row=index // 2, column=index % 2, sticky=tk.EW, padx=(0, 6), pady=2)
        subs_grid.columnconfigure(0, weight=1)
        subs_grid.columnconfigure(1, weight=1)
        self._load_profile_editor()

    def _profile_index(self, value: str | None = None) -> int:
        text = value or self.profile_slot.get()
        try:
            return max(0, min(3, int(text.split()[-1]) - 1))
        except (ValueError, IndexError):
            return 0

    def _load_profile_values(self, profile: PreferenceProfile) -> None:
        self.profile_name.set(profile.name)
        for name, variable in self.profile_audio_vars.items():
            variable.set(name in profile.audio_formats)
        self.profile_keep_original.set(profile.keep_original_audio)
        self.profile_create_audio.set(profile.create_missing_audio)
        self.profile_prefer_compatible.set(profile.prefer_compatible_main_audio)
        self.profile_lazy_audio.set(profile.lazy_audio_mode)
        for code, variable in self.profile_subtitle_vars.items():
            variable.set(code in profile.subtitle_languages)

    def _load_profile_editor(self) -> None:
        self._load_profile_values(self.profiles[self._profile_index()])

    def _sync_audio_mode(self, mode: str) -> None:
        try:
            if mode == "compatible" and self.profile_prefer_compatible.get() and self.profile_lazy_audio.get():
                self.profile_lazy_audio.set(False)
            elif mode == "lazy" and self.profile_lazy_audio.get() and self.profile_prefer_compatible.get():
                self.profile_prefer_compatible.set(False)
        except tk.TclError:
            pass

    def _apply_profile_preset(self, kind: str) -> None:
        slot = self._profile_index() + 1
        profile = preset_lazy(slot) if kind == "lazy" else preset_compatible(slot)
        self._load_profile_values(profile)
        name = "懒人偏好" if kind == "lazy" else "兼容偏好"
        self.batch_status.set(f"已套用{name}到当前编辑框；点“保存偏好”后才会写入该槽位。")

    def _save_profile_editor(self) -> None:
        index = self._profile_index()
        formats = [name for name, variable in self.profile_audio_vars.items() if variable.get()]
        languages = [code for code, variable in self.profile_subtitle_vars.items() if variable.get()]
        if not formats and self.profile_create_audio.get():
            messagebox.showwarning("偏好未完成", "启用兼容音轨生成时，至少选择一种音频编码。")
            return
        profile = PreferenceProfile(
            slot=index + 1,
            name=self.profile_name.get().strip() or f"偏好 {index + 1}",
            audio_formats=formats,
            keep_original_audio=self.profile_keep_original.get(),
            create_missing_audio=self.profile_create_audio.get(),
            subtitle_languages=languages,
            prefer_compatible_main_audio=(self.profile_prefer_compatible.get() and not self.profile_lazy_audio.get()),
            lazy_audio_mode=self.profile_lazy_audio.get(),
        )
        self.profiles[index] = profile
        save_profiles(self.profiles)
        self.batch_status.set(f"偏好 {profile.slot} 已保存；重新分析后应用新规则。")

    def batch_add_files(self) -> None:
        paths = filedialog.askopenfilenames(title="添加批量影片", filetypes=[("视频文件", "*.mkv *.mp4 *.mov *.avi *.m4v"), ("所有文件", "*.*")])
        self._batch_add_paths(paths)

    def batch_add_folder(self) -> None:
        folder = filedialog.askdirectory(title="添加影片文件夹（包含所有下级文件夹）")
        if folder:
            paths = batch_core.videos_in_folder(folder)
            self._batch_add_paths(paths)
            if not paths:
                self.batch_status.set("所选文件夹及其下级文件夹中未发现支持的视频文件。")

    def _batch_add_paths(self, paths) -> None:
        added = 0
        default_slot = self._profile_index(self.batch_default_profile.get()) + 1
        existing_paths = {os.path.normcase(os.path.abspath(path)) for path in self.batch_paths}
        for raw in paths:
            path = str(Path(raw))
            normalized_path = os.path.normcase(os.path.abspath(path))
            if batch_core.is_output_path(path):
                continue
            if Path(path).suffix.lower() not in batch_core.VIDEO_EXTENSIONS or normalized_path in existing_paths:
                continue
            self.batch_paths.append(path)
            existing_paths.add(normalized_path)
            plan = BatchPlan(path=path, profile_slot=default_slot, output_path=str(Path(path).with_suffix("")) + batch_core.OUTPUT_SUFFIX)
            self.batch_plans[path] = plan
            self._batch_render(plan)
            added += 1
        self.batch_status.set(f"已添加 {added} 部影片，共 {len(self.batch_paths)} 部。点击“分析全部”。")
        self._batch_log(f"已添加 {added} 部影片，共 {len(self.batch_paths)} 部。")

    def batch_remove_selected(self) -> None:
        selected = list(self.batch_tree.selection())
        for item in selected:
            path = self.batch_tree.item(item, "text") or item
            if path in self.batch_paths:
                self.batch_paths.remove(path)
                self.batch_plans.pop(path, None)
            self.batch_tree.delete(item)
        self.batch_status.set(f"当前批次 {len(self.batch_paths)} 部影片。")

    def batch_clear(self) -> None:
        if self.batch_running:
            return
        self.batch_paths.clear()
        self.batch_plans.clear()
        for item in self.batch_tree.get_children():
            self.batch_tree.delete(item)
        self.batch_progress.configure(value=0)
        if hasattr(self, "batch_log_text"):
            self.batch_log_text.delete("1.0", tk.END)
        self.batch_status.set("批次已清空。")

    def _batch_item_id(self, path: str) -> str:
        return path

    def _batch_render(self, plan: BatchPlan) -> None:
        item = self._batch_item_id(plan.path)
        status_label = plan.status_label
        if plan.status == "review" and "字幕" in (plan.summary + plan.detail):
            status_label = "需确认：缺字幕"
        values = (
            Path(plan.path).name, plan.main_audio or "待分析", plan.audio_codecs or "待分析",
            plan.subtitles or "待分析", f"偏好 {plan.profile_slot}", plan.summary or "待分析", status_label,
        )
        if self.batch_tree.exists(item):
            self.batch_tree.item(item, values=values, tags=(plan.status,))
        else:
            self.batch_tree.insert("", tk.END, iid=item, text=plan.path, values=values, tags=(plan.status,))

    def batch_analyze_all(self) -> None:
        if self.batch_running or not self.batch_paths:
            return
        self.batch_running = True
        self.cancel_event.clear()
        self.batch_start_button.config(state=tk.DISABLED)
        self.batch_stop_button.config(state=tk.NORMAL)
        self.batch_progress.configure(value=0)
        self.batch_status.set("正在分析影片轨道…")
        threading.Thread(target=self._batch_analyze_worker, daemon=True).start()

    def _analysis_failure_plan(self, path: str, profile: PreferenceProfile, exc: Exception) -> BatchPlan:
        detail = str(exc) or repr(exc)
        lowered = detail.lower()
        summary = "读取失败"
        if "mkvmerge" in lowered or "mkvtoolnix" in lowered:
            summary = "读取失败：找不到 mkvmerge"
            detail += "\n分析 MKV/MP4 轨道需要 MKVToolNix 的 mkvmerge.exe。"
        elif "ffmpeg" in lowered:
            summary = "读取失败：找不到 ffmpeg"
        return BatchPlan(
            path=path, profile_slot=profile.slot, status="blocked", status_label="无法处理",
            summary=summary, detail=detail,
        )

    def _batch_analyze_worker(self) -> None:
        total = max(1, len(self.batch_paths))
        for index, path in enumerate(list(self.batch_paths), 1):
            if self.cancel_event.is_set():
                break
            old = self.batch_plans[path]
            profile = self.profiles[old.profile_slot - 1]
            try:
                plan = batch_core.analyze_video(path, profile, old.external_subtitle, old.external_language)
            except Exception as exc:
                plan = self._analysis_failure_plan(path, profile, exc)
            self.batch_queue.put(("plan", plan))
            if plan.status != "ready":
                self.batch_queue.put(("log", path, f"{plan.summary or plan.status_label}：{(plan.detail or '').splitlines()[0] if plan.detail else ''}"))
            self.batch_queue.put(("progress", int(index / total * 100), f"已分析 {index}/{total}"))
        self.batch_queue.put(("analyze_done",))

    def _batch_table_click(self, event) -> None:
        row = self.batch_tree.identify_row(event.y)
        column = self.batch_tree.identify_column(event.x)
        if not row or column != "#5" or self.batch_running:
            return
        menu = tk.Menu(self, tearoff=False)
        for slot in range(1, 5):
            menu.add_command(label=f"偏好 {slot} · {self.profiles[slot - 1].name}", command=lambda value=slot, item=row: self._change_row_profile(item, value))
        menu.tk_popup(event.x_root, event.y_root)

    def _change_row_profile(self, item: str, slot: int) -> None:
        path = item
        plan = self.batch_plans[path]
        plan.profile_slot = slot
        plan.status = "pending"
        plan.status_label = "待重新分析"
        plan.summary = "偏好已修改"
        self._batch_render(plan)
        self.batch_status.set(f"{Path(path).name} 已改为偏好 {slot}；请重新分析。")

    def _batch_double_click(self, event) -> None:
        row = self.batch_tree.identify_row(event.y)
        if not row or self.batch_running:
            return
        plan = self.batch_plans[row]
        if plan.status == "review" and "字幕" in (plan.summary + plan.detail):
            self.batch_status.set("正在打开在线字幕搜索；下载后会自动重新分析该影片。")
            OnlineSubtitleDialog(self, plan.path, lambda path, candidate, item=row: self._batch_subtitle_downloaded(item, path, candidate))
        elif plan.status != "ready":
            messagebox.showinfo(plan.status_label, plan.detail or plan.summary)

    def _batch_subtitle_downloaded(self, item: str, path: str, candidate) -> None:
        plan = self.batch_plans[item]
        plan.external_subtitle = path
        plan.external_language = candidate.language
        try:
            refreshed = batch_core.analyze_video(plan.path, self.profiles[plan.profile_slot - 1], path, candidate.language)
            self.batch_plans[item] = refreshed
            self._batch_render(refreshed)
            self.batch_status.set(f"已下载并套用字幕：{candidate.release}")
        except Exception as exc:
            messagebox.showerror("重新分析失败", str(exc))

    def _batch_tooltip_text(self, item: str) -> str:
        plan = self.batch_plans.get(item)
        return plan.detail if plan else ""

    def batch_start(self) -> None:
        if self.batch_running or not self.batch_paths:
            return
        ready_paths = [path for path in self.batch_paths if self.batch_plans[path].status == "ready"]
        skipped = [self.batch_plans[path] for path in self.batch_paths if self.batch_plans[path].status != "ready"]
        if not ready_paths:
            messagebox.showwarning("没有可处理影片", "当前没有绿色“可以处理”的影片。请先分析或解决黄色、红色项目。")
            return
        okay, detail = batch_core.enough_space(ready_paths)
        if not okay:
            messagebox.showerror("磁盘空间不足", detail)
            return
        skipped_text = f"\n\n将跳过 {len(skipped)} 部黄色/红色项目。" if skipped else ""
        if not messagebox.askyesno("开始批量处理", f"将以{self.batch_work_mode.get()}处理 {len(ready_paths)} 部绿色影片。{skipped_text}\n\n{detail}\n\n是否继续？"):
            return
        self.batch_process_paths = ready_paths
        for plan in skipped:
            self._batch_log(f"{Path(plan.path).name}：已跳过，原因：{plan.summary or plan.status_label}")
        self.batch_running = True
        self.cancel_event.clear()
        if self.batch_hardware is not None:
            self.batch_work_mode.set(self.batch_hardware.recommended_mode)
        self.batch_active_mode = f"智能模式（{self.batch_work_mode.get()}）"
        self.batch_active_workers = self._batch_movie_parallelism()
        self.batch_start_button.config(state=tk.DISABLED)
        self.batch_stop_button.config(state=tk.NORMAL)
        self.batch_mode_combo.configure(state=tk.DISABLED)
        self.batch_parallel_spin.configure(state=tk.DISABLED)
        self.batch_progress.configure(value=0)
        threading.Thread(target=self._batch_process_worker, daemon=True).start()

    def _batch_process_worker(self) -> None:
        process_paths = list(self.batch_process_paths) or [
            path for path in self.batch_paths if self.batch_plans[path].status == "ready"
        ]
        total = max(1, len(process_paths))
        completed = 0
        failed = 0
        workers = self.batch_active_workers
        parallel_targets = 2 if workers == 1 else 1
        ai_slots = 1
        if self.batch_hardware is not None:
            ai_slots = max(1, min(workers, self.batch_hardware.ai_slots))
        ai_gate = threading.Semaphore(ai_slots)
        guard = system_resources.RuntimeGuard()
        pending = list(enumerate(process_paths, 1))
        active: dict[concurrent.futures.Future, tuple[int, str]] = {}
        last_wait_reason = ""
        self.batch_queue.put(("status", f"{self.batch_active_mode}已启动。"))

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="movie-batch") as executor:
            while pending or active:
                if self.cancel_event.is_set():
                    pending.clear()

                if pending and len(active) < workers and not self.cancel_event.is_set():
                    allowed, reason = guard.can_start_next()
                    if allowed:
                        index, path = pending.pop(0)
                        plan = self.batch_plans[path]
                        if plan.status != "ready":
                            self.batch_queue.put(("log", path, f"已跳过，原因：{plan.summary or plan.status_label}"))
                            continue
                        profile = self.profiles[plan.profile_slot - 1]
                        plan.status = "processing"
                        plan.status_label = "正在处理"
                        self.batch_queue.put(("plan", plan))
                        self.batch_queue.put(("log", path, f"开始处理 {index}/{total}"))
                        future = executor.submit(
                            batch_core.process_plan,
                            plan,
                            profile,
                            lambda message, item=path: self.batch_queue.put(("log", item, message)),
                            self.cancel_event,
                            parallel_targets,
                            ai_gate,
                        )
                        active[future] = (index, path)
                        last_wait_reason = ""
                        active_names = "；".join(Path(active_path).name for _active_index, active_path in active.values())
                        self.batch_queue.put(("status", f"正在处理：{active_names}"))
                    elif reason != last_wait_reason:
                        last_wait_reason = reason
                        self.batch_queue.put(("status", reason))

                if not active:
                    if pending and not self.cancel_event.is_set():
                        time.sleep(1.0)
                        continue
                    break

                done, _not_done = concurrent.futures.wait(
                    active, timeout=1.0, return_when=concurrent.futures.FIRST_COMPLETED,
                )
                for future in done:
                    _index, path = active.pop(future)
                    plan = self.batch_plans[path]
                    try:
                        future.result()
                        plan.status = "completed"
                        plan.status_label = "已完成"
                        completed += 1
                    except Exception as exc:
                        batch_core.append_process_log(plan, f"处理失败：{exc}")
                        plan.status = "blocked"
                        plan.status_label = "处理失败"
                        plan.detail += f"\n失败：{exc}"
                        plan.summary = f"失败：{exc}"
                        failed += 1
                    self.batch_queue.put(("plan", plan))
                    finished = completed + failed
                    self.batch_queue.put(("progress", int(finished / total * 100), f"已处理 {finished}/{total}"))
        self.batch_queue.put(("process_done", completed, failed, self.cancel_event.is_set()))

    def batch_stop(self) -> None:
        self.cancel_event.set()
        self.batch_stop_button.config(state=tk.DISABLED)
        self.batch_status.set("正在停止当前外部工具；未开始的影片不会处理。")

    def _batch_poll(self) -> None:
        processed = 0
        deadline = time.monotonic() + 0.05
        while processed < 80 and time.monotonic() < deadline:
            try:
                event = self.batch_queue.get_nowait()
            except queue.Empty:
                break
            processed += 1
            kind = event[0]
            if kind == "plan":
                plan = event[1]
                self.batch_plans[plan.path] = plan
                self._batch_render(plan)
            elif kind == "hardware":
                profile = event[1]
                self.batch_hardware = profile
                if profile is None:
                    self.batch_recommendation.set("硬件：未检测，建议稳定模式")
                    self.hardware_status.set("硬件检测失败")
                    self.hardware_detail.set("无法读取本机 CPU、内存或显卡信息。")
                    self.hardware_reason.set("简短原因：检测命令没有返回有效结果。")
                else:
                    self.batch_recommendation.set(f"硬件：{profile.tier_label} · 建议{profile.recommended_mode}")
                    self.hardware_status.set(system_resources.hardware_status_text(profile))
                    self.hardware_detail.set(system_resources.hardware_detail_text(profile))
                    self.hardware_reason.set(system_resources.hardware_reason_text(profile))
                    if not self.batch_mode_user_selected and not self.batch_running:
                        self.batch_work_mode.set(profile.recommended_mode)
                        self._show_batch_custom_parallel(False)
            elif kind == "progress":
                self.batch_progress.configure(value=event[1])
                self.batch_status.set(event[2])
            elif kind == "status":
                self.batch_status.set(event[1])
            elif kind == "log":
                self._batch_log(f"{Path(event[1]).name}：{event[2]}")
            elif kind == "analyze_done":
                self.batch_running = False
                self.batch_stop_button.config(state=tk.DISABLED)
                self.batch_start_button.config(state=tk.NORMAL)
                ready = sum(plan.status == "ready" for plan in self.batch_plans.values())
                review = sum(plan.status == "review" for plan in self.batch_plans.values())
                blocked = sum(plan.status == "blocked" for plan in self.batch_plans.values())
                message = f"分析完成：可处理 {ready}，需确认 {review}，无法处理 {blocked}。偏好列可直接点击修改；悬停任意行查看完整方案。"
                self.batch_status.set(message)
                self._batch_log(message)
            elif kind == "process_done":
                self.batch_running = False
                self.batch_stop_button.config(state=tk.DISABLED)
                self.batch_start_button.config(state=tk.NORMAL)
                self.batch_mode_combo.configure(state="readonly")
                self._show_batch_custom_parallel(self.batch_work_mode.get() == "自定义模式")
                completed, failed, cancelled = event[1:]
                skipped = len(self.batch_paths) - len(self.batch_process_paths)
                suffix = f"，跳过 {skipped}" if skipped > 0 else ""
                message = f"{'已停止' if cancelled else '批量完成'}：成功 {completed}，失败 {failed}{suffix}。"
                self.batch_process_paths = []
                self.batch_status.set(message)
                self._batch_log(message)
        self.after(20 if not self.batch_queue.empty() else 120, self._batch_poll)

    def _batch_log(self, message: str) -> None:
        if not hasattr(self, "batch_log_text"):
            return
        follow_tail = self.batch_log_text.yview()[1] >= 0.995
        self.batch_log_text.insert(tk.END, message + "\n")
        if follow_tail:
            self.batch_log_text.see(tk.END)
