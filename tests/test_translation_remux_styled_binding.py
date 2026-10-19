"""Bind real translation routes and preserve resolved display settings on edits."""
import base64
import copy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import subtitle_fonts as fonts
import subtitle_tool_core as core
import translation_remux as remux
import translation_review as review


class StyledTranslationBindingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.output = self.root / 'movie.SF.mkv'
        self.output.write_bytes(b'completed-movie')
        self.work = remux.work_directory(self.output)
        self.work.mkdir()
        self.events = [core.SubtitleEvent('00:00:01,001', '00:00:04,057', 'Think differently.')]
        self.style = dict(family='Example Family', size=39, size_mode='auto', color='#FFE0A3')
        self.private = fonts.header(self.style)
        self.props = dict(uid=913, number=1, codec_id='S_TEXT/ASS', language='ger',
                          language_ietf='de', track_name=core.LANGUAGES['de'][2] + ' Auto',
                          codec_private_data=self.private.hex(), default_track=True,
                          forced_track=False, enabled_track=True)
        self.media = dict(tracks=[dict(id=0, type='subtitles', properties=self.props)])
        self.target, self.report = self.make_report('de', track=3)
        self.write_receipt()
        self.logs = []

    def make_report(self, target, track=None, name=None):
        stem = f'-track-{track}' if track is not None else ''
        target_path = self.work / (name or f'translated{stem}-{target}.srt')
        cache = self.work / f'translation-cache{stem}-{target}.jsonl'
        issue = dict(index=1, start=self.events[0].start, end=self.events[0].end,
                     source=self.events[0].text, translation='Denke anders框.', reason='含中文',
                     status='needs_review', repair_attempts=2)
        review.save_progress(self.events, target, 'en', cache, target_path, {1: issue},
                             complete=True, translations={1: issue['translation']})
        return target_path, review.report_path(cache)

    def write_receipt(self, target=None, style=None, private=None, source_sha=None, **extra):
        target = target or self.target
        private = self.private if private is None else private
        receipt = dict(style=style or self.style,
                       source_sha256=source_sha or remux._hash(target),
                       private=base64.b64encode(private).decode('ascii'),
                       private_sha256=hashlib.sha256(private).hexdigest(), **extra)
        (self.work / 'font-style-receipt.json').write_text(
            json.dumps({str(target.resolve()): receipt}), encoding='utf8')
        return receipt

    def register(self):
        with patch.object(core, 'inspect_media', return_value=self.media):
            return remux.register_output(self.output, self.work, ['de'], self.logs.append)

    def test_embedded_generated_filename_is_bound_to_the_exact_font_receipt(self):
        self.assertEqual(self.register(), 1)
        manifest, reports = remux.load_bound_reports(self.output)
        binding = manifest['bindings'][0]
        self.assertEqual(binding['target_srt'], str(self.target))
        self.assertEqual(binding['track']['uid'], 913)
        self.assertEqual(binding['font_style']['style']['size'], 39)
        self.assertEqual(binding['font_style']['style']['size_mode'], 'auto')
        self.assertEqual(reports[0][0], self.report)

    def test_embedded_and_external_utf8_routes_bind_without_stale_font_style(self):
        self.props.update(codec_id='S_TEXT/UTF8')
        self.assertEqual(self.register(), 1)
        binding = remux.load_bound_reports(self.output)[0]['bindings'][0]
        self.assertIsNone(binding['font_style'])
        self.report.unlink()
        self.target, self.report = self.make_report('de')
        self.assertEqual(self.register(), 1)
        binding = remux.load_bound_reports(self.output)[0]['bindings'][0]
        self.assertEqual(binding['target_srt'], str(self.target))

    def test_arbitrary_name_and_mismatched_cache_provenance_do_not_bind(self):
        self.report.unlink()
        self.target, self.report = self.make_report('de', name='personal-notes.srt')
        self.write_receipt()
        self.assertIsNone(self.register())
        self.assertFalse(remux.available(self.output))
        self.report.unlink()
        self.target, self.report = self.make_report('de', track=3, name='translated-track-4-de.srt')
        self.write_receipt()
        self.assertIsNone(self.register())
        self.assertFalse(remux.available(self.output))

    def test_two_reports_for_one_language_never_silently_pick_the_last_one(self):
        self.make_report('de', track=4)
        self.assertEqual(self.register(), 0)
        self.assertFalse(remux.available(self.output))
        self.assertTrue(any('多份' in line for line in self.logs))

    def test_duplicate_language_tracks_remain_uneditable(self):
        self.media['tracks'].append(dict(id=1, type='subtitles', properties=dict(self.props, uid=914)))
        self.assertEqual(self.register(), 0)
        self.assertFalse(remux.available(self.output))

    def test_stale_or_corrupt_receipt_and_header_are_not_adopted(self):
        cases = (
            dict(source_sha='0' * 64),
            dict(style=dict(self.style, size=48)),
            dict(private=self.private.replace(b'Example Family', b'Different Family')),
            dict(private_sha256='0' * 64),
            dict(track_uid=914),
        )
        for change in cases:
            with self.subTest(change=change):
                if 'private_sha256' in change:
                    receipt = self.write_receipt()
                    receipt.update(change)
                    (self.work / 'font-style-receipt.json').write_text(
                        json.dumps({str(self.target): receipt}), encoding='utf8')
                else:
                    self.write_receipt(**change)
                self.assertEqual(self.register(), 0)
                self.assertFalse(remux.available(self.output))
        self.write_receipt()
        self.props['codec_private_data'] = self.private.replace(b',39,', b',40,').hex()
        self.assertEqual(self.register(), 0)
        self.assertFalse(remux.available(self.output))

    def execute_edit(self, changes=None, captured=None):
        merge = self.root / 'mkvmerge.exe'
        merge.write_bytes(b'tool')
        (self.root / 'mkvpropedit.exe').write_bytes(b'tool')

        def run(args, **kwargs):
            if args[0] == str(merge):
                Path(args[args.index('-o') + 1]).write_bytes(b'new-movie-' + self.target.read_bytes())
            if 'tags' in args:
                Path(args[-1]).write_text('<Tags/>', encoding='utf8')

        write = fonts.write_subtitle_mkv
        def write_and_capture(path, events, style, **kwargs):
            if captured is not None:
                captured.append(dict(events=events, style=copy.deepcopy(style),
                                     private=kwargs['private'], packets=kwargs.get('packets', False)))
            return write(path, events, style, **kwargs)

        with patch.object(core, 'MKVMERGE', str(merge)), \
             patch.object(core, 'inspect_media', return_value=self.media), \
             patch.object(core, 'run_command', side_effect=run), \
             patch.object(core, 'ensure_mux_disk_space'), \
             patch.object(core, 'validate_media_output'), \
             patch.object(remux.mkv_fast_mux, 'read_headers', return_value={}), \
             patch.object(remux, '_verify_preserved', side_effect=changes), \
             patch.object(fonts, 'write_subtitle_mkv', side_effect=write_and_capture), \
             patch.object(core, 'translate_events', side_effect=AssertionError('Must not retranslate')):
            return remux.remux_corrected_output(self.output)

    def test_resolved_auto_and_manual_sizes_and_exact_timing_survive_repeated_edits(self):
        for mode, size in [('auto', 39), ('manual', 48)]:
            with self.subTest(mode=mode):
                self.style.update(size_mode=mode, size=size)
                self.private = fonts.header(self.style)
                self.props['codec_private_data'] = self.private.hex()
                self.write_receipt()
                self.assertEqual(self.register(), 1)
                captured = []
                for text in ('Denke anders.', 'Denke ganz anders.'):
                    review.save_corrections(self.report, {1: text})
                    self.execute_edit(captured=captured)
                    manifest = remux.load_bound_reports(self.output)[0]
                    self.assertEqual(manifest['bindings'][0]['font_style']['source_sha256'], remux._hash(self.target))
                self.assertEqual(len(captured), 2)
                for record in captured:
                    self.assertEqual(record['style']['size'], size)
                    self.assertEqual(record['style']['size_mode'], mode)
                    self.assertEqual(record['private'], self.private)
                    self.assertEqual(record['events'][0][:2], (1001, 4057))
                self.assertEqual(manifest['bindings'][0]['track']['uid'], 913)
                # Restore warning fixture for the next independent mode.
                self.target, self.report = self.make_report('de', track=3)

    def test_successive_html_edits_follow_saved_font_colour_and_resolved_size(self):
        from subtitle_style_engine import plain
        self.style.update(canvas_width=1440, canvas_height=720, change_font=True, change_color=True)
        self.private = fonts.header(self.style)
        self.props['codec_private_data'] = self.private.hex()
        self.write_receipt(track_uid=913)
        self.register()
        captured = []
        for text in ('<font face="Other Font" color="#00FF00"><i>Denke</i>\nanders.</font>',
                     '<font face="Yet Another Font" color="red">Denke ganz anders.</font>'):
            review.save_corrections(self.report, {1: text})
            self.execute_edit(captured=captured)
        for record, visible in zip(captured, ['Denke\nanders.', 'Denke ganz anders.']):
            body = record['events'][0][2].split(',', 8)[8]
            self.assertEqual(plain(body), visible)
            self.assertNotIn('Other Font', body)
            self.assertNotIn('Another Font', body)
            self.assertNotIn('00FF00', body)
            self.assertNotIn('0000FF', body)
            self.assertEqual(record['style']['size'], 39)
            self.assertEqual(record['style']['size_mode'], 'auto')
            self.assertEqual(record['private'], self.private)
            self.assertTrue(record['packets'])
            self.assertEqual(record['events'][0][:2], (1001, 4057))
        manifest = remux.load_bound_reports(self.output)[0]
        self.assertEqual(manifest['bindings'][0]['font_style']['track_uid'], 913)
        self.assertEqual(manifest['bindings'][0]['track']['uid'], 913)
        self.assertIn(r'\i1', captured[0]['events'][0][2])

    def test_exact_2_0_73_header_remains_editable_and_other_legacy_headers_are_rejected(self):
        self.style = dict(family='Example Family', size=38, color='#FFE0A3')
        self.private = fonts.header(self.style).replace(
            b',&H00A3E0FF,&H00A3E0FF,&H00000000',
            b',&H00A3E0FF,&H000000FF,&H00000000', 1)
        self.props['codec_private_data'] = self.private.hex()
        self.write_receipt()
        self.assertEqual(self.register(), 1)
        captured = []
        for text in ('<font face="Other Font" color="red">Denke anders.</font>', 'Denke ganz anders.'):
            review.save_corrections(self.report, {1: text})
            self.execute_edit(captured=captured)
        for record in captured:
            self.assertEqual(record['private'], self.private)
            self.assertEqual(record['style']['size'], 38)
            self.assertNotIn('Other Font', record['events'][0][2])
        # Make the receipt and mocked actual output agree with each other, but
        # differ from the single known legacy header: still refuse binding.
        altered = self.private.replace(b'WrapStyle: 0', b'WrapStyle: 2')
        self.props['codec_private_data'] = altered.hex()
        self.write_receipt(private=altered)
        self.assertEqual(self.register(), 0)
        self.assertFalse(remux.available(self.output))
        # New adaptive receipts may never opt into the legacy exception.
        self.style['size_mode'] = 'auto'
        self.props['codec_private_data'] = self.private.hex()
        self.write_receipt()
        self.assertEqual(self.register(), 0)
        self.assertFalse(remux.available(self.output))

    def test_manifest_style_tampering_is_rejected_before_any_mux(self):
        self.register()
        review.save_corrections(self.report, {1: 'Denke anders.'})
        path = self.work / remux.MANIFEST
        manifest = json.loads(path.read_text(encoding='utf8'))
        manifest['bindings'][0]['font_style']['style']['size'] = 48
        path.write_text(json.dumps(manifest), encoding='utf8')
        before = self.output.read_bytes()
        with self.assertRaisesRegex(ValueError, '样式与记录不一致'):
            self.execute_edit()
        self.assertEqual(self.output.read_bytes(), before)

    def test_changed_actual_header_rejected_before_any_mux(self):
        self.register()
        review.save_corrections(self.report, {1: 'Denke anders.'})
        self.props['codec_private_data'] = self.private.replace(b',39,', b',40,').hex()
        before = self.output.read_bytes()
        with self.assertRaisesRegex(ValueError, '成品字体样式'):
            self.execute_edit()
        self.assertEqual(self.output.read_bytes(), before)

    def test_failed_preservation_leaves_original_and_saved_edit_available(self):
        self.register()
        review.save_corrections(self.report, {1: 'Denke anders.'})
        before = self.output.read_bytes()
        with self.assertRaisesRegex(ValueError, '附件未保留'):
            self.execute_edit(changes=ValueError('附件未保留'))
        self.assertEqual(self.output.read_bytes(), before)
        self.assertEqual(review.load_report(self.report)['issues'][0]['translation'], 'Denke anders.')
        self.assertFalse(list(self.root.glob('.translation-remux-*')))


if __name__ == '__main__':
    unittest.main()
