import hashlib
import json
import os
import platform
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path


LOCAL_APPDATA = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
APP_DIR = LOCAL_APPDATA / "SubFlow"
LEGACY_APP_DIRS = (
    LOCAL_APPDATA / "MovieSubtitleTool",
    LOCAL_APPDATA / "MovieSubtitleTool-ProMax5",
)
LEGACY_LICENSE_FILE = APP_DIR / "license.json"
RUNTIME_DIR = APP_DIR
REACTIVATION_FILE = RUNTIME_DIR / "license-reactivation.json"
FINGERPRINT_VERSION = 2


def app_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


CONFIG_FILE = app_base_dir() / "product_config.json"


def load_config() -> dict:
    if not CONFIG_FILE.exists():
        return {"license": {"enabled": False}}
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


def windows_hardware_id() -> str:
    if os.name != "nt":
        return ""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\SystemInformation",
        ) as key:
            value, _ = winreg.QueryValueEx(key, "ComputerHardwareId")
            return str(value).strip().lower()
    except OSError:
        return ""


def windows_machine_guid() -> str:
    if os.name != "nt":
        return ""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as key:
            value, _ = winreg.QueryValueEx(key, "MachineGuid")
            return str(value).strip().lower()
    except OSError:
        return ""


def device_hash() -> str:
    stable_id = windows_hardware_id() or windows_machine_guid() or str(uuid.getnode())
    raw = "|".join([f"v{FINGERPRINT_VERSION}", platform.machine(), stable_id])
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def license_file(config: dict) -> Path:
    edition = str(config.get("edition", "")).strip().lower() or "default"
    return APP_DIR / edition / "license.json"


def read_license_file(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def read_local_license(config: dict) -> dict | None:
    data = read_license_file(license_file(config))
    if data:
        return data

    expected_edition = str(config.get("edition", "")).strip().lower()
    legacy_files = [LEGACY_LICENSE_FILE]
    legacy_files.extend(root / expected_edition / "license.json" for root in LEGACY_APP_DIRS)
    legacy_files.extend(root / "license.json" for root in LEGACY_APP_DIRS)
    for legacy_file in legacy_files:
        legacy = read_license_file(legacy_file)
        legacy_edition = str((legacy or {}).get("edition", "")).strip().lower()
        if legacy and legacy_edition in {expected_edition, "all"}:
            write_local_license(legacy, config)
            return legacy
    return None


def write_local_license(data: dict, config: dict) -> None:
    path = license_file(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def normalize_key(license_key: str) -> str:
    return license_key.strip().upper().replace(" ", "")


def reactivate_from_receipt(license_key: str, config: dict) -> tuple[bool, str]:
    receipt = read_license_file(REACTIVATION_FILE)
    if not receipt:
        return False, ""
    if normalize_key(str(receipt.get("license_key", ""))) != normalize_key(license_key):
        return False, ""
    if str(receipt.get("device_hash", "")) != device_hash():
        return False, ""
    receipt_edition = str(receipt.get("edition", "")).strip().lower()
    if receipt_edition not in {product_edition(config), "all"}:
        return False, ""

    receipt.update(
        {
            "license_key": normalize_key(license_key),
            "device_hash": device_hash(),
            "fingerprint_version": FINGERPRINT_VERSION,
            "mode": "server",
            "api_base_url": server_url(config),
            "edition": receipt_edition or product_edition(config),
            "activated_at": now_ts(),
            "last_verified_at": now_ts(),
        }
    )
    write_local_license(receipt, config)
    REACTIVATION_FILE.unlink(missing_ok=True)
    return True, "当前电脑已重新激活"


def now_ts() -> int:
    return int(time.time())


def parse_server_time(value: str | None) -> int:
    if not value:
        return now_ts()
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return now_ts()


def server_url(config: dict) -> str:
    return str(config.get("license", {}).get("api_base_url", "")).strip().rstrip("/")


def product_edition(config: dict) -> str:
    return str(config.get("edition", "")).strip().lower()


def app_version(config: dict) -> str:
    return str(config.get("version", "1.0.0")).strip()


def api_post(base_url: str, endpoint: str, payload: dict, timeout: int = 8) -> dict:
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url}{endpoint}",
        data=raw,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8")
            return json.loads(body)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            return json.loads(body)
        except json.JSONDecodeError:
            return {"ok": False, "message": body or f"HTTP {exc.code}"}


def license_payload(license_key: str, config: dict) -> dict:
    return {
        "license_key": normalize_key(license_key),
        "device_hash": device_hash(),
        "edition": product_edition(config),
        "app_version": app_version(config),
    }


def save_server_license(license_key: str, config: dict, result: dict) -> None:
    write_local_license(
        {
            "license_key": normalize_key(license_key),
            "device_hash": device_hash(),
            "fingerprint_version": FINGERPRINT_VERSION,
            "mode": "server",
            "api_base_url": server_url(config),
            "edition": result.get("edition", product_edition(config)),
            "expires_at": result.get("expires_at"),
            "activated_at": now_ts(),
            "last_verified_at": parse_server_time(result.get("server_time")),
        },
        config,
    )


def verify_with_server(data: dict, config: dict) -> tuple[bool, str]:
    base_url = server_url(config)
    if not base_url:
        return False, "授权服务器未配置"
    result = api_post(base_url, "/verify", license_payload(str(data.get("license_key", "")), config))
    if bool(result.get("ok")):
        save_server_license(str(data.get("license_key", "")), config, result)
        return True, str(result.get("message", "授权有效"))
    return False, str(result.get("message", "授权无效"))


def local_license_valid(config: dict) -> tuple[bool, str]:
    data = read_local_license(config)
    if not data:
        return False, "未激活"

    saved_edition = str(data.get("edition", "")).strip().lower()
    if saved_edition not in {product_edition(config), "all"}:
        return False, "授权版本不匹配"

    current_hash = device_hash()
    if data.get("device_hash") != current_hash:
        # Pre-release builds used a network-adapter MAC as the device identity.
        # Migrate that local license once so virtual adapters cannot invalidate it.
        is_legacy_server_license = (
            data.get("fingerprint_version") is None
            and data.get("mode") == "server"
            and bool(data.get("license_key"))
        )
        if not is_legacy_server_license:
            return False, "授权不属于当前电脑"
        data["device_hash"] = current_hash
        data["fingerprint_version"] = FINGERPRINT_VERSION
        data["fingerprint_migrated_at"] = now_ts()
        write_local_license(data, config)

    if bool(data.get("license_key")):
        return True, "本机已激活"
    return False, "授权文件无效"


def activate_with_server(license_key: str, config: dict) -> tuple[bool, str]:
    base_url = server_url(config)
    result = api_post(base_url, "/activate", license_payload(license_key, config))
    if bool(result.get("ok")):
        save_server_license(license_key, config, result)
        return True, str(result.get("message", "激活成功"))
    return False, str(result.get("message", "激活失败"))


def activate_dev_key(license_key: str, config: dict) -> tuple[bool, str]:
    license_config = config.get("license", {})
    dev_keys = {normalize_key(str(item)) for item in license_config.get("dev_license_keys", [])}
    normalized = normalize_key(license_key)
    if normalized not in dev_keys:
        return False, "授权码无效"
    write_local_license(
        {
            "license_key": normalized,
            "device_hash": device_hash(),
            "fingerprint_version": FINGERPRINT_VERSION,
            "mode": "local-dev-placeholder",
            "edition": product_edition(config),
            "activated_at": now_ts(),
            "last_verified_at": now_ts(),
        },
        config,
    )
    return True, "激活成功"


def activate_license(license_key: str, config: dict) -> tuple[bool, str]:
    if server_url(config):
        ok, message = activate_with_server(license_key, config)
        if ok:
            REACTIVATION_FILE.unlink(missing_ok=True)
            return ok, message
        if message == "激活码已绑定其他电脑":
            receipt_ok, receipt_message = reactivate_from_receipt(license_key, config)
            if receipt_ok:
                return True, receipt_message
        return False, message
    return activate_dev_key(license_key, config)


def ensure_licensed(parent=None) -> bool:
    from tkinter import messagebox, simpledialog

    config = load_config()
    license_config = config.get("license", {})
    if not license_config.get("enabled", False):
        return True

    ok, _ = local_license_valid(config)
    if ok:
        return True

    while True:
        license_key = simpledialog.askstring("软件激活", "请输入激活码：", parent=parent)
        if license_key is None:
            return False
        license_key = license_key.strip()
        if not license_key:
            messagebox.showwarning("激活码为空", "请输入有效激活码。", parent=parent)
            continue
        try:
            ok, message = activate_license(license_key, config)
        except Exception as exc:
            messagebox.showerror("激活失败", f"无法连接授权服务器：{exc}", parent=parent)
            return False
        if ok:
            messagebox.showinfo("激活成功", "授权已绑定当前电脑。", parent=parent)
            return True
        messagebox.showerror("激活失败", message, parent=parent)
