# -*- coding: utf-8 -*-
from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qt_app import ProMaxQt, _CombinedCancelEvent


class _Index:
    @staticmethod
    def currentIndex() -> int:
        return 0


class _Text:
    @staticmethod
    def text() -> str:
        return "我的偏好"


class _Label:
    def __init__(self) -> None:
        self.text = ""

    def setText(self, value: str) -> None:
        self.text = value


class _Progress:
    def __init__(self) -> None:
        self.value = -1
        self.format = ""

    def setValue(self, value: int) -> None:
        self.value = value

    def setFormat(self, value: str) -> None:
        self.format = value


class BatchItemCancellationTest(unittest.TestCase):
    def test_combined_cancel_event_isolated_per_item(self) -> None:
        global_event = threading.Event()
        first_item = threading.Event()
        second_item = threading.Event()
        first = _CombinedCancelEvent(global_event, first_item)
        second = _CombinedCancelEvent(global_event, second_item)

        first_item.set()

        self.assertTrue(first.is_set())
        self.assertFalse(second.is_set())

    def test_single_item_stop_sets_only_selected_event(self) -> None:
        first_item = threading.Event()
        second_item = threading.Event()
        fake = SimpleNamespace(
            batch_plans={
                "first.mkv": SimpleNamespace(task_state="processing"),
                "second.mkv": SimpleNamespace(task_state="processing"),
            },
            batch_item_cancel_events={"first.mkv": first_item, "second.mkv": second_item},
            _batch_log_line=Mock(),
        )

        ProMaxQt._batch_stop_one(fake, "first.mkv")

        self.assertTrue(first_item.is_set())
        self.assertFalse(second_item.is_set())
        fake._batch_log_line.assert_called_once()

    def test_selected_queued_item_can_be_stopped_before_it_starts(self) -> None:
        queued = threading.Event()
        other = threading.Event()
        fake = SimpleNamespace(
            batch_plans={
                "queued.mkv": SimpleNamespace(task_state="waiting"),
                "other.mkv": SimpleNamespace(task_state="processing"),
            },
            batch_item_cancel_events={"queued.mkv": queued, "other.mkv": other},
            _batch_log_line=Mock(),
        )

        ProMaxQt._batch_stop_one(fake, "queued.mkv")

        self.assertTrue(queued.is_set())
        self.assertFalse(other.is_set())
        self.assertIn("排队", fake._batch_log_line.call_args.args[0])

class ProgressFormatTest(unittest.TestCase):
    def test_batch_progress_always_contains_percentage_placeholder(self) -> None:
        progress = _Progress()
        fake = SimpleNamespace(batch_progress=progress)

        ProMaxQt._batch_progress_update(fake, 37, "正在处理影片")

        self.assertEqual(progress.value, 37)
        self.assertEqual(progress.format, "%p% · 正在处理影片")


if __name__ == "__main__":
    unittest.main()
