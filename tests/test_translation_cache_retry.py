import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import subtitle_tool_core as core
import pro_core


class TranslationCacheRetryTest(unittest.TestCase):
    def test_translation_failure_reason_is_logged(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subtitle = root / "external-verified.srt"
            subtitle.write_text(
                "1\n00:00:00,000 --> 00:00:01,000\nHello\n",
                encoding="utf-8",
            )
            messages: list[str] = []

            with patch.object(
                pro_core,
                "prepare_embedded_text_corrections",
                return_value=({}, {}, None),
            ), patch.object(core, "ensure_output_disk_space"), patch.object(
                core, "ensure_ollama_running"
            ), patch.object(
                core,
                "translate_events",
                side_effect=RuntimeError("连接意外断开"),
            ):
                with self.assertRaisesRegex(RuntimeError, "翻译中断.*连接意外断开"):
                    pro_core.process_pro(
                        input_path="movie.mkv",
                        output_path=str(root / "output.mkv"),
                        keep_audio_ids=[],
                        keep_subtitle_ids=[],
                        source_mode="external",
                        embedded_source_id=None,
                        external_subtitle=str(subtitle),
                        audio_source_id=0,
                        speech_language="en",
                        source_language="en",
                        target_codes=["de"],
                        work_dir=str(root / "work"),
                        log=messages.append,
                        parallel_targets=1,
                        cancel_event=None,
                        trust_external_original_timeline=True,
                    )

            self.assertTrue(
                any(
                    "德语翻译中断" in message
                    and "RuntimeError: 连接意外断开" in message
                    and "翻译缓存仍然保留" in message
                    for message in messages
                )
            )

    def test_append_cache_retries_transient_permission_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "translation-cache-zh-CN.jsonl"
            real_open = Path.open
            attempts = 0

            def flaky_open(path, *args, **kwargs):
                nonlocal attempts
                attempts += 1
                if attempts < 3:
                    raise PermissionError(13, "temporarily locked", str(path))
                return real_open(path, *args, **kwargs)

            with patch.object(Path, "open", flaky_open), patch.object(core.time, "sleep") as sleep:
                core.append_cache(cache, "zh-CN", 1, "Hello", "你好")

            self.assertEqual(attempts, 3)
            self.assertEqual(sleep.call_count, 2)
            item = json.loads(cache.read_text(encoding="utf-8"))
            self.assertEqual(item["translation"], "你好")

    def test_retry_reports_path_after_exhaustion(self) -> None:
        cache = Path("translation-cache-zh-CN.jsonl")

        def always_locked() -> None:
            raise PermissionError(13, "locked", str(cache))

        with patch.object(core.time, "sleep"):
            with self.assertRaisesRegex(PermissionError, "8 attempts"):
                core.retry_file_operation(always_locked, cache, "append translation cache")

    def test_batch_cache_uses_one_file_open(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "translation-cache-zh-CN.jsonl"
            real_open = Path.open
            opens = 0

            def counted_open(path, *args, **kwargs):
                nonlocal opens
                opens += 1
                return real_open(path, *args, **kwargs)

            with patch.object(Path, "open", counted_open):
                core.append_cache_records(
                    cache,
                    "zh-CN",
                    [(1, "One", "一"), (2, "Two", "二"), (3, "Three", "三")],
                )

            self.assertEqual(opens, 1)
            self.assertEqual(len(cache.read_text(encoding="utf-8").splitlines()), 3)


if __name__ == "__main__":
    unittest.main()
