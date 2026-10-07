from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import time
import zipfile
from collections import deque
from pathlib import Path


CHUNK_SIZE = 1024 * 1024
MAX_ATTEMPTS = 2
CREATE_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0


class DownloadCancelled(RuntimeError):
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest().upper()


def file_is_valid(path: Path, expected_size: int, expected_sha256: str) -> bool:
    try:
        return (
            path.is_file()
            and path.stat().st_size == expected_size
            and sha256_file(path) == expected_sha256.upper()
        )
    except OSError:
        return False


def runtime_is_current(runtime_dir: Path, expected_ollama_sha256: str) -> bool:
    ollama = runtime_dir / "ollama.exe"
    server = runtime_dir / "lib" / "ollama" / "llama-server.exe"
    try:
        return (
            ollama.is_file()
            and server.is_file()
            and sha256_file(ollama) == expected_ollama_sha256.upper()
        )
    except OSError:
        return False


class StatusWriter:
    def __init__(self, status_file: Path, result_file: Path, log_file: Path) -> None:
        self.status_file = status_file
        self.result_file = result_file
        self.log_file = log_file
        self.status_file.parent.mkdir(parents=True, exist_ok=True)
        self.result_file.parent.mkdir(parents=True, exist_ok=True)
        self.log_file.parent.mkdir(parents=True, exist_ok=True)

    def update(
        self,
        state: str,
        asset: str = "",
        done: int = 0,
        total: int = 0,
        speed: float = 0.0,
        eta: float = 0.0,
        detail: str = "",
    ) -> None:
        payload = "\n".join(
            (
                state,
                asset,
                str(max(0, int(done))),
                str(max(0, int(total))),
                str(max(0, int(speed))),
                str(max(0, int(eta))),
                detail or str(time.monotonic_ns()),
            )
        )
        temporary = self.status_file.with_suffix(self.status_file.suffix + ".tmp")
        temporary.write_text(payload, encoding="ascii", errors="replace")
        for _ in range(5):
            try:
                os.replace(temporary, self.status_file)
                return
            except OSError:
                time.sleep(0.02)
        self.status_file.write_text(payload, encoding="ascii", errors="replace")
        temporary.unlink(missing_ok=True)

    def log(self, message: str) -> None:
        with self.log_file.open("a", encoding="utf-8") as target:
            target.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")

    def finish(self, result: int, message: str = "") -> None:
        self.result_file.write_text(f"{result}\n{message}", encoding="utf-8")


def find_curl() -> Path:
    candidates: list[Path] = []
    system_root = os.environ.get("SystemRoot")
    if system_root:
        candidates.append(Path(system_root) / "System32" / "curl.exe")
    discovered = shutil.which("curl.exe") or shutil.which("curl")
    if discovered:
        candidates.append(Path(discovered))
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError("System curl.exe is unavailable.")


def terminate_process(process: subprocess.Popen[str]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)


def download_file(
    url: str,
    destination: Path,
    *,
    expected_size: int,
    expected_sha256: str,
    asset: str,
    cancel_file: Path,
    reporter: StatusWriter,
) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if file_is_valid(destination, expected_size, expected_sha256):
        reporter.log(f"{asset} payload is already complete and valid.")
        reporter.update("complete", asset, expected_size, expected_size)
        return destination

    partial = destination.with_name(destination.name + ".part")
    destination.unlink(missing_ok=True)
    if partial.exists() and partial.stat().st_size > expected_size:
        partial.unlink()
    if partial.exists() and partial.stat().st_size == expected_size:
        reporter.update("verifying", asset, expected_size, expected_size)
        if sha256_file(partial) == expected_sha256.upper():
            os.replace(partial, destination)
            return destination
        partial.unlink()

    curl = find_curl()
    last_error = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        if cancel_file.exists():
            raise DownloadCancelled("cancelled")
        starting_size = partial.stat().st_size if partial.exists() else 0
        reporter.log(f"{asset} transfer attempt {attempt}/{MAX_ATTEMPTS}; resume={starting_size} bytes")
        reporter.update("connecting", asset, starting_size, expected_size)
        command = [
            str(curl),
            "--location",
            "--fail",
            "--silent",
            "--show-error",
            "--retry",
            "3",
            "--retry-delay",
            "2",
            "--retry-all-errors",
            "--connect-timeout",
            "20",
            "--continue-at",
            "-",
            "--output",
            str(partial),
            url,
        ]
        process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=CREATE_NO_WINDOW,
        )
        samples: deque[tuple[float, int]] = deque()
        while process.poll() is None:
            if cancel_file.exists():
                terminate_process(process)
                raise DownloadCancelled("cancelled")
            now = time.monotonic()
            current_size = partial.stat().st_size if partial.exists() else 0
            samples.append((now, current_size))
            while len(samples) > 1 and now - samples[0][0] > 10.0:
                samples.popleft()
            speed = 0.0
            if len(samples) > 1:
                elapsed = samples[-1][0] - samples[0][0]
                if elapsed >= 1.0:
                    speed = max(0.0, (samples[-1][1] - samples[0][1]) / elapsed)
            remaining = max(0, expected_size - current_size)
            eta = remaining / speed if speed > 0 else 0.0
            reporter.update("transferring", asset, current_size, expected_size, speed, eta)
            time.sleep(0.25)

        _, stderr = process.communicate()
        current_size = partial.stat().st_size if partial.exists() else 0
        if process.returncode == 0 and current_size == expected_size:
            reporter.update("verifying", asset, current_size, expected_size)
            if sha256_file(partial) == expected_sha256.upper():
                os.replace(partial, destination)
                reporter.update("complete", asset, expected_size, expected_size)
                return destination
            partial.unlink(missing_ok=True)
            last_error = "SHA256 mismatch"
        else:
            last_error = (stderr or f"curl exit code {process.returncode}").strip()
        reporter.log(f"{asset} transfer attempt {attempt} failed: {last_error}")
        if attempt < MAX_ATTEMPTS:
            reporter.update("retrying", asset, current_size, expected_size)
            for _ in range(8):
                if cancel_file.exists():
                    raise DownloadCancelled("cancelled")
                time.sleep(0.25)
    raise RuntimeError(f"{asset} transfer failed: {last_error}")


def safe_extract_zip(
    archive: Path,
    destination: Path,
    *,
    asset: str,
    cancel_file: Path,
    reporter: StatusWriter,
) -> None:
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    root = destination.resolve()
    with zipfile.ZipFile(archive) as source:
        members = source.infolist()
        total = len(members)
        for index, member in enumerate(members, 1):
            if cancel_file.exists():
                raise DownloadCancelled("cancelled")
            target = (destination / member.filename).resolve()
            if root != target and root not in target.parents:
                raise RuntimeError(f"Unsafe archive path in {asset} payload")
            source.extract(member, destination)
            if index == 1 or index == total or index % 25 == 0:
                reporter.update("extracting", asset, index, total)


def prepare_assets(args: argparse.Namespace, reporter: StatusWriter) -> None:
    cache_dir = args.cache_dir.resolve()
    stage_dir = args.stage_dir.resolve()
    cancel_file = args.cancel_file.resolve()
    app_archive = cache_dir / Path(args.app_url).name
    runtime_archive = cache_dir / Path(args.runtime_url).name
    runtime_dir = args.app_dir.resolve() / "tools" / "ollama"

    reporter.update("checking")
    current_runtime = runtime_is_current(runtime_dir, args.ollama_exe_sha256)
    if current_runtime:
        reporter.log(f"Existing Ollama runtime is current: {runtime_dir}")
    else:
        reporter.log("Ollama runtime is missing or outdated.")

    download_file(
        args.app_url,
        app_archive,
        expected_size=args.app_size,
        expected_sha256=args.app_sha256,
        asset="app",
        cancel_file=cancel_file,
        reporter=reporter,
    )
    if not current_runtime:
        download_file(
            args.runtime_url,
            runtime_archive,
            expected_size=args.runtime_size,
            expected_sha256=args.runtime_sha256,
            asset="runtime",
            cancel_file=cancel_file,
            reporter=reporter,
        )

    safe_extract_zip(
        app_archive,
        stage_dir / "app",
        asset="app",
        cancel_file=cancel_file,
        reporter=reporter,
    )
    runtime_stage = stage_dir / "runtime"
    if current_runtime:
        if runtime_stage.exists():
            shutil.rmtree(runtime_stage)
    else:
        safe_extract_zip(
            runtime_archive,
            runtime_stage,
            asset="runtime",
            cancel_file=cancel_file,
            reporter=reporter,
        )
        reporter.update("verifying_runtime", "runtime")
        if not runtime_is_current(runtime_stage, args.ollama_exe_sha256):
            raise RuntimeError("Ollama runtime validation failed")
    reporter.update("ready", "", 1, 1)
    reporter.log("Web installation assets are ready.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--app-url", required=True)
    parser.add_argument("--app-sha256", required=True)
    parser.add_argument("--app-size", type=int, required=True)
    parser.add_argument("--runtime-url", required=True)
    parser.add_argument("--runtime-sha256", required=True)
    parser.add_argument("--runtime-size", type=int, required=True)
    parser.add_argument("--ollama-exe-sha256", required=True)
    parser.add_argument("--app-dir", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--stage-dir", type=Path, required=True)
    parser.add_argument("--status-file", type=Path, required=True)
    parser.add_argument("--result-file", type=Path, required=True)
    parser.add_argument("--cancel-file", type=Path, required=True)
    parser.add_argument("--log-file", type=Path, required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    reporter = StatusWriter(args.status_file, args.result_file, args.log_file)
    args.status_file.unlink(missing_ok=True)
    args.result_file.unlink(missing_ok=True)
    args.cancel_file.unlink(missing_ok=True)
    try:
        prepare_assets(args, reporter)
        reporter.finish(0)
        return 0
    except DownloadCancelled as exc:
        reporter.update("cancelled")
        reporter.log(str(exc))
        reporter.finish(2, str(exc))
        return 2
    except Exception as exc:
        reporter.update("error", detail=type(exc).__name__)
        reporter.log(f"{type(exc).__name__}: {exc}")
        reporter.finish(1, str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
