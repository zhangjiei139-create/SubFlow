# -*- coding: utf-8 -*-
from __future__ import annotations

import concurrent.futures
from contextlib import ExitStack
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import qt_app
from qt_app import ProMaxQt, _BatchResourceNotice


def memory_reason(percent: int) -> str:
    return f"内存占用已达 {percent}%，暂停启动下一部影片。"


class BatchResourceNoticeTest(unittest.TestCase):
    def test_percentage_changes_share_the_same_thirty_second_window(self) -> None:
        notice = _BatchResourceNotice()
        self.assertIn("85%", notice.update(False, memory_reason(85), 0.0))
        for second in range(1, 30):
            self.assertIsNone(notice.update(False, memory_reason(85 + second % 2), second))
        self.assertIn("86%", notice.update(False, memory_reason(86), 30.0))
        self.assertIsNone(notice.update(False, memory_reason(85), 59.99))
        self.assertIn("85%", notice.update(False, memory_reason(85), 60.0))

    def test_resource_category_change_is_reported_immediately(self) -> None:
        notice = _BatchResourceNotice()
        notice.update(False, memory_reason(85), 0.0)
        self.assertIn("磁盘", notice.update(False, "磁盘负载已达 92%，正在等待负载下降。", 1.0))
        self.assertIsNone(notice.update(False, "磁盘负载已达 95%，正在等待负载下降。", 2.0))
        self.assertIn("内存", notice.update(False, memory_reason(86), 3.0))

    def test_recovery_is_reported_once_and_new_wait_starts_immediately(self) -> None:
        notice = _BatchResourceNotice()
        self.assertIsNone(notice.update(True, "", 0.0))
        notice.update(False, memory_reason(85), 1.0)
        self.assertIn("已恢复", notice.update(True, "", 2.0))
        self.assertIsNone(notice.update(True, "", 3.0))
        self.assertIn("正在等待", notice.update(False, memory_reason(85), 4.0))


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.on_sleep = lambda: None

    def monotonic(self) -> float:
        return self.now

    def sleep(self, duration: float) -> None:
        self.now += duration
        if self.now > 300:
            raise AssertionError("The scheduler did not recover or honour cancellation")
        self.on_sleep()


class _ImmediateExecutor:
    def __init__(self, **_kwargs) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def submit(self, function, *args):
        future = concurrent.futures.Future()
        try:
            future.set_result(function(*args))
        except Exception as exc:
            future.set_exception(exc)
        return future


class BatchResourceSchedulingTest(unittest.TestCase):
    def run_batch(self, *, recover_at=None, cancel_at=None, item_cancel_at=None):
        clock = _FakeClock()
        path = "queued.mkv"
        plan = SimpleNamespace(
            status="ready", status_label="可处理", burned_subtitle=False,
            task_state="waiting", task_status_label="等待处理", profile_slot=1,
        )
        window = SimpleNamespace(
            batch_running=False, batch_plans={path: plan}, batch_paths=[path],
            profiles=[object()], batch_cancel_event=threading.Event(),
            batch_hardware=None, batch_start=Mock(), batch_stop=Mock(),
            batch_progress=Mock(), batch_mode=Mock(), _batch_workers=lambda: 1,
            _run_worker=Mock(), _batch_process_done=Mock(), _batch_error=Mock(),
            _batch_log_line=Mock(), _batch_progress_update=Mock(),
            _batch_item_state_update=Mock(), _batch_idle=Mock(),
        )
        messages = []
        states = []
        started_at = []
        signals = SimpleNamespace(
            message=SimpleNamespace(emit=lambda value: messages.append((clock.now, value))),
            state=SimpleNamespace(emit=lambda *value: states.append(value)),
            progress=SimpleNamespace(emit=Mock()),
        )

        def on_sleep():
            if cancel_at is not None and clock.now >= cancel_at:
                ProMaxQt._batch_stop(window)
            if item_cancel_at is not None and clock.now == item_cancel_at:
                ProMaxQt._batch_stop_one(window, path)

        def memory_status():
            if recover_at is not None and clock.now >= recover_at:
                return 84.0, 32.0
            return float(85 + int(clock.now) % 2), 32.0

        clock.on_sleep = on_sleep
        with ExitStack() as stack:
            stack.enter_context(patch.object(qt_app.batch_core, "plan_requires_online_subtitle_service", return_value=False))
            stack.enter_context(patch.object(qt_app.batch_core, "enough_space", return_value=(True, "")))
            process = stack.enter_context(patch.object(qt_app.batch_core, "process_plan", side_effect=lambda *_args: started_at.append(clock.now)))
            stack.enter_context(patch.object(qt_app.system_resources, "memory_status", side_effect=memory_status))
            stack.enter_context(patch.object(qt_app.system_resources, "_disk_busy_percent", return_value=0.0))
            stack.enter_context(patch.object(qt_app, "time", clock))
            stack.enter_context(patch.object(qt_app.concurrent.futures, "ThreadPoolExecutor", _ImmediateExecutor))
            ProMaxQt._batch_start_paths(window)
            task = window._run_worker.call_args.args[0]
            result = task(signals)

        return SimpleNamespace(
            result=result, messages=messages, states=states, started_at=started_at,
            process=process, plan=plan, clock=clock,
        )

    def test_eighty_three_high_memory_polls_log_three_waits_then_recover_once(self) -> None:
        outcome = self.run_batch(recover_at=83)
        waits = [(at, text) for at, text in outcome.messages if "正在等待资源恢复" in text]
        self.assertEqual([at for at, _text in waits], [0.0, 30.0, 60.0])
        self.assertTrue(all("可随时停止" in text for _at, text in waits))
        self.assertEqual(sum("已恢复" in text for _at, text in outcome.messages), 1)
        self.assertEqual(outcome.started_at, [83.0])
        self.assertEqual(outcome.result[:5], (1, 0, 0, 0, False))
        self.assertEqual(outcome.plan.task_state, "completed")

    def test_global_cancel_ends_idle_high_memory_wait_without_starting_work(self) -> None:
        outcome = self.run_batch(cancel_at=185)
        self.assertEqual(outcome.clock.now, 185.0)
        outcome.process.assert_not_called()
        self.assertEqual(outcome.result[:5], (0, 0, 1, 0, True))
        self.assertEqual(outcome.plan.task_state, "stopped")
        self.assertFalse(any("已恢复" in text for _at, text in outcome.messages))
        waits = [at for at, text in outcome.messages if "正在等待资源恢复" in text]
        self.assertEqual(waits, [0.0, 30.0, 60.0, 90.0, 120.0, 150.0, 180.0])

    def test_cancelled_queued_item_is_not_submitted_after_resource_recovery(self) -> None:
        outcome = self.run_batch(recover_at=31, item_cancel_at=30)
        outcome.process.assert_not_called()
        self.assertEqual(outcome.result[:5], (0, 0, 1, 0, False))
        self.assertEqual(outcome.plan.task_state, "stopped")


if __name__ == "__main__":
    unittest.main()
