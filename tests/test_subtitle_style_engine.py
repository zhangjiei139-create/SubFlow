import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

import re
import unittest
from pathlib import Path

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QRawFont
import subtitle_style_engine as engine

APP = QApplication.instance() or QApplication([])
ROOT = Path(__file__).resolve().parents[1]

FIELDS = ('Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,'
          'BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,'
          'BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding')


def private(width=384, height=288, scale_x=100, scale_y=100):
    return (f'[Script Info]\nScriptType: v4.00+\nPlayResX: {width}\nPlayResY: {height}\n'
            '[V4+ Styles]\nFormat: ' + FIELDS + '\n'
            f'Style: Default,Old Font,19,&H00AAAAAA,&H200000FF,&H00111111,&H40000000,'
            f'0,0,0,0,{scale_x},{scale_y},3,0,1,1,2,2,12,12,10,1\n'
            'Style: Sign,Old Sign,23,&H00555555,&H000000FF,&H00111111,&H40000000,'
            '0,0,0,0,100,100,0,0,1,1,2,8,0,0,0,1\n').encode('utf8')


def event(body, margins=('0', '0', '0'), effect='', name='Default'):
    return (1001, 2033, '0,1,' + name + ',Actor,' + ','.join(margins) + ',' + effect + ',' + body)


STYLE = dict(family='Chosen Font', size=38, color='#13ACEF', bold=False, italic=False)


def transformed(body, **kwargs):
    events, header, report = engine.update(private(), [event(body)], dict(STYLE, **kwargs), 1920, 1080)
    return events[0][2].split(',', 8)[8], header.decode('utf8'), report


class UniformTextStyleTests(unittest.TestCase):
    def test_positioned_lines_resets_and_inline_overrides_obey_selected_values(self):
        source = event(r'{\an8\pos(120,42)\fnOld\fs19\1c&H123456&}中文'
                       r'{\rSign\fnAgain\fs+5\fscx160\fscy80\c&H0000FF&}text',
                       margins=('12', '6', '9'), name='Sign')
        values, changed, report = engine.update(private(), [source], STYLE, 1920, 1080)
        self.assertEqual(values[0][:2], source[:2])
        parts = values[0][2].split(',', 8)
        self.assertEqual(parts[:4], source[2].split(',', 8)[:4])
        self.assertEqual(parts[4:7], ['40', '20', '22'])
        self.assertIn(r'\pos(400,105)', parts[8])
        self.assertIn(r'\an8', parts[8])
        self.assertIn(r'\rSign', parts[8])
        self.assertEqual(engine.plain(parts[8]), '中文text')
        self.assertNotIn(r'\fnOld', parts[8])
        self.assertNotIn(r'\fnAgain', parts[8])
        self.assertEqual(set(re.findall(r'\\fs(?![cp])([-+\d.]+)', parts[8])), {'38'})
        self.assertEqual(set(re.findall(r'\\(?:[12]?c)&H([a-fA-F0-9]+)&', parts[8])), {'EFAC13'})
        self.assertEqual(report['new'], [1280, 720])
        self.assertEqual(report['style_count'], 2)
        self.assertIn(b'Style: Sign,Chosen Font,38', changed)

    def test_original_canvas_never_changes_manual_size_units(self):
        for canvas in ((384, 288), (1280, 720), (1920, 1080)):
            for selected in (38, 48):
                values, changed, report = engine.update(private(*canvas), [event('hello')],
                                                        dict(STYLE, size=selected), 1920, 800)
                self.assertEqual(report['selected_size'], selected)
                self.assertEqual(report['new'], [1728, 720])
                self.assertIn(f',Chosen Font,{selected},'.encode(), changed)
                self.assertIn(r'\fs' + str(selected), values[0][2])

    def test_keep_font_and_colour_are_independent_of_size(self):
        body, header, _ = transformed(r'{\fnInline Font\c&H778899&\fs19\fscx160}Words',
                                     change_font=False, change_color=False)
        self.assertIn('Style: Default,Old Font,38,&H00AAAAAA', header)
        self.assertIn(r'\fnInline Font', body)
        self.assertIn(r'\c&H778899&', body)
        self.assertNotIn('Chosen Font', body)
        self.assertNotIn(r'\fs19', body)
        self.assertNotIn(r'\fscx160', body)
        self.assertIn(r'\fs38', body)

    def test_nested_animations_preserve_geometry_timing_and_nonfont_animation(self):
        body, _, _ = transformed(r'{\move(0,10,120,200,100,900)'
                                 r'\t(100,900,2,\fs80\fnOld\c&HFF0000&'
                                 r'\clip(0,10,60,90)\t(200,500,\frz45\fs70))'
                                 r'\fad(250,500)\alpha&H20&}Hello')
        self.assertIn(r'\move(0,25,400,500,100,900)', body)
        self.assertIn(r'\t(100,900,2,\clip(0,25,200,225)\t(200,500,\frz45))', body)
        self.assertIn(r'\fad(250,500)', body)
        self.assertIn(r'\alpha&H20&', body)
        self.assertNotIn(r'\fs80', body)
        self.assertNotIn(r'\fs70', body)
        self.assertNotIn(r'\fnOld', body)
        self.assertEqual(engine.plain(body), 'Hello')

    def test_font_only_animation_removed_without_leaving_invalid_t(self):
        body, _, _ = transformed(r'{\t(0,1000,\fs90\fscx200\fnOld)}Hello')
        self.assertNotIn(r'\t(', body)
        self.assertEqual(engine.plain(body), 'Hello')

    def test_vectors_clips_origin_and_drawing_mode_preserved(self):
        original = (r'{\an7\pos(20,40)\org(120,80)\clip(2,m 0 0 l 30 40 90 120)'
                    r'\p1\pbo4\fscx125}m 0 0 l 30 40 90 120{\p0}Hello')
        values, _, report = engine.update(private(scale_x=150, scale_y=80), [event(original)],
                                          STYLE, 1920, 1080)
        body = values[0][2].split(',', 8)[8]
        self.assertIn(r'\pos(66.666667,100)', body)
        self.assertIn(r'\org(400,200)', body)
        self.assertIn(r'\clip(2,m 0 0 l 100 100 300 300)', body)
        self.assertIn(r'\pbo10', body)
        self.assertIn('m 0 0 l 100 100 300 300', body)
        self.assertIn(r'\fscx125\fscy80', body)  # drawing scale, not text size
        self.assertIn(r'\p0\fnChosen Font\fs38', body)
        self.assertEqual(engine.plain(body), 'Hello')
        self.assertEqual(report['drawing_runs'], 1)

    def test_plain_excludes_paths_and_preserves_escaped_braces(self):
        self.assertEqual(engine.plain(r'{\p1}m 0 0 l 90 90{\p0}文本\Nnext\hline'),
                         '文本\nnext line')
        source = r'literal \{brace\} and \\ slash'
        body, _, _ = transformed(source)
        self.assertEqual(engine.plain(body), engine.plain(source))
        self.assertIn(source, body)

    def test_plain_matches_ass_backslash_semantics_and_literal_word_joiner_guard(self):
        self.assertEqual(engine.plain(r'C:\temp\folder'), r'C:\temp\folder')
        self.assertEqual(engine.plain(r'Two \\ slashes'), r'Two \\ slashes')
        self.assertEqual(engine.plain(r'\\N'), '\\\n')  # player: slash then newline
        self.assertEqual(engine.plain(r'\\{literal\\}'), r'\{literal\}')
        guarded = 'Literal \\\u2060N, \\\u2060n and \\\u2060h'
        self.assertEqual(engine.plain(guarded), r'Literal \N, \n and \h')
        body, _, _ = transformed(guarded + r' and \\{literal\\}')
        self.assertEqual(engine.plain(body), r'Literal \N, \n and \h and \{literal\}')

    def test_mixed_vector_appearance_is_independent_of_selected_text_colour_and_size(self):
        source = (r'{\an7\pos(60,30)\p1\c&H0000FF&\fscx120\fscy80}'
                  r'm 0 0 l 90 0 90 50 0 50 c{\p0\fnArial\fs19\c&HFFFFFF&}Text')
        drawn = []
        for size in (38, 48):
            values, _, _ = engine.update(private(scale_x=150, scale_y=80), [event(source)],
                                          dict(STYLE, size=size), 1920, 1080)
            body = values[0][2].split(',', 8)[8]
            # The drawing has red fill and its original 19*2.5 font metrics,
            # while p0 returns to the selected text size and selected RGB.
            first, after = body.split(r'{\p0', 1)
            self.assertIn(r'\fnOld Font\fs47.5\1c&H0000FF&', first)
            self.assertIn(r'\fnChosen Font\fs' + str(size), after)
            self.assertIn(r'\1c&HEFAC13&', after)
            self.assertEqual(engine.plain(body), 'Text')
            drawn.append(first[first.index(r'{\an7'):])
        self.assertEqual(drawn[0], drawn[1])

    def test_scroll_banner_distances_scaled_but_timing_and_alignment_preserved(self):
        first = event('a', effect='Scroll up;10;100;25;8')
        second = event('b', effect='Banner;20;1;12')
        values, changed, _ = engine.update(private(), [first, second], STYLE, 1920, 1080)
        self.assertEqual(values[0][2].split(',', 8)[7], 'Scroll up;25;250;25;20')
        self.assertEqual(values[1][2].split(',', 8)[7], 'Banner;20;1;40')
        self.assertIn(b',8,0,0,0,1', changed)  # sign style remains top aligned

    def test_unscaled_outline_shadow_and_blur_keep_layout_pixel_distances(self):
        data = private().replace(b'[V4+ Styles]', b'ScaledBorderAndShadow: no\nLayoutResX: 1024\nLayoutResY: 576\n[V4+ Styles]')
        source = r'{\bord2\shad3\xbord4\ybord5\xshad6\yshad7\blur1.25\t(0,500,\bord8\blur2)}text'
        values, changed, report = engine.update(data, [event(source)], STYLE, 1920, 1080)
        body = values[0][2].split(',', 8)[8]
        self.assertIn(r'\bord2\shad3\xbord4\ybord5\xshad6\yshad7\blur1.25\t(0,500,\bord8\blur2)', body)
        self.assertIn(b'LayoutResX: 1024\nLayoutResY: 576', changed)
        style_values = next(line for line in changed.decode().splitlines() if line.startswith('Style: Default')).split(':', 1)[1].split(',')
        self.assertEqual(style_values[16:18], ['1', '2'])
        self.assertFalse(report['scaled_border_and_shadow'])

    def test_scaled_outline_shadow_are_mapped_per_axis_and_blur_is_not_font_scaled(self):
        data = private().replace(b'[V4+ Styles]', b'ScaledBorderAndShadow: yes\nLayoutResX: 640\nLayoutResY: 360\n[V4+ Styles]')
        source = r'{\t(0,500,\bord2\shad3\blur1.25)\rSign\bord4}text'
        values, changed, report = engine.update(data, [event(source)], STYLE, 1920, 1080)
        body = values[0][2].split(',', 8)[8]
        self.assertIn(r'\xbord3.333333\ybord2.5\xshad6.666667\yshad5', body)
        self.assertIn(r'\t(0,500,\xbord6.666667\ybord5\xshad10\yshad7.5\blur1.25)', body)
        self.assertIn(r'\rSign\xbord3.333333\ybord2.5\xshad6.666667\yshad5', body)
        self.assertIn(r'\xbord13.333333\ybord10', body)
        self.assertIn(b'LayoutResX: 640\nLayoutResY: 360', changed)
        self.assertTrue(report['scaled_border_and_shadow'])

    def test_malformed_coordinate_drawing_animation_and_header_fail_explicitly(self):
        for body in (r'{\pos(1,bad)}x', r'{\move(1,2,3)}x', r'{\clip(m 1)}x',
                     r'{\t(0,100,\clip(1,2,3,4)}x', r'{\t(0,100)}x', r'{\p1}wrong words'):
            with self.subTest(body=body), self.assertRaises(ValueError):
                transformed(body)
        for data in (b'[V4 Styles]\n', private().replace(b'PlayResY: 288\n', b''),
                     private().replace(b',19,', b',19,bad,')):
            with self.assertRaises(ValueError):
                engine.update(data, [event('x')], STYLE, 1920, 1080)
        with self.assertRaisesRegex(ValueError, '效果'):
            engine.update(private(), [event('x', effect='Unknown animation')], STYLE, 1920, 1080)

    def test_invalid_dimensions_font_and_unresolved_auto_value_fail_explicitly(self):
        for width, height in ((0, 1080), (1920, float('nan')), (1920, -1)):
            with self.assertRaises(ValueError):
                engine.update(private(), [event('x')], STYLE, width, height)
        for change in (dict(size='auto'), dict(size=True), dict(size=float('inf')),
                       dict(family='name,inject'), dict(family=r'name\fs99'), dict(color='oops')):
            with self.assertRaises(ValueError):
                engine.update(private(), [event('x')], dict(STYLE, **change), 1920, 1080)


class RealFontMetricsTests(unittest.TestCase):
    def test_player_family_uses_legacy_name_required_by_directwrite(self):
        path = ROOT / 'assets/subtitle-fonts/chill/ChillRoundF v3.0.ttf'
        self.assertEqual(engine.player_family(path), '寒蝉全圆体')

    def test_auto_size_uses_actual_glyphs_one_size_per_track(self):
        cases = [
            ('assets/subtitle-fonts/chill/ChillRoundF v3.0.ttf', '别担心，我们一定能找到回家的路。'),
            ('assets/subtitle-fonts/lxgw/LXGWWenKaiGBScreen.ttf', 'We will find our way home.'),
            ('assets/subtitle-fonts/lxgw/LXGWWenKaiGBScreen.ttf', 'Überraschung: ä, ö, ü und ß.'),
            ('assets/subtitle-fonts/chill/ChillRoundF v3.0.ttf', '心配しないで、家に帰る道を探そう。'),
        ]
        for path, text in cases:
            with self.subTest(text=text):
                size, report = engine.automatic_size(ROOT / path, [text] * 5, 1920, 1080)
                doubled, _ = engine.automatic_size(ROOT / path, [text] * 5, 3840, 2160)
                self.assertEqual(size, doubled)
                self.assertIsInstance(size, int)
                scale = engine.ass_font_scale(ROOT / path)
                self.assertTrue(round(12 * scale) <= size <= round(90 * scale))
                self.assertEqual(report['dialogue_lines'], 5)
                self.assertLess(abs(size * report['glyph_ratio'] / 720 - .05), .002)

    def test_long_captions_keep_B_size_instead_of_shrinking(self):
        path = ROOT / 'assets/subtitle-fonts/lxgw/LXGWWenKaiGBScreen.ttf'
        size, report = engine.automatic_size(path, ['Long dialogue ' * 100] * 20, 1920, 1080)
        self.assertEqual(report['target_fraction'], .05)
        self.assertLess(abs(size * report['glyph_ratio'] / 720 - .05), .002)
        self.assertEqual(size, engine.automatic_size(path, ['Long dialogue'], 1920, 1080)[0])

    def test_raw_font_instance_is_resized_for_measurement_without_mutating_caller(self):
        path = ROOT / 'assets/subtitle-fonts/rubik/Rubik[wght].ttf'
        raw = QRawFont(str(path), 32)
        by_file = engine.automatic_size(path, ['Normal English dialogue'], 1920, 1080)
        by_instance = engine.automatic_size(raw, ['Normal English dialogue'], 1920, 1080)
        self.assertEqual(by_file, by_instance)
        self.assertEqual(raw.pixelSize(), 32)

    def test_zero_width_format_characters_do_not_measure_missing_glyph_boxes(self):
        path = ROOT / 'assets/subtitle-fonts/rubik/Rubik[wght].ttf'
        reference = engine.automatic_size(path, ['long text ' * 10], 1920, 1080)
        guarded = engine.automatic_size(path, ['long\u2060 text ' * 10], 1920, 1080)
        self.assertEqual(reference, guarded)

    def test_numbers_have_measurable_auto_size_and_drawing_only_has_explicit_error(self):
        path = ROOT / 'assets/subtitle-fonts/rubik/Rubik[wght].ttf'
        size, _ = engine.automatic_size(path, ['12345'], 1920, 1080)
        self.assertTrue(size > 0)
        with self.assertRaisesRegex(ValueError, '对白'):
            engine.automatic_size(path, [r'{\p1}m 0 0 l 90 90'], 1920, 1080)
        with self.assertRaises(ValueError):
            engine.player_family(ROOT / 'nonexistent.ttf')


if __name__ == '__main__':
    unittest.main()
