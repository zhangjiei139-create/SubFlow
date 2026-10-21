"""Source evidence protects a name occurrence; casing alone cannot protect dialogue."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import subtitle_tool_core as core
import translation_review as review


WARNING = '中文译文残留原文常用词，疑似漏译'


class TranslationNamePolicyTests(unittest.TestCase):
    def test_declared_names_are_not_a_token_allowlist(self):
        # Every alphabetic collision can be a declared name, rather than a list
        # of hand-picked organisations or abbreviations in the implementation.
        names = sorted(word.upper() for word in core._DIALOGUE_WORDS if word.isalpha())
        self.assertGreater(len(names), 40)
        for name in names:
            with self.subTest(name=name):
                source, translated = f'The company is named {name}.', f'这家公司名叫{name}。'
                self.assertEqual(core.translation_quality_issue(source, translated, 'zh-CN'), '')

    def test_name_protection_is_per_occurrence_including_all_caps_sources(self):
        for source, translated in (
            ('The Axe and Cross is here, and the dog is outside.', 'The Axe and Cross在这里，the狗在外面。'),
            ('The Axe and Cross is here, and I go.', 'The Axe and Cross在这里，and我走了。'),
            ('The company is named IT, and it is here.', '这家公司叫IT，it在这里。'),
            ('MR. NO SAID NO.', 'NO先生说NO。'),
            ('THE WHO ORGANIZATION ASKED WHO CALLED?', 'WHO组织问WHO打来电话？'),
            ('The project is called STOP.', '项目名叫STOP和STOP。'),
        ):
            with self.subTest(source=source):
                self.assertTrue(core.translation_quality_issue(source, translated, 'zh-CN'))

    def test_multiple_real_name_roles_and_title_boundaries(self):
        for source, translated in (
            ('Dr. No arrived.', 'No博士到了。'),
            ('Mr.You called.', 'You先生来电。'),
            ('The US embassy called.', 'US大使馆来电。'),
            ('The department is named IT.', '这个部门叫IT。'),
            ('The film is titled I.', '电影名叫I。'),
            ('Her callsign is GO.', '她的呼号是GO。'),
            ('The book is titled Please Go Home.', '这本书叫Please Go Home。'),
            ('For a Pontiac Trans Am.', '是为了辆庞蒂克Trans Am。'),
            ('I. M. Pei designed this.', 'I. M. Pei设计了这个。'),
        ):
            with self.subTest(source=source):
                self.assertEqual(core.translation_quality_issue(source, translated, 'zh-CN'), '')

    def test_dialogue_cannot_become_a_name_by_title_case_or_caps(self):
        for source, translated in (
            ('I LOVE YOU.', '我说I LOVE YOU。'),
            ('THE DOG IS HERE.', '他说THE DOG IS HERE。'),
            ('Take The Bag.', '请Take The Bag。'),
            ('Call The Police.', '请Call The Police。'),
            ('John Told Me To Go.', 'John Told Me To Go，说完就走。'),
            ('Thank You.', 'Thank You。'),
            ('HELP!', 'HELP！'),
        ):
            with self.subTest(source=source):
                self.assertTrue(core.translation_quality_issue(source, translated, 'zh-CN'))

    def test_name_case_and_source_spelling_remain_bound(self):
        self.assertTrue(core.translation_quality_issue('Mr. No called.', 'no先生来电。', 'zh-CN'))
        self.assertTrue(core.translation_quality_issue('The company is named THEM.', '这家公司叫THE。', 'zh-CN'))
        self.assertEqual(core._translation_name_spans('Mr. No called.', 'No先生来电。'), {0, 1})

    def test_repair_prompts_allow_source_names_without_forcing_pure_chinese(self):
        prompt = core.make_prompt(['Mr. No called the IT department.'], 'zh-CN', 'en',
                                  require_target_script=True)
        self.assertIn('人名、机构名', prompt)
        self.assertIn('普通对白必须翻译', prompt)
        self.assertIn('保留名称的原始拼写', prompt)
        self.assertNotIn('禁止夹带原文英文单词', prompt)
        self.assertIn('Mr. No called the IT department.', prompt)

    def test_old_name_warning_resolves_with_no_model_and_unchanged_text(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            cache, output = root / 'cache.jsonl', root / 'out.srt'
            event = core.SubtitleEvent('00:00:01,123', '00:00:03,987', 'Mr. No called.')
            translated = 'No先生来电。'
            item = dict(index=1, source=event.text, start=event.start, end=event.end,
                        translation=translated, reason=WARNING, original_reason=WARNING,
                        repair_attempts=2, status='needs_review')
            review.save_progress([event], 'zh-CN', 'en', cache, output, {1: item},
                                 complete=True, translations={1: translated})
            before = output.read_bytes()
            self.assertFalse(core.needs_model_translation([event], 'zh-CN', cache, 'en',
                                                          retry_issues=True, output_srt=output))
            with patch.object(core, 'translate_batch', side_effect=AssertionError('must reuse')):
                core.translate_events([event], output, cache, 'zh-CN', 'en', lambda _: None,
                                      retry_issues=True)
            saved = review.restore_issues([event], 'zh-CN', cache, output)[1]
            self.assertEqual(saved['status'], 'resolved')
            self.assertEqual(saved['resolution'], 'source-name-context-rechecked-v1')
            self.assertEqual(saved['original_reason'], WARNING)
            self.assertEqual(saved['repair_attempts'], 2)
            self.assertEqual(output.read_bytes(), before)
            raw = json.loads(cache.read_text(encoding='utf8'))
            self.assertEqual(raw['source'], event.text)
            self.assertFalse(core.needs_model_translation([event], 'zh-CN', cache, 'en',
                                                          retry_issues=True, output_srt=output))

    def test_genuine_leak_only_retries_the_faulty_line(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            events = [core.SubtitleEvent('00:00:01,123', '00:00:02,987', 'Mr. No called.'),
                      core.SubtitleEvent('00:00:03,123', '00:00:04,987', 'I should go.')]
            core.append_cache(root / 'cache.jsonl', 'zh-CN', 1, events[0].text, 'No先生来电。')
            with patch.object(core, 'translate_batch', side_effect=[['我应该go了。'], ['我该走了。']]) as model:
                core.translate_events(events, root / 'out.srt', root / 'cache.jsonl',
                                      'zh-CN', 'en', lambda _: None)
            self.assertEqual([call.args[0] for call in model.call_args_list],
                             [['I should go.'], ['I should go.']])
            self.assertEqual([event.text for event in core.parse_subtitle(root / 'out.srt')],
                             ['No先生来电。', '我该走了。'])


if __name__ == '__main__':
    unittest.main()
