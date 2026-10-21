"""Display syntax must not leak into quality checks or invalidate source identity."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import subtitle_text_formats as formats
import subtitle_tool_core as core
import translation_review as review


WARNING = '中文译文残留原文常用词，疑似漏译'


class SubtitleFormatQualityTests(unittest.TestCase):
    def test_known_style_is_ignored_but_real_words_still_fail(self):
        for source, target in (
            ('{y:i}I enjoy talking about Hal.', '{y:i}我喜欢谈论哈尔。'),
            ('{Y:bi}I said hello.', '{Y:bi}我打了招呼。'),
            ('<i>I said hello.</i>', '<i>我打了招呼。</i>'),
            (r'{\i1}I said hello.{\i0}', r'{\i1}我打了招呼。{\i0}'),
        ):
            with self.subTest(source=source):
                self.assertEqual(core.translation_quality_issue(source, target, 'zh-CN'), '')
        for source, target in (
            ('{y:i}I enjoy talking about Hal.', '{y:i}I喜欢谈论哈尔。'),
            ('{y:i}I said hello.', '{y:i}我说hello。'),
            ('<i>Somehow,</i>', '<i>总是somehow。</i>'),
            ('{note:I}I said hello.', '{note:I}我打了招呼。'),
            ('{banana}', '{banana}'),
        ):
            with self.subTest(source=source):
                self.assertTrue(core.translation_quality_issue(source, target, 'zh-CN'))
        self.assertEqual(core.translation_quality_issue('{y:i}Hello', '{y:i}', 'zh-CN'), '译文为空')

    def test_platform_role_requires_matching_source_and_occurrence(self):
        for source, target in (
            ('The train is now boarding at platform A.', '列车现在在A站台开始登车。'),
            ('Go to Platform A.', '前往月台A。'),
            ('Platform I is open.', 'I站台已开放。'),
            ('{y:i}Platform A.', '<i>A月台。</i>'),
        ):
            with self.subTest(source=source):
                self.assertEqual(core.translation_quality_issue(source, target, 'zh-CN'), '')
        for source, target in (
            ('A good day.', 'A美好的一天。'),
            ('Platform A and a dog.', 'A站台和a狗。'),
            ('Platform A and I go.', 'A站台，I走了。'),
            ('Platform A.', 'A站台和A站台。'),
            ('Plan A.', 'A站台。'),
            ('Platform A.', 'A计划。'),
        ):
            with self.subTest(source=source):
                self.assertTrue(core.translation_quality_issue(source, target, 'zh-CN'))

    def test_model_and_context_use_plain_view_without_mutating_source(self):
        sources = ['{y:i}I said hello.', '<i>Go to platform A.</i>', 'Keep {ordinary} braces.']
        original = list(sources)
        prompt = core.make_prompt(sources, 'zh-CN', 'en', require_target_script=True,
                                  context_lines=('{Y:b}Before.', '<i>After.</i>'),
                                  repair_feedback='Faulty translation: {y:i}I说了你好。')
        self.assertEqual(sources, original)
        self.assertIn('1. I said hello.', prompt)
        self.assertIn('2. Go to platform A.', prompt)
        self.assertIn('3. Keep {ordinary} braces.', prompt)
        self.assertIn('前句：Before.', prompt)
        self.assertIn('后句：After.', prompt)
        self.assertNotIn('{y:i}', prompt)
        self.assertNotIn('<i>', prompt)
        self.assertNotIn('{Y:b}', prompt)

    def test_raw_cache_source_is_still_strict_and_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / 'cache.jsonl'
            source = '{y:i}I enjoy talking about Hal.'
            target = '{y:i}我喜欢谈论哈尔。'
            core.append_cache(cache, 'zh-CN', 1, source, target)
            accepted, stale = core.load_cache(cache, 'zh-CN', {1: source})
            self.assertEqual((accepted, stale), ({1: target}, 0))
            self.assertEqual(core.load_cache(cache, 'zh-CN', {1: formats.translation_text(source)}), ({}, 1))
            saved = json.loads(cache.read_text(encoding='utf8'))
            self.assertEqual(saved['source'], source)
            self.assertEqual(saved['translation'], target)

    def test_old_format_or_platform_warning_resolves_without_model(self):
        for source, target, resolution in (
            ('{y:i}I enjoy talking about Hal.', '{y:i}我喜欢谈论哈尔。', 'display-format-rechecked-v1'),
            ('The train is boarding at platform A.', '列车在A站台登车。', 'identifier-context-rechecked-v1'),
        ):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                cache, output = root / 'cache.jsonl', root / 'translated.srt'
                event = core.SubtitleEvent('00:00:01,123', '00:00:03,456', source)
                issues = {1: dict(index=1, start=event.start, end=event.end, source=source,
                                  translation=target, reason=WARNING, original_reason=WARNING,
                                  status='needs_review', repair_attempts=2)}
                review.save_progress([event], 'zh-CN', 'en', cache, output, issues,
                                     complete=True, translations={1: target})
                before = {p.name: p.read_bytes() for p in root.iterdir()}
                self.assertFalse(core.needs_model_translation([event], 'zh-CN', cache, 'en',
                                                              retry_issues=True, output_srt=output))
                self.assertEqual({p.name: p.read_bytes() for p in root.iterdir()}, before)
                with patch.object(core, 'translate_batch', side_effect=AssertionError('no model')) as model:
                    core.translate_events([event], output, cache, 'zh-CN', 'en', lambda _: None,
                                          retry_issues=True)
                model.assert_not_called()
                result = review.restore_issues([event], 'zh-CN', cache, output)[1]
                self.assertEqual(result['status'], 'resolved')
                self.assertEqual(result['translation'], target)
                self.assertEqual(result['resolution'], resolution)
                self.assertEqual(result['original_reason'], WARNING)
                self.assertEqual(result['repair_attempts'], 2)
                self.assertEqual(output.read_bytes(), before[output.name])
                self.assertFalse(core.needs_model_translation([event], 'zh-CN', cache, 'en',
                                                              retry_issues=True, output_srt=output))

    def test_style_does_not_waive_script_or_unknown_content(self):
        self.assertTrue(core.translation_quality_issue('{y:i}The skin.', '<i>Die 橡胶haut.</i>', 'de'))
        self.assertFalse(core.translation_quality_issue('<font face="宋体">The skin.</font>',
                                                       '<font face="宋体">Die Haut.</font>', 'de'))
        self.assertEqual(formats.translation_text('Keep {ordinary} <unknown>hello</unknown>.'),
                         'Keep {ordinary} <unknown>hello</unknown>.')


if __name__ == '__main__':
    unittest.main()
