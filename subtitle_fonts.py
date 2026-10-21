"""Per-language text styling; preserve packet timestamps and original media.

The processing context is local to one movie's worker, never a global preference.
"""
from __future__ import annotations
import base64
import contextvars
import hashlib
import json
import os
import re
import secrets
import sys
from contextlib import contextmanager
from pathlib import Path
from profile_model import normalize_language

MAX_FONT_BYTES = 64 * 1024 * 1024
CONTEXT = contextvars.ContextVar('subtitle_font_preferences', default=None)
BUILTINS = [
    ('lxgw', '霞鹜文楷屏幕版', 'LXGWWenKaiGBScreen.ttf'),
    ('chill', '寒蝉全圆体', 'ChillRoundF v3.0.ttf'),
    ('ma', '毛笔楷体（简体）', 'MaShanZheng-Regular.ttf'),
    ('rubik', 'Rubik', 'Rubik[wght].ttf'),
    ('bree', 'Bree Serif', 'BreeSerif-Regular.ttf'),
]
SAMPLES = {'zh-CN': '别担心，我们一定能找到回家的路。',
           'zh-TW': '別擔心，我們一定能找到回家的路。',
           'en': 'Every choice opens a new possibility.',
           'de': 'Überraschung: ä, ö, ü und ß. Wir finden den Weg zurück.',
           'es': 'Una nueva oportunidad: ¿dónde estás?', 'fr': 'Une nouvelle possibilité, où êtes-vous ?',
           'pt': 'Uma nova possibilidade: coração e ação.', 'ja': '心配しないで、家に帰る道を探そう。',
           'ko': '걱정하지 마세요. 집으로 가는 길을 찾을 거예요.',
           'ru': 'Мы обязательно найдём дорогу домой.', 'hi': 'हम घर जाने का रास्ता खोज लेंगे।'}

def assets_root():
    return Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent)) / 'assets/subtitle-fonts'

def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024*1024), b''): h.update(block)
    return h.hexdigest()

def font_info(path):
    from PySide6.QtGui import QRawFont,QFont
    path = Path(path).resolve()
    if path.suffix.lower() not in {'.ttf', '.otf'} or not 0 < path.stat().st_size <= MAX_FONT_BYTES:
        raise ValueError('请选择不超过64 MiB的 TTF 或 OTF 字体文件。')
    raw = QRawFont(str(path), 32)
    if not raw.isValid() or not raw.familyName(): raise ValueError('这个文件不是有效的字体。')
    from subtitle_style_engine import player_family
    family = player_family(path)
    if any(c in family for c in ',\n\r'): raise ValueError('字体名称不适合字幕样式。')
    return dict(path=str(path), family=family, ui_family=raw.familyName(), sha256=digest(path),
                bold=raw.weight()>=600,italic=raw.style()!=QFont.StyleNormal)

def missing_characters(path, text):
    from PySide6.QtGui import QRawFont
    import unicodedata
    raw = QRawFont(str(path), 32)
    if not raw.isValid(): raise ValueError('字体文件无效或已损坏。')
    return sorted({c for c in text if not c.isspace() and unicodedata.category(c) not in {'Cc','Cf'} and not raw.supportsCharacter(ord(c))})


def glyph_plan(info, text):
    """Cover only absent glyphs with bundled, attachable fonts; fail if uncovered."""
    missing = missing_characters(info['path'], text)
    replacements = {}
    candidates = []
    for ident in ('rubik', 'lxgw', 'chill') if missing else ():
        candidate = resolve_font({'font_id': 'builtin:' + ident})
        candidate['font_id'] = 'builtin:' + ident
        candidates.append((candidate, set(missing_characters(candidate['path'], ''.join(missing)))))
    for char in missing:
        font = next((font for font, absent in candidates if char not in absent), None)
        if font is None:
            raise ValueError('所选字体及内置补字字体均缺字：' + char + '；请更换字体。')
        replacements[char] = font
    return replacements


def preflight_text(code, text, log):
    """Check known source text before spending time translating the film."""
    context = CONTEXT.get()
    style = context['styles'].get(normalize_language(code)) if context else None
    if not style or not style.get('font_id'):
        return
    info = resolve_font(style)
    replacements = glyph_plan(info, text)
    if replacements:
        names = '、'.join(dict.fromkeys(font['family'] for font in replacements.values()))
        log(f'字体预检：{info["family"]}缺少“{"".join(replacements)}”，仅缺字使用{names}补齐；其余文字保留所选字体。')


def _literal_units(value):
    """Keep ASS escapes and combining sequences together when selecting a font."""
    import unicodedata
    cursor = 0
    while cursor < len(value):
        start = cursor
        if value.startswith('\\\u2060', cursor) and cursor + 2 < len(value) and value[cursor+2] in 'Nnh':
            cursor += 3
            yield value[start:cursor], '\\' + value[cursor-1]
            continue
        if value[cursor] == '\\' and cursor+1 < len(value) and value[cursor+1] in 'Nnh{}':
            cursor += 2
            yield value[start:cursor], {'N': '\n', 'n': ' ', 'h': ' '}.get(value[cursor-1], value[cursor-1])
            continue
        cursor += 1
        while cursor < len(value) and (unicodedata.category(value[cursor]).startswith('M')
                or value[cursor] == '\u200d' or value[cursor-1] == '\u200d'):
            cursor += 1
        yield value[start:cursor], value[start:cursor]


def apply_glyph_fallbacks(events, info, style, replacements):
    """Add explicit font runs after style normalization, preserving text/timing."""
    if not replacements:
        return events
    from subtitle_style_engine import _runs, _tags, ass_font_scale, plain
    from PySide6.QtGui import QRawFont
    import unicodedata
    primary = info['family']
    primary_scale = ass_font_scale(info['path'])
    raw_fonts = {font['sha256']: QRawFont(font['path'], 100) for font in replacements.values()}
    primary_raw = QRawFont(info['path'], 100)
    sizes = {}
    def fallback_size(font, visible):
        # Match the base letter's visible height (e -> é), rather than em units
        # alone: fonts with different x-heights otherwise make accents larger.
        base = next((char for char in unicodedata.normalize('NFD', visible)
                     if char.isalpha() and not unicodedata.combining(char)), '')
        key = (font['sha256'], base)
        if key not in sizes:
            ratio = ass_font_scale(font['path']) / primary_scale
            raw = raw_fonts[font['sha256']]
            if base and primary_raw.supportsCharacter(ord(base)) and raw.supportsCharacter(ord(base)):
                first = primary_raw.boundingRect(primary_raw.glyphIndexesForString(base)[0]).height()
                second = raw.boundingRect(raw.glyphIndexesForString(base)[0]).height()
                if first > 0 and second > 0: ratio *= first / second
            sizes[key] = format(style['size'] * ratio, '.6f').rstrip('0').rstrip('.')
        return sizes[key]
    output = []
    for start, end, payload in events:
        fields = payload.split(',', 8)
        body, drawing = [], 0
        for override, part in _runs(fields[8]):
            if override:
                body.append('{' + part + '}')
                for tag, arg, _ in _tags(part):
                    if tag == 'p': drawing = int(float(arg))
                    elif tag == 'r': drawing = 0
                continue
            if drawing:
                body.append(part)
                continue
            for literal, visible in _literal_units(part):
                font = next((replacements[c] for c in visible if c in replacements), None)
                if font:
                    if any(not char.isspace() and unicodedata.category(char) not in {'Cc', 'Cf'}
                           and not raw_fonts[font['sha256']].supportsCharacter(ord(char)) for char in visible):
                        raise ValueError('补字字体不能完整呈现组合字符，未生成缺字字幕。')
                    body.append('{\\fn' + font['family'] + '\\fs' + fallback_size(font,visible) + '}' + literal
                                + '{\\fn' + primary + '\\fs' + str(style['size']) + '}')
                else:
                    body.append(literal)
        rewritten = ''.join(body)
        if plain(rewritten) != plain(fields[8]):
            raise ValueError('补字改变了字幕正文，未采用。')
        fields[8] = rewritten
        output.append((start, end, ','.join(fields)))
    return output


def reapply_styled_fallbacks(events, style):
    """Reuse attached fallback fonts after translation edits; never assume new attachments."""
    if not style.get('font_id'):
        return events
    from subtitle_style_engine import plain
    info = resolve_font(style)
    replacements = glyph_plan(info, '\n'.join(plain(event[2].split(',',8)[8]) for event in events))
    attached = {value['sha256'] for value in style.get('glyph_fallbacks', {}).values()}
    if any(font['sha256'] not in attached for font in replacements.values()):
        raise ValueError('编辑后的字幕需要新的补字字体附件，请重新处理影片以更新字体；原成品保留。')
    return apply_glyph_fallbacks(events, info, style, replacements)

def import_font(path):
    info = font_info(path)
    root = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'SubFlow/subtitle-fonts'
    root.mkdir(parents=True, exist_ok=True)
    dest = root / (info['sha256'] + Path(path).suffix.lower())
    if not dest.exists():
        pending = dest.with_suffix(dest.suffix+'.pending')
        pending.write_bytes(Path(path).read_bytes()); os.replace(pending,dest)
    return dict(info, path=str(dest), id='import:'+info['sha256'], label=info['family'])

def resolve_font(style):
    code = style.get('font_id', '')
    if code.startswith('builtin:'):
        item = next((x for x in BUILTINS if x[0] == code[8:]), None)
        if item is None: raise ValueError('内置字体不存在。')
        return font_info(assets_root()/item[0]/item[2])
    info = font_info(style.get('path',''))
    if info['sha256'] != style.get('sha256'): raise ValueError('字体文件已经变化，请重新选择字体。')
    return info

def normalize_styles(styles):
    # Profile loading, the font editor and processing share one migration rule.
    from profile_model import normalize_subtitle_styles
    return normalize_subtitle_styles(styles)

@contextmanager
def processing(styles, work, output):
    token=CONTEXT.set(dict(styles=normalize_styles(styles),work=str(work),output=str(output)))
    try: yield
    finally: CONTEXT.reset(token)

def element(tag,data):
    ident=tag.to_bytes((tag.bit_length()+7)//8,'big')
    width=next(n for n in range(1,9) if len(data)<(1<<(7*n))-1)
    return ident+((1<<(7*width))|len(data)).to_bytes(width,'big')+data

def uint(tag,value):
    if value < 0: raise ValueError('字幕时间不能为负数。')
    return element(tag,value.to_bytes(max(1,(value.bit_length()+7)//8),'big'))

def text(tag,value): return element(tag,value.encode('utf8'))

def milliseconds(value):
    h,m,s,ms=map(int,value.replace('.',',').replace(',',':').split(':'))
    return ((h*60+m)*60+s)*1000+ms

def header(style):
    family=style['family']; size=style['size']; rgb=style['color'][1:]; bgr=rgb[4:6]+rgb[2:4]+rgb[:2]
    return f'''[Script Info]
ScriptType: v4.00+
PlayResX: {style.get('canvas_width',1280)}
PlayResY: {style.get('canvas_height',720)}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,{family},{size},&H00{bgr},&H00{bgr},&H00000000,&H00000000,{-1 if style.get('bold') else 0},{-1 if style.get('italic') else 0},0,0,100,100,0,0,1,2,0,2,25,25,28,1
'''.encode('utf8')

def ass_text(value):
    """Convert safe SRT markup without allowing text to inject ASS commands."""
    from html.parser import HTMLParser
    from subtitle_text_formats import normalize_srt_markup
    class Markup(HTMLParser):
        def __init__(self):
            super().__init__(convert_charrefs=True)
            self.output=[]; self.font_stack=[]; self.appearance={'face':'','color':''}
            self.style_depth={tag:0 for tag in 'bius'}
        def handle_data(self,data):
            # ASS has no generic backslash escape. Guard its three text escape
            # sequences with a zero-width word joiner, preserving visible text.
            data=re.sub(r'\\(?=[Nnh])',lambda m:'\\\u2060',data)
            self.output.append(data.replace('{',r'\{').replace('}',r'\}'))
        def font_commands(self):
            rgb=self.appearance['color']
            color=('&H'+rgb[5:7]+rgb[3:5]+rgb[1:3]+'&') if rgb else ''
            self.output.append('{\\fn'+self.appearance['face']+'\\1c'+color+'\\2c'+color+'}')
        def handle_starttag(self,tag,attrs):
            if tag in self.style_depth:
                if not self.style_depth[tag]: self.output.append('{\\'+tag+'1}')
                self.style_depth[tag]+=1
            elif tag=='br': self.output.append(r'\N')
            elif tag=='font':
                self.font_stack.append(dict(self.appearance))
                for key,v in attrs:
                    if key=='face' and v and not any(c in v for c in ',\\{}\r\n'):
                        self.appearance['face']=v
                    elif key=='color' and v:
                        from PySide6.QtGui import QColor
                        color=QColor(v)
                        if color.isValid(): self.appearance['color']=color.name().upper()
                self.font_commands()
        def handle_endtag(self,tag):
            if tag in self.style_depth:
                self.style_depth[tag]=max(0,self.style_depth[tag]-1)
                if not self.style_depth[tag]: self.output.append('{\\'+tag+'0}')
            elif tag=='font' and self.font_stack:
                self.appearance=self.font_stack.pop();self.font_commands()
    parser=Markup();parser.feed(normalize_srt_markup(value).replace('\r',''));parser.close()
    return ''.join(parser.output).replace('\n',r'\N')

def write_subtitle_mkv(path,events,style,language='und',private=None,packets=False,uid=None):
    """ASS timestamps live in Matroska Blocks, preserving SRT milliseconds."""
    ebml=b''.join([uint(0x4286,1),uint(0x42F7,1),uint(0x42F2,4),uint(0x42F3,8),text(0x4282,'matroska'),uint(0x4287,4),uint(0x4285,2)])
    info=uint(0x2AD7B1,1000000)+text(0x4D80,'SubFlow')+text(0x5741,'SubFlow')
    track=uint(0xD7,1)+uint(0x73C5,uid or secrets.randbits(63) or 1)+uint(0x83,17)+uint(0xB9,1)+uint(0x88,0)+text(0x86,'S_TEXT/ASS')+text(0x22B59C,language)+element(0x63A2,private or header(style))
    with Path(path).open('wb') as output:
        output.write(element(0x1A45DFA3,ebml)); output.write(b'\x18\x53\x80\x67\x01'+b'\xff'*7)
        output.write(element(0x1549A966,info)); output.write(element(0x1654AE6B,element(0xAE,track)))
        for index,(start,end,body) in enumerate(events):
            if start < 0 or end <= start: raise ValueError('字幕时间区间无效。')
            payload=body if packets else f'{index},0,Default,,0,0,0,,'+ass_text(body)
            group=element(0xA1,b'\x81\x00\x00\x00'+payload.encode('utf8'))+uint(0x9B,end-start)
            output.write(element(0x1F43B675,uint(0xE7,start)+element(0xA0,group)))

def srt_events(path):
    import subtitle_tool_core as core
    # Translation's parser deliberately removes markup and joins lines; styling
    # must preserve both the visible line breaks and original color/font tags.
    content=core.read_text(Path(path)).replace('\r\n','\n').replace('\r','\n')
    events=[]
    for block in re.split(r'\n\s*\n',content):
        if not block.strip(): continue
        lines=block.strip('\n\ufeff').splitlines()
        timing=next(((i,core.SRT_TIME_RE.search(line)) for i,line in enumerate(lines) if core.SRT_TIME_RE.search(line)),None)
        if timing is None: raise ValueError('SRT含有无法识别的时间区间；未应用字体。')
        index,match=timing
        start=milliseconds(core.normalize_srt_time(match['start']));end=milliseconds(core.normalize_srt_time(match['end']))
        body='\n'.join(lines[index+1:])
        if end<=start or not body.strip(): raise ValueError('SRT含有无效时间或空白正文；未应用字体。')
        events.append((start,end,body))
    return events

def _dimensions(media):
    for track in media.get('tracks', []):
        if track.get('type') == 'video':
            props = track.get('properties', {})
            value = props.get('display_dimensions') or props.get('pixel_dimensions', '')
            try:
                width, height = map(int, value.split('x'))
                if width > 0 and height > 0: return width, height
            except (ValueError, AttributeError): pass
    raise ValueError('影片画面尺寸无法读取；没有应用字体设置。')


def _packet_bytes(value):
    # ffprobe's data is a hex dump followed by two spaces and an ASCII column.
    return b''.join(bytes.fromhex(line.split(':', 1)[1].split('  ', 1)[0].strip().replace(' ', ''))
                    for line in value.splitlines() if ':' in line)


def _text_packets(input_path, media, log, cancel, work=None, selected_ids=None):
    """Read all subtitle packets in one pass, retaining exact millisecond timing."""
    import subtitle_tool_core as core
    from decimal import Decimal
    probe = Path(core.FFMPEG).with_name('ffprobe.exe' if os.name == 'nt' else 'ffprobe')
    subtitles = [t for t in media['tracks'] if t['type'] == 'subtitles'
        and t['properties'].get('codec_id') in {'S_TEXT/UTF8', 'S_TEXT/ASS'}
        and (selected_ids is None or t['id'] in selected_ids)]
    if not subtitles: return {}
    isolated = Path(work) / 'retained-text.mkv'
    def read_log(message):
        # This is subtitle preparation, not the final movie mux. Do not drive
        # the orange output progress bar to 100% before output writing begins.
        log(message.replace('mkvmerge 封装','文本字幕读取'))
    # Limit probe output to the selected text; PGS packets can be hundreds of
    # megabytes. The movie is read once, then only this small file is probed.
    core.run_command([core.MKVMERGE, '-o', str(isolated), '--no-video', '--no-audio',
        '--no-attachments', '--no-chapters', '--no-global-tags', '--no-track-tags',
        '--subtitle-tracks', ','.join(str(t['id']) for t in subtitles),
        '--track-order', ','.join(f'0:{t["id"]}' for t in subtitles), str(input_path)],
        log=read_log, cancel_event=cancel)
    result = core.run_command([str(probe), '-v', 'error', '-select_streams', 's',
        '-show_packets', '-show_streams', '-show_data', '-show_entries',
        'packet=stream_index,pts_time,duration_time,data:stream=index,codec_name',
        '-of', 'json', str(isolated)], log=log, cancel_event=cancel)
    data = json.loads(result.stdout)
    streams = data.get('streams', [])
    if len(streams) != len(subtitles):
        raise ValueError('文本字幕流与影片轨道不一致；没有应用字体设置。')
    by_index = {s['index']: t['id'] for s, t in zip(streams, subtitles)}
    events = {t['id']: [] for t in subtitles}
    selected = {t['id'] for t in subtitles if t['properties'].get('codec_id') in {'S_TEXT/UTF8', 'S_TEXT/ASS'}}
    for packet in data.get('packets', []):
        core.check_cancel(cancel)
        tid = by_index[packet['stream_index']]
        if tid not in selected: continue
        try:
            start = int(Decimal(packet['pts_time']) * 1000)
            duration = int(Decimal(packet['duration_time']) * 1000)
            if start < 0 or duration <= 0: raise ValueError('无效字幕时间')
            body = _packet_bytes(packet['data']).decode('utf8')
        except (KeyError, UnicodeError, ValueError) as exc:
            raise ValueError(f'字幕轨{tid}的正文或持续时间无法读取。') from exc
        events[tid].append((start, start + duration, body))
    return events


def _original_family(private, events):
    """Dominant original style, used only to measure automatic keep-font sizing."""
    from collections import Counter
    content = private.decode('utf-8-sig')
    fields = None; styles = {}; in_styles = False
    for line in content.splitlines():
        line=line.strip()
        if line.startswith('['): in_styles = line.lower() == '[v4+ styles]'
        if in_styles and line.lower().startswith('format:'):
            fields = [v.strip() for v in line.split(':', 1)[1].split(',')]
        if in_styles and line.lower().startswith('style:'):
            values = [v.strip() for v in line.split(':', 1)[1].split(',')]
            if not fields or len(values) != len(fields): raise ValueError('原字幕样式无法读取。')
            item = dict(zip((f.lower() for f in fields), values)); styles[item['name']] = item['fontname']
    counts = Counter(e[2].split(',', 8)[2] for e in events if len(e[2].split(',', 8)) == 9)
    if not counts or counts.most_common(1)[0][0] not in styles:
        raise ValueError('原字幕字体无法识别；请明确选择一种字体。')
    return styles[counts.most_common(1)[0][0]]


def _original_raw(family, input_path, media, work, log, cancel):
    from PySide6.QtGui import QFont, QFontDatabase, QRawFont
    # QRawFont.fromFont must not silently substitute an unavailable family.
    if family.casefold() in {name.casefold() for name in QFontDatabase.families()}:
        font = QFont(family); font.setPixelSize(100)
        raw = QRawFont.fromFont(font)
        if raw.isValid(): return raw
    # Windows can expose localized names, and offscreen workers need not have
    # the system family registry loaded. Read common system fonts directly.
    from subtitle_style_engine import player_family
    for path in _system_font_paths():
        raw=QRawFont(str(path),100)
        if not raw.isValid():continue
        try:aliases={raw.familyName().casefold(),player_family(path).casefold()}
        except ValueError:aliases={raw.familyName().casefold()}
        if family.casefold() in aliases:return raw
    import subtitle_tool_core as core
    for attachment in media.get('attachments', []):
        name = attachment.get('file_name', '')
        if Path(name).suffix.lower() not in {'.ttf', '.otf'}: continue
        path = work / ('original-font-' + str(attachment['id']) + Path(name).suffix.lower())
        core.run_command([core.MKVEXTRACT, str(input_path), 'attachments', f'{attachment["id"]}:{path}'], log=log, cancel_event=cancel)
        info = font_info(path)
        if family.casefold() in {info['family'].casefold(), info['ui_family'].casefold()}:
            return QRawFont(str(path), 100)
    raise ValueError(f'无法度量原字体“{family}”；请选择可用字体或填写手动字号。')


def _system_font_paths(code=None):
    root=Path(os.environ.get('WINDIR','C:/Windows'))/'Fonts'
    preferred={'zh-CN':['msyh.ttc','simhei.ttf'],'zh-TW':['msjh.ttc','msyh.ttc'],
               'ja':['YuGothM.ttc','msgothic.ttc'],'ko':['malgun.ttf'],
               'hi':['Nirmala.ttf']}.get(code,[])
    names=preferred+['segoeui.ttf','arial.ttf','msyh.ttc','msjh.ttc','YuGothM.ttc',
                     'msgothic.ttc','malgun.ttf','Nirmala.ttf','simhei.ttf','simsun.ttc']
    return [root/name for name in dict.fromkeys(names) if (root/name).is_file()]


def _default_family(code):
    # A plain SRT has no original family. Use installed language-appropriate
    # fonts without attaching or distributing system font files.
    from PySide6.QtGui import QFontDatabase
    candidates = {'zh-CN': ['Microsoft YaHei', 'SimHei'], 'zh-TW': ['Microsoft JhengHei', 'Microsoft YaHei'],
                  'ja': ['Yu Gothic', 'MS Gothic'], 'ko': ['Malgun Gothic'],
                  'hi': ['Nirmala UI']}.get(code, []) + ['Segoe UI', 'Arial']
    families = {f.casefold(): f for f in QFontDatabase.families()}
    for candidate in candidates:
        if candidate.casefold() in families: return families[candidate.casefold()]
    from PySide6.QtGui import QRawFont
    from subtitle_style_engine import player_family
    for path in _system_font_paths(code):
        if QRawFont(str(path),100).isValid():return player_family(path)
    raise ValueError('没有找到适合文本字幕的默认字体；请手动选择字体。')


def _measurement_raw(family, code, captions, input_path, media, work, log, cancel):
    """Measure retained fonts without making an unavailable family block defaults.

    A retained ASS family may only exist on the original author's computer.
    Players already fall back for that family. Measure a usable local font but
    preserve the original font declaration; an explicitly selected new font
    never takes this fallback path.
    """
    import unicodedata
    from subtitle_style_engine import plain
    chars={c for value in captions for c in plain(value)
           if not c.isspace() and unicodedata.category(c) not in {'Cc','Cf'}}
    try:
        raw=_original_raw(family,input_path,media,work,log,cancel)
    except (OSError,ValueError):
        raw=None
    missing={c for c in chars if raw is None or not raw.supportsCharacter(ord(c))}
    if not missing:return raw
    # Prefer the track language, then cover mixed dialogue with installed
    # system fonts. Only measurement changes, never the subtitle's Fontname.
    candidates=[_default_family(code),'Microsoft YaHei','Microsoft JhengHei',
                'Yu Gothic','Malgun Gothic','Nirmala UI','Segoe UI','Arial']
    best=raw;score=len(chars)-len(missing)
    for candidate in dict.fromkeys(candidates):
        try:
            measured=_original_raw(candidate,input_path,dict(media,attachments=[]),work,log,cancel)
        except (OSError,ValueError):continue
        coverage=sum(measured.supportsCharacter(ord(c)) for c in chars)
        if coverage>score:best,score=measured,coverage
        if coverage==len(chars):break
    if best is None or score==0:raise ValueError('没有可度量字幕文字的本机字体；请明确选择可用字体。')
    log(f'原字体“{family}”在本机不可用或缺字，按“{best.familyName()}”估算默认字号；保留原字幕字体声明。')
    return best


def layout_styled_events(events, style, private, *, raw=None, code='und',
                         input_path=None, media=None, work=None, log=lambda _: None, cancel=None):
    """The first mux and edited translations share exactly the same B layout."""
    from subtitle_style_engine import SUPPORTED_DIALOGUE_LAYOUTS, layout_dialogue
    if style.get('dialogue_layout') not in SUPPORTED_DIALOGUE_LAYOUTS:
        return events
    if raw is None:
        raw = resolve_font(style)['path'] if style.get('font_id') else _measurement_raw(
            style['family'], code, [event[2].split(',',8)[8] for event in events],
            input_path, media or {}, Path(work), log, cancel)
    # Top/side signs and rotated styles keep the author's placement.
    fields, in_styles, protected = None, False, []
    for line in private.decode('utf-8-sig').splitlines():
        stripped = line.strip()
        if stripped.startswith('['): in_styles = stripped.lower() == '[v4+ styles]'
        if in_styles and stripped.lower().startswith('format:'):
            fields = [v.strip().lower() for v in stripped.split(':',1)[1].split(',')]
        if in_styles and fields and stripped.lower().startswith('style:'):
            data = dict(zip(fields,[v.strip() for v in stripped.split(':',1)[1].split(',')]))
            if data.get('alignment','2') != '2' or float(data.get('angle','0')):
                protected.append(data['name'])
    configured = dict(style, protected_styles=protected)
    output, report = layout_dialogue(events, configured, raw)
    if report['reflowed_cues']:
        log(f'字幕排版：B档按可见字高适配，{report["reflowed_cues"]}条对白；'
            f'超两行{report["multiline_cues"]}条，最多{report["max_lines"]}行；'
            '按单词及标点换行，字号、正文和原毫秒时间轴保持。')
    _record_layout_diagnostics(report, events, style, code, work, log, cancel)
    return output


def _record_layout_diagnostics(report, events, style, code, work, log, cancel):
    """Diagnostics never change trust, timing or the success of a valid output."""
    import subtitle_tool_core as core
    from subtitle_style_engine import DIALOGUE_LAYOUT_VERSION
    if report.get('layout_version') != DIALOGUE_LAYOUT_VERSION:
        return
    joins = report.get('hardbreak_joins', [])
    removed = report.get('duplicate_events_removed', [])
    warnings = report.get('repeated_text_warnings', [])
    if not (joins or removed or warnings or report.get('multiline_cues')):
        return

    def notice(message):
        try:
            log(message)
        except core.CancelledError:
            raise
        except Exception:
            pass

    if joins:
        notice(f'普通断行整理：{len(joins)}条字幕，共{sum(len(row["edits"]) for row in joins)}处；'
               '保留说话人、声音说明及样式分组，不改变字号和时间轴。')
    if removed:
        notice(f'已去除{len(removed)}个时间、事件标识和正文完全相同的冗余字幕事件。')
    if warnings:
        notice(f'发现{len(warnings)}条疑似重复正文；已保留原文，需确认是否为真实复述，未作为处理失败。')
    if report.get('multiline_cues'):
        notice(f'仍有{report["multiline_cues"]}条字幕超过两行；保留必要内容，未自动删字、缩小字号或移动时间。')
    if work is None:
        return
    core.check_cancel(cancel)
    try:
        signature = hashlib.sha256(json.dumps(dict(events=events, style=style),
            ensure_ascii=False, sort_keys=True, default=str).encode('utf8')).hexdigest()
        language = re.sub(r'[^A-Za-z0-9_-]', '_', code or 'und')
        destination = Path(work)/f'subtitle-layout-{language}-{signature[:12]}.json'
        destination.parent.mkdir(parents=True, exist_ok=True)
        document = dict(report, language=code, source_signature=signature,
            font_family=style.get('family'), size=style.get('size'),
            note='显示质量诊断；疑似重复或阅读密度不改变字幕可信度和处理状态。')
        destination.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding='utf8')
        notice('字幕排版诊断：'+str(destination))
    except core.CancelledError:
        raise
    except Exception as exc:
        notice(f'字幕排版诊断未保存（{type(exc).__name__}）；字幕处理继续。')


def prepare_mux(input_path,output_path,media,kept,generated,offsets,default,log,cancel,equivalent=False):
    """Apply each requested text setting before the single primary mux.

    Fail explicitly if a requested setting cannot be applied. A completed file
    must never claim to use a font/size while silently keeping the old track.
    """
    import subtitle_tool_core as core
    from subtitle_languages import subtitle_language
    from subtitle_style_engine import automatic_size, plain, update
    context=CONTEXT.get()
    if not context or not context['styles']: return None
    if Path(context['output']).suffix.lower()!='.mkv':
        raise ValueError('字体设置需要MKV输出；请选择MKV后再处理。')
    width,height=_dimensions(media)
    styles=context['styles']; work=Path(context['work'])/'font-styles'; work.mkdir(parents=True,exist_ok=True)
    remaining=list(kept); result=list(generated); replacements={}; metadata={}; fonts={}; receipts={}
    tracks={t['id']:t for t in media.get('tracks',[])}
    def select(code):
        code=normalize_language(code)
        if code in styles: return styles[code]
        if equivalent and code in {'zh-CN','zh-TW'}: return styles.get('zh-TW' if code=='zh-CN' else 'zh-CN')
        return None
    def remember_font(info, requested):
        font=dict(info)
        ident=requested.get('font_id','')
        if ident.startswith('builtin:'):
            folder=assets_root()/ident[8:]
            font['notices']=[str(p) for p in (folder/'OFL.txt',folder/'FONTLOG.txt') if p.is_file()]
        fonts[info['sha256']]=font
    def make(path,code,events,requested,private=None,packets=False,uid=None):
        style=dict(requested)
        captions=[e[2].split(',',8)[-1] if packets else ass_text(e[2]) for e in events]
        if not events: raise ValueError('没有可读取的文本字幕。')
        change_font=bool(style.get('font_id'))
        change_color=style.get('color','keep')!='keep'
        info=resolve_font(style) if change_font else None
        replacements={}
        if info:
            style.update(family=info['family'],ui_family=info['ui_family'],bold=info['bold'],italic=info['italic'])
            replacements=glyph_plan(info,'\n'.join(plain(value) for value in captions))
        else:
            style['family']=_original_family(private,events) if packets else _default_family(code)
        if style.get('size_mode')=='auto':
            raw=info['path'] if info else _measurement_raw(style['family'],code,captions,input_path,media,work,log,cancel)
            style['size'],metrics=automatic_size(raw,captions,width,height)
            style['adaptive_metrics']=metrics
            if metrics.get('layout_version'):
                style['dialogue_layout']=metrics['layout_version']
        style.update(canvas_width=round(width/height*720),canvas_height=720)
        if not change_color: style['color']='#FFFFFF' # new SRT has no colors; ASS keep-color handled below
        style.update(change_font=change_font,change_color=change_color)
        if packets:
            events,private,_=update(private,events,style,width,height)
        else:
            private=header(style)
            events=[(a,b,f'{i},0,Default,,0,0,0,,'+ass_text(body)) for i,(a,b,body) in enumerate(events)]
            events,_,_=update(private,events,style,width,height)
            packets=True
        events=layout_styled_events(events,style,private,raw=raw if style.get('size_mode')=='auto' else None,
            code=code,input_path=input_path,media=media,work=work,log=log,cancel=cancel)
        if replacements:
            events=apply_glyph_fallbacks(events,info,style,replacements)
            style['glyph_fallbacks']={char:dict(family=font['family'],sha256=font['sha256']) for char,font in replacements.items()}
            for font in replacements.values(): remember_font(font,{'font_id':font['font_id']})
            log(f'字体补字：{info["family"]}缺少“{"".join(replacements)}”，仅这些字符使用'+
                '、'.join(dict.fromkeys(font['family'] for font in replacements.values()))+'；正文和时间轴保持。')
        uid=uid or secrets.randbits(63) or 1
        write_subtitle_mkv(path,events,style,code,private,packets,uid)
        if info: remember_font(info,requested)
        return style,private,uid
    for index,(source,code,name,is_default) in enumerate(list(result)):
        requested=select(code)
        if requested is None: continue
        try:
            if Path(source).suffix.lower()!='.srt': raise ValueError('新增文本字幕格式暂不支持应用字体，请改用SRT。')
            path=work/f'generated-{index}.mkv'; styled,private,uid=make(path,code,srt_events(source),requested)
            result[index]=(path,code,name,is_default)
            receipts[str(Path(source).resolve())]=dict(style=styled,source_sha256=digest(source),
                private=base64.b64encode(private).decode('ascii'),private_sha256=hashlib.sha256(private).hexdigest(),track_uid=uid)
            mode='默认自适应' if requested['size_mode']=='auto' else '手动'
            log(f'{name}已应用字体：{styled["family"]}，{mode}字号{styled["size"]}；保留原文和毫秒时间轴。')
        except (OSError,ValueError,KeyError) as exc:
            raise ValueError(f'{name}的字体设置无法应用：{exc}') from exc
    packet_cache=None
    for track_id in kept:
        core.check_cancel(cancel)
        track=tracks[track_id]; props=track.get('properties',{}); code=subtitle_language(props.get('language_ietf') or props.get('language','und'),props.get('track_name',''))
        requested=select(code)
        if requested is None: continue
        codec=props.get('codec_id','')
        if not codec.startswith('S_TEXT/'):
            log(f'字幕轨{track_id}为图片字幕，字体设置只适用于文本。'); continue
        try:
            if codec not in {'S_TEXT/UTF8','S_TEXT/ASS'}:
                raise ValueError(f'文本格式{codec}暂不支持应用字体，请改用SRT或ASS。')
            if packet_cache is None:
                log('一次读取保留的文本字幕轨；字体、字号和颜色统一应用。')
                selected_ids={tid for tid in kept if tracks[tid]['properties'].get('codec_id') in {'S_TEXT/UTF8','S_TEXT/ASS'}
                    and select(subtitle_language(tracks[tid]['properties'].get('language_ietf') or tracks[tid]['properties'].get('language','und'),tracks[tid]['properties'].get('track_name',''))) is not None}
                packet_cache=_text_packets(input_path,media,log,cancel,work,selected_ids)
            events=packet_cache[track_id]; packets=codec=='S_TEXT/ASS'
            private=bytes.fromhex(props.get('codec_private_data','')) if packets else None
            offset=int((offsets or {}).get(track_id,0))
            events=[(max(0,a+offset),b+offset,body) for a,b,body in events if b+offset>0]
            path=work/f'original-{track_id}.mkv'
            styled,private,uid=make(path,code,events,requested,private,packets,props.get('uid'))
            index=len(result); is_default=(default==('retained',track_id)) if default is not None else bool(props.get('default_track'))
            result.append((path,code,props.get('track_name',''),is_default)); replacements[track_id]=index
            metadata[str(path)]=props; remaining.remove(track_id)
            mode='默认自适应' if requested['size_mode']=='auto' else '手动'
            log(f'原文本字幕轨{track_id}已应用字体：{styled["family"]}，{mode}字号{styled["size"]}；定位、正文和已确认时间轴保留。')
        except (OSError,ValueError,KeyError) as exc:
            raise ValueError(f'原文本字幕轨{track_id}的字体设置无法应用：{exc}') from exc
    receipt=Path(context['work'])/'font-style-receipt.json'
    receipt.write_text(json.dumps(receipts,ensure_ascii=False,indent=2),encoding='utf8')
    existing=media.get('attachments',[])
    for font in fonts.values():
        font['embed']=True
        candidates=[a for a in existing if a.get('size')==Path(font['path']).stat().st_size
                    and a.get('content_type') in {'font/ttf','font/otf','application/x-truetype-font','application/vnd.ms-opentype'}]
        for attachment in candidates:
            extracted=work/f'existing-attachment-{attachment["id"]}.bin'
            core.run_command([core.MKVEXTRACT,str(input_path),'attachments',f'{attachment["id"]}:{extracted}'],log=log,cancel_event=cancel)
            if digest(extracted)==font['sha256']:
                font['embed']=False; log(f'字体{font["family"]}已在原片附件中，不重复内嵌。'); break
        font['notices']=[p for p in font.get('notices',[]) if not any(
            a.get('file_name')==f'SubFlow-{font["sha256"][:16]}-{Path(p).name}' for a in existing)]
    return dict(kept=remaining,generated=result,replacements=replacements,metadata=metadata,fonts=list(fonts.values()))


def styled_receipt(work,target_path):
    try:
        receipt=json.loads((Path(work)/'font-style-receipt.json').read_text(encoding='utf8')).get(str(Path(target_path).resolve()))
        if receipt and receipt['source_sha256']==digest(target_path): return receipt
    except (OSError,ValueError,KeyError): pass
    return None
