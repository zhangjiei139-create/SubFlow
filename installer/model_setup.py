import argparse
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import ai_runtime


MODEL = ai_runtime.MODEL_NAME
OLLAMA_HOST = "http://127.0.0.1:11435"
CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


def resource_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parents[1]


def product_version() -> str:
    try:
        payload = json.loads((resource_dir() / "product_config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return "2.0.66 Beta"
    return str(payload.get("version") or "2.0.66 Beta")


def api_json(path: str, payload: dict | None = None, timeout: int = 5) -> dict:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{OLLAMA_HOST}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def server_ready() -> bool:
    try:
        api_json("/api/tags", timeout=2)
        return True
    except Exception:
        return False


def model_files_valid(root: Path) -> bool:
    manifest = (
        root
        / "manifests"
        / "registry.ollama.ai"
        / "library"
        / "qwen3"
        / "8b"
    )
    blob = root / "blobs" / f"sha256-{ai_runtime.MODEL_SHA256}"
    try:
        return (
            manifest.is_file()
            and blob.is_file()
            and blob.stat().st_size == ai_runtime.MODEL_SIZE_BYTES
        )
    except OSError:
        return False


def model_installed() -> bool:
    return ai_runtime.model_artifacts_valid(MODEL)


class ModelSetupApp:
    def __init__(
        self,
        ollama_exe: Path,
        previous_root: Path | None = None,
        window_left: int | None = None,
        window_top: int | None = None,
    ) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.ollama_exe = ollama_exe
        self.previous_root = previous_root
        self.cancel_event = threading.Event()
        self.messages: queue.Queue[tuple[str, object]] = queue.Queue()
        self.server_process: subprocess.Popen | None = None
        self.finished = False
        self.result = 0

        self.root = tk.Tk()
        self.root.title(f"SubFlow v{product_version()} · 翻译模型准备")
        geometry = "560x360"
        if window_left is not None and window_top is not None:
            geometry += f"+{window_left}+{window_top}"
        self.root.geometry(geometry)
        self.root.resizable(False, False)
        self.root.protocol("WM_DELETE_WINDOW", self.cancel)
        icon = resource_dir() / "subflow-icon.ico"
        if icon.is_file():
            self.root.iconbitmap(default=str(icon))
        if os.name == "nt":
            try:
                import ctypes

                self.root.update_idletasks()
                preference = ctypes.c_int(2)
                window_handle = (
                    ctypes.windll.user32.GetParent(self.root.winfo_id())
                    or self.root.winfo_id()
                )
                result = ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    window_handle,
                    33,
                    ctypes.byref(preference),
                    ctypes.sizeof(preference),
                )
                if result != 0:
                    region = ctypes.windll.gdi32.CreateRoundRectRgn(
                        0,
                        0,
                        self.root.winfo_width() + 1,
                        self.root.winfo_height() + 1,
                        20,
                        20,
                    )
                    if region:
                        ctypes.windll.user32.SetWindowRgn(window_handle, region, True)
            except (AttributeError, OSError):
                pass

        frame = ttk.Frame(self.root, padding=16)
        frame.pack(fill=tk.BOTH, expand=True)
        ttk.Label(frame, text="准备本地 AI 翻译模型", font=("Microsoft YaHei UI", 13, "bold")).pack(anchor=tk.W)
        ttk.Label(
            frame,
            text="模型从国内魔搭下载，支持断点续传；主程序和桌面快捷方式已经安装完成。",
            foreground="#555555",
        ).pack(anchor=tk.W, pady=(5, 16))
        self.status = tk.StringVar(value="正在检查本地环境...")
        ttk.Label(frame, textvariable=self.status).pack(anchor=tk.W)
        self.progress = ttk.Progressbar(frame, mode="indeterminate", maximum=100)
        self.progress.pack(fill=tk.X, pady=(8, 14))
        self.progress.start(12)

        log_frame = ttk.LabelFrame(frame, text="模型准备日志")
        log_frame.pack(fill=tk.BOTH, expand=True)
        scrollbar = ttk.Scrollbar(log_frame, orient=tk.VERTICAL)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text = tk.Text(log_frame, height=8, wrap=tk.WORD, state=tk.DISABLED, yscrollcommand=scrollbar.set)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True, padx=(8, 0), pady=8)
        scrollbar.config(command=self.log_text.yview)

        row = ttk.Frame(frame)
        row.pack(fill=tk.X, pady=(12, 0))
        self.button = ttk.Button(row, text="取消", command=self.cancel)
        self.button.pack(side=tk.RIGHT)

        self.root.after(100, self.poll)
        threading.Thread(target=self.worker, daemon=True).start()

    def log(self, message: str) -> None:
        self.messages.put(("log", message))

    def set_progress(self, value: int | None, message: str) -> None:
        self.messages.put(("progress", (value, message)))

    def poll(self) -> None:
        while True:
            try:
                kind, payload = self.messages.get_nowait()
            except queue.Empty:
                break
            if kind == "log":
                self.log_text.config(state=self.tk.NORMAL)
                self.log_text.insert(self.tk.END, str(payload) + "\n")
                self.log_text.see(self.tk.END)
                self.log_text.config(state=self.tk.DISABLED)
            elif kind == "progress":
                value, message = payload
                self.status.set(message)
                if value is None:
                    if str(self.progress["mode"]) != "indeterminate":
                        self.progress.config(mode="indeterminate")
                        self.progress.start(12)
                else:
                    self.progress.stop()
                    self.progress.config(mode="determinate", value=max(0, min(100, int(value))))
            elif kind == "finish":
                self.finished = True
                self.result = int(payload)
                self.progress.stop()
                if self.result == 0:
                    self.progress.config(mode="determinate", value=100)
                self.button.config(text="关闭", command=self.root.destroy)
        self.root.after(100, self.poll)

    def start_server(self) -> None:
        if server_ready():
            self.log("已检测到安装器专用 Ollama 服务。")
            return
        self.set_progress(None, "正在启动本地 AI 服务...")
        self.server_process = subprocess.Popen(
            [str(self.ollama_exe), "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=ai_runtime.ollama_environment(),
            creationflags=CREATE_NO_WINDOW,
        )
        for _ in range(60):
            if self.cancel_event.is_set():
                raise ai_runtime.DownloadCancelled()
            if server_ready():
                return
            time.sleep(1)
        raise RuntimeError("本地 AI 服务启动超时。")

    def verify_model(self) -> None:
        self.set_progress(None, "正在验证翻译模型可用性...")
        result = api_json(
            "/api/generate",
            {
                "model": MODEL,
                "prompt": "Reply with only OK. /no_think",
                "stream": False,
                "options": {"num_predict": 8},
            },
            timeout=180,
        )
        if not result.get("done"):
            raise RuntimeError("翻译模型响应不完整。")

    def move_previous_download(self) -> None:
        if not self.previous_root:
            return
        current_root = ai_runtime.resolve_model_root()
        try:
            if self.previous_root.resolve() == current_root.resolve():
                return
        except OSError:
            pass

        source_download = self.previous_root.parent / "downloads"
        target = ai_runtime.model_download_path()
        for suffix in ("", ".part"):
            source = Path(str(source_download / ai_runtime.MODEL_FILE_NAME) + suffix)
            destination = Path(str(target) + suffix)
            if not source.is_file():
                continue
            try:
                source_size = source.stat().st_size
                destination_size = destination.stat().st_size if destination.exists() else -1
            except OSError:
                continue
            if destination_size >= source_size:
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.unlink(missing_ok=True)
            self.log(f"正在迁移已有模型下载文件：{source}")
            shutil.move(str(source), str(destination))

    def migrate_previous_model(self) -> bool:
        if not self.previous_root:
            return False
        source_blob = self.previous_root / "blobs" / f"sha256-{ai_runtime.MODEL_SHA256}"
        try:
            source_blob_valid = source_blob.is_file() and source_blob.stat().st_size == ai_runtime.MODEL_SIZE_BYTES
        except OSError:
            source_blob_valid = False
        if not source_blob_valid:
            self.move_previous_download()
            return False

        current_root = ai_runtime.resolve_model_root()
        try:
            if self.previous_root.resolve() == current_root.resolve():
                return False
        except OSError:
            pass

        self.set_progress(None, "正在迁移已有翻译模型...")
        self.log(f"检测到旧模型位置：{self.previous_root}")
        ai_runtime.import_model(
            self.ollama_exe,
            source_blob,
            model_name=MODEL,
            log=self.log,
            cancelled=self.cancel_event.is_set,
        )
        old_manifest = (
            self.previous_root
            / "manifests"
            / "registry.ollama.ai"
            / "library"
            / "qwen3"
            / "8b"
        )
        old_manifest.unlink(missing_ok=True)
        self.log(f"模型已迁移到：{current_root}")
        return True

    def repair_current_model(self) -> bool:
        blob = ai_runtime.model_blob_path()
        try:
            valid_blob = blob.is_file() and blob.stat().st_size == ai_runtime.MODEL_SIZE_BYTES
        except OSError:
            valid_blob = False
        if not valid_blob:
            return False

        self.set_progress(None, "正在修复本地模型注册信息...")
        self.log("检测到完整模型文件，正在修复 Ollama 清单，无需重新下载。")
        ai_runtime.import_model(
            self.ollama_exe,
            blob,
            model_name=MODEL,
            log=self.log,
            cancelled=self.cancel_event.is_set,
        )
        return True

    def worker(self) -> None:
        try:
            if not self.ollama_exe.is_file():
                raise RuntimeError("主程序内缺少 Ollama 运行库。")
            model_root = ai_runtime.resolve_model_root()
            self.log(f"模型存放位置：{model_root}")
            self.start_server()
            if model_installed():
                self.log(f"已检测到翻译模型：{MODEL}，无需重复下载。")
            elif self.migrate_previous_model():
                self.log(f"已复用并迁移翻译模型：{MODEL}。")
            elif self.repair_current_model():
                self.log(f"已修复翻译模型：{MODEL}。")
            else:
                self.log("正在从国内魔搭下载 qwen3:8b，首次约 5GB。")
                gguf = ai_runtime.download_model(
                    ai_runtime.model_download_path(),
                    progress=self.set_progress,
                    log=self.log,
                    cancelled=self.cancel_event.is_set,
                )
                self.set_progress(None, "模型下载完成，正在注册到 Ollama...")
                ai_runtime.import_model(
                    self.ollama_exe,
                    gguf,
                    model_name=MODEL,
                    log=self.log,
                    cancelled=self.cancel_event.is_set,
                )
            self.verify_model()
            self.log("翻译模型已准备完成。")
            self.set_progress(100, "翻译模型已准备完成")
            self.messages.put(("finish", 0))
        except ai_runtime.DownloadCancelled:
            self.log("模型准备已取消；主程序不受影响，可稍后在软件内继续。")
            self.set_progress(0, "模型准备已取消")
            self.messages.put(("finish", 1))
        except Exception as exc:
            self.log(f"模型准备失败：{exc}")
            self.log("主程序已经安装完成，可稍后在软件内重试模型准备。")
            self.set_progress(0, "模型准备失败，主程序仍可使用")
            self.messages.put(("finish", 2))
        finally:
            if self.server_process and self.server_process.poll() is None:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(self.server_process.pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=CREATE_NO_WINDOW,
                        check=False,
                    )
                else:
                    self.server_process.terminate()
                    try:
                        self.server_process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self.server_process.kill()

    def cancel(self) -> None:
        if self.finished:
            self.root.destroy()
            return
        self.cancel_event.set()
        self.status.set("正在停止模型准备...")
        self.button.config(state=self.tk.DISABLED)

    def run(self) -> int:
        self.root.mainloop()
        return self.result


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("ollama_exe", type=Path)
    parser.add_argument("--model-root", type=Path, required=True)
    parser.add_argument("--previous-root", type=Path)
    parser.add_argument("--window-left", type=int)
    parser.add_argument("--window-top", type=int)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if ai_runtime._is_temporary_path(args.model_root):
        from tkinter import messagebox
        messagebox.showerror("模型目录不可用", "所选位置是临时目录。请重新运行安装器，确认一个永久模型目录；不会自动换盘或下载。")
        return 1
    os.environ["OLLAMA_MODELS"] = str(args.model_root)
    os.environ["OLLAMA_HOST"] = OLLAMA_HOST.removeprefix("http://")
    args.model_root.mkdir(parents=True, exist_ok=True)
    return ModelSetupApp(
        args.ollama_exe,
        args.previous_root,
        args.window_left,
        args.window_top,
    ).run()


if __name__ == "__main__":
    raise SystemExit(main())
