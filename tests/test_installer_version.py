from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from installer import pro_installer


class InstallerVersionTest(unittest.TestCase):
    def test_installer_version_reads_product_config(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "product_config.json").write_text(
                json.dumps({"version": "2.0.59"}),
                encoding="utf-8",
            )
            with patch.object(pro_installer.sys, "_MEIPASS", str(root), create=True):
                self.assertEqual(pro_installer.bundled_product_version(), "2.0.59")


if __name__ == "__main__":
    unittest.main()
