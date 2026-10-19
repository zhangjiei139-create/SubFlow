"""Real small MKV first-mux/edit checks for the B-layout version boundary.

Only the legacy calibration's recorded version and deliberate failure injection
are mocked.  Video generation, subtitle styling, all three MKVToolNix tools,
receipt binding, metadata validation and publication use the production path.
All artifacts belong to this test's temporary directories.
"""
import base64
import copy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as ET

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication

import mkv_fast_mux
import subtitle_fonts as fonts
import subtitle_style_engine as engine
import subtitle_tool_core as core
import translation_remux as remux
import translation_review as review
from tests.test_subtitle_fonts_pipeline import actual_events, actual_private

APP = QApplication.instance() or QApplication([])


class DialogueLayoutCompatibilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.shared = tempfile.TemporaryDirectory(prefix='subflow-layout-compat-video-')
        cls.addClassCleanup(cls.shared.cleanup)
        cls.video = Path(cls.shared.name) / 'fixture.mkv'
        core.run_command([
            core.FFMPEG, '-hide_banner', '-loglevel', 'error', '-y',
            '-f', 'lavfi', '-i', 'testsrc2=size=640x360:rate=24',
            '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=16000',
            '-t', '6', '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '18',
            '-c:a', 'pcm_s16le', str(cls.video),
        ])

    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='subflow-layout-compat-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.output = self.root / 'fixture.SF.mkv'
        self.work = remux.work_directory(self.output)
        self.work.mkdir()
        self.messages = []
        # Keep remux's optional moved-file lookup entirely within this fixture.
        locations = patch.object(remux, 'LOCATION_FILE', self.root / 'locations.json')
        locations.start()
        self.addCleanup(locations.stop)
        self.target = self.work / 'translated-de.srt'
        self.cache = self.work / 'translation-cache-de.jsonl'
        self.source_cues = [core.SubtitleEvent(
            '00:00:01,001', '00:00:04,057', 'We should go home now.')]
        self.initial = 'Wir müssen\njetzt nach\nHause gehen.'
        self.edited = 'Wir sollten\nheute nach\nHause gehen.'
        issue = dict(index=1, start=self.source_cues[0].start,
                     end=self.source_cues[0].end, source=self.source_cues[0].text,
                     translation=self.initial, reason='Compatibility fixture',
                     status='needs_review', repair_attempts=0)
        review.save_progress(self.source_cues, 'de', 'en', self.cache, self.target,
                             {1: issue}, complete=True,
                             translations={1: self.initial})
        self.report = review.report_path(self.cache)

    @staticmethod
    def digest(path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    def subtitle_packets(self):
        media = core.inspect_media(str(self.output))
        track = next(t for t in media['tracks'] if t['type'] == 'subtitles')
        destination = self.root / ('subtitle-readback-' + str(len(list(
            self.root.glob('subtitle-readback-*.mkv')))) + '.mkv')
        core.run_command([
            core.MKVMERGE, '-o', str(destination), '--no-audio', '--no-video',
            '--no-attachments', '--no-chapters', '--no-global-tags',
            '--no-track-tags', '--subtitle-tracks', str(track['id']),
            str(self.output),
        ], log=self.messages.append)
        return actual_events(destination), actual_private(destination)

    def first_mux(self, version):
        media = core.inspect_media(str(self.video))
        automatic_size = engine.automatic_size

        def calibrate(*args, **kwargs):
            size, metrics = automatic_size(*args, **kwargs)
            return size, dict(metrics, layout_version=version)

        settings = dict(font_id='builtin:rubik', size_mode='auto', size=0,
                        color='#FFE0A3')
        with patch.object(engine, 'automatic_size', side_effect=calibrate), \
             fonts.processing({'de': settings}, self.work, self.output):
            prepared = fonts.prepare_mux(
                self.video, self.output, media, [],
                [(self.target, 'de', core.LANGUAGES['de'][2] + ' Auto', True)],
                {}, None, self.messages.append, None)
        styled = prepared['generated'][0][0]
        receipt = fonts.styled_receipt(self.work, self.target)
        self.assertEqual(receipt['style']['dialogue_layout'], version)
        chapters = self.root / 'chapters.txt'
        chapters.write_text('CHAPTER01=00:00:00.000\nCHAPTER01NAME=Fixture start\n'
                            'CHAPTER02=00:00:03.000\nCHAPTER02NAME=Fixture end\n',
                            encoding='utf8')
        tags = self.root / 'tags.xml'
        tags.write_text('<Tags><Tag><Targets/><Simple><Name>TITLE</Name>'
                        '<String>Layout compatibility fixture</String>'
                        '<TagLanguage>eng</TagLanguage></Simple></Tag></Tags>',
                        encoding='utf8')
        attachment = self.root / 'metadata-proof.txt'
        # This real attachment also keeps the tiny fixture above the normal
        # production minimum-size guard; that guard is never bypassed.
        attachment.write_bytes(b'layout compatibility metadata proof\n' * 65536)
        args = [core.MKVMERGE, '-o', str(self.output), '--chapters', str(chapters),
                '--global-tags', str(tags), '--no-subtitles', str(self.video),
                '--language', '0:de', '--track-name',
                '0:' + core.LANGUAGES['de'][2] + ' Auto',
                '--default-track-flag', '0:yes', '--hearing-impaired-flag',
                '0:yes', str(styled), '--attachment-name', 'metadata-proof.txt',
                '--attachment-mime-type', 'text/plain', '--attach-file',
                str(attachment)]
        self.fonts = prepared['fonts']
        for font in self.fonts:
            args += ['--attachment-name', Path(font['path']).name,
                     '--attachment-mime-type', 'application/x-truetype-font',
                     '--attach-file', str(font['path'])]
        core.run_command(args, log=self.messages.append)
        self.assertGreater(self.output.stat().st_size, 1024 * 1024)
        self.assertEqual(remux.register_output(
            self.output, self.work, ['de'], self.messages.append), 1)
        self.before_media = core.inspect_media(str(self.output))
        self.before_headers = mkv_fast_mux.read_headers(self.output)
        self.before_receipt = copy.deepcopy(receipt)
        self.before_private = base64.b64decode(receipt['private'])
        self.before_tags = self.root / 'tags-before.xml'
        core.run_command([core.MKVEXTRACT, str(self.output), 'tags',
                          str(self.before_tags)])
        # Counts of the replaced subtitle's bytes/frames legitimately change.
        # Independently normalize only those statistics; global semantic tags
        # and all metadata belonging to untouched video/audio stay exact.
        tree = ET.parse(self.before_tags)
        statistics = {'BPS', 'DURATION', 'NUMBER_OF_BYTES', 'NUMBER_OF_FRAMES',
                      '_STATISTICS_WRITING_APP', '_STATISTICS_WRITING_DATE_UTC',
                      '_STATISTICS_TAGS'}
        for tag in list(tree.getroot()):
            if str(receipt['track_uid']) not in {
                    node.text for node in tag.findall('./Targets/TrackUID')}:
                continue
            for simple in list(tag.findall('Simple')):
                if simple.findtext('Name') in statistics:
                    tag.remove(simple)
            if not tag.findall('Simple'):
                tree.getroot().remove(tag)
        tree.write(self.before_tags, encoding='utf-8', xml_declaration=True)
        events, private = self.subtitle_packets()
        self.assertEqual(private, self.before_private)
        self.assertEqual({e[:2] for e in events}, {(1001, 4057)})
        return events

    def assert_preserved_after_edit(self, version, expected_lines):
        review.save_corrections(self.report, {1: self.edited})
        with patch.object(engine, 'automatic_size',
                          side_effect=AssertionError('Edit recalibrated the saved B size')), \
             patch.object(core, 'translate_events',
                          side_effect=AssertionError('Edit requested model translation')):
            result = remux.remux_corrected_output(self.output, self.messages.append)
        self.assertEqual(Path(result), self.output)
        events, private = self.subtitle_packets()
        self.assertEqual(len(events), expected_lines)
        self.assertEqual({e[:2] for e in events}, {(1001, 4057)})
        expected = self.edited.replace('\n', ' ' if version == 'visible-b2' else '')
        self.assertEqual(''.join(engine.plain(e[2].split(',', 8)[8])
                                 for e in events), expected)
        self.assertEqual(private, self.before_private)
        manifest, _ = remux.load_bound_reports(self.output)
        saved = manifest['bindings'][0]['font_style']
        self.assertEqual(saved['style'], self.before_receipt['style'])
        self.assertEqual(saved['style']['dialogue_layout'], version)
        self.assertEqual(saved['private'], self.before_receipt['private'])
        self.assertEqual(saved['source_sha256'], self.digest(self.target))
        self.assertEqual(saved['track_uid'], self.before_receipt['track_uid'])
        after_media = core.inspect_media(str(self.output))
        after_headers = mkv_fast_mux.read_headers(self.output)
        old_track = next(t for t in self.before_media['tracks']
                         if t['type'] == 'subtitles')
        remux._verify_preserved(self.before_media, after_media,
                               self.before_headers, after_headers,
                               {old_track['id']: object()})
        self.assertEqual(after_headers['attachments'],
                         self.before_headers['attachments'])
        self.assertEqual(after_headers['chapters'], self.before_headers['chapters'])
        after_tags = self.root / 'tags-after.xml'
        core.run_command([core.MKVEXTRACT, str(self.output), 'tags', str(after_tags)])
        self.assertEqual(remux._canonical_tags(after_tags),
                         remux._canonical_tags(self.before_tags))
        # Inspect the actual embedded font bytes instead of trusting names.
        expected_fonts = {self.digest(font['path']) for font in self.fonts}
        actual_fonts = set()
        for attachment in after_media.get('attachments', []):
            if attachment.get('file_name') == 'metadata-proof.txt':
                continue
            destination = self.root / ('attachment-' + str(attachment['id']))
            core.run_command([core.MKVEXTRACT, str(self.output), 'attachments',
                              str(attachment['id']) + ':' + str(destination)])
            actual_fonts.add(self.digest(destination))
        self.assertTrue(expected_fonts)
        self.assertTrue(expected_fonts <= actual_fonts)

    def test_new_b2_first_mux_and_actual_edit_reflow_preserve_metadata(self):
        events = self.first_mux('visible-b2')
        self.assertEqual(len(events), 1)
        self.assertEqual(engine.plain(events[0][2].split(',', 8)[8]),
                         self.initial.replace('\n', ' '))
        self.assert_preserved_after_edit('visible-b2', 1)

    def test_existing_b1_first_mux_and_actual_edit_keep_old_hardbreak_layout(self):
        events = self.first_mux('visible-b1')
        self.assertEqual(len(events), 3)
        self.assertEqual(''.join(engine.plain(e[2].split(',', 8)[8])
                                 for e in events), self.initial.replace('\n', ''))
        self.assert_preserved_after_edit('visible-b1', 3)

    def test_actual_failed_edit_keeps_output_and_manifest_and_saved_correction(self):
        self.first_mux('visible-b2')
        review.save_corrections(self.report, {1: self.edited})
        output_before = self.digest(self.output)
        manifest = self.work / remux.MANIFEST
        manifest_before = manifest.read_bytes()
        original_verify = remux._verify_preserved

        def fail_after_real_validation(*args, **kwargs):
            original_verify(*args, **kwargs)
            raise ValueError('Injected publication failure after real metadata check')

        with patch.object(remux, '_verify_preserved',
                          side_effect=fail_after_real_validation):
            with self.assertRaisesRegex(ValueError, 'Injected publication failure'):
                remux.remux_corrected_output(self.output, self.messages.append)
        self.assertEqual(self.digest(self.output), output_before)
        self.assertEqual(manifest.read_bytes(), manifest_before)
        self.assertEqual(review.load_report(self.report)['issues'][0]['translation'],
                         self.edited)
        self.assertFalse(list(self.root.glob('.translation-remux-*')))

    def test_english_style_and_speaker_boundaries_keep_distinct_groups(self):
        info = fonts.resolve_font(dict(font_id='builtin:rubik'))
        size, metrics = engine.automatic_size(
            info['path'], ['Keep watching that door.'], 1280, 720)
        style = dict(info, size=size, size_mode='auto', color='#FFFFFF',
                     canvas_width=1280, canvas_height=720,
                     dialogue_layout='visible-b2')
        groups = (
            ('<i>Keep watching\nthat door.</i>\nI am watching\nthis one.',
             ['Keep watching that door.', 'I am watching this one.']),
            ('- Keep watching\n- I am watching',
             ['- Keep watching', '- I am watching']),
            ('JOHN: Keep watching\nMARY: I am watching',
             ['JOHN: Keep watching', 'MARY: I am watching']),
        )
        for body, expected in groups:
            with self.subTest(body=body):
                events = [(1001, 4057, '0,0,Default,,0,0,0,,' + fonts.ass_text(body))]
                events, private, _ = engine.update(
                    fonts.header(style), events, style, 1280, 720)
                result = fonts.layout_styled_events(
                    events, style, private, raw=info['path'], work=self.root)
                self.assertEqual([engine.plain(e[2].split(',', 8)[8]) for e in result],
                                 expected)
                self.assertEqual({e[:2] for e in result}, {(1001, 4057)})
                if body.startswith('<i>'):
                    self.assertIn(r'\i1', result[0][2])
                    self.assertIn(r'\i0', result[1][2])


if __name__ == '__main__':
    unittest.main()
