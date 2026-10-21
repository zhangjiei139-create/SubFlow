"""Source-aware name preservation must retain the bounded recovery contract."""
import json
import re
import unittest
from unittest.mock import Mock, patch

import subtitle_tool_core as core
import translation_recovery as recovery


def response(*segments):
    return json.dumps(dict(whole=''.join(segments),
                          **{f's{index}': text for index, text in enumerate(segments, 1)}),
                      ensure_ascii=False)


class RecoveryPreservedNameTests(unittest.TestCase):
    def test_source_aware_recovery_keeps_names_at_start_and_in_sentence(self):
        for sources, segments in (
            (['NASA called.'], ['NASA打来了电话。']),
            (['Müller arrived.'], ['Müller到了。']),
            (['Élodie arrived.'], ['Élodie到了。']),
            (['Contact the FBI,', 'and return.'], ['联系FBI，', '然后回来。']),
        ):
            with self.subTest(sources=sources):
                diagnostics = []
                request = Mock(return_value=response(*segments))
                actual = recovery.recover(sources, '简体中文', request, diagnostics, lambda _: None)
                self.assertEqual(actual, segments)
                request.assert_called_once()
                self.assertNotIn('error', diagnostics[0])

    def test_schema_stays_bounded_and_explains_names_vs_dialogue(self):
        prompt, schema = recovery.request_spec(['NASA called.'], '简体中文')
        self.assertIn('人名、机构名、品牌、缩写和代号可原样保留', prompt)
        self.assertIn('普通对白仍须翻译', prompt)
        self.assertFalse(schema['additionalProperties'])
        self.assertEqual(set(schema['required']), {'whole', 's1'})
        rule = schema['properties']['whole']
        self.assertEqual(rule['maxLength'], 161)
        self.assertTrue(re.fullmatch(rule['pattern'], 'NASA打来了电话。'))
        self.assertTrue(re.fullmatch(rule['pattern'], 'Élodie到了。'))
        for bad in ('...', '!正常正文', '正常\n正文', '中' * 162):
            with self.subTest(value=bad):
                self.assertFalse(re.fullmatch(rule['pattern'], bad))

    def test_real_untranslated_dialogue_rejected_without_second_request(self):
        for source, target in (
            ('I LOVE YOU.', 'I LOVE YOU。'),
            ('HELP! STOP!', 'HELP! STOP!'),
            ('NASA said I love you.', 'NASA说I爱you。'),
            ('We should go now.', '我们应该go了。'),
        ):
            with self.subTest(source=source):
                request = Mock(return_value=response(target))
                diagnostics = []
                with self.assertRaisesRegex(ValueError, '正文质量检查'):
                    recovery.recover([source], '简体中文', request, diagnostics, lambda _: None)
                request.assert_called_once()
                self.assertIn('error', diagnostics[0])

    def test_unknown_latin_words_do_not_gain_a_new_fallback_pass(self):
        # The main checker is intentionally narrow. Recovery still requires
        # positive name evidence for every retained Latin occurrence.
        for source, target in (('Welcome!', 'Welcome!'), ('RUN!', 'RUN!'),
                               ('Chance!', 'Chance!'), ('Welcome!', 'Welcome回来。')):
            with self.subTest(source=source, target=target):
                request = Mock(return_value=response(target))
                diagnostics = []
                with self.assertRaises(ValueError):
                    recovery.recover([source], '简体中文', request, diagnostics, lambda _: None)
                request.assert_called_once()
                self.assertIn('error', diagnostics[0])

    def test_real_voice_cache_leak_is_not_reclassified_as_an_acronym(self):
        # Frozen real source/translation from the prior Godzilla cache review.
        # The narrow main checker need not recognize every ordinary word, but
        # this branch must not gain a new Latin exception from capitals alone.
        source = 'Who is the VOICE behind the TRUTH ABOUT TITANS podcast And why you should listen to...'
        target = '谁是《泰坦真相》播客背后的 VOICE，以及为什么你应该收听……'
        request = Mock(return_value=response(target))
        diagnostics = []
        with self.assertRaises(ValueError):
            recovery.recover([source], '简体中文', request, diagnostics, lambda _: None)
        request.assert_called_once()
        self.assertIn('error', diagnostics[0])

    def test_ordinary_titlecase_needs_entity_purpose_in_strict_recovery(self):
        # Capitalized ordinary words and an unlabelled multiword phrase must
        # not gain the Latin permission that the old recovery branch lacked.
        for source, target in (
            ('I Feel Sick.', '我Feel Sick。'),
            ('We Fight Together.', '我们Fight Together。'),
            ('Feel Sick.', 'Feel Sick，我不舒服。'),
            ('Fight Together.', 'Fight Together，一起。'),
        ):
            with self.subTest(source=source, target=target):
                request = Mock(return_value=response(target))
                diagnostics = []
                with self.assertRaisesRegex(ValueError, '未经原文名称或编号证据确认'):
                    recovery.recover([source], '简体中文', request, diagnostics, lambda _: None)
                request.assert_called_once()
                self.assertIn('error', diagnostics[0])
        # The exact same words can legitimately be a title when the source
        # explicitly identifies that purpose; no word blacklist decides this.
        source = 'Read the book titled Feel Sick.'
        target = '读这本名为Feel Sick的书。'
        self.assertEqual(recovery.parse_response(response(target), 1, sources=[source]), [target])

    def test_digit_leading_name_requires_exact_source_evidence(self):
        self.assertEqual(recovery.parse_response(response('3M公司来电。'), 1,
                                                 sources=['The company 3M called.']), ['3M公司来电。'])
        with self.assertRaises(ValueError):
            recovery.parse_response(response('5M公司来电。'), 1, sources=['The company 3M called.'])

    def test_names_and_identifiers_share_source_occurrence_budget(self):
        for source, target in (
            ('The Point A Corporation called.', 'Point A公司来电，A点。'),
            ('The Plan A Foundation called.', 'The Plan A Foundation来电，计划A。'),
            ('The Room A Corporation called.', 'Room A公司来电，A室。'),
        ):
            with self.subTest(source=source):
                self.assertTrue(core.translation_quality_issue(source, target, 'zh-CN'))
                with self.assertRaises(ValueError):
                    recovery.parse_response(response(target), 1, sources=[source])
        source = 'The Point A Corporation called from Point A.'
        target = 'Point A公司从A点来电。'
        self.assertEqual(core.translation_quality_issue(source, target, 'zh-CN'), '')
        self.assertEqual(recovery.parse_response(response(target), 1, sources=[source]), [target])

    def test_each_segment_and_whole_receive_the_exact_sources(self):
        sources = ['Contact NASA,', 'and return.']
        segments = ['联系NASA，', '然后回来。']
        with patch.object(core, 'translation_quality_issue', return_value='') as quality:
            actual = recovery.parse_response(response(*segments), 2, sources=sources)
        self.assertEqual(actual, segments)
        self.assertEqual([call.args for call in quality.call_args_list],
                         [(' '.join(sources), ''.join(segments), 'zh-CN'),
                          (sources[0], segments[0], 'zh-CN'),
                          (sources[1], segments[1], 'zh-CN')])

    def test_segment_name_evidence_cannot_be_borrowed_from_the_whole(self):
        # The joined source explicitly names No, but the first source fragment
        # does not yet contain that name. Whole-window evidence must not grant
        # an arbitrary segment permission to introduce it.
        sources = ['My name is', 'No.']
        request = Mock(return_value=response('我叫No', '不。'))
        diagnostics = []
        with self.assertRaisesRegex(ValueError, '未经原文名称或编号证据确认'):
            recovery.recover(sources, '简体中文', request, diagnostics, lambda _: None)
        self.assertEqual(request.call_count, 1)
        self.assertIn('error', diagnostics[0])

    def test_legacy_parse_without_sources_retains_its_restriction(self):
        with self.assertRaisesRegex(ValueError, '有界中文'):
            recovery.parse_response(response('NASA打来了电话。'), 1)
        self.assertEqual(recovery.parse_response(response('打来了电话。'), 1), ['打来了电话。'])

    def test_exact_json_keys_and_split_consistency_stay_enforced(self):
        good = dict(whole='联系NASA，然后回来。', s1='联系NASA，', s2='然后回来。')
        for bad in (dict(good, extra='无关'), dict(good, s2='回来。'),
                    dict(good, s1='然后回来。', s2='联系NASA，')):
            with self.subTest(data=bad), self.assertRaises(ValueError):
                recovery.parse_response(json.dumps(bad), 2, sources=['Contact NASA,', 'and return.'])

    def test_output_budget_controls_and_nonempty_text_stay_enforced(self):
        self.assertEqual(recovery.parse_response(response('中' * 161), 1, sources=['Hello.']), ['中' * 161])
        for bad in ('中' * 162, '中文\x00', '中文\t', '中文\r', '中文\n', '中文\ud800', '', '123', '...'):
            with self.subTest(text=repr(bad)), self.assertRaises(ValueError):
                recovery.parse_response(response(bad), 1, sources=['Hello.'])

    def test_original_input_and_source_binding_budgets_stay_enforced(self):
        for lines in ([], ['Hello.'] * 4, [''], ['x' * 601]):
            with self.subTest(lines=lines), self.assertRaises(ValueError):
                recovery.request_spec(lines, '简体中文')
        for sources in ([], ['Hello.'] * 2, [''], ['x' * 601]):
            with self.subTest(sources=sources), self.assertRaises(ValueError):
                recovery.parse_response(response('你好。'), 1, sources=sources)


if __name__ == '__main__':
    unittest.main()
