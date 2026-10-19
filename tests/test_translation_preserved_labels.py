import tempfile
import unittest
from pathlib import Path

import subtitle_tool_core as core


class TranslationPreservedIdentifierTests(unittest.TestCase):
    REAL_CASES = (
        ('Narrator: Take the number of vehicles in the field, "A,"',
         '旁白：取战场上车辆的数量，“A”，'),
        ('"A" times " B" times "C"', 'A"乘以"B"乘以"C'),
        ('Checkpoint A, Westway and Royal Oak.', '检查点A，韦斯特韦和皇家橡树'),
        ("It's of paramount importance, during the sad weeks ahead, the eyes of the W.B.A. Remain..",
         '在接下来的悲痛几周里，W.B.A.的目光必须保持专注。'),
    )

    def assertIssue(self, source, translation):
        self.assertTrue(core.translation_quality_issue(source, translation, 'zh-CN'), (source, translation))

    def assertClean(self, source, translation):
        self.assertEqual(core.translation_quality_issue(source, translation, 'zh-CN'), '', (source, translation))

    def test_four_real_cache_false_positives(self):
        for source, translation in self.REAL_CASES:
            with self.subTest(source=source):
                self.assertClean(source, translation)

    def test_lowercase_and_bare_uppercase_articles_still_fail(self):
        for source, translation in (
            ('A good day.', 'A美好的一天。'), ('A good day.', 'a美好的一天。'),
            ('A is here.', 'A在这里。'), ('A dog is outside.', 'A狗在外面。'),
            ('Take a number.', '拿a数字。'),
        ):
            with self.subTest(source=source, translation=translation):
                self.assertIssue(source, translation)

    def test_pronouns_and_common_words_still_fail(self):
        for source, translation in (
            ('I love you.', 'I爱你。'), ('"I" love you.', '“I”爱你。'),
            ('We should go now.', '我们应该go了。'), ('The dog is here.', 'the狗在这里。'),
            ('Label the point.', 'Label这个点。'),
        ):
            with self.subTest(source=source):
                self.assertIssue(source, translation)

    def test_initialism_needs_same_exact_source_and_case(self):
        self.assertClean('The W.B.A. called.', 'W.B.A.打来电话。')
        for source, translation in (
            ('The W.B.A. called.', 'a打来电话。'),
            ('The W.B.A. called.', 'w.b.a.打来电话。'),
            ('The W.B.A. called.', 'W.B.A打来电话。'),
            ('The W.B.A. called.', 'W.B.A.和a打来电话。'),
            ('The W.B.A. called.', 'W.B.A.和A打来电话。'),
            ('The W.B.C. called a club.', 'W.B.A.打来电话。'),
            ('A good day.', 'W.B.A.的美好一天。'),
        ):
            with self.subTest(source=source, translation=translation):
                self.assertIssue(source, translation)

    def test_initialism_occurrence_budget(self):
        self.assertClean('W.B.A. and W.B.A.', 'W.B.A.和W.B.A.。')
        self.assertIssue('W.B.A. and a club.', 'W.B.A.和W.B.A.俱乐部。')

    def test_quoted_letter_requires_corresponding_quote_context(self):
        self.assertClean('Call the variable "A".', '称这个变量为“A”。')
        self.assertClean('Call it \'A,\'.', '称它为‘A’，')
        for source, translation in (
            ('Call the variable "A".', '称这个变量为a。'),
            ('Call the variable "A".', 'A在这里。'),
            ('Call it "B", a variable.', '称它为“A”。'),
            ('A good day.', '“A”美好的一天。'),
            ('Call the variable "A" and a dog.', '称这个变量为“A”和a狗。'),
            ('Call the variable "A" and A is here.', '变量“A”，A在这里。'),
        ):
            with self.subTest(source=source, translation=translation):
                self.assertIssue(source, translation)

    def test_quoted_occurrence_budget(self):
        self.assertClean('"A", "A", and "B".', '“A”、“A”和“B”。')
        self.assertIssue('"A" and a dog.', '“A”和“A”狗。')

    def test_formula_roles_preserve_letters_and_operation(self):
        for source, translation in (
            ('A times B equals C.', 'A乘以B等于C。'),
            ('"A" plus "B".', '“A”加上“B”。'),
            ('A / B.', 'A除以B。'),
            ('A multiplied by B.', 'A乘以B。'),
        ):
            with self.subTest(source=source):
                self.assertClean(source, translation)
        for source, translation in (
            ('A times B.', 'A加B。'), ('A times B.', 'A乘以C。'),
            ('A times B.', 'a乘以B。'), ('A times B.', 'A在这里。'),
            ('A dog and B.', 'A乘以B。'),
            ('A times B and a dog.', 'A乘以B和a狗。'),
            ('A times B and A is here.', 'A乘以B，A在这里。'),
            ('A times B and I go.', 'A乘以B，我go。'),
        ):
            with self.subTest(source=source, translation=translation):
                self.assertIssue(source, translation)

    def test_formula_occurrence_budget(self):
        self.assertClean('A times B and A times B.', 'A乘以B和A乘以B。')
        self.assertIssue('A times B and a dog.', 'A乘以B和A乘以B狗。')

    def test_label_context_and_matching_letter_required(self):
        for source, translation in (
            ('Checkpoint A.', '检查点A。'), ('Point A.', 'A点。'),
            ('Plan A.', 'A计划。'), ('Option A.', '选项A。'),
            ('Gate A.', 'A登机口。'), ('Room A.', 'A室。'),
        ):
            with self.subTest(source=source):
                self.assertClean(source, translation)
        for source, translation in (
            ('Checkpoint B, a checkpoint.', '检查点A。'), ('Checkpoint A.', '检查点a。'),
            ('Checkpoint A.', 'A在这里。'), ('A good day.', '检查点A的好日子。'),
            ('Point A.', '计划A。'), ('Checkpoint A and a dog.', '检查点A和a狗。'),
            ('Checkpoint A and A is here.', '检查点A，A在这里。'),
        ):
            with self.subTest(source=source, translation=translation):
                self.assertIssue(source, translation)

    def test_label_occurrence_budget_and_embedded_words(self):
        self.assertClean('Checkpoint A and checkpoint A.', '检查点A和A检查站。')
        self.assertIssue('Checkpoint A and a checkpoint.', '检查点A和检查点A。')
        self.assertIssue('Checkpoint A and a checkpoint.', '检查点AB和a检查点。')

    def test_protection_is_per_occurrence_and_not_whole_line(self):
        source = 'The W.B.A. checks "A" times "B" at Checkpoint A and a dog is here.'
        self.assertClean(source, 'W.B.A.在检查点A检查A乘以B，狗在这里。')
        for leak in ('a', 'A', 'the', 'is', 'here'):
            with self.subTest(leak=leak):
                self.assertIssue(source, 'W.B.A.在检查点A检查A乘以B，' + leak + '狗。')

    def test_unmatched_role_gets_no_protection(self):
        # This guard is not a semantic verifier for changed labels absent from source.
        for source, translation in (
            ('The W.B.C. called.', 'W.B.A.打来电话。'),
            ('Checkpoint B.', '检查点A。'), ('Call it "B".', '称它为“A”。'),
            ('Point A.', '计划A。'), ('A times B.', 'A加B。'),
        ):
            with self.subTest(source=source):
                self.assertEqual(core._translation_identifier_spans(source, translation), set())

    def test_name_protection_is_unchanged(self):
        self.assertClean('For a Pontiac Trans Am.', '是为了辆庞蒂克Trans Am。')
        self.assertClean('The dissolution of The Axe and Cross.', 'The Axe and Cross解散了。')
        self.assertIssue('The dissolution of the club.', 'The Axe and Cross解散了。')
        self.assertIssue('I am here.', 'I am在这里。')

    def test_non_cjk_script_leak_guard_is_unchanged(self):
        for target in ('en', 'de', 'fr', 'es', 'ru', 'hi'):
            with self.subTest(target=target):
                self.assertIn('中日韩', core.translation_quality_issue(
                    'The W.B.A. called at Checkpoint A.', 'W.B.A. 橡胶 Checkpoint A.', target))
        self.assertEqual(core.translation_quality_issue('Ask 王伟 about W.B.A.', 'Frag 王伟 nach W.B.A.', 'de'), '')

    def test_accepted_cache_reuses_four_real_cases_without_mutation(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'cache.jsonl'
            rows = [(index, source, translation)
                    for index, (source, translation) in enumerate(self.REAL_CASES, 1)]
            core.append_cache_records(path, 'zh-CN', rows)
            before = path.read_bytes()
            self.assertEqual(core.load_cache(path, 'zh-CN', {row[0]: row[1] for row in rows}),
                             ({row[0]: row[2] for row in rows}, 0))
            events = [core.SubtitleEvent('00:00:00,000', '00:00:01,000', row[1]) for row in rows]
            self.assertFalse(core.needs_model_translation(events, 'zh-CN', path, 'en', retry_issues=True))
            self.assertEqual(path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
