"""Controls for the shared format view, independent of model/network availability."""
import unittest

from subtitle_text_formats import normalize_srt_markup, translation_text
from subtitle_fonts import ass_text
from subtitle_style_engine import plain


class LegacySubtitleTextTests(unittest.TestCase):
    def test_same_style_repeated_inside_joined_line(self):
        value = '{y:i}No, Dr. Chandra. {y:i}Do you have anything for me?'
        self.assertEqual(translation_text(value), 'No, Dr. Chandra. Do you have anything for me?')
        self.assertEqual(ass_text(value).count(r'{\i1}'), 1)

    def test_all_declared_styles_are_real_ass(self):
        value = '{y:bius}Styled text'
        converted = ass_text(value)
        self.assertEqual(plain(converted), 'Styled text')
        for tag in 'bius':
            self.assertIn('{\\' + tag + '1}', converted)
            self.assertIn('{\\' + tag + '0}', converted)

    def test_local_scope_stops_at_physical_break(self):
        self.assertEqual(normalize_srt_markup('{y:i}first\nsecond'), '<i>first</i>\nsecond')

    def test_local_scope_stops_at_html_break(self):
        self.assertEqual(normalize_srt_markup('{y:i}first<br/>second'), '<i>first</i><br/>second')

    def test_persistent_scope_and_local_style(self):
        self.assertEqual(translation_text('{Y:i}first\n{y:b}second\nthird'), 'first\nsecond\nthird')
        value = normalize_srt_markup('{Y:i}first\nsecond')
        self.assertEqual(value, '<i>first\nsecond</i>')

    def test_inline_marker_in_translated_dialogue(self):
        value = '-那里不可能有人！-{y:i}也许霍曼回到了...'
        self.assertEqual(plain(ass_text(value)), '-那里不可能有人！-也许霍曼回到了...')

    def test_unknown_and_literal_braces_are_kept(self):
        for value in ('{x:i}I am here.', '{y:secret}I am here.', '{y:}empty syntax',
                      r'\{y:i\} is a literal marker', '{I am here}', 'A | B', 'set {a,b}'):
            with self.subTest(value=value):
                self.assertEqual(normalize_srt_markup(value), value)
                self.assertEqual(translation_text(value), value)
                self.assertEqual(plain(ass_text(value)), value)

    def test_unknown_markup_is_not_dropped_from_quality_view(self):
        for value in ('<unknown/>', '<unknown>I am here</unknown>', '<!-- I am here -->',
                      '<MYTAG>Speak</MYTAG>', '<?I?> text', '<![CDATA[I > you]]>'):
            self.assertEqual(translation_text(value), value)

    def test_known_markup_does_not_hide_dialogue(self):
        self.assertEqual(translation_text('<i>I am here.</i>'), 'I am here.')
        self.assertEqual(translation_text(r'{\i1\an8\pos(12,34)}I am here.'), 'I am here.')

    def test_unknown_ass_override_is_kept(self):
        for value in (r'{\unknown1}I am here', r'{\iHello}I am here', '{plain text}'):
            self.assertEqual(translation_text(value), value)

    def test_srt_braces_cannot_inject_ass_commands(self):
        value = r'{\pos(0,0)}{y:i}Hello'
        self.assertEqual(plain(ass_text(value)), r'{\pos(0,0)}Hello')
        self.assertIn(r'\{\pos(0,0)\}', ass_text(value))

    def test_existing_html_and_entities_preserved(self):
        self.assertEqual(plain(ass_text('<b>A &amp; B</b>\n<i>C</i>')), 'A & B\nC')
        self.assertEqual(translation_text('<b>A &amp; B</b>\n<i>C</i>'), 'A & B\nC')

    def test_html_and_legacy_style_scopes_do_not_turn_each_other_off(self):
        converted = ass_text('<i>first {y:i}second</i> third')
        self.assertEqual(plain(converted), 'first second third')
        self.assertEqual(converted.count(r'{\i1}'), 1)
        self.assertEqual(converted.count(r'{\i0}'), 1)
        self.assertTrue(converted.endswith(r'{\i0}'))


if __name__ == '__main__':
    unittest.main()
