"""Real translation control flow with deterministic, in-memory model responses."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import subtitle_tool_core as core
import translation_review as review


SOURCE = 'I know that you think outside of the box.'
LEAK = 'Ich weiß, dass du außerhalb des框 denkst.'
FIXED = 'Ich weiß, dass du über den Tellerrand hinausdenkst.'


class TranslationReviewTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.output = self.root / 'translated-de.srt'
        self.cache = self.root / 'translation-cache-de.jsonl'
        self.report = review.report_path(self.cache)
        self.logs = []
        self.events = [core.SubtitleEvent('00:15:49,830', '00:15:51,891', SOURCE),
                       core.SubtitleEvent('00:16:00,000', '00:16:02,000', 'Hello.')]

    def run_translation(self, events=None, **kwargs):
        core.translate_events(events or self.events, self.output, self.cache, 'de', 'en', self.logs.append, **kwargs)

    def make_review(self):
        with patch.object(core, 'translate_batch', side_effect=[[LEAK, 'Hallo.'], [LEAK], [LEAK]]):
            self.run_translation()
        return review.load_report(self.report)

    def test_actual_cue_277_continues_after_two_distinct_repairs_and_resumes_without_model(self):
        events = [core.SubtitleEvent('00:00:00,000', '00:00:01,000', f'Previous line {index}.')
                  for index in range(1, 277)] + self.events
        core.append_cache_records(self.cache, 'de', [(index, event.text, f'Vorherige Zeile {index}.')
                                                  for index, event in enumerate(events[:276], 1)])
        with patch.object(core, 'translate_batch', side_effect=[[LEAK], [LEAK], [LEAK], ['Hallo.']]) as model:
            self.run_translation(events, batch_size=1)
        self.assertEqual(model.call_count, 4)
        self.assertEqual(model.call_args_list[1].kwargs['repair_feedback'], '')
        self.assertIn(LEAK, model.call_args_list[2].kwargs['repair_feedback'])
        self.assertIn('fresh, idiomatic translation', core.make_prompt([SOURCE], 'de', 'en',
            require_target_script=True, repair_feedback=model.call_args_list[2].kwargs['repair_feedback']))
        data = review.load_report(self.report)
        self.assertTrue(data['complete'])
        self.assertEqual(data['issues'], [dict(index=277, start='00:15:49,830', end='00:15:51,891',
            source=SOURCE, translation=LEAK, reason=core.translation_quality_issue(SOURCE, LEAK, 'de'),
            original_reason=core.translation_quality_issue(SOURCE, LEAK, 'de'), status='needs_review', repair_attempts=2)])
        accepted, stale = core.load_cache(self.cache, 'de', {index: event.text for index, event in enumerate(events, 1)})
        self.assertEqual(len(accepted), 277)
        self.assertNotIn(277, accepted)
        self.assertEqual(stale, 0)
        self.assertEqual(len(core.parse_subtitle(self.output)), 278)
        self.assertEqual(core.parse_subtitle(self.output)[277].text, 'Hallo.')
        self.assertEqual(review.discover_reports(self.root), [self.report])
        self.assertFalse(core.needs_model_translation(events, 'de', self.cache, 'en'))
        with patch.object(core, 'translate_batch', side_effect=AssertionError('must reuse saved warning')):
            self.run_translation(events)
        self.assertEqual(review.load_report(self.report)['issues'][0]['translation'], LEAK)

    def test_incomplete_warning_survives_connection_failure_and_retries_only_missing_rows(self):
        with patch.object(core, 'translate_batch', side_effect=[[LEAK], [LEAK], [LEAK], ConnectionError('offline')]):
            with self.assertRaisesRegex(ConnectionError, 'offline'):
                self.run_translation(batch_size=1)
        self.assertFalse(self.output.exists())
        self.assertEqual(review.discover_reports(self.root), [])
        self.assertEqual(list(review.restore_issues(self.events, 'de', self.cache)), [1])
        with patch.object(core, 'translate_batch', return_value=['Hallo.']) as model:
            self.run_translation(batch_size=1)
        self.assertEqual(model.call_args.args[0], ['Hello.'])
        self.assertTrue(review.load_report(self.report)['complete'])

    def test_empty_damaged_and_disconnected_response_remain_errors(self):
        for response in (['', 'Hallo.'], [None, 'Hallo.'], ['x\x00', 'Hallo.'], ['only one'],
                         ['a\n\nb', 'Hallo.'], ['00:00:01,000 --> 00:00:02,000', 'Hallo.']):
            with self.subTest(response=response), patch.object(core, 'translate_batch', return_value=response):
                with self.assertRaises(RuntimeError):
                    self.run_translation()
                self.assertFalse(self.output.exists())
                self.assertFalse(self.report.exists())
        with patch.object(core, 'translate_batch', side_effect=ConnectionError('offline')):
            with self.assertRaises(ConnectionError):
                self.run_translation()

    def test_empty_or_damaged_repair_preserves_last_usable_translation(self):
        for response in ([''], [None], [], ['x\x00'], None):
            with self.subTest(response=response), patch.object(core, 'translate_batch', side_effect=[[LEAK, 'Hallo.'], response, response]) as model:
                self.run_translation()
                self.assertEqual(model.call_count, 3)
                issue = review.load_report(self.report)['issues'][0]
                self.assertEqual(issue['translation'], LEAK)
                self.assertEqual(issue['status'], 'needs_review')
                self.assertEqual(len(issue['repair_errors']), 2)
                self.assertNotIn(1, core.load_cache(self.cache, 'de', {1: SOURCE, 2: 'Hello.'})[0])
                self.report.unlink()
                self.cache.unlink()

    def test_invalid_first_repair_can_be_followed_by_successful_second_repair(self):
        with patch.object(core, 'translate_batch', side_effect=[[LEAK, 'Hallo.'], [''], [FIXED]]):
            self.run_translation()
        self.assertFalse(self.report.exists())
        self.assertEqual(core.parse_subtitle(self.output)[0].text, FIXED)

    def test_actual_empty_repair_parser_response_keeps_valid_initial_translation(self):
        with patch.object(core, 'call_ollama_retry', side_effect=[f'1. {LEAK}\n2. Hallo.', '', '', '', '']) as model:
            self.run_translation()
        self.assertEqual(model.call_count, 5)
        issue = review.load_report(self.report)['issues'][0]
        self.assertEqual(issue['translation'], LEAK)
        self.assertEqual(len(issue['repair_errors']), 2)

    def test_connection_failure_during_repair_still_aborts(self):
        with patch.object(core, 'translate_batch', side_effect=[[LEAK, 'Hallo.'], ConnectionError('offline')]):
            with self.assertRaisesRegex(ConnectionError, 'offline'):
                self.run_translation()
        self.assertFalse(self.output.exists())
        self.assertFalse(self.report.exists())

    def test_warning_persists_before_later_repair_in_same_batch_fails(self):
        with patch.object(core, 'translate_batch', side_effect=[[LEAK, 'Hallo.框'], [LEAK], [LEAK], ConnectionError('offline')]):
            with self.assertRaises(ConnectionError):
                self.run_translation()
        self.assertFalse(self.output.exists())
        self.assertEqual(list(review.restore_issues(self.events, 'de', self.cache)), [1])
        with patch.object(core, 'translate_batch', return_value=['Hallo.']) as model:
            self.run_translation()
        self.assertEqual(model.call_args.args[0], ['Hello.'])

    def test_cancel_after_last_failed_repair_does_not_publish(self):
        cancel = threading.Event()
        count = 0
        def model(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 3:
                cancel.set()
            return [LEAK, 'Hallo.'] if count == 1 else [LEAK]
        with patch.object(core, 'translate_batch', side_effect=model):
            with self.assertRaises(core.CancelledError):
                self.run_translation(cancel_event=cancel)
        self.assertFalse(self.output.exists())
        self.assertFalse(self.report.exists())

    def test_save_correction_preserves_timeline_good_rows_markup_and_survives_retry(self):
        with patch.object(core, 'translate_batch', side_effect=[[LEAK, '<i>Hallo.</i>\nGuten Tag!'], [LEAK], [LEAK]]):
            self.run_translation()
        source_before = Path(review.load_report(self.report)['source_srt']).read_bytes()
        corrected = '<i>Ich weiß, dass du</i>\nüber den Tellerrand hinausdenkst.'
        data = review.save_corrections(self.report, {1: corrected})
        self.assertEqual(data['issues'][0]['status'], 'resolved')
        self.assertEqual(data['issues'][0]['reason'], '')
        self.assertEqual(Path(data['source_srt']).read_bytes(), source_before)
        self.assertIn('00:15:49,830 --> 00:15:51,891', self.output.read_text(encoding='utf-8-sig'))
        self.assertIn('<i>Hallo.</i>\nGuten Tag!', self.output.read_text(encoding='utf-8-sig'))
        self.assertEqual(core.load_cache(self.cache, 'de', {1: SOURCE, 2: 'Hello.'})[0][1], corrected)
        with patch.object(core, 'translate_batch', side_effect=AssertionError('manual edit lost')):
            self.run_translation()
        self.assertEqual(review.load_report(self.report)['issues'][0]['translation'], corrected)

    def test_user_can_explicitly_keep_warning_but_never_blank_or_invalid_subtitle(self):
        self.make_review()
        review.save_corrections(self.report, {1: FIXED})
        before = {path: path.read_bytes() for path in (self.report, self.cache, self.output)}
        with self.assertRaises(review.QualityWarningError) as error:
            review.save_corrections(self.report, {1: LEAK})
        self.assertEqual(error.exception.issues[0]['index'], 1)
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        saved = review.save_corrections(self.report, {1: LEAK}, allow_quality_warning=True)
        self.assertEqual(saved['issues'][0]['status'], 'user_approved')
        self.assertNotIn(1, core.load_cache(self.cache, 'de', {1: SOURCE, 2: 'Hello.'})[0])
        with patch.object(core, 'translate_batch', side_effect=AssertionError('explicit choice lost')):
            self.run_translation()
        self.assertEqual(review.load_report(self.report)['issues'][0]['status'], 'user_approved')
        for text in ('', 'a\n\nb', '00:00:01,000 --> 00:00:02,000', '\x00'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                review.save_corrections(self.report, {1: text}, allow_quality_warning=True)

    def test_source_timing_output_or_folder_change_cannot_reuse_other_movie_report(self):
        data = self.make_review()
        changed = [core.SubtitleEvent('00:15:50,830', '00:15:52,891', SOURCE), self.events[1]]
        self.assertEqual(review.restore_issues(changed, 'de', self.cache), {})
        changed = [core.SubtitleEvent(self.events[0].start, self.events[0].end, 'Different source'), self.events[1]]
        self.assertEqual(review.restore_issues(changed, 'de', self.cache), {})
        self.assertEqual(review.restore_issues(self.events, 'fr', self.cache), {})
        self.assertEqual(review.restore_issues(self.events, 'de', self.cache, self.root / 'different.srt'), {})
        other = self.root / 'other-film'
        other.mkdir()
        copied = other / self.report.name
        copied.write_bytes(self.report.read_bytes())
        with self.assertRaises(ValueError):
            review.load_report(copied)
        self.output.write_text('replaced subtitle', encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '字幕文件已变化'):
            review.save_corrections(self.report, {1: FIXED})
        self.assertEqual(Path(data['source_srt']).parent, self.root)

    def test_source_hash_and_cross_directory_paths_reject_edit(self):
        data = self.make_review()
        source = Path(data['source_srt'])
        original = source.read_bytes()
        source.write_bytes(original + b'x')
        with self.assertRaisesRegex(ValueError, '字幕文件已变化'):
            review.save_corrections(self.report, {1: FIXED})
        source.write_bytes(original)
        data['target_srt'] = str(self.root.parent / 'other-film.srt')
        self.report.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, '其他影片目录'):
            review.save_corrections(self.report, {1: FIXED})

    def test_only_issue_rows_can_be_edited_and_empty_source_does_not_shift_index(self):
        events = [core.SubtitleEvent('00:00:00,000', '00:00:01,000', '')] + self.events
        with patch.object(core, 'translate_batch', side_effect=[[LEAK, 'Hallo.'], [LEAK], [LEAK]]):
            self.run_translation(events)
        self.assertEqual(review.load_report(self.report)['issues'][0]['index'], 2)
        with self.assertRaises(ValueError):
            review.save_corrections(self.report, {3: 'changed good row'})
        review.save_corrections(self.report, {2: FIXED})
        self.assertEqual(core.parse_subtitle(self.output)[0].text, FIXED)
        self.assertEqual(len(core.parse_subtitle(self.output)), 2)

    def test_correction_publish_failure_rolls_back_target_cache_and_report(self):
        self.make_review()
        before = {path: path.read_bytes() for path in (self.report, self.cache, self.output)}
        original_replace = Path.replace
        def fail_report(path, destination):
            if Path(destination) == self.report and path.name.endswith('.tmp'):
                raise OSError('simulated report write failure')
            return original_replace(path, destination)
        with patch.object(Path, 'replace', fail_report), self.assertRaisesRegex(OSError, 'simulated'):
            review.save_corrections(self.report, {1: FIXED})
        self.assertEqual({path: path.read_bytes() for path in before}, before)
        self.assertFalse(list(self.root.glob('*.tmp')))
        self.assertFalse(list(self.root.glob('*.rollback')))

    def test_empty_single_response_in_parser_fallback_is_not_accepted(self):
        with patch.object(core, 'call_ollama_retry', side_effect=['', '']):
            with self.assertRaisesRegex(RuntimeError, '空响应'):
                core.translate_batch([SOURCE], 'de', 'en', 'test', 'unused', 1)


if __name__ == '__main__':
    unittest.main()
