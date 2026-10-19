"""Track purpose clues, never a guess based on channel count or voice density."""
import re

RULE = 'audio-purpose-v1'

def excluded_reason(track):
    if getattr(track, 'commentary', False):
        return '容器标记为评论音轨'
    if getattr(track, 'visual_impaired', False):
        return '容器标记为口述解说音轨'
    name = str(getattr(track, 'name', '') or '').casefold()
    patterns = (
        (r'\bcommentary\b|\bcomments?\b|评论|評論|解说|解說', '名称标记为评论/解说音轨'),
        (r'\baudio[ ._-]*description\b|\bdescriptive[ ._-]*audio\b|\bdescriptive\b|口述影像|视障|視障', '名称标记为口述解说音轨'),
        (r'\bisolated[ ._-]*(?:score|music)\b|\bmusic[ ._-]*only\b|纯音乐|純音樂', '名称标记为纯音乐音轨'),
        (r'\bpartial(?:[ ._-]*dialogue)?\b|\bsample[ ._-]*only\b|片段音轨|片段音軌', '名称标记为片段音轨'),
    )
    return next((reason for pattern, reason in patterns if re.search(pattern, name)), '')

def dialogue_tracks(tracks):
    return [t for t in tracks if t.type == 'audio' and not excluded_reason(t)]

def describe(track):
    channels = getattr(track, 'channels', None)
    layout = f'{channels}声道' if channels else '声道未知'
    reason = excluded_reason(track)
    return f'轨{track.id} · {getattr(track,"language","und")} · {track.codec} · {layout} · {getattr(track,"name","") or "用途未标注"}' + (f'（{reason}）' if reason else '')
