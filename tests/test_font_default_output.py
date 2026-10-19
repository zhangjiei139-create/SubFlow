"""Default sizing works without editor interaction on real SRT/ASS subtitle files."""
import os
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
import tempfile,unittest,json
from pathlib import Path
from unittest.mock import patch
from PySide6.QtWidgets import QApplication
import subtitle_fonts as fonts
import subtitle_tool_core as core
from profile_model import effective_subtitle_styles
from tests.test_subtitle_fonts_pipeline import actual_private,actual_events
APP=QApplication.instance() or QApplication([])
TOOLS=Path('C:/Program Files/SubFlow/_internal/tools')
class DefaultSizingOutputTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name);self.work=self.root/'work';self.out=self.root/'out.mkv';self.messages=[]
  for name,value in [('MKVMERGE',str(TOOLS/'mkvtoolnix/mkvmerge.exe')),('FFMPEG',str(TOOLS/'ffmpeg/bin/ffmpeg.exe')),('MKVEXTRACT',str(TOOLS/'mkvtoolnix/mkvextract.exe'))]:
   p=patch.object(core,name,value);p.start();self.addCleanup(p.stop)
 def prepare(self,source,media,kept,generated,styles):
  with fonts.processing(styles,self.work,self.out):return fonts.prepare_mux(source,self.out,media,kept,generated,{},None,self.messages.append,None)
 def test_unconfigured_srt_automatically_sized_without_any_font_editor_save(self):
  source=self.root/'new.srt';source.write_text('1\n00:00:01,001 --> 00:00:02,033\nDefault English dialogue.\n',encoding='utf8')
  media=dict(tracks=[dict(type='video',id=99,properties=dict(pixel_dimensions='1280x720'))],attachments=[])
  result=self.prepare(source,media,[],[(source,'en','英文 Auto',False)],effective_subtitle_styles({},['en']))
  receipt=fonts.styled_receipt(self.work,source)
  self.assertEqual(receipt['style']['size_mode'],'auto');self.assertGreater(receipt['style']['size'],0)
  self.assertEqual(result['fonts'],[])
  self.assertEqual(actual_events(result['generated'][0][0])[0][:2],(1001,2033))
 def test_unconfigured_ass_with_uninstalled_original_family_still_gets_default(self):
  source=self.root/'original.mkv';original=dict(family='Unavailable Original Film Font',size=19,color='#ABCDEF')
  fonts.write_subtitle_mkv(source,[(1001,2033,'Default dialogue.')],original,'en',uid=712)
  media=core.inspect_media(str(source));media['tracks'].append(dict(type='video',id=99,properties=dict(pixel_dimensions='1280x720')))
  result=self.prepare(source,media,[0],[],effective_subtitle_styles({},['en']))
  private=actual_private(result['generated'][0][0]).decode('utf8')
  self.assertIn('Unavailable Original Film Font',private);self.assertIn('&H00EFCDAB',private)
  self.assertEqual(actual_events(result['generated'][0][0])[0][:2],(1001,2033))
  self.assertTrue(any('估算默认字号' in text for text in self.messages))
 def test_old_default38_srt_gets_adaptive_and_keeps_selected_face_and_color(self):
  source=self.root/'new.srt';source.write_text('1\n00:00:01,001 --> 00:00:02,033\nDefault English dialogue.\n',encoding='utf8')
  media=dict(tracks=[dict(type='video',id=99,properties=dict(pixel_dimensions='1280x720'))],attachments=[])
  old={'en':dict(font_id='builtin:rubik',size_mode='manual',size=38,color='#AABBCC')}
  result=self.prepare(source,media,[],[(source,'en','英文 Auto',False)],effective_subtitle_styles(old,['en']))
  receipt=fonts.styled_receipt(self.work,source);self.assertEqual(receipt['style']['size_mode'],'auto')
  self.assertEqual(receipt['style']['font_id'],'builtin:rubik');self.assertEqual(receipt['style']['color'],'#AABBCC')
 def test_explicit_user38_srt_is_exactly38_and_never_adapted(self):
  source=self.root/'new.srt';source.write_text('1\n00:00:01,001 --> 00:00:02,033\nManually chosen 38.\n',encoding='utf8')
  media=dict(tracks=[dict(type='video',id=99,properties=dict(pixel_dimensions='1280x720'))],attachments=[])
  settings={'en':dict(font_id='',size_mode='manual',size=38,size_origin='user',color='keep')}
  with patch('subtitle_style_engine.automatic_size',side_effect=AssertionError('manual size adapted')):
   result=self.prepare(source,media,[],[(source,'en','英文 Auto',False)],effective_subtitle_styles(settings,['en']))
  self.assertIn(',38,',actual_private(result['generated'][0][0]).decode('utf8'))
if __name__=='__main__':unittest.main()
