import ctypes
import hashlib
import json
import locale
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable


MODEL_NAME = "qwen3:8b"
MODEL_FILE_NAME = "Qwen3-8B-Q4_K_M.gguf"
MODEL_URL = (
    "https://www.modelscope.cn/models/Qwen/Qwen3-8B-GGUF/resolve/master/"
    "Qwen3-8B-Q4_K_M.gguf"
)
MODEL_SHA256 = "d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785"
MODEL_SIZE_BYTES = 5_027_783_488
MIN_FREE_BYTES = MODEL_SIZE_BYTES + 1024**3
DOWNLOAD_CHUNK_SIZE = 4 * 1024**2
CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


class DownloadCancelled(Exception):
    pass


def runtime_state_dir() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "SubFlow"


def model_root_marker() -> Path:
    return runtime_state_dir() / "ollama-model-root.txt"


def _fixed_drive_roots() -> list[Path]:
    if os.name != "nt":
        return [Path.home().anchor and Path(Path.home().anchor) or Path("/")]
    roots: list[Path] = []
    bitmask = ctypes.windll.kernel32.GetLogicalDrives()
    for index in range(26):
        if not bitmask & (1 << index):
            continue
        root = Path(f"{chr(ord('A') + index)}:\\")
        if ctypes.windll.kernel32.GetDriveTypeW(str(root)) == 3:
            roots.append(root)
    return roots


def _write_model_root_marker(model_root: Path) -> None:
    state_dir = runtime_state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    model_root_marker().write_text(str(model_root), encoding="utf-8")


def _marker_encodings() -> list[str]:
    encodings = ["utf-8-sig", "cp936", "gbk"]
    if os.name == "nt":
        encodings.append("mbcs")
    preferred = locale.getpreferredencoding(False)
    if preferred:
        encodings.append(preferred)

    result: list[str] = []
    for encoding in encodings:
        normalized = encoding.lower()
        if normalized not in result:
            result.append(normalized)
    return result


def _decode_model_root_marker(raw: bytes) -> str | None:
    for encoding in _marker_encodings():
        try:
            value = raw.decode(encoding).strip()
        except (LookupError, UnicodeDecodeError):
            continue
        if value:
            return value
    return None


def _marked_model_root() -> Path | None:
    marker = model_root_marker()
    try:
        value = _decode_model_root_marker(marker.read_bytes())
    except OSError:
        return None
    return Path(value) if value else None


def _is_temporary_path(path: Path) -> bool:
    try:
        resolved = path.resolve()
    except OSError:
        resolved = path.absolute()
    candidates = [Path(tempfile.gettempdir())]
    local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
    if local_appdata:
        candidates.append(Path(local_appdata) / "Temp")
    for candidate in candidates:
        try:
            temp_root = candidate.resolve()
        except OSError:
            temp_root = candidate.absolute()
        if resolved == temp_root or temp_root in resolved.parents:
            return True
    return any(part.upper().startswith("_MEI") for part in resolved.parts)


def resolve_model_root(create: bool = True) -> Path:
    configured = os.environ.get("OLLAMA_MODELS", "").strip()
    configured_root = Path(os.path.expandvars(configured)).expanduser() if configured else None
    marked_root = _marked_model_root()
    if configured_root is not None and not _is_temporary_path(configured_root):
        root = configured_root
    elif marked_root is not None and not _is_temporary_path(marked_root):
        root = marked_root
    else:
        default_root = Path.home() / ".ollama" / "models"
        if default_root.exists() and any(default_root.rglob("*")):
            root = default_root
        else:
            candidates: list[tuple[int, Path]] = []
            for drive in _fixed_drive_roots():
                try:
                    free = shutil.disk_usage(drive).free
                except OSError:
                    continue
                candidates.append((free, drive))
            if candidates:
                _, drive = max(candidates, key=lambda item: item[0])
                root = drive / "SubFlow-AI" / "models"
            else:
                root = runtime_state_dir() / "models"

    if create:
        root.mkdir(parents=True, exist_ok=True)
        _write_model_root_marker(root)
    return root


def ollama_environment() -> dict[str, str]:
    env = os.environ.copy()
    env["OLLAMA_MODELS"] = str(resolve_model_root())
    env["OLLAMA_NUM_PARALLEL"] = "1"
    env["OLLAMA_MAX_LOADED_MODELS"] = "1"
    env["NO_COLOR"] = "1"
    env["TERM"] = "dumb"
    return env


def model_manifest_path(model_name: str = MODEL_NAME) -> Path:
    name, _, tag = model_name.strip().lower().partition(":")
    return (
        resolve_model_root(create=False)
        / "manifests"
        / "registry.ollama.ai"
        / "library"
        / name
        / (tag or "latest")
    )


def model_blob_path() -> Path:
    return resolve_model_root(create=False) / "blobs" / f"sha256-{MODEL_SHA256}"


def model_artifacts_valid(model_name: str = MODEL_NAME) -> bool:
    manifest = model_manifest_path(model_name)
    blob = model_blob_path()
    try:
        return manifest.is_file() and blob.is_file() and blob.stat().st_size == MODEL_SIZE_BYTES
    except OSError:
        return False


def model_download_path() -> Path:
    download_dir = resolve_model_root().parent / "downloads"
    download_dir.mkdir(parents=True, exist_ok=True)
    return download_dir / MODEL_FILE_NAME


def ensure_model_disk_space(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(path.parent).free
    partial = path.with_suffix(path.suffix + ".part")
    existing = max(
        path.stat().st_size if path.exists() else 0,
        partial.stat().st_size if partial.exists() else 0,
    )
    required = max(512 * 1024**2, MIN_FREE_BYTES - existing)
    if free < required:
        raise RuntimeError(
            f"完成翻译模型至少还需要 {required / 1024**3:.1f} GiB 可用空间，"
            f"当前模型磁盘仅剩 {free / 1024**3:.1f} GiB。"
        )


def _expected_total(response, downloaded: int) -> int:
    content_range = response.headers.get("Content-Range", "")
    match = re.search(r"/(\d+)$", content_range)
    if match:
        return int(match.group(1))
    content_length = int(response.headers.get("Content-Length") or 0)
    return downloaded + content_length if response.status == 206 else content_length


def download_model(
    destination: Path,
    progress: Callable[[int, str], None] | None = None,
    log: Callable[[str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
    retries: int = 3,
) -> Path:
    partial = destination.with_suffix(destination.suffix + ".part")
    if destination.exists() and sha256_file(destination) == MODEL_SHA256:
        if progress:
            progress(100, "翻译模型文件已存在，正在导入...")
        return destination
    ensure_model_disk_space(destination)

    for attempt in range(1, retries + 1):
        if cancelled and cancelled():
            raise DownloadCancelled()
        downloaded = partial.stat().st_size if partial.exists() else 0
        headers = {"User-Agent": "SubFlow/2.0.68", "Accept-Encoding": "identity"}
        if downloaded:
            headers["Range"] = f"bytes={downloaded}-"
        request = urllib.request.Request(MODEL_URL, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                if downloaded and response.status != 206:
                    partial.unlink(missing_ok=True)
                    downloaded = 0
                total = _expected_total(response, downloaded) or MODEL_SIZE_BYTES
                started = time.monotonic()
                sample_time = started
                sample_bytes = downloaded
                with partial.open("ab" if downloaded else "wb") as output:
                    while True:
                        if cancelled and cancelled():
                            raise DownloadCancelled()
                        chunk = response.read(DOWNLOAD_CHUNK_SIZE)
                        if not chunk:
                            break
                        output.write(chunk)
                        downloaded += len(chunk)
                        now = time.monotonic()
                        if progress and (now - sample_time >= 0.5 or downloaded >= total):
                            speed = (downloaded - sample_bytes) / max(0.001, now - sample_time)
                            remaining = max(0, total - downloaded)
                            eta = remaining / speed if speed > 0 else 0
                            percent = max(0, min(100, int(downloaded * 100 / total)))
                            progress(
                                percent,
                                "正在从魔搭下载翻译模型... "
                                f"{percent}% · {downloaded / 1024**3:.2f}/{total / 1024**3:.2f} GiB · "
                                f"{speed / 1024**2:.1f} MiB/秒 · 剩余约 {int(eta)} 秒",
                            )
                            sample_time = now
                            sample_bytes = downloaded
                if total and downloaded != total:
                    raise IOError(f"下载不完整：{downloaded}/{total}")
            partial.replace(destination)
            if log:
                log("模型下载完成，正在校验文件完整性...")
            if sha256_file(destination, cancelled=cancelled) != MODEL_SHA256:
                destination.unlink(missing_ok=True)
                raise RuntimeError("翻译模型 SHA256 校验失败，已删除损坏文件。")
            return destination
        except DownloadCancelled:
            raise
        except (OSError, urllib.error.URLError, RuntimeError) as exc:
            if attempt >= retries:
                raise RuntimeError(f"魔搭模型下载失败：{exc}") from exc
            if log:
                log(f"模型下载中断，正在断点续传（{attempt}/{retries}）：{exc}")
            time.sleep(min(2 * attempt, 5))
    raise RuntimeError("魔搭模型下载失败。")


def sha256_file(path: Path, cancelled: Callable[[], bool] | None = None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(DOWNLOAD_CHUNK_SIZE):
            if cancelled and cancelled():
                raise DownloadCancelled()
            digest.update(chunk)
    return digest.hexdigest().lower()


def import_model(
    ollama_exe: Path,
    gguf_path: Path,
    model_name: str = MODEL_NAME,
    log: Callable[[str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> None:
    if not ollama_exe.is_file():
        raise RuntimeError("未找到 Ollama 运行库，无法导入翻译模型。")
    if not gguf_path.is_file():
        raise RuntimeError("未找到已下载的 GGUF 模型文件。")

    model_root = resolve_model_root()
    model_digest = f"sha256:{MODEL_SHA256}"
    blobs_dir = model_root / "blobs"
    blobs_dir.mkdir(parents=True, exist_ok=True)
    model_blob = blobs_dir / f"sha256-{MODEL_SHA256}"

    if model_blob.exists():
        if model_blob.stat().st_size != MODEL_SIZE_BYTES:
            model_blob.unlink()
        elif gguf_path.resolve() != model_blob.resolve():
            gguf_path.unlink(missing_ok=True)
    if not model_blob.exists():
        if log:
            log("正在将翻译模型写入 Ollama 模型库...")
        shutil.move(str(gguf_path), str(model_blob))

    config = {
        "model_format": "gguf",
        "model_family": "qwen3",
        "model_families": ["qwen3"],
        "model_type": "8.2B",
        "file_type": "Q4_K_M",
        "architecture": "amd64",
        "os": "windows",
        "rootfs": {"type": "layers", "diff_ids": [model_digest]},
    }
    config_bytes = (json.dumps(config, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    config_digest = hashlib.sha256(config_bytes).hexdigest()
    config_blob = blobs_dir / f"sha256-{config_digest}"
    config_blob.write_bytes(config_bytes)

    manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
        "config": {
            "mediaType": "application/vnd.docker.container.image.v1+json",
            "digest": f"sha256:{config_digest}",
            "size": len(config_bytes),
        },
        "layers": [
            {
                "mediaType": "application/vnd.ollama.image.model",
                "digest": model_digest,
                "size": MODEL_SIZE_BYTES,
            }
        ],
    }
    manifest_path = model_manifest_path(model_name)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    process = subprocess.run(
        [str(ollama_exe), "show", model_name],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=ollama_environment(),
        creationflags=CREATE_NO_WINDOW,
        timeout=60,
        check=False,
    )
    if cancelled and cancelled():
        raise DownloadCancelled()
    if process.returncode != 0:
        manifest_path.unlink(missing_ok=True)
        detail = process.stdout.strip() or f"退出代码 {process.returncode}"
        raise RuntimeError(f"Ollama 无法读取本地翻译模型：{detail}")

    if log:
        log(f"翻译模型已注册到 Ollama：{model_name}")


def runtime_metadata() -> dict[str, str | int]:
    return {
        "model": MODEL_NAME,
        "model_url": MODEL_URL,
        "model_file": MODEL_FILE_NAME,
        "model_sha256": MODEL_SHA256,
        "model_size": MODEL_SIZE_BYTES,
    }
