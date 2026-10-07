from __future__ import annotations

import unittest

import batch_core


class OutputSuffixPolicyTests(unittest.TestCase):
    def test_only_sf_output_is_skipped(self) -> None:
        self.assertEqual(batch_core.OUTPUT_SUFFIX, ".SF.mkv")
        self.assertTrue(batch_core.is_output_path("movie.SF.mkv"))
        self.assertTrue(batch_core.is_output_path("movie.sf.MKV"))
        self.assertFalse(batch_core.is_output_path("movie.promax.mkv"))
        self.assertFalse(batch_core.is_output_path("movie.subflow.mkv"))


if __name__ == "__main__":
    unittest.main()
