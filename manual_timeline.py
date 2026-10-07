"""Manual three-anchor correction. Offsets always refer to original cue times."""
from __future__ import annotations
from bisect import bisect_right
import math


def validate(points, offsets):
    if len(points) != 3 or len(offsets) != 3:
        raise ValueError('需要且只需要前、中、后三个调节值。')
    if not all(math.isfinite(float(v)) for v in (*points, *offsets)):
        raise ValueError('时间和偏移必须为有限数值。')
    if any(abs(v) > 20 for v in offsets):
        raise ValueError('手动偏移范围为 ±20 秒。')
    if any(b <= a for a, b in zip(points, points[1:])):
        raise ValueError('三个核听点必须按前、中、后排列。')
    # Human observations must agree with a single gradual trend. Reuse the
    # existing timing tolerance; do not fit an arbitrary kink through any three
    # values and call an irregular timeline repaired.
    from subtitle_offset_guard import REGION_CLUSTER_TOLERANCE_SECONDS
    ratio = (points[1] - points[0]) / (points[2] - points[0])
    expected_middle = offsets[0] + ratio * (offsets[2] - offsets[0])
    if abs(offsets[1] - expected_middle) > REGION_CLUSTER_TOLERANCE_SECONDS:
        raise ValueError('中点明显不符合前后趋势，无法用统一或线性修正解决；请换字幕或取消。')
    # Also prevent a backwards time mapping.
    mapped = [p + d for p, d in zip(points, offsets)]
    if any(b <= a for a, b in zip(mapped, mapped[1:])):
        raise ValueError('当前修正会使字幕时间倒序，请调整偏移。')


def map_time(value, points, offsets):
    if value <= points[0]:
        return value + offsets[0]
    if value >= points[-1]:
        return value + offsets[-1]
    i = bisect_right(points, value) - 1
    ratio = (value - points[i]) / (points[i + 1] - points[i])
    return value + offsets[i] + ratio * (offsets[i + 1] - offsets[i])


def apply_to_srt(source, destination, points, offsets):
    import subtitle_tool_core as core
    validate(points, offsets)
    def seconds(s):
        h, m, t = s.replace(',', '.').split(':')
        return int(h) * 3600 + int(m) * 60 + float(t)
    events = core.parse_subtitle(source)
    if not events:
        raise ValueError('字幕没有可修正的时间条目。')
    corrected = []
    for event in events:
        start, end = seconds(event.start), seconds(event.end)
        if end <= start:
            raise ValueError('原字幕存在结束早于开始的条目。')
        a, b = map_time(start, points, offsets), map_time(end, points, offsets)
        # Clipping at movie zero may collapse an early cue. Retain its text
        # with a minimal positive duration instead of dropping or reordering it.
        a = max(0, round(a * 1000)); b = max(a + 1, round(b * 1000))
        corrected.append(core.SubtitleEvent(core.srt_time_from_milliseconds(a), core.srt_time_from_milliseconds(b), event.text))
    core.write_srt(destination, corrected, {i: e.text for i, e in enumerate(corrected, 1)})
    return destination
