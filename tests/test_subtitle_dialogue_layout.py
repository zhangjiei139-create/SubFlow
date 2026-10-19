import base64,json,os,pathlib,re,tempfile,unittest
os.environ.setdefault('QT_QPA_PLATFORM','offscreen')
from unittest.mock import patch
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QRawFont
import subtitle_style_engine as engine,subtitle_fonts as fonts,subtitle_tool_core as core
from tests.test_subtitle_fonts_pipeline import actual_events,actual_private
APP=QApplication.instance() or QApplication([])
FONT=fonts.assets_root()/'lxgw/LXGWWenKaiGBScreen.ttf'
LONG='We need to get everyone back to the ship before the next storm reaches us, because there will not be another chance to leave.'
QUOTE="It's just the belt's a bit closer\nthan I thought."

def settings(path=FONT,text=LONG,width=1920,height=1080):
    info=fonts.font_info(path);size,metrics=engine.automatic_size(path,[fonts.ass_text(text)],width,height)
    return dict(info,size=size,size_mode='auto',color='#FFFFFF',canvas_width=round(width/height*720),canvas_height=720,dialogue_layout=metrics['layout_version'])

def packet(body,start=1001,end=4057,index=0):return(start,end,f'{index},0,Default,,0,0,0,,'+body)
def visible(events):return ''.join(engine.plain(e[2].split(',',8)[8]) for e in events)

class BDialogueLayoutTests(unittest.TestCase):
    def test_long_words_and_CJK_wrap_at_same_B_size_without_losing_any_text_or_milliseconds(self):
        for text in (LONG, 'LongWordWithoutAnyBreak'*12, '超长对白应该避开句首的标点符号，这样阅读起来才会自然。'*5,
                     'First paragraph.\n'+LONG, '<i>'+LONG+'</i>', '<font color="red">'+LONG+'</font>'):
            with self.subTest(text=text[:32]):
                style=settings(text=text);source=[packet(fonts.ass_text(text))]
                normalized,private,_=engine.update(fonts.header(style),source,style,1920,1080)
                output,report=engine.layout_dialogue(normalized,style,FONT)
                self.assertEqual(visible(output).replace('\n',''),engine.plain(source[0][2].split(',',8)[8]).replace('\n',''))
                self.assertEqual({event[:2] for event in output},{(1001,4057)})
                self.assertEqual([int(e[2].split(',',1)[0]) for e in output],list(range(len(output))))
                metrics=engine.DialogueMetrics(FONT)
                for event in output:
                    text_line=engine.plain(event[2].split(',',8)[8]);a,_,c,_,advance=metrics.ink(text_line,style['size'],ass=True)
                    self.assertLessEqual(max(c-a,advance),style['canvas_width']*.84+1e-6)
                    self.assertNotIn('\n',text_line)
                    self.assertEqual(set(re.findall(r'\\fs(?![cp])([\d.]+)',event[2])),{str(style['size'])})
                self.assertGreaterEqual(report['max_lines'],2)

    def test_width_problem_becomes_three_lines_instead_of_refusal_or_shrinking(self):
        style=settings();source=[packet(fonts.ass_text(LONG))]
        normalized,_,_=engine.update(fonts.header(style),source,style,1920,1080)
        output,report=engine.layout_dialogue(normalized,style,FONT)
        self.assertEqual(len(output),3)
        self.assertEqual(report['multiline_cues'],1)
        self.assertEqual(style['size'],settings()['size'])
        widths=[engine.DialogueMetrics(FONT).ink(engine.plain(e[2].split(',',8)[8]),style['size'],ass=True)[4] for e in output]
        self.assertGreater(min(widths)/max(widths),.65)
        for event in output:
            body=event[2].split(',',8)[8]
            if engine.plain(body).endswith(' '):self.assertTrue(body.endswith('{}'))

    def test_word_boundaries_CJK_punctuation_and_combining_sequences_are_kept(self):
        for text in (LONG,'这种问题应该修复，字幕不能在一行开头出现逗号、句号。'*5,
                     ('Café cafe\u0301 fiancé e\u0301lan ')*12):
            style=settings(text=text)
            output,_=engine.layout_dialogue([packet(fonts.ass_text(text))],style,FONT)
            lines=[engine.plain(e[2].split(',',8)[8]) for e in output]
            self.assertEqual(''.join(lines),text)
            self.assertFalse(any(line.lstrip().startswith(tuple('，。、！？；：）】》')) for line in lines))
            self.assertFalse(any(line and __import__('unicodedata').combining(line[0]) for line in lines))
            if text==LONG:self.assertEqual(' '.join(''.join(lines).split()),LONG)

    def test_italic_and_color_state_is_carried_to_later_lines(self):
        style=settings();body=r'{\i1\1c&H0000FF&}'+LONG+r'{\i0\1c&HFFFFFF&} End.'
        output,_=engine.layout_dialogue([packet(body)],style,FONT)
        for event in output[:-1]:
            self.assertIn(r'\i1',event[2]);self.assertIn(r'\1c&H0000FF&',event[2])
        self.assertIn(r'\i0',output[-1][2]);self.assertIn(r'\1c&HFFFFFF&',output[-1][2])
        self.assertEqual(visible(output),LONG+' End.')

    def test_literal_ASS_spellings_paths_and_braces_survive_automatic_reflow(self):
        text=(r'Literal \N \n \h {braces} C:\new\folder and normal words. ')*3
        style=settings(text=text)
        normalized,_,_=engine.update(fonts.header(style),[packet(fonts.ass_text(text))],style,1920,1080)
        output,_=engine.layout_dialogue(normalized,style,FONT)
        self.assertEqual(visible(output),text)

    def test_position_effect_drawing_karaoke_and_top_style_are_not_reflowed(self):
        style=settings();style['protected_styles']=['Sign']
        bodies=[r'{\an8\pos(100,50)}'+LONG,r'{\t(0,500,\frz30)}'+LONG,
                r'{\p1}m 0 0 l 50 0 50 50{\p0}'+LONG,r'{\k20}'+LONG]
        events=[packet(body,index=i) for i,body in enumerate(bodies)]
        events.append((1001,4057,'4,0,Sign,,0,0,0,,'+LONG))
        output,report=engine.layout_dialogue(events,style,FONT)
        self.assertEqual(output,events);self.assertEqual(report['protected_cues'],5)

    def test_overlapping_ordinary_cues_are_stacked_and_read_orders_are_unique(self):
        style=settings(text=QUOTE)
        events=[packet(fonts.ass_text(QUOTE),start=1001,end=4057),packet('Another overlapping line.',start=2003,end=5009,index=1)]
        for version,count,expected in ((engine.LEGACY_DIALOGUE_LAYOUT_VERSION,3,QUOTE.replace('\n','')),
                                       (engine.DIALOGUE_LAYOUT_VERSION,2,QUOTE.replace('\n',' '))):
            with self.subTest(version=version):
                output,_=engine.layout_dialogue(events,dict(style,dialogue_layout=version),FONT)
                self.assertEqual(len(output),count)
                first=[e for e in output if e[:2]==(1001,4057)]
                second=[e for e in output if e[:2]==(2003,5009)]
                self.assertEqual(visible(first),expected)
                self.assertEqual(visible(second),'Another overlapping line.')
                y=lambda e:float(re.search(r'\\pos\([^,]+,([^)]+)\)',e[2])[1])
                self.assertLess(y(second[0]),min(y(e) for e in first))
                self.assertEqual(len({e[2].split(',',1)[0] for e in output}),count)

    def test_manual_and_legacy_receipts_keep_their_previous_layout(self):
        events=[packet(fonts.ass_text(QUOTE))]
        for change in (dict(dialogue_layout=None),dict(dialogue_layout='old'),dict(size_mode='manual')):
            output,_=engine.layout_dialogue(events,dict(settings(),**change),FONT)
            self.assertEqual(output,events)

    def test_impossible_height_has_explicit_error_not_clipped_or_dropped_text(self):
        events=[packet('Very long subtitle '*200)]
        before=list(events)
        with self.assertRaisesRegex(ValueError,'安全高度'):
            engine.layout_dialogue(events,settings(),FONT)
        self.assertEqual(events,before)

    def test_full_caption_calibration_retains_B_when_input_contains_only_short_letters(self):
        for text in ('H','ooooo','Hgjpqy','12345'):
            size,report=engine.automatic_size(FONT,[text],1920,1080)
            self.assertLess(abs(size*report['glyph_ratio']/720-.05),.002)
        self.assertEqual(engine.automatic_size(FONT,[LONG],1920,1080)[0],engine.automatic_size(FONT,[LONG],3840,2160)[0])

    def test_real_pipeline_embedded_packets_and_receipt_support_multi_event_cues(self):
        with tempfile.TemporaryDirectory() as d:
            root=pathlib.Path(d);source=root/'translated.srt';source.write_text('1\n00:00:01,001 --> 00:00:04,057\n'+LONG+'\n',encoding='utf-8')
            original=source.read_bytes();messages=[]
            media=dict(tracks=[dict(id=0,type='video',properties=dict(pixel_dimensions='1920x1080'))],attachments=[])
            with fonts.processing({'en':dict(font_id='builtin:lxgw',size_mode='auto',size=0,color='#FFFFFF')},root/'work',root/'out.mkv'):
                prepared=fonts.prepare_mux(source,root/'out.mkv',media,[],[(source,'en','English Auto',True)],{},None,messages.append,None)
            result=prepared['generated'][0][0];events=actual_events(result);receipt=fonts.styled_receipt(root/'work',source)
            self.assertEqual(len(events),3);self.assertEqual(visible(events),LONG)
            self.assertEqual({e[:2] for e in events},{(1001,4057)})
            self.assertEqual(actual_private(result),base64.b64decode(receipt['private']))
            self.assertEqual(receipt['style']['dialogue_layout'],engine.DIALOGUE_LAYOUT_VERSION)
            import translation_remux
            translation_remux._validated_font_receipt(receipt,fonts.digest(source))
            self.assertTrue(any('超两行1条' in message for message in messages))
            self.assertEqual(source.read_bytes(),original)

    def test_translation_edit_reuses_B_size_and_reflows_long_sentence(self):
        from tests import test_translation_remux_styled_binding as helper_module
        import translation_remux as remux,translation_review as review
        helper=helper_module.StyledTranslationBindingTests(methodName='runTest');helper.setUp();self.addCleanup(helper.doCleanups)
        settings_value=dict(font_id='builtin:lxgw',size_mode='auto',size=0,color='#FFFFFF')
        media=dict(tracks=[dict(id=0,type='video',properties=dict(pixel_dimensions='1920x1080'))],attachments=[])
        with fonts.processing({'de':settings_value},helper.work,helper.output):
            fonts.prepare_mux(helper.target,helper.output,media,[],[(helper.target,'de','German Auto',True)],{},None,helper.logs.append,None)
        receipt=fonts.styled_receipt(helper.work,helper.target)
        helper.style=receipt['style'];helper.private=base64.b64decode(receipt['private'])
        helper.props['codec_private_data']=helper.private.hex();helper.props['uid']=receipt['track_uid']
        self.assertEqual(helper.register(),1)
        captured=[]
        for value in (LONG,'<i>'+LONG+'</i>'):
            review.save_corrections(helper.report,{1:value});helper.execute_edit(captured=captured)
        for record in captured:
            self.assertEqual(len(record['events']),3)
            self.assertEqual(visible(record['events']),LONG)
            self.assertEqual({e[:2] for e in record['events']},{(1001,4057)})
            self.assertEqual(record['style']['size'],receipt['style']['size'])
            self.assertEqual(record['private'],helper.private)

if __name__=='__main__':unittest.main()
