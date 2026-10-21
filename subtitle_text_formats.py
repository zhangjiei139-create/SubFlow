"""Small, conservative text views for legacy styling embedded in SRT.

Keep source/cache keys untouched.  Only known style syntax is interpreted here;
unrecognized braces remain dialogue and are never broadly deleted.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser


_STYLE_ORDER = "bius"
_LEGACY_OR_BREAK = re.compile(r"(?<!\\)\{([yY]):([ibus]{1,4})\}|\r\n|\r|\n|<br\s*/?>", re.I)


def normalize_srt_markup(value: str) -> str:
    """Convert declared MicroDVD style tokens into safe SRT HTML markup.

    Lowercase ``y`` is local to a physical/HTML line; uppercase ``Y`` persists
    within this cue.  No timings, pipe characters, font choices or coordinates
    are inferred.  Backslash-escaped and unknown brace text stays literal.
    """
    value = str(value)
    if not re.search(r"(?<!\\)\{[yY]:[ibus]{1,4}\}", value):
        return value
    output: list[str] = []
    local: set[str] = set()
    persistent: set[str] = set()
    active: tuple[str, ...] = ()

    def change(desired: set[str]) -> None:
        nonlocal active
        ordered = tuple(tag for tag in _STYLE_ORDER if tag in desired)
        if ordered == active:
            return
        output.extend(f"</{tag}>" for tag in reversed(active))
        output.extend(f"<{tag}>" for tag in ordered)
        active = ordered

    cursor = 0
    for match in _LEGACY_OR_BREAK.finditer(value):
        output.append(value[cursor:match.start()])
        if match[1] is None:
            local.clear()
            change(persistent)
            output.append(match[0])
        else:
            # Only the declared lowercase style alphabet is accepted.  Case
            # of y/Y carries scope, not an invitation to parse unknown tags.
            flags = match[2]
            if any(flag not in _STYLE_ORDER for flag in flags):
                output.append(match[0])
                cursor = match.end()
                continue
            if match[1] == "Y":
                persistent = set(flags)
            else:
                local = set(flags)
            change(local | persistent)
        cursor = match.end()
    output.append(value[cursor:])
    change(set())
    return ''.join(output)


_NUMBER = r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)"
_ASS_BLOCK = re.compile(r"(?<!\\)\{([^{}]*)\}")
_ASS_NUMERIC = frozenset({"b", "i", "u", "s", "fs", "fscx", "fscy", "fsp",
    "fe", "an", "a", "frx", "fry", "frz", "fr", "fax", "fay", "bord",
    "xbord", "ybord", "shad", "xshad", "yshad", "blur", "be"})
_ASS_COLOR = frozenset({"c", "1c", "2c", "3c", "4c", "alpha", "1a", "2a", "3a", "4a"})
_ASS_NAMES = tuple(sorted(_ASS_NUMERIC | _ASS_COLOR | {"fn", "r", "pos", "org", "move", "fad", "fade"}, key=len, reverse=True))


def _known_ass_block(match: re.Match) -> str:
    """Drop a complete, valid presentation block; retain unknown/literal text."""
    block = match[1]
    cursor = 0
    found = False
    while cursor < len(block):
        if block[cursor] != '\\':
            return match[0]
        cursor += 1
        name = next((name for name in _ASS_NAMES if block[cursor:].lower().startswith(name)), '')
        if not name:
            return match[0]
        cursor += len(name)
        end = block.find('\\', cursor)
        end = len(block) if end < 0 else end
        argument = block[cursor:end].strip()
        if name in _ASS_NUMERIC:
            valid = not argument or re.fullmatch(_NUMBER, argument) is not None
        elif name in _ASS_COLOR:
            valid = not argument or re.fullmatch(r"&H[0-9A-Fa-f]{1,8}&?", argument) is not None
        elif name in {"pos", "org", "move", "fad", "fade"}:
            count = {"pos": {2}, "org": {2}, "move": {4, 6}, "fad": {2}, "fade": {7}}[name]
            values = argument[1:-1].split(',') if argument.startswith('(') and argument.endswith(')') else []
            valid = len(values) in count and all(re.fullmatch(_NUMBER, item.strip()) for item in values)
        else:  # font/reset names contain no nested brace or command.
            valid = not any(char in argument for char in '{}\r\n')
        if not valid:
            return match[0]
        found = True
        cursor = end
    return '' if found else match[0]


class _TextView(HTMLParser):
    def __init__(self, source: str) -> None:
        super().__init__(convert_charrefs=True)
        self.source = source
        self.line_offsets = [0]
        self.line_offsets.extend(match.end() for match in re.finditer('\n', source))
        self.output: list[str] = []

    def raw_token(self, terminator: str = '>') -> str:
        line, column = self.getpos()
        start = self.line_offsets[line-1] + column
        end = self.source.find(terminator, start)
        return self.source[start:] if end < 0 else self.source[start:end+len(terminator)]

    def handle_data(self, data: str) -> None:
        self.output.append(data)

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag == 'br':
            self.output.append('\n')
        elif tag not in {'b', 'i', 'u', 's', 'font'}:
            self.output.append(self.get_starttag_text())

    def handle_endtag(self, tag: str) -> None:
        if tag not in {'b', 'i', 'u', 's', 'font', 'br'}:
            self.output.append(self.raw_token())

    def handle_startendtag(self, tag: str, attrs: list) -> None:
        if tag == 'br':
            self.output.append('\n')
        elif tag not in {'b', 'i', 'u', 's', 'font'}:
            self.output.append(self.get_starttag_text())

    def handle_comment(self, data: str) -> None:
        self.output.append(self.raw_token('-->'))

    def handle_decl(self, decl: str) -> None:
        self.output.append(self.raw_token())

    def handle_pi(self, data: str) -> None:
        self.output.append(self.raw_token())

    def unknown_decl(self, data: str) -> None:
        self.output.append(self.raw_token(']]>' if data.startswith('CDATA[') else '>'))


def translation_text(value: str) -> str:
    """Return a formatting-free model/quality view, never a replacement cache key."""
    normalized = normalize_srt_markup(str(value))
    normalized = _ASS_BLOCK.sub(_known_ass_block, normalized)
    parser = _TextView(normalized)
    parser.feed(normalized)
    parser.close()
    return ''.join(parser.output)
