import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import subtitle_tool_core as core
import translation_remux as remux
import translation_review as review


class TranslationRemuxSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.output = self.root / 'movie.SF.mkv'
        self.output.write_bytes(b'valid-old-movie')
        self.work = remux.work_directory(self.output)
        self.work.mkdir()
        self.events = [core.SubtitleEvent('00:00:01,000','00:00:04,000','Think differently.')]
        self.target = self.work / 'translated-de.srt'
        self.cache = self.work / 'translation-cache-de.jsonl'
        issue = dict(index=1,start=self.events[0].start,end=self.events[0].end,
                     source=self.events[0].text,translation='Denke anders框.',reason='含中文',
                     status='needs_review',repair_attempts=2)
        review.save_progress(self.events,'de','en',self.cache,self.target,{1:issue},
                             complete=True,translations={1:issue['translation']})
        self.props = dict(uid=93,number=1,codec_id='S_TEXT/UTF8',language='ger',
                          language_ietf='de',track_name=core.LANGUAGES['de'][2]+' Auto',
                          default_track=True,forced_track=True,enabled_track=True)
        self.media = dict(tracks=[dict(id=0,type='subtitles',properties=self.props)])
        with patch.object(core,'inspect_media',return_value=self.media):
            remux.register_output(self.output,self.work,['de'])
        self.report = review.report_path(self.cache)
        review.save_corrections(self.report,{1:'Denke anders.'})
        self.merge = self.root/'mkvmerge.exe'; self.merge.write_bytes(b'tool')
        self.editor = self.root/'mkvpropedit.exe'; self.editor.write_bytes(b'tool')
        self.calls = []

    def _run(self,args,**kwargs):
        self.calls.append(args)
        if args[0] == str(self.merge):
            Path(args[args.index('-o')+1]).write_bytes(b'valid-new-movie')
        if 'tags' in args:
            Path(args[-1]).write_text('<Tags/>',encoding='utf8')

    def _execute(self, failure=None, cancel=None, command=None):
        with patch.object(core,'MKVMERGE',str(self.merge)), \
             patch.object(core,'inspect_media',return_value=self.media), \
             patch.object(core,'run_command',side_effect=command or self._run), \
             patch.object(core,'ensure_mux_disk_space'), \
             patch.object(core,'validate_media_output'), \
             patch.object(remux.mkv_fast_mux,'read_headers',return_value={}), \
             patch.object(remux,'_verify_preserved',side_effect=failure), \
             patch.object(core,'translate_events',side_effect=AssertionError('retranslated')):
            return remux.remux_corrected_output(self.output,cancel=cancel)

    def test_valid_remux_replaces_only_after_validation_and_refreshes_receipt(self):
        self._execute()
        self.assertEqual(self.output.read_bytes(),b'valid-new-movie')
        manifest,_ = remux.load_bound_reports(self.output)
        self.assertEqual(manifest['bindings'][0]['muxed_sha256'],remux._hash(self.target))
        merge = next(args for args in self.calls if args[0] == str(self.merge))
        self.assertIn('--track-order',merge)
        self.assertEqual(merge[merge.index('--track-order')+1],'1:0')
        self.assertIn(str(self.output.resolve()),merge)
        self.assertNotIn('--engage',merge)
        edit = next(args for args in self.calls if args[0] == str(self.editor))
        self.assertIn('track-uid=93',edit)
        self.assertIn('--tags',edit)

    def test_metadata_failure_preserves_old_output_and_saved_edits_for_retry(self):
        with self.assertRaisesRegex(ValueError,'metadata'):
            self._execute(failure=ValueError('metadata changed'))
        self.assertEqual(self.output.read_bytes(),b'valid-old-movie')
        self.assertEqual(review.load_report(self.report)['issues'][0]['translation'],'Denke anders.')
        remux.load_bound_reports(self.output)
        self.assertFalse(list(self.root.glob('.translation-remux-*')))

    def test_external_tool_failure_preserves_old_output(self):
        def fail(args,**kwargs):
            if args[0] == str(self.merge): raise RuntimeError('disk full')
            return self._run(args,**kwargs)
        with self.assertRaisesRegex(RuntimeError,'disk full'):
            self._execute(command=fail)
        self.assertEqual(self.output.read_bytes(),b'valid-old-movie')

    def test_cancel_does_not_run_mux_or_replace_output(self):
        stop = threading.Event(); stop.set()
        with self.assertRaises(core.CancelledError): self._execute(cancel=stop)
        self.assertEqual(self.output.read_bytes(),b'valid-old-movie')
        self.assertFalse(self.calls)

    def test_changed_output_is_rejected_before_any_tool(self):
        self.output.write_bytes(b'external-new-movie')
        with self.assertRaisesRegex(ValueError,'成品已变化'): self._execute()
        self.assertEqual(self.output.read_bytes(),b'external-new-movie')
        self.assertFalse(self.calls)

    def test_ambiguous_same_language_auto_tracks_do_not_enable_editing(self):
        media = dict(tracks=[self.media['tracks'][0],dict(self.media['tracks'][0],id=1)])
        with patch.object(core,'inspect_media',return_value=media):
            remux.register_output(self.output,self.work,['de'])
        self.assertFalse(remux.available(self.output))

    def test_default_tag_language_is_equivalent_but_real_changes_are_rejected(self):
        expected=self.root/'tags-before.xml'
        actual=self.root/'tags-after.xml'
        before='<Tags><Tag><Targets><TrackUID>93</TrackUID></Targets><Simple><Name>TITLE</Name><String>Title</String></Simple></Tag></Tags>'
        expected.write_text(before,encoding='utf8')
        actual.write_text(before.replace('</Simple>','<TagLanguageIETF>und</TagLanguageIETF></Simple>'),encoding='utf8')
        self.assertEqual(remux._canonical_tags(expected),remux._canonical_tags(actual))
        for change in (before.replace('Title','Other'),before.replace('93','94'),
                       before.replace('</Simple>','<TagLanguageIETF>en</TagLanguageIETF></Simple>'),
                       '<Tags/>'):
            actual.write_text(change,encoding='utf8')
            self.assertNotEqual(remux._canonical_tags(expected),remux._canonical_tags(actual))
        before=before.replace('</Simple>','<TagLanguage>eng</TagLanguage></Simple>')
        expected.write_text(before,encoding='utf8')
        actual.write_text(before.replace('</Simple>','<TagLanguageIETF>und</TagLanguageIETF></Simple>'),encoding='utf8')
        self.assertNotEqual(remux._canonical_tags(expected),remux._canonical_tags(actual))


if __name__ == '__main__': unittest.main()
