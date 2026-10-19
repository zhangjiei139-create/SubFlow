import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import subtitle_tool_core as core
import translation_review as review


WARNING = '中文译文残留原文常用词，疑似漏译'
CASES = (
    ('Narrator: Take the number of vehicles in the field, "A,"', '旁白：取战场上车辆的数量，“A”，'),
    ('"A" times " B" times "C"', 'A"乘以"B"乘以"C'),
    ('Checkpoint A, Westway and Royal Oak.', '检查点A，韦斯特韦和皇家橡树'),
    ("It's of paramount importance, during the sad weeks ahead, the eyes of the W.B.A. Remain..",
     '在接下来的悲痛几周里，W.B.A.的目光必须保持专注。'),
)


class TranslationLabelCacheReuseTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.cache = self.root / 'translation-cache-zh-CN.jsonl'
        self.output = self.root / 'translated-zh-CN.srt'
        self.events = [core.SubtitleEvent(f'00:00:{index:02d},123', f'00:00:{index:02d},987', source)
                       for index, (source, _) in enumerate(CASES, 1)]
        self.logs = []

    def make_report(self, *, events=None, pairs=None, status='needs_review', reason=WARNING,
                    complete=True, output=None):
        events = events or self.events
        pairs = pairs or CASES
        issues = {index: dict(index=index, start=event.start, end=event.end, source=source,
                  translation=translation, reason=reason, original_reason=reason,
                  status=status, repair_attempts=2, repair_errors=['legacy repair error'])
                  for index, (event, (source, translation)) in enumerate(zip(events, pairs), 1)}
        review.save_progress(events, 'zh-CN', 'en', self.cache, output or self.output, issues,
                             complete=complete,
                             translations={index: pair[1] for index, pair in enumerate(pairs, 1)} if complete else None)
        return issues

    def needs_model(self, *, events=None, output=None, target='zh-CN'):
        return core.needs_model_translation(events or self.events, target, self.cache, 'en',
                                            retry_issues=True, output_srt=output or self.output)

    def translate(self, *, events=None, output=None):
        return core.translate_events(events or self.events, output or self.output, self.cache,
                                     'zh-CN', 'en', self.logs.append, retry_issues=True)

    def snapshot(self):
        return {path.name: path.read_bytes() for path in self.root.iterdir() if path.is_file()}

    def test_four_real_texts_recheck_without_writes_or_model_then_persist(self):
        before_issues = self.make_report()
        before = self.snapshot()
        self.assertFalse(self.needs_model())
        self.assertEqual(self.snapshot(), before)
        with patch.object(core, 'translate_batch', side_effect=AssertionError('must reuse')) as model:
            self.translate()
        model.assert_not_called()
        parsed = core.parse_subtitle(self.output)
        self.assertEqual([(row.start, row.end, row.text) for row in parsed],
                         [(event.start, event.end, pair[1]) for event, pair in zip(self.events, CASES)])
        saved = review.restore_issues(self.events, 'zh-CN', self.cache, self.output)
        for index, row in saved.items():
            self.assertEqual(row['status'], 'resolved')
            self.assertEqual(row['reason'], '')
            self.assertEqual(row['original_reason'], WARNING)
            self.assertEqual(row['translation'], before_issues[index]['translation'])
            self.assertEqual(row['repair_errors'], before_issues[index]['repair_errors'])
            self.assertEqual(row['repair_attempts'], 2)
            self.assertEqual(row['resolution'], 'identifier-context-rechecked-v1')
        accepted, stale = core.load_cache(self.cache, 'zh-CN', dict(enumerate((e.text for e in self.events), 1)))
        self.assertEqual(accepted, dict(enumerate((pair[1] for pair in CASES), 1)))
        self.assertEqual(stale, 0)
        self.assertTrue(any('旧翻译提醒复核：4条' in message for message in self.logs))
        self.assertFalse(any('仍保留质量标记' in message for message in self.logs))
        self.logs.clear()
        self.assertFalse(self.needs_model())
        with patch.object(core, 'translate_batch', side_effect=AssertionError('must reuse restart')):
            self.translate()
        self.assertFalse(any('旧翻译提醒复核：' in message for message in self.logs))

    def test_legacy_api_without_explicit_output_remains_conservative(self):
        self.make_report()
        before = self.snapshot()
        self.assertTrue(core.needs_model_translation(self.events, 'zh-CN', self.cache, 'en', retry_issues=True))
        self.assertEqual(self.snapshot(), before)

    def test_nonstandard_cache_name_works_only_with_explicit_output(self):
        self.cache = self.root / 'custom-cache.jsonl'
        self.output = self.root / 'custom-subtitles.srt'
        self.make_report()
        self.assertTrue(core.needs_model_translation(self.events, 'zh-CN', self.cache, 'en', retry_issues=True))
        self.assertFalse(self.needs_model())

    def test_no_protection_or_true_remaining_article_stays_pending(self):
        for source, translation in (
            ('A good day.', 'A美好的一天。'),
            ('Checkpoint A and a dog.', '检查点A和a狗。'),
            ('Checkpoint A and I go.', '检查点A，我go。'),
            ('Checkpoint A and I go.', '检查点A，I走了。'),
        ):
            with self.subTest(source=source):
                event = core.SubtitleEvent('00:00:01,123', '00:00:02,987', source)
                self.make_report(events=[event], pairs=[(source, translation)])
                before = self.snapshot()
                self.assertTrue(self.needs_model(events=[event]))
                self.assertEqual(self.snapshot(), before)

    def test_other_warning_reason_is_not_reclassified(self):
        for reason in ('需要人工语义检查', '译文为空', '中文译文仍为外文，疑似漏译', WARNING + '。'):
            with self.subTest(reason=reason):
                self.make_report(reason=reason)
                self.assertTrue(self.needs_model())
                self.assertEqual(review.restore_issues(self.events, 'zh-CN', self.cache, self.output)[1]['reason'], reason)

    def test_user_approved_and_resolved_records_keep_status_and_history(self):
        for status in ('user_approved', 'resolved'):
            with self.subTest(status=status):
                self.cache.unlink(missing_ok=True)
                before_issues = self.make_report(status=status)
                self.assertFalse(self.needs_model())
                with patch.object(core, 'translate_batch', side_effect=AssertionError('must preserve user decision')):
                    self.translate()
                self.assertEqual(review.restore_issues(self.events, 'zh-CN', self.cache, self.output), before_issues)
                self.assertFalse(core.load_cache(self.cache, 'zh-CN', dict(enumerate((e.text for e in self.events), 1)))[0])

    def test_changed_source_or_millisecond_fingerprint_does_not_reuse(self):
        self.make_report()
        for changed in (
            core.SubtitleEvent(self.events[0].start, self.events[0].end, 'A good day.'),
            core.SubtitleEvent('00:00:01,124', self.events[0].end, self.events[0].text),
        ):
            events = [changed, *self.events[1:]]
            with self.subTest(event=changed):
                self.assertTrue(self.needs_model(events=events))

    def test_report_bound_to_different_output_is_not_consumed(self):
        self.make_report()
        other_output = self.root / 'another-output.srt'
        before = self.snapshot()
        self.assertTrue(self.needs_model(output=other_output))
        self.assertEqual(self.snapshot(), before)
        new_text = ['新的译文一', '新的译文二', '新的译文三', '新的译文四']
        with patch.object(core, 'translate_batch', return_value=new_text) as model:
            self.translate(output=other_output)
        model.assert_called_once()
        self.assertEqual([row.text for row in core.parse_subtitle(other_output)], new_text)
        self.assertEqual(self.output.read_bytes(), before[self.output.name])
        self.assertEqual(review.report_path(self.cache).read_bytes(), before[review.report_path(self.cache).name])

    def test_wrong_target_and_report_paths_do_not_reuse(self):
        self.make_report()
        self.assertTrue(self.needs_model(target='zh-TW'))
        path = review.report_path(self.cache)
        payload = json.loads(path.read_text(encoding='utf-8'))
        for key, value in (('cache_path', str(self.root / 'other.jsonl')),
                           ('target_srt', str(self.root.parent / 'other.srt')),
                           ('report_path', str(self.root / 'other.issues.json'))):
            with self.subTest(key=key):
                changed = dict(payload, **{key: value})
                path.write_text(json.dumps(changed, ensure_ascii=False), encoding='utf-8')
                self.assertTrue(self.needs_model())

    def test_invalid_subtitle_structure_is_not_reused(self):
        self.make_report()
        path = review.report_path(self.cache)
        payload = json.loads(path.read_text(encoding='utf-8'))
        for text in ('检查点A\x00', '检查点A\n\n正常正文', ''):
            with self.subTest(text=text):
                payload['issues'][0]['translation'] = text
                path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
                self.assertTrue(self.needs_model())

    def test_partial_restart_only_requests_still_missing_line(self):
        events = [self.events[2], core.SubtitleEvent('00:00:07,123', '00:00:08,987', 'Hello.')]
        self.make_report(events=events, pairs=[CASES[2]], complete=False)
        before = self.snapshot()
        with patch.object(core, 'translate_batch', side_effect=ConnectionError('offline')) as model:
            with self.assertRaises(ConnectionError):
                self.translate(events=events)
        self.assertEqual(model.call_args.args[0], ['Hello.'])
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(self.needs_model(events=events))
        with patch.object(core, 'translate_batch', return_value=['你好。']) as model:
            self.translate(events=events)
        self.assertEqual(model.call_args.args[0], ['Hello.'])
        self.assertEqual([row.text for row in core.parse_subtitle(self.output)], [CASES[2][1], '你好。'])
        self.assertEqual(review.restore_issues(events, 'zh-CN', self.cache, self.output)[1]['status'], 'resolved')

    def test_cancel_before_migration_never_writes(self):
        self.make_report()
        before = self.snapshot()
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(core.CancelledError), patch.object(core, 'translate_batch') as model:
            core.translate_events(self.events, self.output, self.cache, 'zh-CN', 'en', self.logs.append,
                                  retry_issues=True, cancel_event=cancel)
        model.assert_not_called()
        self.assertEqual(self.snapshot(), before)


if __name__ == '__main__':
    unittest.main()
