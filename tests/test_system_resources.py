# -*- coding: utf-8 -*-
from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

import system_resources


class SystemResourceFailOpenTest(unittest.TestCase):
    def test_hidden_probe_timeout_returns_empty_output(self) -> None:
        with patch.object(
            system_resources.subprocess,
            "run",
            side_effect=subprocess.TimeoutExpired(["probe"], 8),
        ):
            self.assertEqual(system_resources._run_hidden(["probe"]), "")

    def test_runtime_guard_allows_work_when_disk_probe_fails(self) -> None:
        guard = system_resources.RuntimeGuard()
        with patch.object(system_resources, "memory_status", return_value=(20.0, 32.0)), patch.object(
            system_resources,
            "_disk_busy_percent",
            side_effect=subprocess.TimeoutExpired(["powershell"], 8),
        ):
            allowed, reason = guard.can_start_next()

        self.assertTrue(allowed)
        self.assertEqual(reason, "")


if __name__ == "__main__":
    unittest.main()
