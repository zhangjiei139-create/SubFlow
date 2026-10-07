import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import winreg
import zipfile
from pathlib import Path

try:
    import ai_runtime
except ModuleNotFoundError:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import ai_runtime


def configure_tcl_tk_paths() -> None:
    if getattr(sys, "frozen", False):
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
        if str(base) not in sys.path:
            sys.path.insert(0, str(base))
        tcl_dir = base / "tcl" / "tcl8.6"
        tk_dir = base / "tcl" / "tk8.6"
    else:
        base = Path(sys.executable).resolve().parent
        tcl_dir = base / "Library" / "lib" / "tcl8.6"
        tk_dir = base / "Library" / "lib" / "tk8.6"
    if tcl_dir.exists():
        os.environ["TCL_LIBRARY"] = str(tcl_dir)
    if tk_dir.exists():
        os.environ["TK_LIBRARY"] = str(tk_dir)


configure_tcl_tk_paths()

import tkinter as tk
from tkinter import messagebox, ttk


def bundled_product_version() -> str:
    bases = [
        Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent)),
        Path(__file__).resolve().parents[1],
    ]
    for base in bases:
        try:
            payload = json.loads((base / "product_config.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
        version = str(payload.get("version") or "").strip()
        if re.fullmatch(r"\d+\.\d+\.\d+(?:\s+Beta)?", version, re.IGNORECASE):
            return version
    return "0.0.0"


PRODUCT_NAME = "SubFlow"
APP_DIR_NAME = "SubtitleTrackTool-ProMax"
EXE_NAME = "SubFlow.exe"
APP_VERSION = bundled_product_version()
WINDOW_TITLE = f"SubFlow v{APP_VERSION} 安装"
PUBLISHER = "SubFlow"
MODEL = "qwen3:8b"
OLLAMA_HOST = "http://127.0.0.1:11434"
APP_PAYLOAD_ARCHIVE = "SubFlow-App-Payload-2.0.66-Beta.zip"
OLLAMA_RUNTIME_VERSION = "0.32.13"
OLLAMA_RUNTIME_ARCHIVE = f"SubFlow-Ollama-Runtime-v{OLLAMA_RUNTIME_VERSION}-windows-amd64.zip"
OLLAMA_RUNTIME_SHA256 = "20d61a8075038694f5b6db1e937551dbc79d470e85217003facf6ecaac394258"
CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
LOG_CALLBACK = None
PROGRESS_CALLBACK = None
CANCEL_EVENT = threading.Event()
CURRENT_PROCESS = None
INSTALL_SNAPSHOT = None
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*(?:\x07|\x1b\\)")
CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class InstallCancelled(Exception):
    pass


def log(message: str) -> None:
    if LOG_CALLBACK:
        LOG_CALLBACK(message)
    else:
        print(message, flush=True)


def progress(value: int | None, message: str = "") -> None:
    if PROGRESS_CALLBACK:
        PROGRESS_CALLBACK(value, message)


def format_size(num_bytes: int) -> str:
    value = float(max(0, num_bytes))
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def format_duration(seconds: float) -> str:
    if seconds < 0 or seconds == float("inf"):
        return "未知"
    total = int(seconds)
    minutes, secs = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}小时{minutes}分"
    if minutes:
        return f"{minutes}分{secs}秒"
    return f"{secs}秒"


def parse_size_text(value: str) -> int:
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*([KMGT]?B)", value.strip(), re.IGNORECASE)
    if not match:
        return 0
    number = float(match.group(1))
    unit = match.group(2).upper()
    scale = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}.get(unit, 1)
    return int(number * scale)


def clean_console_text(value: str) -> str:
    value = ANSI_RE.sub("", value)
    value = CONTROL_RE.sub("", value)
    return value.strip()


def parse_model_progress(line: str) -> tuple[int, str] | None:
    pct_match = re.search(r"(\d{1,3})%", line)
    if not pct_match:
        return None
    pct = max(0, min(100, int(pct_match.group(1))))
    detail = ""
    size_match = re.search(r"([0-9.]+\s*[KMGT]?B)\s*/\s*([0-9.]+\s*[KMGT]?B)", line, re.IGNORECASE)
    speed_match = re.search(r"([0-9.]+\s*[KMGT]?B/s)", line, re.IGNORECASE)
    if size_match:
        downloaded = parse_size_text(size_match.group(1))
        total = parse_size_text(size_match.group(2))
        detail_parts = [f"{format_size(downloaded)} / {format_size(total)}"]
        if speed_match:
            speed_text = speed_match.group(1).replace(" ", "")
            detail_parts.append(speed_text)
            speed_bytes = parse_size_text(speed_text.replace("/s", ""))
            if speed_bytes > 0 and total > downloaded:
                detail_parts.append(f"剩余约 {format_duration((total - downloaded) / speed_bytes)}")
        detail = "    " + "    ".join(detail_parts)
    return pct, detail


def check_cancelled() -> None:
    if CANCEL_EVENT.is_set():
        raise InstallCancelled("安装已取消。")


def set_current_process(process) -> None:
    global CURRENT_PROCESS
    CURRENT_PROCESS = process


def clear_current_process(process) -> None:
    global CURRENT_PROCESS
    if CURRENT_PROCESS is process:
        CURRENT_PROCESS = None


def cleanup_download_jobs() -> None:
    script = """
Get-BitsTransfer -ErrorAction SilentlyContinue |
    Where-Object { $_.DisplayName -eq 'SubtitleTrackToolOllamaDownload' } |
    Remove-BitsTransfer -Confirm:$false -ErrorAction SilentlyContinue
"""
    try:
        run_powershell(script)
    except Exception:
        pass


def cancel_current_work() -> None:
    CANCEL_EVENT.set()
    process = CURRENT_PROCESS
    if process and process.poll() is None:
        try:
            process.terminate()
        except Exception:
            pass
    cleanup_download_jobs()


def desktop_shortcut_path() -> Path:
    return Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop" / "SubFlow.lnk"


def start_menu_dir_path() -> Path:
    programs = Path(os.environ.get("APPDATA", str(Path.home()))) / "Microsoft" / "Windows" / "Start Menu" / "Programs"
    return programs / "SubFlow"


def start_menu_shortcut_path() -> Path:
    return start_menu_dir_path() / "SubFlow.lnk"


def uninstall_reg_path() -> str:
    return r"Software\Microsoft\Windows\CurrentVersion\Uninstall\SubtitleTrackTool-ProMax"


def read_uninstall_registry() -> dict | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, uninstall_reg_path()) as key:
            values = {}
            index = 0
            while True:
                try:
                    name, value, value_type = winreg.EnumValue(key, index)
                except OSError:
                    break
                values[name] = (value, value_type)
                index += 1
            return values
    except OSError:
        return None


def write_uninstall_registry_values(values: dict | None) -> None:
    if not values:
        return
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, uninstall_reg_path()) as key:
        for name, (value, value_type) in values.items():
            winreg.SetValueEx(key, name, 0, value_type, value)


def delete_uninstall_registry() -> None:
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, uninstall_reg_path())
    except OSError:
        pass


def copy_if_exists(source: Path, dest: Path) -> bool:
    if not source.exists():
        return False
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)
    return True


def create_install_snapshot() -> dict:
    backup_root = Path(tempfile.mkdtemp(prefix="SubtitleTrackToolRollback-"))
    snapshot = {
        "backup_root": backup_root,
        "target_existed": install_dir().exists(),
        "target_backup": backup_root / APP_DIR_NAME,
        "desktop_shortcut_existed": desktop_shortcut_path().exists(),
        "desktop_shortcut_backup": backup_root / "desktop-shortcut.lnk",
        "start_shortcut_existed": start_menu_shortcut_path().exists(),
        "start_shortcut_backup": backup_root / "start-shortcut.lnk",
        "reg_values": read_uninstall_registry(),
        "completed": False,
    }
    copy_if_exists(desktop_shortcut_path(), snapshot["desktop_shortcut_backup"])
    copy_if_exists(start_menu_shortcut_path(), snapshot["start_shortcut_backup"])
    return snapshot


def cleanup_install_snapshot(snapshot: dict | None) -> None:
    if not snapshot:
        return
    backup_root = snapshot.get("backup_root")
    if isinstance(backup_root, Path) and backup_root.exists():
        shutil.rmtree(backup_root, ignore_errors=True)


def rollback_install(snapshot: dict | None) -> None:
    if not snapshot or snapshot.get("completed"):
        return
    log("正在回撤本次安装产生的文件...")
    cleanup_download_jobs()
    target_dir = install_dir()
    if target_dir.exists():
        shutil.rmtree(target_dir, ignore_errors=True)
    target_backup = snapshot.get("target_backup")
    if snapshot.get("target_existed") and isinstance(target_backup, Path) and target_backup.exists():
        shutil.move(str(target_backup), str(target_dir))

    for existed_key, backup_key, current_path in (
        ("desktop_shortcut_existed", "desktop_shortcut_backup", desktop_shortcut_path()),
        ("start_shortcut_existed", "start_shortcut_backup", start_menu_shortcut_path()),
    ):
        backup = snapshot.get(backup_key)
        if current_path.exists():
            try:
                current_path.unlink()
            except OSError:
                pass
        if snapshot.get(existed_key) and isinstance(backup, Path) and backup.exists():
            current_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(backup, current_path)

    start_dir = start_menu_dir_path()
    if start_dir.exists():
        try:
            if not any(start_dir.iterdir()):
                start_dir.rmdir()
        except OSError:
            pass

    delete_uninstall_registry()
    write_uninstall_registry_values(snapshot.get("reg_values"))
    cleanup_install_snapshot(snapshot)
    log("已回撤本次安装。")


def app_resource_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
    return Path(__file__).resolve().parent


def local_appdata() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home())))


def install_dir() -> Path:
    return local_appdata() / APP_DIR_NAME


def move_existing_install_to_backup(target_dir: Path, backup_dir: Path) -> None:
    """Move the previous install aside, tolerating brief Windows file locks."""
    last_error: OSError | None = None
    for attempt in range(1, 11):
        check_cancelled()
        try:
            if backup_dir.exists():
                shutil.rmtree(backup_dir, ignore_errors=True)
            shutil.move(str(target_dir), str(backup_dir))
            return
        except OSError as exc:
            last_error = exc
            # WinError 32 is a sharing violation; 5 can be returned by antivirus
            # or Explorer while it is releasing the previous executable.
            if getattr(exc, "winerror", None) not in (5, 32) or attempt == 10:
                break
            log(f"旧版本文件仍在退出，正在重试 ({attempt}/10)...")
            progress(None, f"正在关闭旧版本文件 ({attempt}/10)...")
            time.sleep(1)

    detail = str(last_error) if last_error else "未知错误"
    raise RuntimeError(
        "无法替换旧版本：请关闭字幕音轨整理工具及其文件夹窗口后重新安装。"
        f" 原因：{detail}"
    )


def test_ollama_api(timeout: int = 3) -> bool:
    try:
        with urllib.request.urlopen(f"{OLLAMA_HOST}/api/tags", timeout=timeout):
            return True
    except Exception:
        return False


def ensure_ollama_ready(ollama: Path, seconds: int = 60) -> bool:
    if test_ollama_api():
        return True
    log("正在启动本地 AI 服务...")
    progress(None, "正在启动本地 AI 服务...")
    subprocess.Popen(
        [str(ollama), "serve"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=CREATE_NO_WINDOW,
        env=ai_runtime.ollama_environment(),
    )
    return wait_ollama_ready(seconds)


def model_is_installed(model_name: str = MODEL, timeout: int = 5) -> bool:
    try:
        with urllib.request.urlopen(f"{OLLAMA_HOST}/api/tags", timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception:
        return False
    for model in payload.get("models", []):
        names = {
            str(model.get("name", "")).strip(),
            str(model.get("model", "")).strip(),
        }
        if model_name in names:
            return True
    return ai_runtime.model_manifest_path(model_name).is_file()


def find_ollama() -> Path | None:
    candidates = [
        install_dir() / "tools" / "ollama" / "ollama.exe",
        local_appdata() / "Programs" / "Ollama" / "ollama.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Ollama" / "ollama.exe",
    ]
    path_hit = shutil.which("ollama")
    if path_hit:
        candidates.append(Path(path_hit))
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def disable_ollama_login_start() -> None:
    startup_link = (
        Path(os.environ.get("APPDATA", str(Path.home())))
        / "Microsoft"
        / "Windows"
        / "Start Menu"
        / "Programs"
        / "Startup"
        / "Ollama.lnk"
    )
    try:
        if startup_link.exists():
            startup_link.unlink()
            log("已关闭 Ollama 登录自启动；需要翻译时由本工具按需启动。")
    except OSError as exc:
        log(f"关闭 Ollama 登录自启动失败：{exc}")


def wait_ollama_ready(seconds: int = 60) -> bool:
    started = time.time()
    while time.time() - started < seconds:
        if test_ollama_api():
            return True
        time.sleep(2)
    return False


def run_powershell(script: str) -> None:
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
        check=False,
        creationflags=CREATE_NO_WINDOW,
    )


def create_shortcuts(target: Path) -> None:
    desktop = desktop_shortcut_path().parent
    start_dir = start_menu_dir_path()
    start_dir.mkdir(parents=True, exist_ok=True)
    shortcut_script = rf"""
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut('{desktop}\SubFlow.lnk')
$shortcut.TargetPath = '{target}'
$shortcut.Arguments = ''
$shortcut.WorkingDirectory = '{target.parent}'
$shortcut.IconLocation = '{target},0'
$shortcut.Save()
$shortcut = $shell.CreateShortcut('{start_dir}\SubFlow.lnk')
$shortcut.TargetPath = '{target}'
$shortcut.Arguments = ''
$shortcut.WorkingDirectory = '{target.parent}'
$shortcut.IconLocation = '{target},0'
$shortcut.Save()
"""
    run_powershell(shortcut_script)


def install_size_kb(target_dir: Path) -> int:
    total = 0
    for item in target_dir.rglob("*"):
        if item.is_file():
            try:
                total += item.stat().st_size
            except OSError:
                pass
    return max(1, total // 1024)


def write_uninstaller(target_dir: Path) -> Path:
    uninstall_ps1 = target_dir / "Uninstall.ps1"
    uninstall_cmd = target_dir / "Uninstall.cmd"
    script = r'''
$ErrorActionPreference = "SilentlyContinue"
$AppDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$DesktopShortcut = Join-Path ([Environment]::GetFolderPath("Desktop")) "SubFlow.lnk"
$StartMenuDir = Join-Path ([Environment]::GetFolderPath("Programs")) "SubFlow"
$RegPath = "HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\SubtitleTrackTool-ProMax"

Write-Host "Uninstalling Subtitle Track Tool - Pro Max..."

Remove-Item -LiteralPath $DesktopShortcut -Force
Remove-Item -LiteralPath (Join-Path $StartMenuDir "SubFlow.lnk") -Force
if (Test-Path -LiteralPath $StartMenuDir) {
    $items = Get-ChildItem -LiteralPath $StartMenuDir -Force
    if ($items.Count -eq 0) {
        Remove-Item -LiteralPath $StartMenuDir -Force
    }
}
Remove-Item -LiteralPath $RegPath -Recurse -Force

$ModelMarker = Join-Path $env:LOCALAPPDATA "SubtitleTrackTool-Runtime\ollama-model-root.txt"
$deleteModels = Read-Host "Delete the SubFlow qwen model too? This frees about 5 GB. [y/N]"
if ($deleteModels -match "^[Yy]" -and (Test-Path -LiteralPath $ModelMarker)) {
    $ModelRoot = (Get-Content -LiteralPath $ModelMarker -Encoding UTF8 | Select-Object -First 1).Trim()
    if ($ModelRoot -and (Test-Path -LiteralPath $ModelRoot)) {
        Remove-Item -LiteralPath $ModelRoot -Recurse -Force
    }
    Remove-Item -LiteralPath $ModelMarker -Force
}

$TempScript = Join-Path $env:TEMP ("SubtitleTrackToolRemove-" + [guid]::NewGuid().ToString("N") + ".cmd")
Set-Content -LiteralPath $TempScript -Encoding ASCII -Value "@echo off`r`ntimeout /t 2 /nobreak >nul`r`nrd /s /q ""$AppDir""`r`ndel ""%~f0""`r`n"
Start-Process -FilePath $TempScript -WindowStyle Hidden
Write-Host "Uninstall finished."
'''
    command = '@echo off\r\nchcp 65001 >nul\r\npowershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Uninstall.ps1"\r\npause\r\n'
    uninstall_ps1.write_text(script, encoding="utf-8")
    uninstall_cmd.write_text(command, encoding="ascii")
    return uninstall_cmd


def register_uninstall(target_dir: Path, exe: Path, uninstall_cmd: Path) -> None:
    key_path = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\SubtitleTrackTool-ProMax"
    estimated_size = install_size_kb(target_dir)
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path) as key:
        winreg.SetValueEx(key, "DisplayName", 0, winreg.REG_SZ, PRODUCT_NAME)
        winreg.SetValueEx(key, "DisplayVersion", 0, winreg.REG_SZ, APP_VERSION)
        winreg.SetValueEx(key, "Publisher", 0, winreg.REG_SZ, PUBLISHER)
        winreg.SetValueEx(key, "InstallLocation", 0, winreg.REG_SZ, str(target_dir))
        winreg.SetValueEx(key, "DisplayIcon", 0, winreg.REG_SZ, str(exe))
        winreg.SetValueEx(key, "UninstallString", 0, winreg.REG_SZ, f'"{uninstall_cmd}"')
        winreg.SetValueEx(key, "QuietUninstallString", 0, winreg.REG_SZ, f'"{uninstall_cmd}"')
        winreg.SetValueEx(key, "NoModify", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "NoRepair", 0, winreg.REG_DWORD, 1)
        winreg.SetValueEx(key, "EstimatedSize", 0, winreg.REG_DWORD, estimated_size)

    reg_path = r"HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\SubtitleTrackTool-ProMax"
    fallback_script = rf"""
New-Item -Path '{reg_path}' -Force | Out-Null
New-ItemProperty -Path '{reg_path}' -Name DisplayName -Value '{PRODUCT_NAME}' -PropertyType String -Force | Out-Null
New-ItemProperty -Path '{reg_path}' -Name DisplayVersion -Value '{APP_VERSION}' -PropertyType String -Force | Out-Null
New-ItemProperty -Path '{reg_path}' -Name Publisher -Value '{PUBLISHER}' -PropertyType String -Force | Out-Null
New-ItemProperty -Path '{reg_path}' -Name InstallLocation -Value '{target_dir}' -PropertyType String -Force | Out-Null
New-ItemProperty -Path '{reg_path}' -Name DisplayIcon -Value '{exe}' -PropertyType String -Force | Out-Null
New-ItemProperty -Path '{reg_path}' -Name UninstallString -Value '"{uninstall_cmd}"' -PropertyType String -Force | Out-Null
New-ItemProperty -Path '{reg_path}' -Name QuietUninstallString -Value '"{uninstall_cmd}"' -PropertyType String -Force | Out-Null
New-ItemProperty -Path '{reg_path}' -Name NoModify -Value 1 -PropertyType DWord -Force | Out-Null
New-ItemProperty -Path '{reg_path}' -Name NoRepair -Value 1 -PropertyType DWord -Force | Out-Null
New-ItemProperty -Path '{reg_path}' -Name EstimatedSize -Value {estimated_size} -PropertyType DWord -Force | Out-Null
"""
    run_powershell(fallback_script)


def locate_app_payload() -> Path | None:
    names = (APP_PAYLOAD_ARCHIVE, "payload.zip")
    package_dir = (
        Path(sys.executable).resolve().parent
        if getattr(sys, "frozen", False)
        else app_resource_dir()
    )
    for base in (package_dir, app_resource_dir()):
        for name in names:
            candidate = base / name
            if candidate.is_file():
                return candidate
    return None


def install_application() -> Path:
    global INSTALL_SNAPSHOT
    check_cancelled()
    payload = locate_app_payload()
    if payload is None:
        raise RuntimeError(
            f"缺少主程序载荷 {APP_PAYLOAD_ARCHIVE}。请将发布目录中的三个安装文件放在同一目录。"
        )

    target_dir = install_dir()
    target_parent = target_dir.parent
    progress(5, "正在安装主程序...")
    log(f"正在安装主程序：{target_dir}")
    check_cancelled()

    with zipfile.ZipFile(payload, "r") as archive:
        members = archive.infolist()
        if not any(
            member.filename.replace("\\", "/").startswith(f"{APP_DIR_NAME}/")
            for member in members
        ):
            raise RuntimeError(f"主程序载荷无效：缺少 {APP_DIR_NAME}")
        target_root = target_parent.resolve()
        for member in members:
            member_path = (target_parent / member.filename).resolve()
            if target_root not in member_path.parents and member_path != target_root:
                raise RuntimeError("主程序载荷包含非法路径。")

        if target_dir.exists():
            backup_dir = INSTALL_SNAPSHOT.get("target_backup") if INSTALL_SNAPSHOT else None
            if backup_dir:
                backup_dir.parent.mkdir(parents=True, exist_ok=True)
                move_existing_install_to_backup(target_dir, backup_dir)
            else:
                shutil.rmtree(target_dir)
        check_cancelled()
        archive.extractall(target_parent)

    check_cancelled()
    exe = target_dir / EXE_NAME
    if not exe.exists():
        raise RuntimeError(f"Application executable not found after install: {exe}")
    progress(15, "正在创建快捷方式...")
    create_shortcuts(exe)
    uninstaller = write_uninstaller(target_dir)
    register_uninstall(target_dir, exe, uninstaller)
    progress(25, "主程序已安装")
    log("主程序已安装。")
    return exe


def installer_package_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return app_resource_dir()


def locate_ollama_runtime_archive() -> Path | None:
    names = (OLLAMA_RUNTIME_ARCHIVE, "SubFlow-Ollama-Runtime.zip")
    bases = (installer_package_dir(), app_resource_dir())
    for base in bases:
        for name in names:
            candidate = base / name
            if candidate.is_file():
                return candidate
    return None


def _extract_runtime_archive(archive_path: Path, target_dir: Path) -> Path:
    if ai_runtime.sha256_file(archive_path) != OLLAMA_RUNTIME_SHA256:
        raise RuntimeError("Ollama 本地运行库校验失败，请重新获取完整安装文件。")
    if target_dir.exists():
        shutil.rmtree(target_dir, ignore_errors=True)
    target_dir.mkdir(parents=True, exist_ok=True)
    target_root = target_dir.resolve()
    with zipfile.ZipFile(archive_path, "r") as archive:
        for member in archive.infolist():
            member_path = (target_dir / member.filename).resolve()
            if target_root not in member_path.parents and member_path != target_root:
                raise RuntimeError("Ollama 运行库压缩包包含非法路径。")
        archive.extractall(target_dir)
    executable = next(target_dir.rglob("ollama.exe"), None)
    if executable is None:
        raise RuntimeError("Ollama 本地运行库中缺少 ollama.exe。")
    if executable.parent != target_dir:
        source_root = executable.parent
        for child in list(source_root.iterdir()):
            destination = target_dir / child.name
            if destination.exists():
                if destination.is_dir():
                    shutil.rmtree(destination)
                else:
                    destination.unlink()
            shutil.move(str(child), str(destination))
        executable = target_dir / "ollama.exe"
    return executable


def install_ollama_if_needed() -> Path | None:
    check_cancelled()
    ollama = find_ollama()
    if ollama:
        progress(45, "本地 AI 运行库已检测到")
        log(f"本地 AI 运行库已检测到：{ollama}")
        return ollama

    archive_path = locate_ollama_runtime_archive()
    if archive_path is None:
        log(
            f"未找到 {OLLAMA_RUNTIME_ARCHIVE}。主程序已安装，"
            "但 AI 翻译暂不可用；请将官方运行库包与安装器放在同一目录后重装。"
        )
        return None

    log(f"正在安装随 SubFlow 提供的 Ollama 官方便携运行库 v{OLLAMA_RUNTIME_VERSION}...")
    progress(None, "正在校验并安装本地 AI 运行库...")
    check_cancelled()
    ollama = _extract_runtime_archive(archive_path, install_dir() / "tools" / "ollama")
    check_cancelled()
    progress(45, "本地 AI 运行库已安装")
    log("本地 AI 运行库已安装；安装过程未访问海外下载站点。")
    return ollama


def prepare_model(ollama: Path) -> None:
    check_cancelled()
    if not ensure_ollama_ready(ollama):
        log("本地 AI 服务暂未启动成功，可稍后在程序内重试。")
        return

    if model_is_installed():
        progress(100, "翻译模型已存在")
        log(f"已检测到翻译模型：{MODEL}，无需重新下载。")
        return

    check_cancelled()
    model_root = ai_runtime.resolve_model_root()
    log(f"正在从魔搭准备官方翻译模型：{MODEL}")
    log(f"模型存放位置：{model_root}")
    log("首次下载约 5GB；下载完成后将自动导入 Ollama，无需手动操作。")
    progress(0, "正在连接魔搭模型服务... 0%")

    try:
        gguf_path = ai_runtime.download_model(
            ai_runtime.model_download_path(),
            progress=progress,
            log=log,
            cancelled=lambda: CANCEL_EVENT.is_set(),
        )
        check_cancelled()
        progress(None, "模型下载完成，正在自动导入 Ollama...")
        ai_runtime.import_model(
            ollama,
            gguf_path,
            model_name=MODEL,
            log=log,
            cancelled=lambda: CANCEL_EVENT.is_set(),
        )
    except ai_runtime.DownloadCancelled as exc:
        raise InstallCancelled() from exc

    if not model_is_installed():
        raise RuntimeError(f"模型自动导入后仍未检测到：{MODEL}")
    progress(100, "翻译模型已准备好")
    log("翻译模型已准备好。")


class InstallerApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(WINDOW_TITLE)
        try:
            self.iconbitmap(default=str(app_resource_dir() / "subtitle-track-tool-pro-icon.ico"))
        except tk.TclError:
            pass
        self.geometry("620x430")
        self.resizable(False, False)
        self.messages: queue.Queue[tuple[str, str]] = queue.Queue()
        self.install_thread: threading.Thread | None = None
        self.close_allowed = False
        self.cancel_requested = False
        self.model_confirmed = False

        global LOG_CALLBACK
        global PROGRESS_CALLBACK
        LOG_CALLBACK = self.enqueue_log
        PROGRESS_CALLBACK = self.enqueue_progress

        self.status_text = tk.StringVar(value="准备安装")
        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(100, self.poll_messages)
        self.after(300, self.start_install)

    def _build_ui(self) -> None:
        frame = ttk.Frame(self, padding=18)
        frame.pack(fill=tk.BOTH, expand=True)

        ttk.Label(frame, text="SubFlow 你字幕处理助手", font=("Microsoft YaHei UI", 13, "bold")).pack(anchor=tk.W)
        ttk.Label(
            frame,
            text="安装主程序，并按需准备本地 AI 翻译能力。",
            foreground="#555",
        ).pack(anchor=tk.W, pady=(4, 14))

        self.status_label = ttk.Label(frame, textvariable=self.status_text)
        self.status_label.pack(anchor=tk.W)

        self.progress = ttk.Progressbar(frame, mode="indeterminate")
        self.progress.pack(fill=tk.X, pady=(8, 12))
        self.progress.start(12)

        log_frame = ttk.LabelFrame(frame, text="安装进度")
        log_frame.pack(fill=tk.BOTH, expand=True)
        self.log_text = tk.Text(log_frame, height=12, wrap=tk.WORD, state=tk.DISABLED)
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        button_row = ttk.Frame(frame)
        button_row.pack(fill=tk.X, pady=(12, 0))
        self.close_button = ttk.Button(button_row, text="关闭", command=self.destroy, state=tk.DISABLED)
        self.close_button.pack(side=tk.RIGHT)
        self.cancel_button = ttk.Button(button_row, text="取消安装", command=self.request_cancel)
        self.cancel_button.pack(side=tk.RIGHT, padx=(0, 8))

    def enqueue_log(self, message: str) -> None:
        self.messages.put(("log", message))

    def enqueue_progress(self, value: int | None, message: str = "") -> None:
        payload = "" if value is None else str(value)
        if message:
            payload += "|" + message
        self.messages.put(("progress", payload))

    def set_status(self, message: str) -> None:
        self.messages.put(("status", message))

    def poll_messages(self) -> None:
        while True:
            try:
                kind, message = self.messages.get_nowait()
            except queue.Empty:
                break
            if kind == "status":
                self.status_text.set(message)
            elif kind == "finish":
                self.finish(message)
            elif kind == "progress":
                self.apply_progress(message)
            else:
                self.append_log(message)
        self.after(120, self.poll_messages)

    def apply_progress(self, payload: str) -> None:
        value_text, _, message = payload.partition("|")
        if message:
            self.status_text.set(message)
        if value_text == "":
            if str(self.progress["mode"]) != "indeterminate":
                self.progress.config(mode="indeterminate")
                self.progress.start(12)
            return
        value = max(0, min(100, int(value_text)))
        if str(self.progress["mode"]) != "determinate":
            self.progress.stop()
            self.progress.config(mode="determinate", maximum=100)
        self.progress.config(value=value)

    def append_log(self, message: str) -> None:
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, message + "\n")
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    def start_install(self) -> None:
        if self.install_thread and self.install_thread.is_alive():
            return
        CANCEL_EVENT.clear()
        self.install_thread = threading.Thread(target=self.install_worker, daemon=True)
        self.install_thread.start()

    def ask_model_download(self) -> bool:
        result: queue.Queue[bool] = queue.Queue()

        def ask() -> None:
            answer = messagebox.askyesno(
                "准备 AI 翻译模型",
                "是否现在从国内魔搭下载翻译模型 qwen3:8b？\n\n首次下载约 5GB，完成后会自动导入，无需手动操作。\n"
                "选择“否”也会完成安装，之后需要翻译时再准备模型。",
            )
            result.put(answer)

        self.after(0, ask)
        return result.get()

    def install_worker(self) -> None:
        global INSTALL_SNAPSHOT
        INSTALL_SNAPSHOT = create_install_snapshot()
        try:
            self.set_status("正在安装主程序...")
            exe = install_application()

            self.set_status("正在检查本地 AI 引擎...")
            ollama = install_ollama_if_needed()
            if ollama:
                disable_ollama_login_start()
                self.set_status("正在检查翻译模型...")
                if not ensure_ollama_ready(ollama):
                    log("本地 AI 服务暂未启动成功，已跳过模型检查。")
                elif model_is_installed():
                    progress(100, "翻译模型已存在")
                    log(f"已检测到翻译模型：{MODEL}，无需重新下载。")
                else:
                    self.set_status("等待确认是否准备翻译模型...")
                    if self.ask_model_download():
                        self.set_status("正在准备翻译模型...")
                        prepare_model(ollama)
                    else:
                        log("已跳过翻译模型下载。音轨/字幕整理功能可直接使用。")
            else:
                log("本地 AI 翻译暂未准备完成。")

            log("")
            log("安装完成。")
            log(f"程序位置：{exe}")
            INSTALL_SNAPSHOT["completed"] = True
            cleanup_install_snapshot(INSTALL_SNAPSHOT)
            self.messages.put(("finish", "安装完成"))
        except InstallCancelled:
            rollback_install(INSTALL_SNAPSHOT)
            log("")
            log("安装已取消。")
            self.messages.put(("finish", "安装已取消"))
        except Exception as exc:
            rollback_install(INSTALL_SNAPSHOT)
            log("")
            log(f"安装失败：{exc}")
            self.messages.put(("finish", "安装失败"))

    def finish(self, message: str) -> None:
        self.status_text.set(message)
        self.progress.stop()
        self.progress.config(mode="determinate", maximum=100, value=100 if message == "安装完成" else 0)
        self.close_allowed = True
        self.close_button.config(state=tk.NORMAL)
        self.cancel_button.config(state=tk.DISABLED)

    def request_cancel(self) -> None:
        if self.close_allowed:
            self.destroy()
            return
        if not self.cancel_requested:
            answer = messagebox.askyesno("取消安装", "确定要取消并关闭安装器吗？正在下载的内容会停止。")
            if not answer:
                return
        self.cancel_requested = True
        self.status_text.set("正在取消安装并回撤本次改动...")
        self.cancel_button.config(state=tk.DISABLED)
        cancel_current_work()

    def force_close_if_needed(self) -> None:
        if self.close_allowed:
            return
        self.close_allowed = True
        self.destroy()

    def on_close(self) -> None:
        if self.close_allowed:
            self.destroy()
        else:
            self.request_cancel()


def main() -> int:
    if "--self-test" in sys.argv:
        root = tk.Tk()
        root.withdraw()
        ttk.Label(root, text="ok").pack()
        root.update_idletasks()
        root.destroy()
        return 0

    app = InstallerApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
