"""Cheap, language-independent selection of windows for human listening.

Text and VAD describe candidate quality, never establish subtitle correctness.
No media is opened here. Counts refer to subtitle cues, not recognized words.
"""
from __future__ import annotations

import math
import re

WINDOW_SELECTION_RULE = 'manual-dialogue-pauses-v1-20261010'
WINDOW = 60.0
_MARKUP = re.compile(r'<[^>]*>|\{\\[^}]*\}')
_BRACKETS = re.compile(r'\[([^\]]*)\]|\(([^)]*)\)|【([^】]*)】|（([^）]*)）')
_MUSIC = re.compile('[♪♫♬♩🎵🎶]')
_SUNG = re.compile(r'\b(?:singing|sings|sung|chanting|chants)\b|唱歌|歌唱|吟唱',re.I)
_SPOKEN_AGAIN = re.compile(r'\[(?:speaking|spoken|说话|說話)\]|\((?:speaking|spoken)\)',re.I)
# Deliberately recognize stage directions, not every parenthesized sentence.
_DIRECTION = re.compile(
    r'\b(?:music|musical|instrumental|orchestral|theme|score|song|singing|lyrics?|'
    r'growl(?:s|ing)?|roar(?:s|ing)?|beep(?:s|ing)?|whistl(?:e|es|ing)|'
    r'footsteps?|gunshots?|explosions?|screams?|screaming|sigh(?:s|ing)?|'
    r'gasp(?:s|ing)?|pant(?:s|ing)?|breath(?:ing)?|laugh(?:s|ing|ter)?|'
    r'crying|sobbing|grunts?|grunting|groan(?:s|ing)?|snort(?:s|ing)?|'
    r'whimper(?:s|ing)?|yelp(?:s|ing)?|sniff(?:s|ing)?|chatter(?:s|ing)?|'
    r'chuckles?|chuckling|rumbl(?:e|es|ing)|'
    r'thunder|applause|cheering|sfx|inaudible|indistinct|unintelligible|'
    r'speaking|muffled|cough(?:s|ing)?|silence|door.*(?:opens?|closes?)|'
    r'musik|geräusche?|lacht|seufzt|stöhnt|schreit|musique|rires|música)\b|'
    r'音乐|音樂|配乐|配樂|歌声|歌聲|奏乐|奏樂|吼叫|咆哮|脚步|腳步|'
    r'喘气|喘氣|喘息|呼吸|叹气|嘆氣|笑声|笑聲|哭声|哭聲|枪声|槍聲|'
    r'爆炸|尖叫|哨声|哨聲|口哨|嘟嘟|嘀嘀|轰鸣|轟鳴|雷声|雷聲|音效|'
    r'听不清|聽不清|掌声|掌聲', re.I)
_CREDITS = re.compile(r'^(?:subtitles?\s+(?:by|from)|translated\s+by|'
                      r'字幕(?:制作|製作|翻译|翻譯)|翻译[:：]).*$', re.I)


def dialogue_text(text):
    """Exclude marked lyrics/effects; retain speech in mixed and quoted cues."""
    clean = _MARKUP.sub('', str(text or ''))
    lines = []
    for line in clean.splitlines():
        for mark in _BRACKETS.finditer(line):
            inside=next(group for group in mark.groups() if group is not None)
            if _SUNG.search(inside) and not re.search(r'\b(?:I|you|we|my|your|our)\b|我|你|您|咱',inside,re.I):
                spoken=_SPOKEN_AGAIN.search(line,mark.end())
                line=line[:mark.start()]+(' '+line[spoken.end():] if spoken else '')
                break
        # Subtitle parsers may flatten newlines: "♪ lyrics ♪\nRun!" becomes
        # one line. Remove only paired lyric spans before examining the rest.
        line = re.sub(r'[♪♫♬♩🎵🎶][^♪♫♬♩🎵🎶]*[♪♫♬♩🎵🎶]', ' ', line)
        if _MUSIC.search(line) or _CREDITS.fullmatch(line.strip()):
            continue
        def annotation(match):
            inside = next(group for group in match.groups() if group is not None)
            # Square brackets are SDH speaker/stage annotations; parentheses
            # and East Asian quotation marks can also enclose spoken words.
            if match.group(1) is not None:
                return ' '
            if re.search(r'\b(?:I|you|we|my|your|our)\b|我|你|您|咱',inside,re.I):
                return inside
            return ' ' if _DIRECTION.search(inside) else inside
        line = _BRACKETS.sub(annotation, line)
        line = re.sub(r'^\s*[-–—]?\s*[A-Z][A-Z .\'’-]{1,35}:\s*', '', line)
        if any(char.isalpha() or char.isdigit() for char in line):
            lines.append(line.strip())
    return ' '.join(lines).strip()


def _seconds(value):
    if isinstance(value, (int, float)):
        return float(value)
    hours, minutes, seconds = str(value).replace(',', '.').split(':')
    return float(hours)*3600 + float(minutes)*60 + float(seconds)


def _event(event, name):
    return event[name] if isinstance(event, dict) else getattr(event, name)


def _merge(pairs, bridge=0.0):
    result = []
    for start, end in sorted(pairs):
        if end <= start:
            continue
        if result and start <= result[-1][1] + bridge:
            result[-1][1] = max(end, result[-1][1])
        else:
            result.append([start, end])
    return result


def shortlist_windows(events, duration, region, limit=3):
    if region not in (1, 2, 3) or duration < 180 or limit <= 0:
        return []
    timed = [(_seconds(_event(e, 'start')), _seconds(_event(e, 'end')),
              bool(dialogue_text(_event(e, 'text')))) for e in events]
    lo, hi = (region-1)*duration/3, region*duration/3
    first, last = math.ceil(lo/30)*30, math.floor((hi-WINDOW)/30)*30
    scored = []
    for start in range(int(first), int(last)+1, 30):
        cues = [(max(a, start), min(b, start+WINDOW), spoken)
                for a, b, spoken in timed if b > start and a < start+WINDOW and b > a]
        dialogue = [(a, b) for a, b, spoken in cues if spoken]
        count = len(dialogue)
        halves = [sum((a+b)/2 < start+30 for a, b in dialogue), 0]
        halves[1] = count-halves[0]
        if count < 6 or min(halves) < 2:
            continue
        occupied = _merge(dialogue)
        seconds = sum(b-a for a, b in occupied)
        pauses = [b-a for (_, a), (b, _) in zip(occupied, occupied[1:])]
        useful = sum(2 <= gap <= 8 for gap in pauses)
        preferred = (8 <= count <= 16 and 15 <= seconds <= 40
                     and useful >= 2 and min(halves) >= 3)
        # A long empty intro/outro cannot improve this score. More speech is
        # not inherently better, and excessive cue counts incur a penalty.
        score = 10*min(useful, 3)-abs(seconds-28)*.2-abs(count-12)*.25
        scored.append(dict(start=float(start), cue_count=len(cues),
                           dialogue_count=count, occupied_seconds=round(seconds, 3),
                           half_counts=halves, gap_count=useful, pause_count=useful,
                           tier=0 if preferred else 1, score=round(score, 4)))
    scored.sort(key=lambda row: (row['tier'], -row['score'], row['start']))
    chosen = []
    for row in scored:
        if all(abs(row['start']-other['start']) >= 120 for other in chosen):
            chosen.append(row)
        if len(chosen) >= min(3, limit):
            break
    return chosen


def audio_window_quality(vad, duration=60.0):
    """Measure activity distribution without assuming audio/subtitle alignment."""
    occupied = _merge(((max(0., float(a)), min(duration, float(b)))
                       for a, b in vad if math.isfinite(a) and math.isfinite(b)), .15)
    spoken = sum(b-a for a, b in occupied)
    mid = duration/2
    halves = [sum(max(0., min(b, mid)-a) for a, b in occupied if a < mid),
              sum(max(0., b-max(a, mid)) for a, b in occupied if b > mid)]
    pauses = sum(2 <= b-a <= 8 for (_, a), (b, _) in zip(occupied, occupied[1:]))
    usable = spoken >= 6 and min(halves) >= 1
    return dict(spoken_seconds=round(spoken, 2), half_seconds=[round(x, 2) for x in halves],
                pause_count=pauses, usable=usable,
                preferred=usable and 12 <= spoken <= 45 and min(halves) >= 3 and pauses >= 2)
