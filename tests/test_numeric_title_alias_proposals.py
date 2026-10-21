import unittest

from subtitle_identity_guard import numeric_title_alias_proposals, parse_media_identity


class NumericTitleAliasProposalTests(unittest.TestCase):
    def test_general_year_and_cardinal_readings(self):
        self.assertEqual(numeric_title_alias_proposals('1984'),
                         ('nineteen eighty four', 'one thousand nine hundred eighty four'))
        self.assertEqual(numeric_title_alias_proposals('1917'),
                         ('nineteen seventeen', 'one thousand nine hundred seventeen'))
        self.assertEqual(numeric_title_alias_proposals('1408'),
                         ('fourteen oh eight', 'one thousand four hundred eight'))

    def test_cardinal_and_year_round_numbers(self):
        self.assertEqual(numeric_title_alias_proposals('300'), ('three hundred',))
        self.assertEqual(numeric_title_alias_proposals('21'), ('twenty one',))
        self.assertEqual(numeric_title_alias_proposals('42'), ('forty two',))
        self.assertEqual(numeric_title_alias_proposals('1900'),
                         ('nineteen hundred', 'one thousand nine hundred'))
        self.assertEqual(numeric_title_alias_proposals('2001'), ('two thousand one',))
        self.assertEqual(numeric_title_alias_proposals('2012'), ('twenty twelve', 'two thousand twelve'))

    def test_never_turns_other_titles_or_leading_zeros_into_numbers(self):
        for value in ('007', '01', '1984 (1984)', '1984.mkv', '21 Jump Street', '2001 A Space Odyssey',
                      '-21', '+21', '1.5', '10/10', '', '10000', '1 984', 'One'):
            with self.subTest(value=value):
                self.assertEqual(numeric_title_alias_proposals(value), ())

    def test_nfkc_digits_and_size_bound(self):
        self.assertEqual(numeric_title_alias_proposals('１９８４'), numeric_title_alias_proposals('1984'))
        for number in range(10000):
            proposals = numeric_title_alias_proposals(str(number))
            self.assertGreaterEqual(len(proposals), 1)
            self.assertLessEqual(len(proposals), 2)
            self.assertEqual(len(set(proposals)), len(proposals))
            self.assertTrue(all(len(text) < 100 for text in proposals))

    def test_proposals_do_not_mutate_the_original_identity(self):
        before = parse_media_identity('1984', kind='title')
        numeric_title_alias_proposals(before.title)
        after = parse_media_identity('1984', kind='title')
        self.assertEqual(before, after)
        self.assertEqual(after.title, '1984')
        self.assertEqual(after.year, '')


if __name__ == '__main__':
    unittest.main()
