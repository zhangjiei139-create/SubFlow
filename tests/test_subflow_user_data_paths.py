# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import ai_runtime
import audio_offset_verifier
import batch_core
import burned_subtitle_detector
import license_manager
import online_subtitles
import profile_model
import subdl_subtitles
import subtitle_tool_core
import subflow_worker


class SubFlowUserDataPathTest(unittest.TestCase):
    def test_active_user_data_paths_share_subflow_root(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            resolved_folder = str(Path(folder).resolve())
            environment = {"LOCALAPPDATA": resolved_folder, "PUBLIC": resolved_folder}
            with patch.dict(os.environ, environment, clear=False):
                os.environ.pop("SUBFLOW_RUNTIME_DIR", None)
                expected = Path(resolved_folder) / "SubFlow"
                self.assertEqual(ai_runtime.runtime_state_dir(), expected)
                self.assertEqual(batch_core._analysis_cache_path().parent, expected)
                self.assertEqual(burned_subtitle_detector._cache_path().parent, expected)
                self.assertEqual(online_subtitles.settings_path().parent, expected)
                self.assertEqual(subdl_subtitles.settings_path().parent, expected)
                self.assertEqual(profile_model.profile_file().parent, expected)
                self.assertEqual(subtitle_tool_core.ollama_runtime_dir(), expected)
                fingerprint = audio_offset_verifier.persistent_cache_dir(
                    str(Path(resolved_folder) / "movie.mkv"),
                    audio_stream_index=0,
                    whisper_model=str(Path(resolved_folder) / "model.bin"),
                )
                self.assertEqual(fingerprint.parent.parent, expected)
                burned_work = burned_subtitle_detector._runtime_dir()
                self.assertEqual(burned_work.parent, expected / "burned-check")

    def test_license_and_installed_tool_roots_use_subflow_name(self) -> None:
        self.assertEqual(license_manager.APP_DIR.name, "SubFlow")
        with patch.dict(os.environ, {"ProgramFiles": r"C:\Program Files"}, clear=False):
            self.assertEqual(subflow_worker._installed_root(), Path(r"C:\Program Files\SubFlow"))


if __name__ == "__main__":
    unittest.main()
