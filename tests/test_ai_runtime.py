import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ai_runtime


class AiRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        state = tempfile.TemporaryDirectory()
        self.addCleanup(state.cleanup)
        isolation = patch("ai_runtime.runtime_state_dir", return_value=Path(state.name) / "state")
        isolation.start()
        self.addCleanup(isolation.stop)

    def test_model_root_honors_non_temporary_configured_location(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            configured = Path(temp_name) / "models"
            with patch.dict(os.environ, {"OLLAMA_MODELS": str(configured)}), patch(
                "ai_runtime._is_temporary_path", return_value=False
            ):
                self.assertEqual(ai_runtime.resolve_model_root(), configured)
                self.assertTrue(configured.is_dir())

    def test_model_root_marker_round_trips_english_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            state_dir = Path(temp_name) / "state"
            model_root = Path(temp_name) / "SubFlow-AI" / "models"
            with patch("ai_runtime.runtime_state_dir", return_value=state_dir):
                ai_runtime._write_model_root_marker(model_root)
                self.assertEqual(ai_runtime._marked_model_root(), model_root)

    def test_model_root_marker_round_trips_chinese_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            state_dir = Path(temp_name) / "state"
            model_root = Path(temp_name) / "字幕模型" / "models"
            with patch("ai_runtime.runtime_state_dir", return_value=state_dir):
                ai_runtime._write_model_root_marker(model_root)
                self.assertEqual(ai_runtime._marked_model_root(), model_root)

    def test_legacy_gbk_marker_does_not_override_explicit_chinese_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            state_dir = Path(temp_name) / "state"
            state_dir.mkdir()
            old_root = Path(temp_name) / "旧模型位置" / "models"
            new_root = Path(temp_name) / "影视工具" / "AI模型"
            marker = state_dir / "ollama-model-root.txt"
            marker.write_bytes(str(old_root).encode("gbk"))
            with patch("ai_runtime.runtime_state_dir", return_value=state_dir), patch.dict(
                os.environ, {"OLLAMA_MODELS": str(new_root)}
            ), patch("ai_runtime._is_temporary_path", return_value=False):
                self.assertEqual(ai_runtime.resolve_model_root(), new_root)
                self.assertEqual(marker.read_text(encoding="utf-8").strip(), str(new_root))

    def test_missing_legacy_marker_path_does_not_override_explicit_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            state_dir = Path(temp_name) / "state"
            state_dir.mkdir()
            missing_root = Path(temp_name) / "missing" / "models"
            new_root = Path(temp_name) / "D字幕模型"
            marker = state_dir / "ollama-model-root.txt"
            marker.write_text(str(missing_root), encoding="utf-8")
            with patch("ai_runtime.runtime_state_dir", return_value=state_dir), patch.dict(
                os.environ, {"OLLAMA_MODELS": str(new_root)}
            ), patch("ai_runtime._is_temporary_path", return_value=False):
                self.assertEqual(ai_runtime.resolve_model_root(), new_root)
                self.assertTrue(new_root.is_dir())
                self.assertEqual(marker.read_text(encoding="utf-8").strip(), str(new_root))

    def test_temporary_model_root_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            self.assertTrue(ai_runtime._is_temporary_path(Path(temp_name) / "models"))

    def test_manifest_uses_qwen_tag(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            with patch.dict(os.environ, {"OLLAMA_MODELS": temp_name}), patch(
                "ai_runtime._is_temporary_path", return_value=False
            ):
                manifest = ai_runtime.model_manifest_path("qwen3:8b")
        self.assertEqual(manifest.parts[-5:], ("manifests", "registry.ollama.ai", "library", "qwen3", "8b"))
    def test_model_artifacts_require_manifest_and_complete_blob(self) -> None:
        with tempfile.TemporaryDirectory() as temp_name:
            model_root = Path(temp_name) / "models"
            manifest = model_root / "manifests" / "registry.ollama.ai" / "library" / "qwen3" / "8b"
            blob = model_root / "blobs" / f"sha256-{ai_runtime.MODEL_SHA256}"
            manifest.parent.mkdir(parents=True)
            blob.parent.mkdir(parents=True)
            manifest.write_text("{}", encoding="utf-8")
            blob.write_bytes(b"model")
            with patch.dict(os.environ, {"OLLAMA_MODELS": str(model_root)}), patch(
                "ai_runtime._is_temporary_path", return_value=False
            ), patch("ai_runtime.MODEL_SIZE_BYTES", len(b"model")):
                self.assertTrue(ai_runtime.model_artifacts_valid())
                blob.unlink()
                self.assertFalse(ai_runtime.model_artifacts_valid())

    def test_domestic_model_source_and_hash_are_pinned(self) -> None:
        self.assertIn("modelscope.cn", ai_runtime.MODEL_URL)
        self.assertEqual(len(ai_runtime.MODEL_SHA256), 64)
        self.assertEqual(ai_runtime.MODEL_NAME, "qwen3:8b")

    def test_product_code_does_not_use_ollama_pull_or_create(self) -> None:
        root = Path(__file__).resolve().parents[1]
        core = (root / "subtitle_tool_core.py").read_text(encoding="utf-8")
        runtime = (root / "ai_runtime.py").read_text(encoding="utf-8")
        self.assertNotIn("/api/pull", core)
        self.assertNotIn('"create", model_name', runtime)
        self.assertIn("application/vnd.ollama.image.model", runtime)


if __name__ == "__main__":
    unittest.main()
