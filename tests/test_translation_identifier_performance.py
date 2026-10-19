import unittest
from unittest.mock import patch

import subtitle_tool_core as core


class TranslationIdentifierPerformanceTests(unittest.TestCase):
    def test_redundant_quote_and_space_runs_are_compact_before_regex(self):
        source = 'Checkpoint ' + ' ' * 4000 + '"' * 4000 + 'A'
        translation = '检查点' + '"' * 4000 + ' ' * 4000 + 'A'
        sizes = []
        original = core.re.finditer

        def observed(pattern, text, *args, **kwargs):
            sizes.append(len(text))
            return original(pattern, text, *args, **kwargs)

        with patch.object(core.re, 'finditer', side_effect=observed):
            protected = core._translation_identifier_spans(source, translation)
        self.assertEqual(protected, {len(translation) - 1})
        self.assertTrue(sizes)
        self.assertLessEqual(max(sizes), 16)

    def test_compaction_preserves_adjacent_quotes_budgets_and_original_spans(self):
        for count in (2, 7, 4000):
            source = '"A' + '"' * count + 'A"'
            translation = '“A' + '”' * count + 'A”'
            with self.subTest(count=count):
                self.assertEqual(core._translation_identifier_spans(source, translation),
                                 {1, len(translation) - 2})
                self.assertEqual(core.translation_quality_issue(source, '变量' + translation, 'zh-CN'), '')
                self.assertTrue(core.translation_quality_issue(source, '变量' + translation + '和A', 'zh-CN'))


if __name__ == '__main__':
    unittest.main()
