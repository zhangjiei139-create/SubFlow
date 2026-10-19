import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import subtitle_tool_core as core
import translation_recovery as recovery


class Stream:
    def __init__(self, items):
        self.lines = [json.dumps(item).encode() + b'\n' for item in items]
    def __enter__(self):
        return self
    def __exit__(self, *_):
        return False
    def readline(self):
        return self.lines.pop(0) if self.lines else b''


class GenerationRecoveryTests(unittest.TestCase):
    def test_repetition_is_not_treated_as_retryable_network_timeout(self):
        response = Stream([{'response': 'somehow 白人 ', 'done': False}] * 60)
        with patch.object(core.urllib.request, 'urlopen', return_value=response) as request:
            with self.assertRaises(core.TranslationGenerationError):
                core.call_ollama_retry('translate', 'model', 'http://localhost', 600)
        self.assertEqual(request.call_count, 1)

    def test_length_limited_output_is_never_accepted(self):
        response = Stream([{'response': '1. incomplete', 'done': True, 'done_reason': 'length'}])
        with patch.object(core.urllib.request, 'urlopen', return_value=response):
            with self.assertRaises(core.TranslationGenerationError):
                core.call_ollama('translate', 'model', 'http://localhost', 600)

    def test_recovery_schema_and_generation_budget_reach_existing_api(self):
        schema = {'type': 'object', 'properties': {'t': {'type': 'string'}}}
        response = Stream([{'response': '{"t":"你好"}', 'done': True}])
        with patch.object(core.urllib.request, 'urlopen', return_value=response) as request:
            self.assertEqual(core.call_ollama_retry('hello', 'model', 'http://localhost', 600,
                             response_schema=schema, max_tokens=200), '{"t":"你好"}')
        body = json.loads(request.call_args.args[0].data)
        self.assertEqual(body['format'], schema)
        self.assertEqual(body['options']['num_predict'], 200)

    def test_window_does_not_cross_unavailable_cached_neighbor(self):
        sources = {1: 'Each time I arrived,', 2: 'somehow they had moved...', 3: 'all their people.'}
        self.assertEqual(recovery.window_indices(2, {1, 2, 3}, sources), [1, 2, 3])
        self.assertEqual(recovery.window_indices(2, {2, 3}, sources), [2, 3])
        self.assertFalse(recovery.continuation('This is finished.', 'another line'))

    def test_lossless_check_rejects_added_or_missing_words(self):
        good = {'whole': '每次我到达时，他们已经把所有人迁走了。',
                's1': '每次我到达时，', 's2': '他们已经把所有人', 's3': '迁走了。'}
        self.assertEqual(len(recovery.parse_response(json.dumps(good), 3)), 3)
        good['s2'] = '他们已经'
        with self.assertRaises(ValueError):
            recovery.parse_response(json.dumps(good), 3)

    def _pipeline(self, directory, response):
        events = [core.SubtitleEvent('00:00:01,000', '00:00:02,000', 'Each time I arrived,'),
                  core.SubtitleEvent('00:00:02,000', '00:00:03,000', 'somehow they had moved...'),
                  core.SubtitleEvent('00:00:03,000', '00:00:04,000', 'all their people.')]
        folder = Path(directory)
        with patch.object(core, 'translate_batch', side_effect=[
                ['每次抵达时，', 'somehow他们搬走了', '所有人。'],
                core.TranslationGenerationError('repeating')]), \
                patch.object(core, 'call_ollama_retry', return_value=json.dumps(response)):
            core.translate_events(events, folder/'out.srt', folder/'cache.jsonl', 'zh-CN', 'en', lambda _: None)
        return events, core.parse_subtitle(folder/'out.srt'), [json.loads(line) for line in (folder/'cache.jsonl').read_text('utf-8').splitlines()]

    def test_recovery_updates_previous_and_future_cues_together(self):
        response = {'whole': '每次我到达时，他们已经把所有人迁走了。',
                    's1': '每次我到达时，', 's2': '他们已经把所有人', 's3': '迁走了。'}
        with tempfile.TemporaryDirectory() as directory:
            original, output, records = self._pipeline(directory, response)
        self.assertEqual([e.text for e in output], [response['s1'], response['s2'], response['s3']])
        self.assertEqual([(e.start, e.end) for e in output], [(e.start, e.end) for e in original])
        self.assertEqual([r['index'] for r in records], [1, 2, 3])

    def test_rejected_recovery_cannot_partially_overwrite_good_neighbor(self):
        response = {'whole': '每次我到达时，他们已经把所有人迁走了。',
                    's1': '每次我到达时，', 's2': '他们已经', 's3': '迁走了。'}
        with tempfile.TemporaryDirectory() as directory:
            _, output, records = self._pipeline(directory, response)
        self.assertEqual(output[0].text, '每次抵达时，')
        self.assertEqual(records[0]['translation'], '每次抵达时，')
        self.assertNotIn(2, [r['index'] for r in records])


if __name__ == '__main__':
    unittest.main()
