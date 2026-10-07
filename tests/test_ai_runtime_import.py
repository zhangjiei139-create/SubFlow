import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import ai_runtime


class AiRuntimeImportTests(unittest.TestCase):
    def test_import_registers_manifest_without_ollama_create(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            root = Path(temp_name)
            model_root = root / "models"
            ollama = root / "ollama.exe"
            gguf = root / "model.gguf"
            payload = b"model"
            digest = hashlib.sha256(payload).hexdigest()
            ollama.write_bytes(b"runtime")
            gguf.write_bytes(payload)

            with patch("ai_runtime.MODEL_SHA256", digest), patch(
                "ai_runtime.MODEL_SIZE_BYTES", len(payload)
            ), patch("ai_runtime.resolve_model_root", return_value=model_root), patch(
                "ai_runtime.subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="ok")
            ) as run:
                ai_runtime.import_model(ollama, gguf)

            self.assertFalse(gguf.exists())
            self.assertTrue((model_root / "blobs" / f"sha256-{digest}").is_file())
            manifest_path = model_root / "manifests" / "registry.ollama.ai" / "library" / "qwen3" / "8b"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            self.assertEqual(manifest["layers"][0]["digest"], f"sha256:{digest}")
            command = run.call_args.args[0]
            self.assertEqual(command[1:], ["show", "qwen3:8b"])


if __name__ == "__main__":
    unittest.main()
