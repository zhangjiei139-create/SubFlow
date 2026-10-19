import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import contextvars
import io
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from PySide6.QtWidgets import QApplication
import subtitle_fonts as fonts
import mkv_fast_mux as fast
import profile_model as profiles
from subtitle_font_dialog import SubtitleFontDialog

APP=QApplication.instance() or QApplication([])

class FontSettingsTests(unittest.TestCase):
    def test_old_profiles_default_to_keep_style_and_new_styles_roundtrip(self):
        with tempfile.TemporaryDirectory() as folder,patch.object(profiles,'profile_file',return_value=Path(folder)/'profiles.json'):
            values=profiles.default_profiles(); values[0].subtitle_styles={'de':dict(font_id='builtin:rubik',size=34,color='#FFF0AA')}
            profiles.save_profiles(values)
            self.assertEqual(profiles.load_profiles()[0].subtitle_styles,fonts.normalize_styles(values[0].subtitle_styles))
            raw=json.loads(profiles.profile_file().read_text(encoding='utf8'))
            for row in raw: row.pop('subtitle_styles')
            profiles.profile_file().write_text(json.dumps(raw),encoding='utf8')
            self.assertTrue(all(p.subtitle_styles=={} for p in profiles.load_profiles()))

    def test_dialog_lists_only_current_profile_languages_and_preserves_other_settings(self):
        profile=profiles.PreferenceProfile(2,'My profile',subtitle_languages=['de','zh-CN'])
        profile.subtitle_styles={'en':dict(font_id='builtin:bree',size=33,color='#FFFFFF')}
        with patch('subtitle_font_dialog.system_choices',return_value=[]):
            dialog=SubtitleFontDialog(profile)
            self.assertEqual(list(dialog.rows),['de','zh-CN'])
            dialog.rows['de'][0].setCurrentIndex(4) # Rubik
            dialog.rows['de'][1].setValue(35)
            dialog.save()
            self.assertEqual(dialog.result_styles['de']['font_id'],'builtin:rubik')
            self.assertEqual(dialog.result_styles['de']['size'],35)
            self.assertEqual(dialog.result_styles['en'],fonts.normalize_styles(profile.subtitle_styles)['en'])
            self.assertEqual(dialog.result_styles['zh-CN']['size_mode'],'auto')
            self.assertEqual(dialog.result_styles['zh-CN']['font_id'],'')

    def test_empty_profile_has_no_font_rows(self):
        with patch('subtitle_font_dialog.system_choices',return_value=[]):
            dialog=SubtitleFontDialog(profiles.PreferenceProfile(4,'Empty',subtitle_languages=[]))
            self.assertEqual(dialog.rows,{})
            self.assertFalse(dialog.save_button.isEnabled()); dialog.reject()

    def test_font_files_and_language_checks(self):
        rubik=fonts.resolve_font(dict(font_id='builtin:rubik'))
        self.assertEqual(fonts.missing_characters(rubik['path'],'ÄÖÜäöüß'),[])
        self.assertTrue(fonts.missing_characters(rubik['path'],'中文字幕'))
        ma=fonts.resolve_font(dict(font_id='builtin:ma'))
        self.assertTrue(fonts.missing_characters(ma['path'],'測試體'))
        for ident,_,_ in fonts.BUILTINS:
            info=fonts.resolve_font(dict(font_id='builtin:'+ident))
            self.assertTrue(info['family'])

    def test_invalid_file_and_changed_font_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            bad=Path(folder)/'bad.ttf'; bad.write_bytes(b'not a font')
            with self.assertRaises(ValueError): fonts.font_info(bad)
        info=fonts.resolve_font(dict(font_id='builtin:rubik'))
        with self.assertRaises(ValueError): fonts.resolve_font(dict(font_id='import:any',path=info['path'],sha256='wrong'))

    def test_bad_settings_do_not_enable_styling(self):
        self.assertEqual(fonts.normalize_styles({'de':dict(font_id='builtin:rubik',size=999,color='#FFFFFF')}),{})
        self.assertEqual(fonts.normalize_styles({'de':dict(font_id='builtin:rubik',size=38,color='malicious')}),{})
        self.assertEqual(fonts.normalize_styles(None),{})

    def test_worker_context_isolated_and_restored_on_failure(self):
        events=[]
        def other(): events.append(fonts.CONTEXT.get())
        with self.assertRaises(RuntimeError):
            with fonts.processing({'de':dict(font_id='builtin:rubik',size=34,color='#FFFFFF')},'work','x.mkv'):
                worker=threading.Thread(target=other); worker.start(); worker.join()
                self.assertIn('de',fonts.CONTEXT.get()['styles']); raise RuntimeError()
        self.assertIsNone(fonts.CONTEXT.get()); self.assertEqual(events,[None])

    def test_plain_text_cannot_inject_ass_commands(self):
        self.assertEqual(fonts.ass_text('<i>Test</i>\nnext'),r'{\i1}Test{\i0}\Nnext')
        self.assertEqual(fonts.ass_text(r'{\pos(1,2)}'),r'\{\pos(1,2)\}')
        self.assertEqual(fonts.ass_text(r'C:\temp\folder'),r'C:\temp\folder')
        self.assertEqual(fonts.ass_text(r'literal \N'),'literal \\\u2060N')

class FontAttachmentTests(unittest.TestCase):
    def fixture(self,payload,mime=b'font/ttf'):
        fields=fonts.text(0x466E,'font.ttf')+fonts.element(0x4660,mime)+fonts.element(0x465C,payload)+fonts.uint(0x46AE,123)
        return fonts.element(0x61A7,fields)

    def test_large_font_reading_is_bounded_and_hashes_detect_changed_payload(self):
        data=self.fixture(b'X'*(17*1024*1024))
        class Bounded(io.BytesIO):
            def read(self,size=-1):
                if size<0 or size>1024*1024: raise AssertionError('unbounded read')
                return super().read(size)
        first=fast._attachment_headers(Bounded(data),len(data))
        second_data=self.fixture(b'X'*(17*1024*1024-1)+b'Y')
        second=fast._attachment_headers(Bounded(second_data),len(second_data))
        self.assertNotEqual(first,second)

    def test_equivalent_font_mime_types_only_are_normalized(self):
        one=self.fixture(b'font',b'font/ttf'); two=self.fixture(b'font',b'application/x-truetype-font')
        self.assertEqual(fast._attachment_headers(io.BytesIO(one),len(one)),fast._attachment_headers(io.BytesIO(two),len(two)))
        other=self.fixture(b'font',b'application/octet-stream')
        self.assertNotEqual(fast._attachment_headers(io.BytesIO(one),len(one)),fast._attachment_headers(io.BytesIO(other),len(other)))

    def test_truncated_or_over_limit_attachment_rejected_before_large_read(self):
        with self.assertRaises(fast.HeaderCheckError): fast._attachment_headers(io.BytesIO(),fast.LIMITS[fast.ATTACHMENTS]+1)
        data=self.fixture(b'font')
        with self.assertRaises(fast.HeaderCheckError): fast._attachment_headers(io.BytesIO(data[:-1]),len(data))

if __name__=='__main__': unittest.main()
