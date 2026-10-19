"""Apply text appearance in a single, resolution-independent ASS coordinate system.

Packet timestamps stay in Matroska milliseconds.  ASS positioning, clip paths and
drawings are mapped along with the script canvas; inline appearance instructions
cannot silently override an explicitly selected font or size.  This module never
loads a font into the operating system and never mutates a font file.
"""
from __future__ import annotations

import math
import re
import struct
import unicodedata

REFERENCE_HEIGHT = 720
_FLOAT = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)"
_NUMBER = re.compile(r"^\s*(" + _FLOAT + r")\s*$")
_KNOWN_TAGS = tuple(sorted({
    'iclip', 'clip', 'move', 'pos', 'org', 'fscx', 'fscy', 'fsp', 'fs', 'fn',
    'frx', 'fry', 'frz', 'fr', 'fax', 'fay', 'xbord', 'ybord', 'bord',
    'xshad', 'yshad', 'shad', 'blur', 'be', 'pbo', 'alpha',
    '1c', '2c', '3c', '4c', '1a', '2a', '3a', '4a', 'fade', 'fad',
    'an', 'a', 'b', 'i', 'u', 's', 'r', 'fe', 'q', 'k', 'kf', 'ko', 'kt',
    'p', 't', 'c',
}, key=len, reverse=True))


def _number(value):
    value = float(value)
    if not math.isfinite(value):
        raise ValueError('字幕包含无效的坐标或字号。')
    return format(value, '.6f').rstrip('0').rstrip('.') or '0'


def _numeric(value, label='ASS坐标'):
    match = _NUMBER.fullmatch(value)
    if not match:
        raise ValueError(f'{label}格式无法安全转换：{value!r}')
    number = float(match[1])
    if not math.isfinite(number):
        raise ValueError(f'{label}不是有限数值。')
    return number


def player_family(font_path):
    """Read the unmodified font's legacy family (name ID 1), used by players.

    QRawFont.familyName() can return the typographic name (ID 16).  In particular
    DirectWrite may fail to select an attached font by that name and substitute a
    system font.  Keep the Qt preview name separate from this player name.
    """
    from PySide6.QtGui import QRawFont
    raw = _measurement_font(font_path)
    data = bytes(raw.fontTable('name'))
    if len(data) < 6:
        raise ValueError('字体没有可用的播放器家族名称。')
    _, count, strings = struct.unpack_from('>HHH', data)
    if 6 + count * 12 > len(data):
        raise ValueError('字体名称表不完整。')
    choices = []
    for index in range(count):
        platform, encoding, language, name_id, length, start = struct.unpack_from(
            '>HHHHHH', data, 6 + index * 12)
        if name_id != 1 or platform not in {0, 1, 3}:
            continue
        start += strings
        if start + length > len(data):
            raise ValueError('字体名称记录不完整。')
        try:
            if platform in {0, 3}:
                value = data[start:start + length].decode('utf-16-be')
            elif encoding == 0:
                value = data[start:start + length].decode('mac_roman')
            else:
                continue
        except UnicodeError:
            continue
        value = value.strip('\x00 ')
        if not value or any(c in value for c in ',\\{}\r\n'):
            continue
        rank = (0 if platform == 3 else 1 if platform == 0 else 2,
                0 if language == 0x409 else 1 if language == 0x804 else 2,
                language, encoding)
        choices.append((rank, value))
    if not choices:
        raise ValueError('字体没有可安全写入字幕的播放器家族名称。')
    return min(choices)[1]


def _escaped(value, index):
    # ASS does not implement C-style "\\\\" escaping. Every backslash before a
    # brace escapes that brace, even when preceded by another literal slash.
    return index > 0 and value[index - 1] == '\\'


def _runs(value):
    """Yield literal text and override blocks without treating escaped braces as tags."""
    start = cursor = 0
    while cursor < len(value):
        if value[cursor] != '{' or _escaped(value, cursor):
            cursor += 1
            continue
        end = cursor + 1
        while end < len(value) and (value[end] != '}' or _escaped(value, end)):
            if value[end] == '{' and not _escaped(value, end):
                raise ValueError('ASS覆盖指令中包含未配对的花括号。')
            end += 1
        if end == len(value):
            raise ValueError('ASS覆盖指令缺少右花括号。')
        if cursor > start:
            yield False, value[start:cursor]
        yield True, value[cursor + 1:end]
        cursor = start = end + 1
    if start < len(value):
        yield False, value[start:]


def _tags(block):
    """Tokenize override instructions, keeping balanced parenthesized arguments.

    A transform can contain a clip and another transform, so a regex ending at
    the first ')' is unsafe. Unknown instructions/comments remain untouched.
    """
    cursor = 0
    while cursor < len(block):
        if block[cursor] != '\\':
            end = block.find('\\', cursor)
            end = len(block) if end < 0 else end
            yield '', block[cursor:end], ''
            cursor = end
            continue
        start = cursor
        cursor += 1
        lowered = block[cursor:].lower()
        name = next((tag for tag in _KNOWN_TAGS if lowered.startswith(tag)), '')
        if not name:
            end = block.find('\\', cursor)
            end = len(block) if end < 0 else end
            yield '', block[start:end], ''
            cursor = end
            continue
        original_name = block[cursor:cursor + len(name)]
        cursor += len(name)
        argument_start = cursor
        depth = 0
        while cursor < len(block):
            char = block[cursor]
            if char == '(':
                depth += 1
            elif char == ')':
                depth -= 1
                if depth < 0:
                    raise ValueError('ASS指令括号未配对。')
            elif char == '\\' and depth == 0:
                break
            cursor += 1
        if depth:
            raise ValueError('ASS指令缺少右括号。')
        yield name, block[argument_start:cursor], original_name


def plain(value):
    """Visible text only, excluding vector drawing runs and override instructions."""
    output = []
    drawing = 0
    for override, part in _runs(str(value)):
        if override:
            for tag, arg, _ in _tags(part):
                if tag == 'p':
                    drawing = int(_numeric(arg, 'ASS绘图模式'))
                elif tag == 'r':
                    drawing = 0
        elif not drawing:
            output.append(part)
    text = ''.join(output)
    output, cursor = [], 0
    while cursor < len(text):
        char = text[cursor]
        if char == '\\' and cursor + 1 < len(text):
            following = text[cursor + 1]
            # When converting plain text, the writer puts a zero-width word
            # joiner between a literal slash and N/n/h. It prevents the player
            # from interpreting user text as a break or nonbreaking space.
            if following == '\u2060' and cursor + 2 < len(text) and text[cursor + 2] in 'Nnh':
                output.extend(('\\', text[cursor + 2]))
                cursor += 3
                continue
            if following in 'Nnh{}':
                output.append('\n' if following == 'N' else ' ' if following in 'nh' else following)
                cursor += 2
                continue
        output.append(' ' if char == '\t' else char)
        cursor += 1
    return ''.join(output)


def _percentile(values, quantile):
    values = sorted(values)
    return values[min(len(values) - 1, int((len(values) - 1) * quantile))]


def _measurement_font(font_path):
    from PySide6.QtGui import QRawFont
    if isinstance(font_path, QRawFont):
        raw = QRawFont(font_path)
        raw.setPixelSize(100)
    else:
        raw = QRawFont(str(font_path), 100)
    if not raw.isValid():
        raise ValueError('自适应字号无法读取字体文件。')
    return raw


def ass_font_scale(font_path):
    """Convert Qt em pixels to ASS font units, as used by libass/VSFilter.

    ASS sizes the Windows ascent + descent, not the em square used by Qt.
    In fonts with tall script metrics those can differ by several times.
    Read the original tables; Qt's own ascent/descent can use typo metrics.
    """
    raw = _measurement_font(font_path)
    head, os2 = bytes(raw.fontTable('head')), bytes(raw.fontTable('OS/2'))
    if len(head) >= 20:
        em = struct.unpack_from('>H', head, 18)[0]
        if em:
            if len(os2) >= 78:
                # libass deliberately treats these nominally unsigned fields
                # as signed, matching Windows players' compatibility behavior.
                ascent, descent = struct.unpack_from('>hh', os2, 74)
                if ascent + descent > 0:
                    return (ascent + descent) / em
            hhea = bytes(raw.fontTable('hhea'))
            typo = struct.unpack_from('>hh', os2, 68) if len(os2) >= 72 else (0, 0)
            use_typo = len(os2) >= 64 and struct.unpack_from('>H', os2, 62)[0] & 0x80
            candidates = [typo] if use_typo else []
            if len(hhea) >= 8:
                candidates.append(struct.unpack_from('>hh', hhea, 4))
            candidates.append(typo)
            for ascent, descent in candidates:
                if ascent - descent > 0:
                    return (ascent - descent) / em
            if len(head) >= 44:
                ymin, ymax = struct.unpack_from('>h', head, 38)[0], struct.unpack_from('>h', head, 42)[0]
                if ymax > ymin:
                    return (ymax - ymin) / em
    scale = (raw.ascent() + raw.descent()) / raw.pixelSize()
    if not math.isfinite(scale) or scale <= 0:
        raise ValueError('字体没有可用于字幕播放器的有效高度。')
    return scale


AUTO_TARGET = .050
LEGACY_DIALOGUE_LAYOUT_VERSION = 'visible-b1'
DIALOGUE_LAYOUT_VERSION = 'visible-b2'
SUPPORTED_DIALOGUE_LAYOUTS = frozenset({LEGACY_DIALOGUE_LAYOUT_VERSION, DIALOGUE_LAYOUT_VERSION})


class DialogueMetrics:
    """Visible line metrics, in raw em pixels, cached once per font/track."""
    def __init__(self, font):
        self.raw = _measurement_font(font)
        self.scale = ass_font_scale(self.raw)
        self.cache = {}
        os2 = bytes(self.raw.fontTable('OS/2'))
        if len(os2) >= 78:
            ascent, descent = struct.unpack_from('>hh', os2, 74)
            self.baseline = ascent / (ascent + descent) if ascent + descent > 0 else None
        else:
            self.baseline = None
        if self.baseline is None:
            self.baseline = self.raw.ascent() / max(1, self.raw.ascent() + self.raw.descent())

    def glyph(self, char):
        if char not in self.cache:
            glyphs = [] if unicodedata.category(char) in {'Cc', 'Cf'} else self.raw.glyphIndexesForString(char)
            if not glyphs or not glyphs[0]:
                # A missing accented glyph can still be calibrated from its
                # supported base; actual coverage is handled by the font plan.
                base = next((c for c in unicodedata.normalize('NFD', char)
                             if c != char and not unicodedata.combining(c)), '')
                self.cache[char] = self.glyph(base) if base else (0, 0, 0, 0, 0)
            else:
                box = self.raw.boundingRect(glyphs[0])
                advance = self.raw.advancesForGlyphIndexes(glyphs)[0].x()
                self.cache[char] = (box.left(), box.top(), box.right(), box.bottom(), advance)
        return self.cache[char]

    def ink(self, value, size=100, *, ass=False):
        x = 0.; left = top = math.inf; right = bottom = -math.inf
        for char in value:
            a, b, c, d, advance = self.glyph(char)
            if not char.isspace() and d > b:
                left, top, right, bottom = min(left, x+a), min(top, b), max(right, x+c), max(bottom, d)
            x += advance
        factor = size / (100 * self.scale) if ass else size / 100
        if not math.isfinite(top):
            return (0., 0., 0., 0., x * factor)
        return (left*factor, top*factor, right*factor, bottom*factor, x*factor)


def automatic_size(font_path, captions, width, height):
    """One B size per track, measured from whole visible lines, not single letters.

    Long captions wrap at this same size. A few very tall accents do not drive
    all normal dialogue smaller; a reference ascender/descender union prevents
    a sample containing only short letters from producing an oversized track.
    """
    _dimensions(width, height)
    metrics = DialogueMetrics(font_path)
    heights = []
    for caption in captions:
        for line in plain(caption).splitlines():
            ink = metrics.ink(line)
            if ink[3] > ink[1]:
                heights.append(ink[3]-ink[1])
    if not heights:
        raise ValueError('字幕没有可量度的对白，无法确定自适应字号。')
    reference = metrics.ink('Hgjpqy')
    reference_height = max(_percentile(heights, .95), reference[3]-reference[1])
    ratio = reference_height / (100 * metrics.scale)
    size = max(1, round(REFERENCE_HEIGHT * AUTO_TARGET / ratio))
    return size, dict(mode='auto', layout_version=DIALOGUE_LAYOUT_VERSION,
                      reference_height=REFERENCE_HEIGHT, glyph_ratio=ratio,
                      baseline=REFERENCE_HEIGHT * AUTO_TARGET / ratio,
                      target_fraction=AUTO_TARGET, selected_size=size,
                      dialogue_lines=len(heights), ass_font_scale=metrics.scale)


_STATIC_DIALOGUE_TAGS = {'fn', 'fs', 'b', 'i', 'u', 's', 'fscx', 'fscy', 'fsp',
    'c', '1c', '2c', '3c', '4c', 'alpha', '1a', '2a', '3a', '4a',
    'bord', 'xbord', 'ybord', 'shad', 'xshad', 'yshad', 'blur', 'be', 'fe'}


def _dialogue_units(body, style):
    """Keep literal escapes/graphemes and carry static formatting across wraps.

    Anything with its own geometry, motion, reset or karaoke is intentionally
    left to its authored ASS layout. No drawing or positioned sign is reflowed.
    """
    from subtitle_fonts import _literal_units
    states, units = {}, []
    for override, part in _runs(body):
        if override:
            for tag, arg, name in _tags(part):
                if tag not in _STATIC_DIALOGUE_TAGS:
                    return None
                if tag == 'fn' and arg.strip() not in {'', style['family']}:
                    return None
                if tag in {'fscx', 'fscy'} and _numeric(arg) != 100:
                    return None
                if tag == 'fsp' and _numeric(arg) != 0:
                    return None
                states.pop(tag, None)
                states[tag] = '\\' + name + arg
        else:
            for literal, visible in _literal_units(part):
                units.append((literal, visible, ''.join(states.values())))
    return units


def _line_breaks(units):
    """Use Qt's Unicode line breaking, mapped from UTF-16 back to graphemes."""
    from PySide6.QtCore import QTextBoundaryFinder
    text = ''.join(u[1] for u in units)
    offsets, total = {0: 0}, 0
    for index, (_, visible, _) in enumerate(units, 1):
        total += len(visible.encode('utf-16-le')) // 2
        offsets[total] = index
    finder = QTextBoundaryFinder(QTextBoundaryFinder.Line, text)
    boundaries = {len(units)}
    while True:
        position = finder.toNextBoundary()
        if position < 0:
            break
        if position in offsets:
            boundaries.add(offsets[position])
    return boundaries


def _wrap_dialogue(units, metrics, size, limit):
    def width(items):
        a, _, c, _, advance = metrics.ink(''.join(u[1] for u in items), size, ass=True)
        return max(c-a, advance)
    if width(units) <= limit:
        return [units]
    breaks = _line_breaks(units)
    # Preserve whole words whenever they fit. Balance a two-line sentence so
    # the second line is not left with a single stray word.
    choices = []
    for cut in sorted(breaks - {len(units)}) if width(units) <= 2*limit else ():
        widths = width(units[:cut]), width(units[cut:])
        if max(widths) <= limit:
            choices.append((abs(widths[0]-widths[1]), cut))
    if choices:
        cut = min(choices)[1]
        return [units[:cut], units[cut:]]
    lines, start = [], 0
    # Greedy line breaking with cached advances avoids quadratic work on a
    # pathological unbroken URL/token. Only the end of a line is ink-checked.
    advances = [sum(metrics.glyph(c)[4] for c in u[1]) * size / (100 * metrics.scale) for u in units]
    while start < len(units):
        end, advance, last = start, 0., None
        while end < len(units):
            next_advance = advance + advances[end]
            if end > start and next_advance > limit:
                break
            advance = next_advance
            end += 1
            if end in breaks:
                last = end
        cut = end if end == len(units) else (last or end)
        while cut > start+1 and width(units[start:cut]) > limit:
            earlier = [x for x in breaks if start < x < cut]
            cut = max(earlier) if earlier else cut-1
        if cut <= start or width(units[start:cut]) > limit:
            raise ValueError('单个字幕字形超过画面可用宽度；请改用较小的手动字号。')
        lines.append(units[start:cut]); start = cut
    if 2 < len(lines) <= 16 and len(units) <= 1024:
        # Balance all lines, not only the first two. A greedy wrap would leave
        # a short orphan such as "to leave." or a lone Chinese character.
        from functools import lru_cache
        cuts = sorted(breaks)
        if len(cuts) == 1:
            cuts = list(range(1, len(units)+1))
        prefix = [0.]
        for value in advances:
            prefix.append(prefix[-1]+value)
        target = prefix[-1]/len(lines)
        @lru_cache(maxsize=None)
        def plan(start, remaining):
            if not remaining:
                return (0., ()) if start == len(units) else None
            best = None
            for cut in cuts:
                if cut <= start:
                    continue
                length = prefix[cut]-prefix[start]
                if length > limit:
                    break
                if remaining == 1 and cut != len(units):
                    continue
                rest = plan(cut, remaining-1)
                if rest is not None:
                    choice = ((length-target)**2+rest[0], (cut,)+rest[1])
                    if best is None or choice < best:
                        best = choice
            return best
        planned = plan(0, len(lines))
        if planned is not None:
            balanced, start = [], 0
            for cut in planned[1]:
                balanced.append(units[start:cut]); start = cut
            if all(width(line) <= limit for line in balanced):
                lines = balanced
    return lines


def _dialogue_paragraphs(units):
    paragraphs = [[]]
    for unit in units:
        if unit[1] == '\n':
            paragraphs.append([])
        else:
            paragraphs[-1].append(unit)
    return paragraphs


def _ordinary_structure(parts):
    """Named actors, explicit margins and layers may carry authored structure."""
    return (not parts[3].strip() and parts[1].strip() in {'', '0'}
            and all(value.strip() in {'', '0'} for value in parts[4:8]))


def _supported_join_script(value):
    # Do not invent word separators for scripts absent from the current corpus.
    for char in value:
        if unicodedata.category(char).startswith('L'):
            name = unicodedata.name(char, '')
            if not ('LATIN' in name or name.startswith(('CJK UNIFIED', 'CJK COMPATIBILITY'))):
                return False
    return True


def _han_character(char):
    return unicodedata.name(char, '').startswith(('CJK UNIFIED', 'CJK COMPATIBILITY'))


def _normalize_dialogue_hardbreaks(units):
    """Replace only recorded hard breaks; preserve every other literal and style."""
    paragraphs = [[]]
    separators = []
    offset = 0
    for unit in units:
        if unit[1] == '\n':
            separators.append((unit, offset))
            paragraphs.append([])
        else:
            paragraphs[-1].append(unit)
        offset += len(unit[1])

    def text(line):
        return ''.join(unit[1] for unit in line).strip()

    def appearance(line, first):
        visible = [unit for unit in line if not unit[1].isspace()]
        return visible[0 if first else -1][2] if visible else None

    def boundary(left, right):
        a, b = text(left), text(right)
        speaker = r'^(?:[-–—>]|[\[\(（【]|[^\W\d_][^:：\n]{0,31}[:：])'
        return (not a or not b or re.search(speaker, a) or re.search(speaker, b)
                or any(symbol in a+b for symbol in ('♩', '♪', '♫', '♬'))
                or re.search(r'[.!?。！？:：;；…\-]["\u201d\u2019\']?$', a)
                or appearance(left, False) != appearance(right, True)
                or not _supported_join_script(a+b))

    output = list(paragraphs[0])
    current = list(paragraphs[0])
    edits = []
    for (separator, position), right in zip(separators, paragraphs[1:]):
        if boundary(current, right):
            output.append(separator)
            output.extend(right)
            current = list(right)
            continue
        a, b = text(current), text(right)
        if current[-1][1].isspace() or right[0][1].isspace():
            gap = ''
        elif (_han_character(a[-1]) or re.search(r'[\u3000-\u303f\uff00-\uff65]$', a)) and _han_character(b[0]):
            gap = ''
        else:
            gap = ' '
        if gap:
            replacement = (gap, gap, separator[2])
            output.append(replacement)
            current.append(replacement)
        output.extend(right)
        current.extend(right)
        edits.append(dict(offset=position, before='\n', after=gap))
    expected = ''.join(unit[1] for unit in units)
    for edit in reversed(edits):
        position = edit['offset']
        if expected[position:position+1] != '\n':
            raise ValueError('字幕换行变更位置不一致，未采用。')
        expected = expected[:position] + edit['after'] + expected[position+1:]
    if expected != ''.join(unit[1] for unit in output):
        raise ValueError('字幕合行包含未经批准的正文变化，未采用。')
    return output, edits, expected


def _repeated_dialogue_block(units):
    """A warning only: identical text can also be genuine repeated speech."""
    paragraphs = _dialogue_paragraphs(units)
    if len(paragraphs) < 2 or any(not line for line in paragraphs):
        return None
    for length in range(1, len(paragraphs)//2+1):
        if len(paragraphs) % length:
            continue
        if all(line == paragraphs[index % length] for index, line in enumerate(paragraphs)):
            return dict(kind='repeated_line' if length == 1 else 'repeated_block',
                        block_lines=length, copies=len(paragraphs)//length,
                        sample='\n'.join(''.join(unit[1] for unit in line) for line in paragraphs[:length])[:160],
                        action='retained_for_confirmation')
    return None


def layout_dialogue(events, style, font):
    """Compact B dialogue, preserving each cue's exact interval and text order.

    Each physical line is a separate ASS event with a unique ReadOrder. Long
    captions gain lines rather than losing text or changing size/duration.
    Authored geometry/effects and explicit manual sizes retain their old path.
    """
    version = style.get('dialogue_layout')
    if version not in SUPPORTED_DIALOGUE_LAYOUTS or style.get('size_mode') == 'manual':
        return events, dict(reflowed_cues=0, multiline_cues=0, max_lines=0, protected_cues=0)
    modern = version == DIALOGUE_LAYOUT_VERSION
    metrics = DialogueMetrics(font)
    size = style['size']; canvas = style['canvas_width']; limit = canvas * .84
    gap = REFERENCE_HEIGHT * AUTO_TARGET * .20
    blocks, protected = [], 0
    exact_events = set()
    removed = []
    repeated = []
    normalized = []
    reading_load = []
    # Read original style alignments: new SRT defaults are ordinary bottom
    # dialogue. Callers mark top/side/authored styles as protected beforehand.
    for ordinal, (start, end, payload) in enumerate(events):
        parts = payload.split(',', 8)
        if len(parts) != 9:
            raise ValueError('ASS字幕包字段不完整。')
        units = None if parts[7] or parts[2] in style.get('protected_styles', []) else _dialogue_units(parts[8], style)
        if units is None:
            blocks.append((start, end, parts, None, 0., ordinal)); protected += 1; continue
        ordinary = _ordinary_structure(parts)
        identity = (start, end, payload)
        if modern and ordinary:
            if identity in exact_events:
                removed.append(dict(cue=ordinal+1, start_ms=start, end_ms=end))
                continue
            exact_events.add(identity)
        expected = plain(parts[8])
        if modern and ordinary:
            warning = _repeated_dialogue_block(units)
            if warning:
                repeated.append(dict(warning, cue=ordinal+1, start_ms=start, end_ms=end))
            if expected != ''.join(unit[1] for unit in units):
                raise ValueError('原始字幕正文与字形序列不一致，未采用合行。')
            units, edits, expected = _normalize_dialogue_hardbreaks(units)
            if edits:
                normalized.append(dict(cue=ordinal+1, start_ms=start, end_ms=end, edits=edits))
        paragraphs = _dialogue_paragraphs(units)
        lines = []
        for paragraph in paragraphs:
            if paragraph:
                lines.extend(_wrap_dialogue(paragraph, metrics, size, limit))
        if not lines:
            blocks.append((start, end, parts, None, 0., ordinal)); protected += 1; continue
        measured = [metrics.ink(''.join(u[1] for u in line), size, ass=True) for line in lines]
        total = sum(box[3]-box[1] for box in measured) + gap*(len(lines)-1)
        if total > REFERENCE_HEIGHT * .85:
            raise ValueError(f'第{ordinal+1}条字幕过长，固定B档需{len(lines)}行，超过画面安全高度；请编辑这条字幕或选择较小的手动字号。')
        blocks.append((start, end, parts, (lines, measured, expected), total, ordinal))
        if modern:
            chars = sum(not char.isspace() for char in expected)
            seconds = max(0., (end-start)/1000)
            reading_load.append(dict(cue=ordinal+1, start_ms=start, end_ms=end, lines=len(lines),
                                     characters=chars, seconds=round(seconds,3),
                                     characters_per_second=round(chars/seconds,2) if seconds else None))
    positions, active = {}, []
    for start, end, parts, layout, total, ordinal in sorted(blocks, key=lambda b: (b[0], b[5])):
        if layout is None:
            continue
        active = [(a, b, t) for a, b, t in active if a > start]
        bottom = REFERENCE_HEIGHT * .95
        # Concurrent ordinary cues occupy separate vertical bands. An interval
        # can be placed in a free lower band after an earlier cue disappears.
        for _, occupied_bottom, occupied_top in sorted(active, key=lambda a: -a[1]):
            if bottom > occupied_top and bottom-total < occupied_bottom:
                bottom = occupied_top-gap
        top = bottom-total
        if top < REFERENCE_HEIGHT*.05:
            raise ValueError('同时出现的字幕过多，B档字号无法在画面内完整显示；请检查重叠字幕或选择较小的手动字号。')
        positions[ordinal] = top
        active.append((end, bottom, top))
    output, max_lines, multiline, reflowed = [], 0, 0, 0
    for start, end, parts, layout, total, ordinal in blocks:
        if layout is None:
            parts[0] = str(len(output)); output.append((start, end, ','.join(parts))); continue
        lines, boxes, expected = layout; top = positions[ordinal]
        max_lines = max(max_lines, len(lines)); multiline += len(lines)>2; reflowed += 1
        for line, box in zip(lines, boxes):
            text, previous = [], None
            for literal, _, state in line:
                if state != previous:
                    text.append('{' + state + '}'); previous = state
                text.append(literal)
            # an8's anchor is the font cell. Account for that invisible cell so
            # the visible ink starts at top, independent of the font's leading.
            y = top - (size * metrics.baseline + box[1])
            x = canvas/2 - (box[0]+box[2]-box[4])/2
            body = '{\\an8\\q2\\pos(' + _number(x) + ',' + _number(y) + ')}' + ''.join(text)
            # mkvmerge trims whitespace at the end of an ASS packet. Retain
            # the original inter-word separator without adding a visible glyph.
            if line[-1][1].isspace():
                body += '{}'
            child = list(parts); child[0] = str(len(output)); child[8] = body
            output.append((start, end, ','.join(child)))
            top += box[3]-box[1]+gap
        original = expected.replace('\n', '')
        emitted = ''.join(u[1] for line in lines for u in line)
        if original != emitted:
            raise ValueError('换行改变了字幕正文，未采用。')
    return output, dict(reflowed_cues=reflowed, multiline_cues=multiline,
                        max_lines=max_lines, protected_cues=protected,
                        input_cues=len(events), output_events=len(output), layout_version=version,
                        hardbreak_joins=normalized, duplicate_events_removed=removed,
                        repeated_text_warnings=repeated,
                        reading_load=sorted(reading_load, key=lambda row:row['characters_per_second'] or 0,
                                            reverse=True)[:10])



def _dimensions(width, height):
    try:
        width, height = float(width), float(height)
    except (ValueError, TypeError):
        raise ValueError('影片画面尺寸无法读取。') from None
    if not math.isfinite(width) or not math.isfinite(height) or width <= 0 or height <= 0:
        raise ValueError('影片画面尺寸无效。')
    return width, height


def _drawing(value, sx, sy):
    tokens = re.findall(r'[A-Za-z]|' + _FLOAT, value)
    leftover = re.sub(r'[A-Za-z]|' + _FLOAT, '', value)
    if leftover.strip():
        raise ValueError('ASS绘图坐标包含无法转换的内容。')
    output, command, points = [], None, []

    def finish():
        if command is None:
            if points:
                raise ValueError('ASS绘图缺少命令。')
            return
        if command not in {'m', 'n', 'l', 'b', 's', 'p', 'c'}:
            raise ValueError(f'ASS绘图命令不支持：{command}')
        minimum = 0 if command == 'c' else 6 if command in {'b', 's'} else 2
        if len(points) < minimum or len(points) % 2 or (command == 'c' and points) or (command == 'b' and len(points) % 6):
            raise ValueError(f'ASS绘图命令{command}的坐标不完整。')
        output.append(command)
        output.extend(_number(float(point) * (sx if index % 2 == 0 else sy))
                      for index, point in enumerate(points))

    for token in tokens:
        if token.isalpha():
            finish()
            command, points = token.lower(), []
        else:
            points.append(token)
    finish()
    return ' '.join(output)


def _parentheses(argument, tag):
    stripped = argument.strip()
    if not stripped.startswith('(') or not stripped.endswith(')'):
        raise ValueError(f'ASS {tag}指令的参数无法安全转换。')
    return stripped[1:-1]


def _coordinate(tag, argument, sx, sy):
    value = _parentheses(argument, tag)
    parts = value.split(',')
    if tag in {'pos', 'org', 'move'}:
        counts = {2} if tag in {'pos', 'org'} else {4, 6}
        if len(parts) not in counts:
            raise ValueError(f'ASS {tag}指令的参数数量错误。')
        count = 2 if tag in {'pos', 'org'} else 4
        output = [_number(_numeric(item) * (sx if index % 2 == 0 else sy))
                  if index < count else item.strip() for index, item in enumerate(parts)]
        for item in parts[count:]:
            _numeric(item, 'ASS移动时间')
        return '(' + ','.join(output) + ')'
    if len(parts) == 4 and all(_NUMBER.fullmatch(item) for item in parts):
        return '(' + ','.join(_number(_numeric(item) * (sx if index % 2 == 0 else sy))
                              for index, item in enumerate(parts)) + ')'
    scale, drawing = '', value
    if len(parts) > 1:
        if len(parts) != 2 or not re.fullmatch(r'\s*[1-9]\d*\s*', parts[0]):
            raise ValueError(f'ASS {tag}绘图比例无效。')
        scale, drawing = parts[0].strip() + ',', parts[1]
    return '(' + scale + _drawing(drawing, sx, sy) + ')'


def _drawing_colour(value):
    match = re.fullmatch(r'&H([0-9A-Fa-f]{1,8})&?', value.strip())
    if match:
        rgb = int(match[1], 16) & 0xFFFFFF
    else:
        try:
            rgb = int(value) & 0xFFFFFF
        except (TypeError, ValueError):
            raise ValueError('ASS绘图原颜色无法读取。') from None
    return '&H' + format(rgb, '06X') + '&'


def _drawing_style(data):
    family = data['fontname']
    if not family or any(c in family for c in '\\{}\r\n'):
        raise ValueError('ASS绘图原字体名称不能安全写入。')
    return (family, _numeric(data['fontsize'], 'ASS绘图原字号'),
            _numeric(data.get('scalex', '100'), 'ASS绘图水平比例'),
            _numeric(data.get('scaley', '100'), 'ASS绘图垂直比例'),
            _numeric(data.get('spacing', '0'), 'ASS绘图间距'),
            _drawing_colour(data.get('primarycolour', '&H00FFFFFF')),
            _drawing_colour(data.get('secondarycolour', '&H00FFFFFF')))


def _border_commands(data, sx, sy):
    outline = _numeric(data.get('outline', '0'), 'ASS原描边')
    shadow = _numeric(data.get('shadow', '0'), 'ASS原阴影')
    return ('\\xbord' + _number(outline * sx) + '\\ybord' + _number(outline * sy)
            + '\\xshad' + _number(shadow * sx) + '\\yshad' + _number(shadow * sy))


def _rewrite_block(block, sx, sy, change_font, change_color, nested=False,
                   preserve_scale=False, preserve_appearance=False,
                   scaled_border=False, original_styles=None, initial_style=None):
    output = []
    for tag, argument, original_name in _tags(block):
        if not tag:
            output.append(argument)
            continue
        if (not preserve_appearance and (tag == 'fs' or (change_font and tag == 'fn')
                                        or (change_color and tag in {'c', '1c', '2c'}))) or (
                not preserve_scale and tag in {'fscx', 'fscy', 'fsp'}):
            continue
        if tag in {'pos', 'org', 'move', 'clip', 'iclip'}:
            argument = _coordinate(tag, argument, sx, sy)
        elif tag == 'fs' and preserve_appearance:
            argument = ('+' if argument.strip().startswith('+') else '') + _number(
                _numeric(argument, 'ASS绘图原字号') * sy)
        elif tag == 't':
            value = _parentheses(argument, tag)
            split = value.find('\\')
            if split < 0:
                raise ValueError('ASS动画没有可读取的指令。')
            prefix, commands = value[:split], value[split:]
            # Timing and acceleration precede the first instruction; retain their
            # spelling and never scale them as position coordinates.
            if prefix and not prefix.rstrip().endswith(','):
                raise ValueError('ASS动画时间参数格式错误。')
            prefix_values = prefix.strip().rstrip(',').split(',') if prefix.strip() else []
            if len(prefix_values) > 3:
                raise ValueError('ASS动画时间参数数量错误。')
            for item in prefix_values:
                _numeric(item, 'ASS动画时间/加速度')
            commands = _rewrite_block(commands, sx, sy, change_font, change_color,
                                      nested=True, preserve_scale=preserve_scale,
                                      preserve_appearance=preserve_appearance, scaled_border=scaled_border,
                                      original_styles=original_styles, initial_style=initial_style)
            if not commands.strip():
                continue  # A font-only animation must not restore the old size.
            argument = '(' + prefix + commands + ')'
        elif tag in {'bord', 'shad'} and scaled_border:
            value = _numeric(argument, 'ASS样式距离')
            output.append('\\x' + tag + _number(value * sx) + '\\y' + tag + _number(value * sy))
            continue
        elif tag in {'ybord', 'yshad'} and scaled_border:
            argument = _number(_numeric(argument, 'ASS样式距离') * sy)
        elif tag == 'pbo':
            argument = _number(_numeric(argument, 'ASS样式距离') * sy)
        elif tag in {'xbord', 'xshad'} and scaled_border:
            argument = _number(_numeric(argument, 'ASS样式距离') * sx)
        output.append('\\' + original_name + argument)
        if tag == 'r' and scaled_border:
            data = (original_styles or {}).get(argument.strip(), initial_style or {})
            output.append(_border_commands(data, sx, sy))
    return ''.join(output)


def _effect(value, sx, sy):
    if not value:
        return value
    parts = value.split(';')
    name = parts[0].strip().lower()
    if name in {'scroll up', 'scroll down'} and len(parts) in {4, 5}:
        for index in (1, 2, 4):
            if index < len(parts):
                parts[index] = _number(_numeric(parts[index], 'ASS滚动距离') * sy)
        return ';'.join(parts)
    if name == 'banner' and len(parts) in {2, 3, 4}:
        if len(parts) == 4:
            parts[3] = _number(_numeric(parts[3], 'ASS横幅距离') * sx)
        return ';'.join(parts)
    raise ValueError(f'ASS包含无法安全换算的效果：{value}')


def update(private, events, style, width, height):
    """Return styled ASS packets, CodecPrivate and coordinate diagnostics.

    ``style.size`` is already resolved (auto or manual) in 720-height units.
    ``change_font=False``/``change_color=False`` preserve that aspect, but never
    disable the specified numeric size. Positioned lines remain positioned.
    """
    width, height = _dimensions(width, height)
    try:
        content = private.decode('utf-8-sig')
    except (AttributeError, UnicodeError):
        raise ValueError('ASS样式头不是可读取的UTF-8文本。') from None
    if not re.search(r'^\s*\[V4\+ Styles\]\s*$', content, re.M | re.I):
        raise ValueError('此文本字幕不是支持的ASS v4+样式格式。')
    dimensions = {}
    for axis in ('X', 'Y'):
        match = re.search(r'^\s*PlayRes' + axis + r'\s*:\s*(\d+)\s*$', content, re.M | re.I)
        if not match or not int(match[1]):
            raise ValueError('ASS字幕缺少有效的画面尺寸，不能安全换算字号。')
        dimensions[axis] = int(match[1])
    newy, newx = REFERENCE_HEIGHT, max(1, round(width / height * REFERENCE_HEIGHT))
    sx, sy = newx / dimensions['X'], newy / dimensions['Y']
    border_match = re.search(r'^\s*ScaledBorderAndShadow\s*:\s*([^\r\n]+)', content, re.M | re.I)
    # Border/shadow distances use PlayRes only with this explicit switch. In
    # unscaled mode they use LayoutRes (or unchanged video storage dimensions),
    # just like blur. Never multiply those pixel distances by the font size.
    scaled_border = bool(border_match and border_match[1].strip().lower() in {'yes', '1', '-1', 'true'})
    for axis, value in (('X', newx), ('Y', newy)):
        content = re.sub(r'(^\s*PlayRes' + axis + r'\s*:\s*)\d+\s*$',
                         lambda m: m[1] + str(value), content, flags=re.M | re.I)
    size = style.get('size')
    if isinstance(size, bool) or not isinstance(size, (int, float)) or not math.isfinite(size) or size <= 0:
        raise ValueError('应用字幕时必须已有明确的正数字号。')
    change_font, change_color = style.get('change_font', True), style.get('change_color', True)
    family = style.get('family', '')
    if change_font and (not isinstance(family, str) or not family or any(c in family for c in ',\\{}\r\n')):
        raise ValueError('字体家族名称不能安全写入ASS。')
    rgb = style.get('color', '#FFFFFF')
    if change_color and (not isinstance(rgb, str) or not re.fullmatch(r'#[0-9a-fA-F]{6}', rgb)):
        raise ValueError('字幕颜色不是有效的RGB值。')
    bgr = rgb[5:7] + rgb[3:5] + rgb[1:3] if change_color else ''
    appearance = ('\\fn' + family if change_font else '') + '\\fs' + _number(size)
    if change_color:
        appearance += '\\1c&H' + bgr.upper() + '&\\2c&H' + bgr.upper() + '&'
    forced = appearance + '\\fscx100\\fscy100\\fsp0'
    lines, fields, in_styles, style_count, original_styles = [], None, False, 0, {}
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith('['):
            in_styles = stripped.lower() == '[v4+ styles]'
            fields = None
        if in_styles and stripped.lower().startswith('format:'):
            fields = [v.strip() for v in stripped.split(':', 1)[1].split(',')]
            if len(set(v.lower() for v in fields)) != len(fields):
                raise ValueError('ASS样式字段重复。')
        if in_styles and stripped.lower().startswith('style:'):
            values = [v.strip() for v in stripped.split(':', 1)[1].split(',')]
            if fields is None or len(values) != len(fields):
                raise ValueError('ASS样式字段不完整。')
            data = dict(zip((v.lower() for v in fields), values))
            if not {'name', 'fontname', 'fontsize'}.issubset(data):
                raise ValueError('ASS样式缺少字体或字号字段。')
            original_styles[data['name']] = dict(data)
            data['fontsize'] = _number(size)
            for key in ('scalex', 'scaley'):
                if key in data:
                    data[key] = '100'
            if 'spacing' in data:
                data['spacing'] = '0'
            if change_font:
                data['fontname'] = family
                for key in ('bold', 'italic'):
                    if key in data and key in style:
                        data[key] = '-1' if style[key] else '0'
            if change_color:
                for key in ('primarycolour', 'secondarycolour'):
                    if key in data:
                        # Preserve original opacity; RGB is the selected value.
                        alpha = re.fullmatch(r'&H([0-9A-Fa-f]{8})&?', data[key])
                        data[key] = '&H' + (alpha[1][:2] if alpha else '00') + bgr.upper()
            for key, factor in (('marginl', sx), ('marginr', sx), ('marginv', sy),
                                ('outline', sy if scaled_border else 1),
                                ('shadow', sy if scaled_border else 1)):
                if key in data:
                    value = _numeric(data[key], 'ASS样式距离') * factor
                    data[key] = str(round(value)) if key.startswith('margin') else _number(value)
            line = 'Style: ' + ','.join(data[key.lower()] for key in fields)
            style_count += 1
        lines.append(line)
    if not style_count:
        raise ValueError('ASS字幕没有可修改的样式。')
    output, positioned, drawing_runs = [], 0, 0
    for start, end, payload in events:
        if not isinstance(payload, str):
            raise ValueError('ASS字幕包不是UTF-8文本。')
        parts = payload.split(',', 8)
        if len(parts) != 9:
            raise ValueError('ASS字幕包字段不完整。')
        for index, factor in ((4, sx), (5, sx), (6, sy)):
            if parts[index]:
                parts[index] = str(round(_numeric(parts[index], 'ASS字幕边距') * factor))
        parts[7] = _effect(parts[7], sx, sy)
        initial_style = original_styles.get(parts[2], next(iter(original_styles.values())))
        source_style = initial_style
        draw_family, draw_size, scale_x, scale_y, spacing, draw_primary, draw_secondary = _drawing_style(source_style)
        geometry = _border_commands(initial_style, sx, sy) if scaled_border else ''
        result, drawing = ['{' + geometry + forced + '}'], 0
        for override, part in _runs(parts[8]):
            if override:
                for tag, argument, _ in _tags(part):
                    if tag == 'p':
                        drawing = int(_numeric(argument, 'ASS绘图模式'))
                    elif tag == 'r':
                        drawing = 0
                        source_style = original_styles.get(argument.strip(), initial_style)
                        draw_family, draw_size, scale_x, scale_y, spacing, draw_primary, draw_secondary = _drawing_style(source_style)
                    elif tag == 'fn':
                        draw_family = argument.strip() or source_style['fontname']
                    elif tag == 'fs':
                        value = _numeric(argument, 'ASS绘图原字号')
                        draw_size = draw_size + value if argument.strip().startswith(('+', '-')) else value
                    elif tag in {'c', '1c'}:
                        draw_primary = argument.strip() or _drawing_colour(source_style.get('primarycolour', '&H00FFFFFF'))
                    elif tag == '2c':
                        draw_secondary = argument.strip() or _drawing_colour(source_style.get('secondarycolour', '&H00FFFFFF'))
                    elif tag == 'fscx':
                        scale_x = _numeric(argument, 'ASS绘图水平比例')
                    elif tag == 'fscy':
                        scale_y = _numeric(argument, 'ASS绘图垂直比例')
                    elif tag == 'fsp':
                        spacing = _numeric(argument, 'ASS绘图间距')
                    elif tag in {'pos', 'move', 'org'}:
                        positioned += 1
                mapped = _rewrite_block(part, sx, sy, change_font, change_color,
                                        preserve_scale=bool(drawing), preserve_appearance=bool(drawing),
                                        scaled_border=scaled_border, original_styles=original_styles,
                                        initial_style=initial_style)
                # Glyph scale must obey the selected numeric size. Vector paths,
                # however, retain their own scale instead of being treated as
                # letters; restoring p0 switches back to the enforced text size.
                if drawing:
                    # Shapes are not subtitle letters: restore their own fill,
                    # font metrics and scale. Only map the original coordinates.
                    applied = ('\\fn' + draw_family + '\\fs' + _number(draw_size * sy)
                               + '\\1c' + draw_primary + '\\2c' + draw_secondary
                               + '\\fscx' + _number(scale_x) + '\\fscy' + _number(scale_y)
                               + '\\fsp' + _number(spacing * sy))
                else:
                    applied = forced
                result.append('{' + mapped + applied + '}')
            elif drawing:
                result.append(_drawing(part, sx, sy))
                drawing_runs += 1
            else:
                result.append(part)
        parts[8] = ''.join(result)
        output.append((start, end, ','.join(parts)))
    diagnostics = dict(old=[dimensions['X'], dimensions['Y']], new=[newx, newy],
                       scale=[sx, sy], selected_size=size, style_count=style_count,
                       event_count=len(output), positioned_instructions=positioned,
                       drawing_runs=drawing_runs, change_font=bool(change_font),
                       change_color=bool(change_color), scaled_border_and_shadow=scaled_border,
                       blur_layout_preserved=True)
    return output, ('\n'.join(lines) + '\n').encode('utf8'), diagnostics
