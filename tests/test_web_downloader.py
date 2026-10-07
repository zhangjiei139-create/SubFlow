from __future__ import annotations

import hashlib
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import sys

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "installer"
if str(INSTALLER) not in sys.path:
    sys.path.insert(0, str(INSTALLER))

import web_downloader


class WebDownloaderTest(unittest.TestCase):
    def test_download_resumes_partial_file_and_verifies_hash(self) -> None:
        payload = b"abcdefghijklmno"
        digest = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source = root / "source.zip"
            source.write_bytes(payload)
            destination = root / "cache" / "payload.zip"
            destination.parent.mkdir()
            partial = destination.with_name(destination.name + ".part")
            partial.write_bytes(payload[:5])
            reporter = web_downloader.StatusWriter(
                root / "status.txt",
                root / "result.txt",
                root / "detail.log",
            )
            result = web_downloader.download_file(
                source.as_uri(),
                destination,
                expected_size=len(payload),
                expected_sha256=digest,
                asset="test",
                cancel_file=root / "cancel.txt",
                reporter=reporter,
            )

            self.assertEqual(result.read_bytes(), payload)
            self.assertIn("resume=5 bytes", (root / "detail.log").read_text(encoding="utf-8"))
            self.assertEqual((root / "status.txt").read_text(encoding="ascii").splitlines()[0], "complete")

    def test_current_runtime_is_identified_by_exact_ollama_hash(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            runtime = Path(folder)
            ollama = runtime / "ollama.exe"
            server = runtime / "lib" / "ollama" / "llama-server.exe"
            server.parent.mkdir(parents=True)
            ollama.write_bytes(b"expected runtime")
            server.write_bytes(b"server")
            digest = hashlib.sha256(ollama.read_bytes()).hexdigest()

            self.assertTrue(web_downloader.runtime_is_current(runtime, digest))
            self.assertFalse(web_downloader.runtime_is_current(runtime, "0" * 64))

    def test_prepare_skips_runtime_download_when_current_runtime_exists(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            app_dir = root / "app"
            runtime = app_dir / "tools" / "ollama"
            server = runtime / "lib" / "ollama" / "llama-server.exe"
            server.parent.mkdir(parents=True)
            ollama = runtime / "ollama.exe"
            ollama.write_bytes(b"expected runtime")
            server.write_bytes(b"server")
            digest = hashlib.sha256(ollama.read_bytes()).hexdigest()
            args = SimpleNamespace(
                app_url="https://example.invalid/app.zip",
                app_sha256="a" * 64,
                app_size=10,
                runtime_url="https://example.invalid/runtime.zip",
                runtime_sha256="b" * 64,
                runtime_size=20,
                ollama_exe_sha256=digest,
                app_dir=app_dir,
                cache_dir=root / "cache",
                stage_dir=root / "stage",
                cancel_file=root / "cancel.txt",
            )
            downloads = []
            extractions = []

            def fake_download(_url, destination, **kwargs):
                downloads.append(kwargs["asset"])
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"archive")
                return destination

            def fake_extract(_archive, destination, **kwargs):
                extractions.append(kwargs["asset"])
                destination.mkdir(parents=True, exist_ok=True)

            with patch.object(web_downloader, "download_file", side_effect=fake_download), patch.object(
                web_downloader, "safe_extract_zip", side_effect=fake_extract
            ):
                reporter = web_downloader.StatusWriter(
                    root / "status.txt",
                    root / "result.txt",
                    root / "detail.log",
                )
                web_downloader.prepare_assets(args, reporter)

            self.assertEqual(downloads, ["app"])
            self.assertEqual(extractions, ["app"])
            self.assertTrue(ollama.is_file())

    def test_zip_path_traversal_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            archive = root / "bad.zip"
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr("../escape.txt", "bad")

            with self.assertRaises(RuntimeError):
                web_downloader.safe_extract_zip(
                    archive,
                    root / "stage",
                    asset="bad",
                    cancel_file=root / "cancel.txt",
                    reporter=web_downloader.StatusWriter(
                        root / "status.txt",
                        root / "result.txt",
                        root / "detail.log",
                    ),
                )
            self.assertFalse((root / "escape.txt").exists())


if __name__ == "__main__":
    unittest.main()
