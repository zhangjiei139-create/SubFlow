# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from system_resources import HardwareProfile, hardware_status_text


def profile(
    vram_gib: int,
    memory_gib: int,
    cpu_threads: int,
    mode: str,
    *,
    has_nvme: bool = True,
) -> HardwareProfile:
    return HardwareProfile(
        logical_cpus=cpu_threads,
        memory_gib=float(memory_gib),
        gpu_count=1,
        gpu_memory_mib=(vram_gib * 1024,),
        has_nvme=has_nvme,
        recommended_mode=mode,
        memory_load_percent=20.0,
        tier_key="test",
        tier_label="测试",
        tier_summary="测试",
        reasons=(),
    )


class SmartParallelismTest(unittest.TestCase):
    def test_work_mode_uses_simple_four_level_names(self) -> None:
        self.assertEqual(profile(4, 8, 4, "稳定模式").work_mode_label, "入门")
        self.assertEqual(profile(8, 32, 8, "均衡模式", has_nvme=False).work_mode_label, "基础")
        self.assertEqual(profile(16, 32, 12, "均衡模式").work_mode_label, "均衡")
        self.assertEqual(profile(24, 64, 16, "高性能模式").work_mode_label, "性能")
        self.assertEqual(
            hardware_status_text(profile(8, 32, 8, "均衡模式", has_nvme=False)),
            "本机档位：基础工作模式",
        )

    def test_translation_parallelism_scales_with_complete_hardware_profile(self) -> None:
        self.assertEqual(profile(8, 32, 12, "均衡模式").translation_parallelism, 1)
        self.assertEqual(profile(16, 32, 12, "均衡模式").translation_parallelism, 2)
        self.assertEqual(profile(24, 48, 12, "高性能模式").translation_parallelism, 3)
        self.assertEqual(profile(32, 64, 16, "高性能模式").translation_parallelism, 4)

    def test_batch_workers_follow_detected_mode(self) -> None:
        self.assertEqual(profile(8, 16, 8, "稳定模式").batch_movie_workers, 1)
        self.assertEqual(profile(8, 32, 12, "均衡模式").batch_movie_workers, 2)
        self.assertEqual(profile(24, 64, 16, "高性能模式").batch_movie_workers, 4)

    def test_ai_slots_never_exceed_translation_capacity(self) -> None:
        self.assertEqual(profile(8, 32, 12, "均衡模式").ai_slots, 1)
        self.assertEqual(profile(16, 32, 12, "均衡模式").ai_slots, 2)
        self.assertEqual(profile(24, 64, 16, "高性能模式").ai_slots, 3)


if __name__ == "__main__":
    unittest.main()
