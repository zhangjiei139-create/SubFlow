"""Exercise actual translation entry points with deterministic resource handoffs."""
from __future__ import annotations

from contextlib import ExitStack
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import batch_core as batch
import pro_core as pro
import subtitle_tool_core as core


class TranslationStageTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.movie = self.root / "movie.mkv"
        self.movie.write_bytes(b"movie")
        self.source = self.root / "source.srt"
        self._source("en")
        self.logs = []

    def _source(self, language):
        text = "Hello, please come with me." if language == "en" else "後臺軟體發展"
        self.source.write_text(
            f"1\n00:00:01,000 --> 00:00:03,000\n{text}\n\n"
            f"2\n00:00:05,000 --> 00:00:07,000\n{text}\n",
            encoding="utf-8",
        )
        self.language = language
        self.track = core.Track(1, "subtitles", "S_TEXT/UTF8", language,
                                "", False, False, True, False, False)

    def _mocks(self, mux=None):
        stack = ExitStack()
        for name in ("ensure_output_disk_space", "ensure_mux_disk_space", "validate_media_output"):
            stack.enter_context(patch.object(core, name))
        stack.enter_context(patch.object(core, "inspect_media", return_value={"tracks": []}))
        stack.enter_context(patch.object(core, "inspect_tracks", return_value=[self.track]))
        stack.enter_context(patch.object(core, "prepare_work_input", side_effect=lambda movie, *_a, **_k: movie))
        stack.enter_context(patch.object(core, "extract_subtitle", return_value=self.source))
        stack.enter_context(patch.object(pro, "prepare_embedded_text_corrections", return_value=({}, {}, None)))
        stack.enter_context(patch.object(pro, "_prepare_local_chinese_conversions", return_value=[]))
        stack.enter_context(patch.object(core, "mux_video", side_effect=mux or self._mux))
        stack.enter_context(patch.object(core, "unload_ollama_model"))
        return stack

    @staticmethod
    def _mux(_movie, output, *_args, **_kwargs):
        Path(output).write_bytes(b"muxed")

    @staticmethod
    def _translate(events, output, _cache, target, *_args, **_kwargs):
        core.write_srt(Path(output), events,
                       {i: f"{target}: {event.text}" for i, event in enumerate(events, 1)})

    def _run(self, route, label="one", targets=None, start=None, end=None,
             parallel=1, stop=None, omit_callbacks=False):
        targets = ["de"] if targets is None else targets
        callbacks = {} if omit_callbacks else {"translation_start": start, "translation_end": end}
        work = self.root / label
        output = self.root / f"{label}.mkv"
        if route == "external":
            return pro.process_pro(
                str(self.movie), str(output), [], [], "external", None,
                str(self.source), 0, self.language, self.language, targets, str(work),
                self.logs.append, parallel, stop,
                trust_external_original_timeline=True, **callbacks,
            )
        if route == "embedded":
            # The production pro_core wrapper must forward both callbacks.
            return pro.process_pro(
                str(self.movie), str(output), [], [], "embedded", 1,
                None, 0, self.language, self.language, targets, str(work),
                self.logs.append, parallel, stop, **callbacks,
            )
        raise AssertionError(route)

    def _spawn(self, operation):
        errors = []
        def run():
            try:
                operation()
            except BaseException as exc:
                errors.append(exc)
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        return thread, errors

    def test_callbacks_end_before_mux_for_both_entry_routes(self):
        for route in ("external", "embedded"):
            with self.subTest(route=route):
                order = []
                def translate(*args, **kwargs):
                    order.append("translate")
                    self._translate(*args, **kwargs)
                def mux(*args, **kwargs):
                    order.append("mux")
                    self._mux(*args, **kwargs)
                with self._mocks(mux), patch.object(core, "ensure_ollama_running", side_effect=lambda **_k: order.append("ensure")), \
                        patch.object(core, "translate_events", side_effect=translate):
                    self._run(route, label=route, start=lambda: order.append("start"), end=lambda: order.append("end"))
                self.assertEqual(order, ["start", "ensure", "translate", "end", "mux"])

    def test_next_film_translates_while_previous_film_is_muxing(self):
        for route in ("external", "embedded"):
            with self.subTest(route=route):
                mux_started, release_mux, translated_b = (threading.Event() for _ in range(3))
                gate = threading.BoundedSemaphore(1)
                scopes = {label: batch._TranslationResourceScope(gate, self.logs.append, None)
                          for label in ("A", "B")}
                def translate(events, output, *args, **kwargs):
                    if Path(output).parent.name == "B":
                        translated_b.set()
                    self._translate(events, output, *args, **kwargs)
                def mux(movie, output, *args, **kwargs):
                    if Path(output).name.startswith("A."):
                        mux_started.set()
                        if not release_mux.wait(3):
                            raise AssertionError("test did not release A mux")
                    self._mux(movie, output, *args, **kwargs)
                with self._mocks(mux), patch.object(core, "_OLLAMA_LEASES", 0), \
                        patch.object(core, "_OLLAMA_STOPPING", False), \
                        patch.object(core, "ensure_ollama_running"), \
                        patch.object(core, "translate_events", side_effect=translate):
                    a, a_errors = self._spawn(lambda: self._run(route, "A", start=scopes["A"].start, end=scopes["A"].finish))
                    b = None
                    try:
                        self.assertTrue(mux_started.wait(2))
                        b, b_errors = self._spawn(lambda: self._run(route, "B", start=scopes["B"].start, end=scopes["B"].finish))
                        self.assertTrue(translated_b.wait(2), "B remained blocked by A's mux")
                    finally:
                        release_mux.set()
                        a.join(3)
                        if b is not None:
                            b.join(3)
                    self.assertFalse(a.is_alive())
                    self.assertFalse(b.is_alive())
                    self.assertEqual(a_errors + b_errors, [])
                    self.assertEqual(core._OLLAMA_LEASES, 0)
                self.assertTrue(gate.acquire(blocking=False))
                gate.release()

    def test_failed_parallel_target_waits_for_other_target_before_end(self):
        for route in ("external", "embedded"):
            with self.subTest(route=route):
                second_running, first_failed, release_second, ended = (threading.Event() for _ in range(4))
                def translate(events, output, cache, target, *args, **kwargs):
                    if target == "es":
                        second_running.set()
                        if not release_second.wait(3):
                            raise AssertionError("test did not release second target")
                        self._translate(events, output, cache, target, *args, **kwargs)
                    else:
                        if not second_running.wait(2):
                            raise AssertionError("second target did not start")
                        first_failed.set()
                        raise RuntimeError("first target failed")
                with self._mocks(), patch.object(core, "ensure_ollama_running"), \
                        patch.object(core, "translate_events", side_effect=translate), \
                        patch.object(core, "mux_video") as mux:
                    worker, errors = self._spawn(lambda: self._run(route, label=route, targets=["de", "es"],
                        parallel=2, start=lambda: None, end=ended.set))
                    try:
                        self.assertTrue(first_failed.wait(2))
                        self.assertFalse(ended.wait(.05), "lease ended with a live target worker")
                    finally:
                        release_second.set()
                        worker.join(3)
                    self.assertFalse(worker.is_alive())
                    self.assertTrue(ended.is_set())
                    self.assertEqual(len(errors), 1)
                    self.assertIn("first target failed", str(errors[0]))
                    mux.assert_not_called()

    def test_startup_error_finishes_scope_and_never_muxes(self):
        for route in ("external", "embedded"):
            with self.subTest(route=route):
                start, end = Mock(), Mock()
                with self._mocks(), patch.object(core, "ensure_ollama_running", side_effect=RuntimeError("startup failed")), \
                        patch.object(core, "mux_video") as mux:
                    with self.assertRaisesRegex(RuntimeError, "startup failed"):
                        self._run(route, label=route, start=start, end=end)
                start.assert_called_once_with()
                end.assert_called_once_with()
                mux.assert_not_called()

    def test_cache_script_same_language_and_empty_targets_need_no_scope(self):
        for route in ("external", "embedded"):
            for case in ("cache", "script", "same", "empty"):
                with self.subTest(route=route, case=case):
                    self._source("zh-TW" if case == "script" else "en")
                    label = f"{route}-{case}"
                    targets = {"cache": ["de"], "script": ["zh-CN"], "same": ["en"], "empty": []}[case]
                    if case == "cache":
                        cache_name = ("translation-cache-de.jsonl" if route == "external"
                                      else "translation-cache-track-1-de.jsonl")
                        cache = self.root / label / cache_name
                        cache.parent.mkdir(parents=True, exist_ok=True)
                        for index, event in enumerate(core.parse_subtitle(self.source), 1):
                            core.append_cache(cache, "de", index, event.text.strip(), "cached translation")
                    start, end = Mock(), Mock()
                    with self._mocks(), patch.object(core, "ensure_ollama_running") as ensure, \
                            patch.object(core, "translate_batch", side_effect=AssertionError("model should be unused")):
                        output = self._run(route, label, targets=targets, start=start, end=end)
                    self.assertTrue(Path(output).is_file())
                    start.assert_not_called()
                    end.assert_not_called()
                    ensure.assert_not_called()

    def test_no_callback_arguments_preserves_standalone_entry_compatibility(self):
        for route in ("external", "embedded"):
            with self.subTest(route=route):
                with self._mocks(), patch.object(core, "ensure_ollama_running") as ensure, \
                        patch.object(core, "translate_events", side_effect=self._translate):
                    output = self._run(route, label=route, omit_callbacks=True)
                self.assertTrue(Path(output).is_file())
                ensure.assert_called_once()


class TranslationResourceScopeTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(core, "_OLLAMA_LEASES", 0))
        self.stack.enter_context(patch.object(core, "_OLLAMA_STOPPING", False))
        self.unload = self.stack.enter_context(patch.object(core, "unload_ollama_model"))

    def test_queued_cancel_never_releases_current_owner(self):
        gate, stop, waiting = threading.BoundedSemaphore(1), threading.Event(), threading.Event()
        owner = batch._TranslationResourceScope(gate, lambda _message: None, None)
        waiter = batch._TranslationResourceScope(gate, lambda _message: waiting.set(), stop)
        owner.start()
        errors = []
        def wait():
            try:
                waiter.start()
            except BaseException as exc:
                errors.append(exc)
        thread = threading.Thread(target=wait, daemon=True)
        thread.start()
        try:
            self.assertTrue(waiting.wait(1))
            self.assertEqual(core._OLLAMA_LEASES, 2)
            stop.set()
            thread.join(2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], core.CancelledError)
            self.assertFalse(gate.acquire(blocking=False))
            self.assertEqual(core._OLLAMA_LEASES, 1)
            self.unload.assert_not_called()
        finally:
            stop.set()
            thread.join(2)
            owner.finish()
        self.assertEqual(core._OLLAMA_LEASES, 0)

    def test_cancel_immediately_after_acquire_releases_own_permit(self):
        stop = threading.Event()
        gate = Mock()
        def acquire(**_kwargs):
            stop.set()
            return True
        gate.acquire.side_effect = acquire
        scope = batch._TranslationResourceScope(gate, lambda _message: None, stop)
        with self.assertRaises(core.CancelledError):
            scope.start()
        scope.finish()
        gate.release.assert_called_once_with()
        self.assertFalse(scope.acquired)
        self.assertFalse(scope.leased)
        self.assertEqual(core._OLLAMA_LEASES, 0)
        self.unload.assert_called_once()

    def test_acquire_failure_rolls_back_lease(self):
        gate = Mock()
        gate.acquire.side_effect = RuntimeError("gate failed")
        scope = batch._TranslationResourceScope(gate, lambda _message: None, None)
        with self.assertRaisesRegex(RuntimeError, "gate failed"):
            scope.start()
        self.assertEqual(core._OLLAMA_LEASES, 0)
        gate.release.assert_not_called()
        self.assertFalse(scope.leased)

    def test_log_failure_after_acquire_rolls_back_permit_and_lease(self):
        gate = Mock()
        gate.acquire.return_value = True
        def log(message):
            if "已取得" in message:
                raise RuntimeError("log failed")
        scope = batch._TranslationResourceScope(gate, log, None)
        with self.assertRaisesRegex(RuntimeError, "log failed"):
            scope.start()
        gate.release.assert_called_once_with()
        self.assertEqual(core._OLLAMA_LEASES, 0)

    def test_waiting_translator_prevents_unload_during_handoff(self):
        gate, waiting, acquired, release = (threading.BoundedSemaphore(1), threading.Event(),
                                            threading.Event(), threading.Event())
        first = batch._TranslationResourceScope(gate, lambda _message: None, None)
        second = batch._TranslationResourceScope(gate, lambda _message: waiting.set(), None)
        first.start()
        errors = []
        def run():
            try:
                second.start()
                acquired.set()
                if not release.wait(3):
                    raise AssertionError("test did not release next translator")
            except BaseException as exc:
                errors.append(exc)
            finally:
                second.finish()
        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        try:
            self.assertTrue(waiting.wait(1))
            self.assertEqual(core._OLLAMA_LEASES, 2)
            first.finish()
            self.assertTrue(acquired.wait(2))
            self.unload.assert_not_called()
            self.assertEqual(core._OLLAMA_LEASES, 1)
        finally:
            release.set()
            first.finish()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(core._OLLAMA_LEASES, 0)
        self.unload.assert_called_once()


class TranslationCallbackContractTests(unittest.TestCase):
    def test_partial_callbacks_are_rejected_before_preparation_or_disk_io(self):
        for start, end in ((Mock(), None), (None, Mock())):
            with self.subTest(start=start is not None), patch.object(
                pro, "prepare_embedded_text_corrections",
            ) as prepare, patch.object(core, "ensure_output_disk_space") as disk:
                with self.assertRaisesRegex(ValueError, "同时提供"):
                    pro.process_pro(
                        input_path="movie.mkv", output_path="output.mkv",
                        keep_audio_ids=[], keep_subtitle_ids=[], source_mode="none",
                        embedded_source_id=None, external_subtitle=None, audio_source_id=None,
                        speech_language="en", source_language="en", target_codes=[],
                        work_dir="unused", log=lambda _: None, parallel_targets=1,
                        cancel_event=None, translation_start=start, translation_end=end,
                    )
                with self.assertRaisesRegex(ValueError, "同时提供"):
                    core.process_video(
                        "movie.mkv", "output.mkv", [], [], None, [], "unused",
                        translation_start=start, translation_end=end,
                    )
                prepare.assert_not_called()
                disk.assert_not_called()
                for callback in (start, end):
                    if callback is not None:
                        callback.assert_not_called()


if __name__ == "__main__":
    unittest.main()
