from __future__ import annotations

import os
import unittest
from unittest import mock

import pro_core


class AudioFineShadowSwitchTests(unittest.TestCase):
    def test_stage34_is_disabled_by_default(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SUBFLOW_AUDIO_FINE_SHADOW", None)
            os.environ["SUBFLOW_FINE_ALIGNMENT_SHADOW"] = "1"
            self.assertFalse(pro_core._audio_fine_shadow_enabled())

    def test_stage34_requires_explicit_development_flag(self):
        with mock.patch.dict(os.environ, {"SUBFLOW_AUDIO_FINE_SHADOW": "1"}, clear=False):
            self.assertTrue(pro_core._audio_fine_shadow_enabled())
        with mock.patch.dict(os.environ, {"SUBFLOW_AUDIO_FINE_SHADOW": "0"}, clear=False):
            self.assertFalse(pro_core._audio_fine_shadow_enabled())


if __name__ == "__main__":
    unittest.main()
