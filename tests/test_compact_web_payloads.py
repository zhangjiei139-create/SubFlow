import tempfile
import unittest
import zipfile
from pathlib import Path
from installer import package_payload, web_downloader


class CompactWebPayloadTests(unittest.TestCase):
    def test_app_lzma_payload_extracts_in_existing_downloader(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "SubFlow"
            source.mkdir()
            content = b"app-runtime\x00" * 100
            (source / "SubFlow.exe").write_bytes(content)
            archive = root / "app.zip"
            package_payload.pack_directory(source, archive)
            reporter = web_downloader.StatusWriter(root / "status", root / "result", root / "log")
            web_downloader.safe_extract_zip(archive, root / "stage", asset="app",
                                          cancel_file=root / "cancel", reporter=reporter)
            self.assertEqual((root / "stage/SubFlow/SubFlow.exe").read_bytes(), content)

    def test_runtime_repacking_keeps_every_file_byte(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, output = root / "original.zip", root / "compact.zip"
            contents = {"ollama.exe": b"runtime" * 200, "lib/ollama/llama-server.exe": bytes(range(256)) * 20}
            with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED) as archive:
                for name, data in contents.items():
                    archive.writestr(name, data)
            package_payload.repack_runtime(source, output)
            with zipfile.ZipFile(output) as archive:
                self.assertEqual({name: archive.read(name) for name in archive.namelist()}, contents)
                self.assertTrue(all(item.compress_type == zipfile.ZIP_LZMA for item in archive.infolist()))

    def test_runtime_source_cannot_be_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "original.zip"
            with self.assertRaises(ValueError):
                package_payload.repack_runtime(source, source)


if __name__ == "__main__":
    unittest.main()
