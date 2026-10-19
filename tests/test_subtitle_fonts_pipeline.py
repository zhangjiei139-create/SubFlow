"""Small real subtitle outputs exercise the production styling/mux boundary.

Only input movie packet reads are mocked.  Style transformation, subtitle EBML
writing, receipt checks and output header parsing run through production code.
"""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import base64
import hashlib
import json
import re
import struct
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PySide6.QtWidgets import QApplication
import subtitle_fonts as fonts
import subtitle_style_engine as engine
import mkv_fast_mux as fast
import subtitle_tool_core as core

APP = QApplication.instance() or QApplication([])
FIELDS = ('Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,'
          'BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,'
          'BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding')


def original_private(x=384, y=288):
    return (f'[Script Info]\nScriptType: v4.00+\nPlayResX: {x}\nPlayResY: {y}\n'
            '[V4+ Styles]\nFormat: ' + FIELDS + '\n'
            'Style: Default,Original Text Font,19,&H00AAAAAA,&H200000FF,&H00111111,'
            '&H40000000,0,0,0,0,100,100,3,0,1,1,2,2,12,12,10,1\n'
            'Style: Sign,Original Sign Font,23,&H00555555,&H000000FF,&H00111111,'
            '&H40000000,0,0,0,0,100,100,0,0,1,1,2,8,0,0,0,1\n').encode('utf8')


def ass_event(text, start=1001, end=2033, style='Default'):
    return (start, end, f'0,1,{style},Actor,0,0,0,,{text}')


def track(ident=5, code='en', codec='S_TEXT/ASS', private=None):
    return dict(id=ident, type='subtitles', properties=dict(
        codec_id=codec, uid=7000+ident, number=ident+1, language_ietf=code,
        language='eng' if code == 'en' else 'ger', track_name=code + ' retained',
        default_track=True, forced_track=False,
        codec_private_data=(private or original_private()).hex()))


def style(size=38, font_id='builtin:rubik', color='#13ACEF', mode='manual'):
    value=dict(font_id=font_id, size_mode=mode, size=size, color=color)
    if mode=='manual':value['size_origin']='user'
    return value


def actual_private(path):
    """Read the tiny real output's CodecPrivate (fast headers expose its hash)."""
    with Path(path).open('rb') as stream:
        eid, length = fast._element(stream)
        if eid != 0x1A45DFA3: raise AssertionError('not EBML')
        stream.seek(length, 1)
        eid, _ = fast._element(stream)
        if eid != fast.SEGMENT: raise AssertionError('no Segment')
        while True:
            eid, length = fast._element(stream)
            if eid == fast.TRACKS:
                tracks = list(fast._children(stream.read(length)))
                return dict(fast._children(tracks[0][1]))[0x63A2]
            if length is None: raise AssertionError('unexpected unknown length')
            stream.seek(length, 1)


def actual_events(path):
    """Consume the small output's Matroska blocks as an independent reader."""
    events = []
    with Path(path).open('rb') as stream:
        _, length = fast._element(stream)
        stream.seek(length, 1)
        eid, _ = fast._element(stream)
        if eid != fast.SEGMENT: raise AssertionError('no Segment')
        while stream.tell() < Path(path).stat().st_size:
            eid, length = fast._element(stream)
            if length is None: raise AssertionError('unknown child length')
            if eid != fast.CLUSTER:
                stream.seek(length, 1)
                continue
            fields = list(fast._children(stream.read(length)))
            timestamp = int.from_bytes(next(value for key, value in fields if key == 0xE7), 'big')
            for key, value in fields:
                if key != 0xA0: continue
                group = dict(fast._children(value))
                block = group[0xA1]
                _, position = fast._vint(block, 0)
                offset = struct.unpack('>h', block[position:position+2])[0]
                start = timestamp + offset
                events.append((start, start+int.from_bytes(group[0x9B], 'big'),
                               block[position+3:].decode('utf8')))
    return events


class TextStylingPipelineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.input = self.root / 'original.mkv'
        self.input.write_bytes(b'input movie remains untouched')
        self.output = self.root / 'finished.mkv'
        self.output.write_bytes(b'existing output remains untouched')
        self.work = self.root / 'work'
        self.media = dict(tracks=[dict(id=0, type='video', properties=dict(
            codec_id='V_MPEG4/ISO/AVC', pixel_dimensions='1920x1080')),
            track()], attachments=[], chapters=[])
        self.events = {5: [ass_event('Hello.'), ass_event(
            r'{\an8\pos(120,42)\fnInline Font\fs+5\c&H0000FF&}A positioned sign.',
            start=3007, end=5049, style='Sign')]}
        self.messages = []
        self.cancel = threading.Event()

    def prepare(self, styles, kept=None, generated=None, offsets=None, default=None, equivalent=False):
        with fonts.processing(styles, self.work, self.output):
            return fonts.prepare_mux(self.input, self.output, self.media,
                [5] if kept is None else kept, generated or [], offsets or {},
                default, self.messages.append, self.cancel, equivalent)

    def writes(self):
        return patch.object(fonts, 'write_subtitle_mkv', wraps=fonts.write_subtitle_mkv)

    def assert_originals_intact(self):
        self.assertEqual(self.input.read_bytes(), b'input movie remains untouched')
        self.assertEqual(self.output.read_bytes(), b'existing output remains untouched')

    def source_srt(self, name='translation.srt'):
        path = self.root / name
        path.write_text('1\n00:00:01,001 --> 00:00:02,033\n<i>Hello.</i>\n\n'
                        '2\n00:00:03,007 --> 00:00:05,049\nSecond line.\n', encoding='utf8')
        return path

    def generated_body(self, text, settings):
        source = self.root/'marked-up.srt'
        source.write_text('1\n00:00:01,001 --> 00:00:02,033\n' + text + '\n', encoding='utf8')
        with patch.object(fonts, '_default_family', return_value='Default Sans'):
            result = self.prepare({'en': settings}, kept=[], generated=[(source, 'en', 'English', False)])
        return actual_events(result['generated'][0][0])[0][2].split(',', 8)[8], result

    def test_legal_manual_extremes_and_common_values_are_exact_and_never_auto(self):
        for size in (1, 38, 48, 200):
            with self.subTest(size=size), patch.object(fonts, '_text_packets', return_value=self.events), \
                    patch.object(engine, 'automatic_size', side_effect=AssertionError('manual called auto')), self.writes() as write:
                prepared = self.prepare({'en': style(size)})
                output_track = fast.read_headers(prepared['generated'][0][0])['tracks'][0]
                private = actual_private(prepared['generated'][0][0]).decode('utf8')
                family = prepared['fonts'][0]['family']
                self.assertIn(f',{family},{size},', private)
                styled_events = write.call_args.args[1]
                self.assertEqual([e[:2] for e in styled_events], [e[:2] for e in self.events[5]])
                for event in styled_events:
                    self.assertEqual(set(re.findall(r'\\fs(?![cp])([-+\d.]+)', event[2])), {str(size)})
                self.assert_originals_intact()

    def test_position_and_body_survive_canvas_normalization_all_lines_are_styled(self):
        with patch.object(fonts, '_text_packets', return_value=self.events), self.writes() as write:
            prepared = self.prepare({'en': style(48)}, offsets={5: 731}, default=('retained', 5))
        args = write.call_args.args
        styled_events = args[1]
        private = args[4].decode('utf8')
        self.assertIn('PlayResX: 1280', private)
        self.assertIn('PlayResY: 720', private)
        self.assertEqual([e[:2] for e in styled_events], [(1732, 2764), (3738, 5780)])
        for original, changed in zip(self.events[5], styled_events):
            self.assertEqual(engine.plain(original[2].split(',', 8)[8]), engine.plain(changed[2].split(',', 8)[8]))
            self.assertEqual(original[2].split(',', 8)[:4], changed[2].split(',', 8)[:4])
        body = styled_events[1][2].split(',', 8)[8]
        match = re.search(r'\\pos\(([^)]*)\)', body)
        x, y = map(float, match[1].split(','))
        self.assertAlmostEqual(x/1280, 120/384)
        self.assertAlmostEqual(y/720, 42/288)
        self.assertIn(r'\an8', body)
        self.assertNotIn(r'\fnInline Font', body)
        self.assertNotIn(r'\fs+5', body)
        self.assertIn(r'\fs48', body)
        self.assertEqual(prepared['kept'], [])
        self.assertEqual(prepared['replacements'], {5: 0})
        self.assertTrue(prepared['generated'][0][3])
        self.assertEqual(fast.read_headers(prepared['generated'][0][0])['tracks'][0]['uid'], 7005)
        self.assertEqual(prepared['metadata'][str(prepared['generated'][0][0])], self.media['tracks'][1]['properties'])
        self.assertEqual(actual_events(prepared['generated'][0][0]), styled_events)
        self.assert_originals_intact()

    def test_keep_original_font_does_not_disable_manual_size_and_color(self):
        with patch.object(fonts, '_text_packets', return_value=self.events), \
                patch.object(fonts, 'resolve_font', side_effect=AssertionError('retained font was replaced')), \
                patch.object(fonts, '_original_raw', side_effect=AssertionError('manual needs no font measurement')), self.writes() as write:
            prepared = self.prepare({'en': style(48, font_id='')})
        private = write.call_args.args[4].decode('utf8')
        self.assertIn('Style: Default,Original Text Font,48,&H00EFAC13', private)
        self.assertIn('Style: Sign,Original Sign Font,48,&H00EFAC13', private)
        body = write.call_args.args[1][1][2]
        self.assertIn(r'\fnInline Font', body)
        self.assertIn(r'\fs48', body)
        self.assertIn(r'\1c&HEFAC13&', body)
        self.assertNotIn(r'\c&H0000FF&', body)
        self.assertEqual(prepared['fonts'], [])

    def test_keep_color_retains_original_color_but_manual_size_still_applies(self):
        with patch.object(fonts, '_text_packets', return_value=self.events), self.writes() as write:
            self.prepare({'en': style(48, font_id='', color='keep')})
        private = write.call_args.args[4].decode('utf8')
        self.assertIn('Style: Default,Original Text Font,48,&H00AAAAAA', private)
        body = write.call_args.args[1][1][2]
        self.assertIn(r'\c&H0000FF&', body)
        self.assertIn(r'\fs48', body)

    def test_auto_keep_font_measures_original_family_without_replacing_or_embedding_it(self):
        raw = SimpleNamespace(supportsCharacter=lambda _: True)
        with patch.object(fonts, '_text_packets', return_value=self.events), \
                patch.object(fonts, '_original_raw', return_value=raw) as original, \
                patch.object(engine, 'automatic_size', return_value=(32, {'selected_size': 32})) as automatic, \
                patch.object(fonts, 'resolve_font', side_effect=AssertionError('font was replaced')), self.writes() as write:
            prepared = self.prepare({'en': style(font_id='', color='keep', mode='auto', size=0)})
        self.assertEqual(original.call_args.args[0], 'Original Text Font')
        self.assertIs(automatic.call_args.args[0], raw)
        self.assertIn(b'Style: Default,Original Text Font,32,&H00AAAAAA', write.call_args.args[4])
        self.assertIn(r'\fnInline Font', write.call_args.args[1][1][2])
        self.assertEqual(prepared['fonts'], [])

    def test_generated_srt_receipt_records_resolved_auto_size_uid_and_exact_private(self):
        source = self.source_srt()
        original = source.read_bytes()
        metrics = dict(selected_size=33, reference_height=720, dialogue_lines=2)
        with patch.object(engine, 'automatic_size', return_value=(33, metrics)) as automatic, self.writes() as write:
            prepared = self.prepare({'de': style(mode='auto', size=0)}, kept=[],
                                    generated=[(source, 'de', 'Translated German', True)])
        automatic.assert_called_once()
        self.assertEqual(automatic.call_args.args[2:], (1920, 1080))
        receipt = fonts.styled_receipt(self.work, source)
        self.assertIsNotNone(receipt)
        self.assertEqual(receipt['style']['size_mode'], 'auto')
        self.assertEqual(receipt['style']['size'], 33)
        self.assertEqual(receipt['style']['adaptive_metrics'], metrics)
        actual = fast.read_headers(prepared['generated'][0][0])['tracks'][0]
        self.assertEqual(actual['uid'], receipt['track_uid'])
        private = actual_private(prepared['generated'][0][0])
        self.assertEqual(private, base64.b64decode(receipt['private']))
        self.assertEqual(actual['private'], hashlib.sha256(private).digest())
        self.assertEqual(hashlib.sha256(private).hexdigest(), receipt['private_sha256'])
        self.assertEqual(receipt['source_sha256'], hashlib.sha256(original).hexdigest())
        emitted = actual_events(prepared['generated'][0][0])
        self.assertEqual([event[:2] for event in emitted], [(1001, 2033), (3007, 5049)])
        self.assertEqual([engine.plain(event[2].split(',', 8)[8]) for event in emitted], ['Hello.', 'Second line.'])
        self.assertEqual(prepared['generated'][0][1:], ('de', 'Translated German', True))
        self.assertEqual(source.read_bytes(), original)

    def test_generated_manual_never_calls_auto_and_receipt_is_invalidated_if_text_changes(self):
        source = self.source_srt()
        with patch.object(engine, 'automatic_size', side_effect=AssertionError('manual called auto')):
            self.prepare({'de': style(48)}, kept=[], generated=[(source, 'de', 'German', False)])
        receipt = fonts.styled_receipt(self.work, source)
        self.assertEqual(receipt['style']['size'], 48)
        self.assertEqual(receipt['style']['size_mode'], 'manual')
        self.assertNotIn('adaptive_metrics', receipt['style'])
        source.write_text(source.read_text(encoding='utf8') + '\nchanged', encoding='utf8')
        self.assertIsNone(fonts.styled_receipt(self.work, source))

    def test_srt_source_parser_retains_markup_for_independent_keep_color_and_keep_font(self):
        text = '<font color="#FF0000" face="Arial"><i>Red text.</i></font> Outside.'
        source = self.root/'raw-markup.srt'
        source.write_text('1\n00:00:01,001 --> 00:00:02,033\n' + text + '\n', encoding='utf8')
        self.assertEqual(fonts.srt_events(source), [(1001, 2033, text)])

    def test_generated_keep_color_preserves_red_html_even_when_font_is_changed(self):
        body, result = self.generated_body('<font color="#FF0000">Red text.</font> Outside.',
                                           style(48, color='keep'))
        self.assertEqual(engine.plain(body), 'Red text. Outside.')
        self.assertIn(r'\1c&H0000FF&', body)
        self.assertIn(r'\2c&H0000FF&', body)
        self.assertIn(r'\fs48', body)
        self.assertEqual(fonts.styled_receipt(self.work, self.root/'marked-up.srt')['style']['change_color'], False)

    def test_generated_explicit_white_overrides_every_red_html_instruction(self):
        body, _ = self.generated_body('<font color="#FF0000">Red text.</font> Outside.',
                                     style(48, color='#FFFFFF'))
        self.assertEqual(engine.plain(body), 'Red text. Outside.')
        self.assertNotIn('&H0000FF&', body)
        colors = re.findall(r'\\[12]c&H([A-Fa-f0-9]+)&', body)
        self.assertTrue(colors)
        self.assertEqual(set(value.upper() for value in colors), {'FFFFFF'})

    def test_generated_only_size_preserves_html_face_instead_of_replacing_it(self):
        body, result = self.generated_body('<font face="Uninstalled Original Face">Words.</font> Other.',
                                           style(48, font_id='', color='keep'))
        self.assertEqual(engine.plain(body), 'Words. Other.')
        self.assertIn(r'\fnUninstalled Original Face', body)
        self.assertIn(r'\fs48', body)
        self.assertEqual(result['fonts'], [])

    def test_literal_paths_ass_break_spellings_and_braces_are_saved_as_visible_text(self):
        text = r'Path C:\New\home\note. Literal \N, \n and \h. {not a tag}' + '\nSecond line.'
        body, result = self.generated_body(text, style(48))
        self.assertEqual(engine.plain(body), text)
        self.assertIn(r'\fs48', body)
        events = actual_events(result['generated'][0][0])
        self.assertEqual(events[0][:2], (1001, 2033))

    def test_two_text_tracks_share_one_packet_read_and_preserve_each_uid(self):
        self.media['tracks'].append(track(6, 'de', 'S_TEXT/UTF8'))
        self.events[6] = [(2001, 3456, 'Änderung: schön und groß.')]
        with patch.object(fonts, '_text_packets', return_value=self.events) as read:
            prepared = self.prepare({'en': style(), 'de': style(48)}, kept=[5, 6])
        read.assert_called_once()
        self.assertEqual(prepared['replacements'], {5: 0, 6: 1})
        self.assertEqual([fast.read_headers(row[0])['tracks'][0]['uid'] for row in prepared['generated']], [7005, 7006])
        self.assertEqual(len(prepared['fonts']), 1)  # same file is attached once

    def test_chinese_equivalence_applies_style_but_does_not_convert_original_words(self):
        self.media['tracks'][1]['properties'].update(language_ietf='zh-TW', language='chi')
        self.events[5] = [ass_event('別擔心。')]
        with patch.object(fonts, '_text_packets', return_value=self.events), self.writes() as write:
            prepared = self.prepare({'zh-CN': style(48, font_id='builtin:chill')}, equivalent=True)
        self.assertEqual(prepared['generated'][0][1], 'zh-TW')
        self.assertEqual(engine.plain(write.call_args.args[1][0][2].split(',', 8)[8]), '別擔心。')
        self.assertIn(r'\fs48', write.call_args.args[1][0][2])

    def test_only_selected_text_is_isolated_once_and_probe_never_reads_picture_packets(self):
        self.media['tracks'].extend([track(6, 'de', 'S_TEXT/UTF8'), track(7, 'zh-CN', 'S_HDMV/PGS')])
        payload = 'Exact millisecond text.'
        packet = dict(stream_index=0, pts_time='1.001000', duration_time='1.032000',
                      data='00000000: ' + payload.encode().hex(' ') + '  ascii column')
        response = dict(streams=[dict(index=0, codec_name='ass')], packets=[packet])
        self.work.mkdir()
        with patch.object(core, 'run_command', return_value=SimpleNamespace(stdout=json.dumps(response))) as command:
            result = fonts._text_packets(self.input, self.media, self.messages.append, self.cancel,
                                         work=self.work, selected_ids={5})
        self.assertEqual(command.call_count, 2)
        isolate, probe = [call.args[0] for call in command.call_args_list]
        self.assertEqual(isolate[isolate.index('--subtitle-tracks')+1], '5')
        self.assertEqual(isolate[isolate.index('--track-order')+1], '0:5')
        self.assertIn('--no-video', isolate)
        self.assertIn('--no-audio', isolate)
        self.assertIn('--no-attachments', isolate)
        self.assertEqual(probe[-1], str(self.work/'retained-text.mkv'))
        self.assertNotEqual(probe[-1], str(self.input))
        self.assertEqual(result, {5: [(1001, 2033, payload)]})
        # Its preparation scan must not claim final output mux progress.
        command.call_args_list[0].kwargs['log']('mkvmerge 封装进度：100%')
        self.assertEqual(self.messages[-1], '文本字幕读取进度：100%')

    def test_cancelled_before_reading_text_never_writes_a_style_output(self):
        self.cancel.set()
        with patch.object(fonts, '_text_packets') as read, self.writes() as write:
            with self.assertRaises(core.CancelledError):
                self.prepare({'en': style()})
        read.assert_not_called()
        write.assert_not_called()
        self.assert_originals_intact()

    def test_uncoverable_glyph_fails_before_a_style_output_or_success_claim(self):
        self.events[5] = [ass_event('Unsupported glyph: ' + chr(0x10ffff))]
        with patch.object(fonts, '_text_packets', return_value=self.events), self.writes() as write:
            with self.assertRaisesRegex(ValueError, '所选字体及内置补字字体均缺字'):
                self.prepare({'en': style()})
        write.assert_not_called()
        self.assertFalse(any('已应用' in message for message in self.messages))
        self.assertFalse((self.work/'font-style-receipt.json').exists())
        self.assert_originals_intact()

    def test_unavailable_selected_font_fails_instead_of_retaining_old_track(self):
        with patch.object(fonts, '_text_packets', return_value=self.events), self.writes() as write:
            with self.assertRaisesRegex(ValueError, '内置字体不存在'):
                self.prepare({'en': style(font_id='builtin:missing')})
        write.assert_not_called()
        self.assertFalse(any('已应用' in message for message in self.messages))
        self.assert_originals_intact()

    def test_unsupported_retained_text_format_explicitly_fails_before_reading_movie(self):
        self.media['tracks'][1]['properties']['codec_id'] = 'S_TEXT/WEBVTT'
        with patch.object(fonts, '_text_packets') as read:
            with self.assertRaisesRegex(ValueError, '文本格式S_TEXT/WEBVTT暂不支持'):
                self.prepare({'en': style()})
        read.assert_not_called()
        self.assert_originals_intact()

    def test_unsupported_generated_text_does_not_claim_font_was_applied(self):
        source = self.root/'unhandled.ass'
        source.write_text('[Script Info]\n', encoding='utf8')
        with self.writes() as write:
            with self.assertRaisesRegex(ValueError, '新增文本字幕格式暂不支持'):
                self.prepare({'de': style()}, kept=[], generated=[(source, 'de', 'German', False)])
        write.assert_not_called()
        self.assertFalse(any('已应用' in message for message in self.messages))

    def test_picture_subtitle_is_unchanged_with_explicit_explanation_and_no_packet_read(self):
        self.media['tracks'][1]['properties']['codec_id'] = 'S_HDMV/PGS'
        with patch.object(fonts, '_text_packets') as read:
            prepared = self.prepare({'en': style()})
        read.assert_not_called()
        self.assertEqual(prepared['kept'], [5])
        self.assertEqual(prepared['generated'], [])
        self.assertTrue(any('图片字幕' in message for message in self.messages))
        self.assert_originals_intact()

    def test_actual_render_family_is_recorded_while_ui_family_stays_separate(self):
        self.events[5] = [ass_event('中文字幕。')]
        with patch.object(fonts, '_text_packets', return_value=self.events):
            prepared = self.prepare({'en': style(font_id='builtin:chill')})
        info = prepared['fonts'][0]
        self.assertEqual(info['family'], '寒蝉全圆体')
        self.assertEqual(info['ui_family'], 'ChillRoundF')
        actual = actual_private(prepared['generated'][0][0]).decode('utf8')
        self.assertIn(',寒蝉全圆体,38,', actual)
        self.assertNotIn(',ChillRoundF,', actual)


if __name__ == '__main__':
    unittest.main()
