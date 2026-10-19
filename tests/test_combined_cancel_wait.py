"""Use the actual batch cancellation object in startup/recovery code paths."""
import threading
import time
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch

import subtitle_tool_core as core
from qt_app import _CombinedCancelEvent


class CombinedCancelWaitTests(unittest.TestCase):
    def token(self): return _CombinedCancelEvent(threading.Event(),threading.Event())

    def test_zero_timeout_and_preset_cancellation(self):
        token=self.token()
        self.assertFalse(token.wait(0))
        self.assertFalse(token.wait(-1))
        for source in ('global_event','item_event'):
            token=self.token();getattr(token,source).set()
            self.assertTrue(token.wait(0))
            self.assertTrue(token.wait())

    def test_timeout_expires_without_changing_source_events(self):
        token=self.token();started=time.monotonic()
        self.assertFalse(token.wait(.04))
        self.assertGreaterEqual(time.monotonic()-started,.035)
        self.assertFalse(token.global_event.is_set())
        self.assertFalse(token.item_event.is_set())

    def test_both_sources_wake_a_long_wait(self):
        for source in ('global_event','item_event'):
            with self.subTest(source=source):
                token=self.token()
                trigger=threading.Timer(.04,getattr(token,source).set)
                trigger.start()
                started=time.monotonic()
                try:self.assertTrue(token.wait(5))
                finally:trigger.join()
                self.assertLess(time.monotonic()-started,.5)

    def test_unbounded_wait_wakes_on_global_cancellation(self):
        token=self.token()
        trigger=threading.Timer(.04,token.global_event.set);trigger.start()
        try:self.assertTrue(token.wait())
        finally:trigger.join()

    def test_stopping_one_movie_does_not_stop_another(self):
        global_event=threading.Event()
        first=_CombinedCancelEvent(global_event,threading.Event())
        second=_CombinedCancelEvent(global_event,threading.Event())
        first.item_event.set()
        self.assertTrue(first.wait(0))
        self.assertFalse(second.wait(0))
        global_event.set()
        self.assertTrue(second.wait(0))

    def _startup(self, token, availability, stop_source=None):
        self.saved={name:getattr(core,name) for name in ('_OLLAMA_SERVER_PROCESS','_OLLAMA_SERVER_HOST')}
        core._OLLAMA_SERVER_PROCESS=None;core._OLLAMA_SERVER_HOST=None
        process=Mock(pid=123);process.poll.return_value=None
        messages=[]
        try:
            with tempfile.TemporaryDirectory() as folder,patch.object(core,'OLLAMA_EXE',__file__),patch.object(core,'ollama_runtime_dir',return_value=Path(folder)),patch.object(core,'_write_ollama_diagnostic'),patch.object(core,'_capture_ollama_diagnostics'),patch.object(core.subprocess,'Popen',return_value=process),patch.object(core,'ollama_available',side_effect=availability),patch.object(core,'terminate_process_tree') as terminate:
                if stop_source:
                    timer=threading.Timer(.04,getattr(token,stop_source).set);timer.start()
                    try:
                        with self.assertRaises(core.CancelledError):
                            core.ensure_ollama_running(messages.append,token,wait_seconds=2)
                    finally:timer.join()
                    terminate.assert_called_once_with(process)
                    self.assertIsNone(core._OLLAMA_SERVER_PROCESS)
                else:
                    core.ensure_ollama_running(messages.append,token,wait_seconds=2)
                    terminate.assert_not_called()
                    self.assertIn('Ollama 已启动。',messages)
        finally:
            for name,value in self.saved.items():setattr(core,name,value)

    def test_cold_start_waits_then_succeeds_with_real_batch_token(self):
        self._startup(self.token(),[False,False,True])

    def test_global_stop_during_cold_start_cleans_up_owned_engine(self):
        self._startup(self.token(),lambda *_:False,'global_event')

    def test_item_stop_during_cold_start_cleans_up_owned_engine(self):
        self._startup(self.token(),lambda *_:False,'item_event')

    def test_warm_engine_does_not_enter_wait_or_spawn(self):
        token=self.token()
        with patch.object(core,'ollama_available',return_value=True),patch.object(core.subprocess,'Popen') as spawn,patch.object(token,'wait',wraps=token.wait) as wait:
            core.ensure_ollama_running(cancel_event=token)
        spawn.assert_not_called();wait.assert_not_called()

    def test_translation_retries_after_disconnect_with_batch_token(self):
        token=self.token()
        with patch.object(core,'call_ollama',side_effect=[ConnectionResetError('fixture'),'OK']) as request,patch.object(core,'_write_ollama_diagnostic'),patch.object(core,'_OLLAMA_SERVER_PROCESS',None):
            self.assertEqual(core.call_ollama_retry('fixture','fixture',core.OLLAMA_HOST,90,cancel_event=token),'OK')
        self.assertEqual(request.call_count,2)

    def test_global_stop_during_retry_skips_second_model_request(self):
        token=self.token()
        def request(*_args,**_kwargs):
            token.global_event.set()
            raise ConnectionResetError('fixture')
        with patch.object(core,'call_ollama',side_effect=request) as call,patch.object(core,'_write_ollama_diagnostic'):
            with self.assertRaises(core.CancelledError):
                core.call_ollama_retry('fixture','fixture',core.OLLAMA_HOST,90,cancel_event=token)
        self.assertEqual(call.call_count,1)


if __name__=='__main__': unittest.main()
