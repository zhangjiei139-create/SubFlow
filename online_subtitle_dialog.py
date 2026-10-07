# -*- coding: utf-8 -*-
from __future__ import annotations

import threading
import webbrowser
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

import online_subtitles as opensubtitles
import subdl_subtitles as subdl
from subtitle_identity_guard import identity_conflict_notice


LANGUAGES = [
    ("zh-cn", "简体中文"),
    ("zh-tw", "繁体中文"),
    ("en", "英文"),
    ("ja", "日文"),
    ("ko", "韩文"),
    ("es", "西班牙语"),
    ("fr", "法语"),
    ("de", "德语"),
    ("ru", "俄语"),
]

PROVIDERS = {
    "opensubtitles": {
        "name": "OpenSubtitles",
        "service": opensubtitles,
        "setting": "opensubtitles_api_key",
        "key_label": "OpenSubtitles API Key",
        "key_url": "https://www.opensubtitles.com/consumers",
    },
    "subdl": {
        "name": "SubDL",
        "service": subdl,
        "setting": "subdl_api_key",
        "key_label": "SubDL API Key",
        "key_url": "https://subdl.com/panel/api",
    },
}


class OnlineSubtitleDialog(tk.Toplevel):
    def __init__(self, master, video_path: str, on_download) -> None:
        owner = master.winfo_toplevel() if master is not None else None
        super().__init__(owner)
        self.withdraw()
        self.title("在线查找字幕")
        self.minsize(760, 460)
        self.transient(owner)
        self._owner = owner
        self.video_path = video_path
        self.on_download = on_download
        self.provider = tk.StringVar(value="opensubtitles")
        settings = opensubtitles.load_settings()
        self.key_vars = {
            code: tk.StringVar(value=str(settings.get(info["setting"], "")))
            for code, info in PROVIDERS.items()
        }
        self.language_label = tk.StringVar(value="简体中文")
        self.key_label = tk.StringVar()
        self.status = tk.StringVar(value="选择字幕站和语言后开始搜索。")
        self.results = []
        self.result_provider = ""
        self.identity = opensubtitles.identify_media(video_path)
        self._build()
        self._provider_changed(clear_results=False)
        self._show_centered()

    def _show_centered(self) -> None:
        self._place_over_owner()
        self.deiconify()
        if self._owner is not None:
            self.lift(self._owner)
        else:
            self.lift()
        self.after_idle(self._place_over_owner)
        self.after(120, self._place_over_owner)

    def _place_over_owner(self) -> None:
        self.update_idletasks()
        owner = self._owner
        try:
            if owner is None or not owner.winfo_exists():
                raise tk.TclError
            owner.update_idletasks()
            owner_x = owner.winfo_rootx()
            owner_y = owner.winfo_rooty()
            owner_w = owner.winfo_width()
            owner_h = owner.winfo_height()
            if owner_w <= 1 or owner_h <= 1:
                raise tk.TclError
        except tk.TclError:
            owner_x = 0
            owner_y = 0
            owner_w = self.winfo_screenwidth()
            owner_h = self.winfo_screenheight()

        width = max(760, min(920, int(owner_w * 0.68)))
        height = max(460, min(560, int(owner_h * 0.66)))
        x = owner_x + max(0, (owner_w - width) // 2)
        y = owner_y + max(0, (owner_h - height) // 2)

        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        x = max(0, min(x, screen_w - width))
        y = max(0, min(y, screen_h - height))
        self.geometry(f"{width}x{height}+{x}+{y}")

    def _build(self) -> None:
        outer = ttk.Frame(self, padding=14)
        outer.pack(fill=tk.BOTH, expand=True)
        outer.columnconfigure(1, weight=1)
        outer.rowconfigure(4, weight=1)

        identity = f"{self.identity.title or '未识别片名'} {self.identity.year}".strip()
        ttk.Label(
            outer,
            text=f"识别结果：{identity} · 站点结果按返回顺序显示，下载后验证正文",
        ).grid(row=0, column=0, columnspan=6, sticky=tk.W, pady=(0, 10))

        provider_row = ttk.Frame(outer)
        provider_row.grid(row=1, column=0, columnspan=6, sticky=tk.EW, pady=(0, 8))
        ttk.Label(provider_row, text="字幕站").pack(side=tk.LEFT, padx=(0, 10))
        for code, info in PROVIDERS.items():
            ttk.Radiobutton(
                provider_row,
                text=info["name"],
                value=code,
                variable=self.provider,
                command=self._provider_changed,
            ).pack(side=tk.LEFT, padx=(0, 16))
        ttk.Label(
            provider_row,
            text="一次搜索一个站；无结果时切换另一站，避免重复消耗额度。",
            foreground="#687b78",
        ).pack(side=tk.LEFT, padx=(8, 0))

        ttk.Label(outer, textvariable=self.key_label).grid(row=2, column=0, sticky=tk.W)
        self.key_entry = ttk.Entry(outer, show="●")
        self.key_entry.grid(row=2, column=1, sticky=tk.EW, padx=(8, 8))
        ttk.Button(outer, text="获取 Key", command=self._open_key_page).grid(row=2, column=2, padx=(0, 8))
        self.verify_button = ttk.Button(outer, command=self._save_or_verify)
        self.verify_button.grid(row=2, column=3, padx=(0, 14))

        ttk.Label(outer, text="字幕语言").grid(row=3, column=0, sticky=tk.W, pady=(9, 0))
        ttk.Combobox(
            outer,
            textvariable=self.language_label,
            values=[label for _, label in LANGUAGES],
            width=12,
            state="readonly",
        ).grid(row=3, column=1, sticky=tk.W, padx=(8, 8), pady=(9, 0))
        self.search_button = ttk.Button(outer, text="搜索字幕", command=self.start_search)
        self.search_button.grid(row=3, column=2, padx=(0, 8), pady=(9, 0))

        columns = ("source", "match", "release", "language", "downloads", "rating", "flags")
        self.tree = ttk.Treeview(outer, columns=columns, show="headings", selectmode="browse")
        headings = {
            "source": "来源",
            "match": "筛选状态",
            "release": "字幕版本 / 片源",
            "language": "语言",
            "downloads": "下载量",
            "rating": "评分",
            "flags": "标记",
        }
        for column, label in headings.items():
            self.tree.heading(column, text=label)
        self.tree.column("source", width=105, anchor=tk.CENTER, stretch=False)
        self.tree.column("match", width=105, anchor=tk.CENTER, stretch=False)
        self.tree.column("release", width=480, stretch=True)
        self.tree.column("language", width=70, anchor=tk.CENTER, stretch=False)
        self.tree.column("downloads", width=75, anchor=tk.E, stretch=False)
        self.tree.column("rating", width=55, anchor=tk.CENTER, stretch=False)
        self.tree.column("flags", width=90, anchor=tk.CENTER, stretch=False)
        scrollbar = ttk.Scrollbar(outer, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.grid(row=4, column=0, columnspan=5, sticky=tk.NSEW, pady=(10, 8))
        scrollbar.grid(row=4, column=5, sticky=tk.NS, pady=(10, 8))

        ttk.Label(outer, textvariable=self.status).grid(row=5, column=0, columnspan=3, sticky=tk.W)
        ttk.Button(outer, text="关闭", command=self.destroy).grid(row=5, column=3, sticky=tk.E)
        self.download_button = ttk.Button(
            outer,
            text="下载并使用",
            command=self.start_download,
            state=tk.DISABLED,
        )
        self.download_button.grid(row=5, column=4, sticky=tk.E, padx=(8, 0))
        self.tree.bind("<<TreeviewSelect>>", self._selection_changed)

    def _info(self) -> dict:
        return PROVIDERS[self.provider.get()]

    def _provider_changed(self, clear_results: bool = True) -> None:
        info = self._info()
        self.key_label.set(info["key_label"])
        self.key_entry.configure(textvariable=self.key_vars[self.provider.get()])
        self.verify_button.configure(text="验证并保存" if self.provider.get() == "subdl" else "保存 Key")
        if clear_results:
            self.results = []
            self.result_provider = ""
            self.tree.delete(*self.tree.get_children())
            self.download_button.configure(state=tk.DISABLED)
            self.status.set(f"已切换到 {info['name']}，选择语言后开始搜索。")

    def _language_code(self) -> str:
        return next((code for code, label in LANGUAGES if label == self.language_label.get()), "zh-cn")

    def _open_key_page(self) -> None:
        webbrowser.open(self._info()["key_url"])

    def _save_or_verify(self) -> None:
        provider = self.provider.get()
        key = self.key_vars[provider].get().strip()
        info = PROVIDERS[provider]
        if not key:
            messagebox.showwarning("缺少 API Key", f"请先填写 {info['key_label']}。", parent=self)
            return
        if provider == "opensubtitles":
            opensubtitles.save_settings(key)
            self.status.set("OpenSubtitles API Key 已保存；首次搜索会同时验证有效性。")
            return
        self.verify_button.configure(state=tk.DISABLED)
        self.status.set("正在验证 SubDL API Key…")
        threading.Thread(target=self._verify_subdl_worker, args=(key,), daemon=True).start()

    def _verify_subdl_worker(self, key: str) -> None:
        try:
            quota = subdl.validate_token(key)
            subdl.save_settings(key)
            self.after(0, lambda: self._verify_subdl_done(quota))
        except Exception as exc:
            message = str(exc)
            self.after(0, lambda message=message: self._show_error(message, verify=True))

    def _verify_subdl_done(self, quota: int | None) -> None:
        self.verify_button.configure(state=tk.NORMAL)
        suffix = f"，当前可用额度：{quota}" if quota is not None else ""
        self.status.set(f"SubDL API Key 有效并已保存{suffix}。")

    def start_search(self) -> None:
        provider = self.provider.get()
        info = PROVIDERS[provider]
        key = self.key_vars[provider].get().strip()
        if not key:
            messagebox.showwarning("缺少 API Key", f"请先填写 {info['key_label']}。", parent=self)
            return
        info["service"].save_settings(key)
        self.results = []
        self.result_provider = ""
        self.tree.delete(*self.tree.get_children())
        self.search_button.configure(state=tk.DISABLED)
        self.download_button.configure(state=tk.DISABLED)
        self.status.set(f"正在通过 {info['name']} 搜索字幕…")
        threading.Thread(
            target=self._search_worker,
            args=(provider, key, self._language_code()),
            daemon=True,
        ).start()

    def _search_worker(self, provider: str, key: str, language: str) -> None:
        try:
            service = PROVIDERS[provider]["service"]
            identity, results, meta = service.search(key, self.video_path, language)
            self.after(0, lambda: self._show_results(provider, identity, results, meta))
        except Exception as exc:
            message = str(exc)
            self.after(0, lambda message=message: self._show_error(message))

    def _show_results(self, provider: str, identity, results, meta) -> None:
        self.search_button.configure(state=tk.NORMAL)
        if provider != self.provider.get():
            return
        self.identity = identity
        self.results = results
        self.result_provider = provider
        self.tree.delete(*self.tree.get_children())
        provider_name = PROVIDERS[provider]["name"]
        for index, item in enumerate(results):
            flags = "可信" if item.trusted else ""
            if item.hearing_impaired:
                flags = f"{flags} SDH".strip()
            if item.moviehash_match:
                flags = f"{flags} HASH".strip()
            self.tree.insert(
                "",
                tk.END,
                iid=str(index),
                values=(
                    provider_name,
                    item.recommendation,
                    item.release,
                    item.language,
                    item.downloads,
                    f"{item.rating:.1f}",
                    flags,
                ),
            )
        if results:
            suffix = "，仅显示前 30 条" if meta.truncated else ""
            self.status.set(
                f"{provider_name} 获得候选 {meta.total_count} 条，显示 {len(results)} 条{suffix}。"
            )
            self.tree.selection_set("0")
            self.tree.focus("0")
            self.tree.see("0")
            self._selection_changed()
        else:
            other = "SubDL" if provider == "opensubtitles" else "OpenSubtitles"
            self.status.set(
                f"{provider_name} 没有搜索到{self.language_label.get()}字幕，可切换到 {other} 再搜索。"
            )

    def _selection_changed(self, _event=None) -> None:
        selection = self.tree.selection()
        self.download_button.configure(state=tk.NORMAL if selection else tk.DISABLED)
        if selection:
            item = self.results[int(selection[0])]
            self.status.set(
                f"{item.recommendation}：{item.match_reason}。下载后将验证字幕正文。"
            )

    def _show_error(self, message: str, verify: bool = False) -> None:
        self.search_button.configure(state=tk.NORMAL)
        self.verify_button.configure(state=tk.NORMAL)
        if not verify:
            self.results = []
            self.result_provider = ""
            self.tree.delete(*self.tree.get_children())
            self.download_button.configure(state=tk.DISABLED)
        if notice := identity_conflict_notice(message):
            self.status.set("影片名称不一致；请确认片头，改正名称后重新添加影片。")
            messagebox.showwarning("影片名称不一致，请先确认", notice, parent=self)
        else:
            self.status.set(message)
            messagebox.showerror("在线字幕操作失败", message, parent=self)

    def start_download(self) -> None:
        selection = self.tree.selection()
        if not selection or not self.result_provider:
            return
        candidate = self.results[int(selection[0])]
        provider = self.result_provider
        key = self.key_vars[provider].get().strip()
        self.download_button.configure(state=tk.DISABLED)
        self.status.set(f"正在从 {PROVIDERS[provider]['name']} 下载字幕…")
        destination = str(Path(self.video_path).with_suffix("")) + "_pro_work"
        threading.Thread(
            target=self._download_worker,
            args=(provider, key, candidate, destination),
            daemon=True,
        ).start()

    def _download_worker(self, provider: str, key: str, candidate, destination: str) -> None:
        try:
            path = PROVIDERS[provider]["service"].download(key, candidate, destination)
            self.after(0, lambda: self._download_done(path, candidate))
        except Exception as exc:
            message = str(exc)
            self.after(0, lambda message=message: self._show_error(message))

    def _download_done(self, path: Path, candidate) -> None:
        self.on_download(str(path), candidate)
        self.destroy()
