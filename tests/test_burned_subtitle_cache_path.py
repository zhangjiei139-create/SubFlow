# -*- coding: utf-8 -*-
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import burned_subtitle_detector


class BurnedSubtitleCachePathTest(unittest.TestCase):
    def test_worker_runtime_directory_overrides_gui_cache(self) -> None:
        with tempfile.TemporaryDirectory() as folder, patch.dict(
            os.environ, {"SUBFLOW_RUNTIME_DIR": folder}
        ):
            self.assertEqual(
                burned_subtitle_detector._cache_path(),
                Path(folder) / "burned-subtitle-cache.json",
            )


if __name__ == "__main__":
    unittest.main()
