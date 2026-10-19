# -*- coding: utf-8 -*-
"""Three short manual-review windows; no whole-film VAD or Whisper."""
from __future__ import annotations
import hashlib, json, math, os, shutil, threading, time, wave, zipfile
from pathlib import Path
import numpy as np
import manual_timeline
import pro_core
import subtitle_tool_core as core
from manual_window_selection import (WINDOW_SELECTION_RULE, audio_window_quality,
                                     dialogue_text, shortlist_windows)

WINDOW = 60.0
SHORT_AUDIO_RULE = 'manual-short-window-v1-video-axis-20261002'
SHORT_AUDIO_RATE = 16000
_window_locks_guard = threading.Lock()
_window_locks = {}


def _window_lock(key):
    with _window_locks_guard:
        return _window_locks.setdefault(key, threading.Lock())


def _file_signature(path):
    target=Path(path).resolve()
    result=dict(path=os.path.normcase(str(target)))
    try:
        stat=target.stat()
        result.update(size=stat.st_size,mtime_ns=stat.st_mtime_ns)
    except OSError:
        pass
    return result


def _short_audio_signature(movie,start,audio_id,audio_index,codec,engine,decoder_options,input_options):
    movie_signature=_file_signature(movie)
    if 'size' not in movie_signature:
        raise RuntimeError('无法读取影片文件信息，不能复用人工核听短音频。')
    signature=dict(rule=SHORT_AUDIO_RULE,movie=movie_signature,
        audio_id=audio_id,audio_stream_index=audio_index,audio_codec=codec,
        decode_mode=pro_core.vad_decode_mode(codec),decoder_options=list(decoder_options),
        window_start=round(float(start),6),window_seconds=WINDOW,
        timeline='video-relative',input_options=list(input_options),
        filter='aresample=16000:async=1:first_pts=0',rate=SHORT_AUDIO_RATE,
        channels=1,sample_width=2,vad='webrtc',vad_frame_rate=48000,
        half_speed_filter='atempo=0.5',ffmpeg=_file_signature(core.FFMPEG),
        engine=_file_signature(engine))
    key=hashlib.sha256(json.dumps(signature,sort_keys=True,ensure_ascii=False).encode('utf-8')).hexdigest()
    return signature,key


def _pcm_duration(path,expected):
    with wave.open(str(path),'rb') as audio:
        if audio.getframerate()!=SHORT_AUDIO_RATE or audio.getnchannels()!=1 or audio.getsampwidth()!=2:
            raise ValueError('短音频格式不符合人工核听要求。')
        duration=audio.getnframes()/audio.getframerate()
    if abs(duration-expected)>.25:
        raise ValueError('短音频时长不符合人工核听窗口。')
    return duration


def _load_short_audio(wav,slow,npz,require_slow=True):
    duration=_pcm_duration(wav,WINDOW)
    if require_slow: _pcm_duration(slow,WINDOW*2)
    with np.load(npz,allow_pickle=False) as fingerprint:
        frames=fingerprint['speech']
        if frames.ndim!=1 or not len(frames) or not np.isfinite(frames).all() or abs(len(frames)/100-duration)>.25:
            raise ValueError('局部声音标记损坏或与短音频时长不符。')
        return vad_intervals(frames>=1)


def _artifact_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_short_cache(folder,signature,wav,slow,npz,require_slow=True):
    try:
        manifest=json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
        if manifest.get('signature')!=signature: return None
        names=('audio.wav','audio.half.wav','audio.npz') if require_slow else ('audio.wav','audio.npz')
        cached=[folder/name for name in names]
        if any(_artifact_hash(path)!=manifest.get('artifacts',{}).get(path.name) for path in cached): return None
        vad=_load_short_audio(folder/'audio.wav',folder/'audio.half.wav',folder/'audio.npz',require_slow)
        destinations=(wav,slow,npz) if require_slow else (wav,npz)
        for source,destination in zip(cached,destinations):
            shutil.copy2(source,destination)
        if not require_slow:
            if slow.exists(): slow.unlink()
            cached_slow=folder/'audio.half.wav'
            if cached_slow.is_file() and _artifact_hash(cached_slow)==manifest.get('artifacts',{}).get(cached_slow.name):
                _pcm_duration(cached_slow,WINDOW*2)
                shutil.copy2(cached_slow,slow)
        return vad
    except (OSError,ValueError,KeyError,EOFError,wave.Error,zipfile.BadZipFile):
        return None


def _write_short_cache(folder,signature,wav,slow,npz):
    folder.mkdir(parents=True,exist_ok=True)
    hashes={}
    for source,name in zip((wav,slow,npz),('audio.wav','audio.half.wav','audio.npz')):
        if name=='audio.half.wav' and not source.is_file(): continue
        target=folder/name
        pending=target.with_name(name+'.pending')
        shutil.copy2(source,pending)
        pending.replace(target)
        hashes[name]=_artifact_hash(target)
    manifest=folder/'manifest.json'
    pending=manifest.with_suffix('.pending')
    pending.write_text(json.dumps(dict(signature=signature,artifacts=hashes),ensure_ascii=False,indent=2),encoding='utf-8')
    pending.replace(manifest)

def seconds(stamp):
    return pro_core._subtitle_time_seconds(stamp)

def nearby(events, start, margin=0.0):
    return [e for e in events if seconds(e.end)>start-margin and seconds(e.start)<start+WINDOW+margin]

def merged(pairs):
    out=[]
    for a,b in sorted(pairs):
        if b<=a: continue
        if out and a<=out[-1][1]: out[-1][1]=max(b,out[-1][1])
        else: out.append([a,b])
    return out

def vad_intervals(frames):
    out=[]; opened=None
    for index,active in enumerate(list(frames)+[0]):
        if active and opened is None: opened=index
        elif not active and opened is not None:
            a,b=opened/100,index/100
            if out and a-out[-1][1]<=.15: out[-1][1]=b
            else: out.append([a,b])
            opened=None
    return [[round(a,2),round(b,2)] for a,b in out if b-a>=.1]

def review_points(evidence):
    return [float(clip['subtitle_time']) for clip in evidence['clips']]

def manual_offsets_rejection(evidence,offsets):
    clips=evidence.get('clips',())
    if len(clips)!=3 or [clip.get('region') for clip in clips]!=[1,2,3]:
        return '前、中、后三段核听材料不完整。'
    try: manual_timeline.validate(review_points(evidence),offsets)
    except (ValueError,TypeError) as exc: return str(exc)
    return ''
def run_limited(args, deadline, limit, description):
    local=pro_core._DeadlineCancel(deadline,time.monotonic()+limit)
    try: return core.run_command(args,log=lambda _:None,cancel_event=local)
    except core.CancelledError as exc:
        if local.timed_out: raise RuntimeError(f'{description}超过 {limit:.0f} 秒上限。') from exc
        raise

def prepare_window(movie,events,work,region,option,audio_index,engine,log,deadline,*,audio_id=None,audio_codec='',prepare_slow=True):
    start=option['start']; stem=f'region-{region}-{int(start)}'
    wav=work/f'{stem}.wav'; slow=work/f'{stem}.half.wav'
    npz=wav.with_suffix('.npz')
    decoder_options=pro_core.vad_decoder_options(audio_codec)
    input_options=pro_core.vad_window_input_options(movie,start)
    signature,key=_short_audio_signature(movie,start,audio_id,audio_index,audio_codec,
        engine,decoder_options,input_options)
    film=Path(movie).resolve()
    cache=film.parent/(film.stem+'_pro_work')/'manual-window-cache'/key[:24]
    cache_hit=False
    with _window_lock(key):
        core.check_cancel(deadline)
        vad=_read_short_cache(cache,signature,wav,slow,npz,prepare_slow)
        if vad is not None:
            cache_hit=True
            log(f'人工时间轴第 {region} 区：复用影片 {start:.0f}～{start+60:.0f} 秒短音频和局部声音标记。')
        else:
            log(f'人工时间轴第 {region} 区：读取影片 {start:.0f}～{start+60:.0f} 秒短音频（{signature["decode_mode"]}）。')
            run_limited([core.FFMPEG,'-y','-hide_banner','-nostdin','-loglevel','error',
                         *input_options,*decoder_options,'-i',movie,'-t','60','-map',f'0:a:{audio_index}',
                         '-vn','-sn','-dn','-af','aresample=16000:async=1:first_pts=0',
                         '-ac','1','-ar',str(SHORT_AUDIO_RATE),'-c:a','pcm_s16le',str(wav)],
                        deadline,60,'短音频提取')
            if not wav.is_file() or not wav.stat().st_size: raise RuntimeError(f'第 {region} 区短音频为空。')
            _pcm_duration(wav,WINDOW)
            run_limited([engine,str(wav),'--serialize-speech','--vad','webrtc',
                         '--frame-rate','48000','--ffmpeg-path',str(Path(core.FFMPEG).parent)],
                        deadline,35,'局部声音标记')
            if not npz.is_file(): raise RuntimeError(f'第 {region} 区未生成声音标记。')
            if prepare_slow:
                run_limited([core.FFMPEG,'-y','-hide_banner','-nostdin','-loglevel','error',
                             '-i',str(wav),'-af','atempo=0.5','-ar',str(SHORT_AUDIO_RATE),'-ac','1',
                             '-c:a','pcm_s16le',str(slow)],deadline,40,'半速试听音频准备')
            elif slow.exists():
                slow.unlink()
            vad=_load_short_audio(wav,slow,npz,prepare_slow)
            core.check_cancel(deadline)
            try: _write_short_cache(cache,signature,wav,slow,npz)
            except OSError: log('人工核听短音频缓存写入未完成；本次已准备的核听材料仍可使用。')
        core.check_cancel(deadline)
    rows=[dict(start=round(seconds(e.start),3),end=round(seconds(e.end),3),
               text=e.text,zh='',is_dialogue=bool(dialogue_text(e.text))) for e in nearby(events,start,20)]
    quality=audio_window_quality(vad)
    return dict(region=region,movie_start=start,subtitle_time=start+30,
                duration=60.0,audio=str(wav),slow_audio=str(slow),vad=vad,
                spoken_seconds=round(sum(b-a for a,b in vad),2),rows=rows,
                cue_count=option['cue_count'],large_gap_count=option['gap_count'],
                initial_offset=0.0,audio_id=audio_id,audio_stream_index=audio_index,
                audio_codec=audio_codec,decode_mode=signature['decode_mode'],
                timeline='video-relative',short_audio_cache_hit=cache_hit,
                dialogue_count=option.get('dialogue_count',sum(bool(dialogue_text(e.text)) for e in nearby(events,start))),
                occupied_seconds=option.get('occupied_seconds'),half_counts=option.get('half_counts'),
                selection_tier=option.get('tier',1),selection_score=option.get('score',0),
                selection_rule=WINDOW_SELECTION_RULE,audio_quality=quality,
                selection_warning='' if option.get('tier',1)==0 and quality['preferred'] else
                    '本段为后备片段：对白或声音停顿不够均衡，可试听后换同区片段。',
                _cache_folder=str(cache),_audio_signature=signature)


def ensure_half_audio(clip,log,deadline):
    wav,slow=Path(clip['audio']),Path(clip['slow_audio'])
    try:
        _pcm_duration(slow,WINDOW*2)
        return
    except (OSError,ValueError,EOFError,wave.Error):
        pass
    run_limited([core.FFMPEG,'-y','-hide_banner','-nostdin','-loglevel','error',
                 '-i',str(wav),'-af','atempo=0.5','-ar',str(SHORT_AUDIO_RATE),'-ac','1',
                 '-c:a','pcm_s16le',str(slow)],deadline,40,'半速试听音频准备')
    _pcm_duration(slow,WINDOW*2)
    core.check_cancel(deadline)
    if clip.get('_cache_folder'):
        cache=Path(clip['_cache_folder'])
        # Match the key used by prepare_window; it uses the full signature hash.
        key=hashlib.sha256(json.dumps(clip['_audio_signature'],sort_keys=True,ensure_ascii=False).encode('utf-8')).hexdigest()
        with _window_lock(key):
            try: _write_short_cache(cache,clip['_audio_signature'],wav,slow,wav.with_suffix('.npz'))
            except OSError: log('人工核听半速音频缓存写入未完成；本次仍可使用。')


def _clip_rank(clip):
    quality=clip['audio_quality']
    return (quality['preferred'],clip['selection_tier']==0,
            min(quality['pause_count'],3),clip['selection_score'])


def _save_evidence(evidence):
    path=Path(evidence['review_dir'])/'manual-review.json'
    pending=path.with_suffix('.pending')
    pending.write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
    pending.replace(path)
def prepare_review(movie,subtitle,work_dir,selected_audio_id,log,source_language='en',*,reference_audio_id=None):
    """Select dialogue/pause windows; probe at most three candidates per third."""
    work=Path(work_dir); work.mkdir(parents=True,exist_ok=True)
    source=Path(subtitle); events=core.parse_subtitle(source)
    media=core.inspect_media(movie)
    duration_ns=core.video_track_duration_ns(media) or core.media_duration_ns(media)
    duration=duration_ns/1e9 if duration_ns>0 else max((seconds(e.end) for e in events),default=0)
    if not events or duration<180:
        raise RuntimeError('影片或字幕过短，无法准备前、中、后三个 60 秒核听区。')
    if duration>=900 and not pro_core.subtitle_completeness(events,duration).accepted:
        raise RuntimeError('所选字幕不完整，不能作为完整字幕标杆。')
    started=time.monotonic()
    deadline=pro_core._DeadlineCancel(None,started+180)
    normalized=pro_core.subtitle_events_to_srt(source,work/'manual-source.srt')
    import audio_roles
    tracks=core.inspect_tracks(movie)
    audio_id=pro_core._preferred_validation_audio_id(movie,selected_audio_id,source_language)
    if reference_audio_id is not None:
        if not any(t.id==reference_audio_id for t in audio_roles.dialogue_tracks(tracks)):
            raise RuntimeError('所选参照音轨不存在或用途不适合正片对白。')
        audio_id=reference_audio_id
    log('人工核听参照：'+audio_roles.describe(next(t for t in tracks if t.id==audio_id)))
    audio_index=pro_core._audio_stream_index(movie,audio_id)
    if audio_index is None: raise RuntimeError('找不到供人工核听的对白音轨。')
    audio=next((track for track in core.inspect_tracks(movie) if track.type=='audio' and track.id==audio_id),None)
    if audio is None: raise RuntimeError('找不到供人工核听的对白音轨编码。')
    engine=pro_core._tool(pro_core.FFSUBSYNC_CANDIDATES,'字幕对齐引擎 ffsubsync')
    clips=[]; region_options={}; region_probes={}; region_attempts={}
    for region in (1,2,3):
        options=shortlist_windows(events,duration,region)[:3]
        if not options:
            raise RuntimeError(f'第 {region} 区没有找到至少 6 条有效对白、前后半段都有对白的 60 秒片段；'
                               '纯音效和歌词不计对白。请换一份完整字幕后重新准备。')
        probed=[]
        region_options[str(region)]=options
        region_attempts[str(region)]=[]
        for option in options:
            region_attempts[str(region)].append(option['start'])
            clip=prepare_window(movie,events,work,region,option,audio_index,engine,log,deadline,
                audio_id=audio_id,audio_codec=audio.codec,prepare_slow=False)
            probed.append(clip)
            if clip['selection_tier']==0 and clip['audio_quality']['preferred']: break
        usable=[clip for clip in probed if clip['audio_quality']['usable']]
        if not usable:
            raise RuntimeError(f'第 {region} 区已检查 {len(probed)} 个短片段，声音活动过少或过于集中，'
                               '无法准备可用的核听片段；请检查音轨或更换字幕。')
        selected=max(usable,key=_clip_rank)
        ensure_half_audio(selected,log,deadline)
        region_probes[str(region)]=probed
        clips.append(selected)
        log(f"人工时间轴第 {region} 区：{selected['movie_start']:.0f} 秒起，"
            f"有效对白 {selected['dialogue_count']} 条（全部字幕 {selected['cue_count']} 条），"
            f"对白之间可辨认停顿 {selected['large_gap_count']} 处，"
            f"疑似人声标记约 {selected['spoken_seconds']:.1f} 秒；"
            f"{'首选片段' if not selected['selection_warning'] else '后备片段，可换同区片段'}。")
    evidence=dict(movie=movie,source=str(source),review_dir=str(work),
        source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        normalized_sha256=hashlib.sha256(normalized.read_bytes()).hexdigest(),
        cue_count=len(events),initial_offset=0.0,
        validation_audio_id=audio_id,audio_stream_index=audio_index,audio_codec=audio.codec,
        audio_choices=[dict(id=t.id,label=audio_roles.describe(t),enabled=not bool(audio_roles.excluded_reason(t)))
                       for t in tracks if t.type=='audio'],
        decode_mode=pro_core.vad_decode_mode(audio.codec),timeline='video-relative',
        elapsed_seconds=round(time.monotonic()-started,2),clips=clips,
        preparation_elapsed_seconds=time.monotonic()-started,
        region_options=region_options,region_probes=region_probes,region_attempts=region_attempts,
        movie_signature=_file_signature(movie),selection_rule=WINDOW_SELECTION_RULE,
        review_rule='manual-three-window-timeline-v3')
    _save_evidence(evidence)
    return evidence


def replace_review_window(evidence,index,log=lambda message:None):
    """Prepare a reserve on demand, preserving other regions and the 180s budget."""
    if index not in (0,1,2) or len(evidence.get('clips',[]))!=3:
        raise ValueError('前、中、后三段核听材料不完整。')
    source=Path(evidence['source'])
    if _artifact_hash(source)!=evidence['source_sha256']:
        raise RuntimeError('字幕文件已改变，请重新准备人工核听。')
    movie=evidence['movie']
    if _file_signature(movie)!=evidence.get('movie_signature'):
        raise RuntimeError('影片文件已改变，请重新准备人工核听。')
    key=str(index+1)
    options=evidence.get('region_options',{}).get(key,[])[:3]
    if len(options)<2:
        raise RuntimeError('本区域没有另一处符合对白要求的片段；当前片段已保留。')
    probes=evidence['region_probes'][key]
    attempts=evidence.setdefault('region_attempts',{}).setdefault(key,[c['movie_start'] for c in probes])
    current=evidence['clips'][index]
    remaining=max(0.,180.-float(evidence.get('preparation_elapsed_seconds',180)))
    started=time.monotonic()
    deadline=pro_core._DeadlineCancel(None,started+remaining)
    selected=None
    try:
        unprobed=[option for option in options if option['start'] not in attempts]
        if unprobed and remaining>0:
            engine=pro_core._tool(pro_core.FFSUBSYNC_CANDIDATES,'字幕对齐引擎 ffsubsync')
            events=core.parse_subtitle(source)
            for option in unprobed:
                if len(attempts)>=3: break
                attempts.append(option['start'])
                clip=prepare_window(movie,events,Path(evidence['review_dir']),index+1,option,
                    evidence['audio_stream_index'],engine,log,deadline,
                    audio_id=evidence['validation_audio_id'],audio_codec=evidence['audio_codec'],prepare_slow=False)
                probes.append(clip)
                if clip['audio_quality']['usable']:
                    selected=clip
                    break
        if selected is None:
            # Cycle already checked candidates without re-decoding the same window.
            usable=[clip for clip in probes if clip['audio_quality']['usable']]
            positions=[clip['movie_start'] for clip in usable]
            if len(usable)>1 and current['movie_start'] in positions:
                selected=usable[(positions.index(current['movie_start'])+1)%len(usable)]
        if selected is None:
            reason='准备时间已用完' if remaining<=0 else '本区域的后备片段均不满足核听要求'
            raise RuntimeError(reason+'；当前片段已保留。')
        ensure_half_audio(selected,log,deadline)
        evidence['clips'][index]=selected
        log(f"人工时间轴第 {index+1} 区：已换到 {selected['movie_start']:.0f} 秒起的片段，"
            f"有效对白 {selected['dialogue_count']} 条；本段需重新确认偏移。")
        return selected
    finally:
        evidence['preparation_elapsed_seconds']=float(evidence.get('preparation_elapsed_seconds',0))+time.monotonic()-started
        try:
            _save_evidence(evidence)
        except Exception:
            # The GUI receives failure and keeps its old page. Its offset must
            # still refer to that old page's control point in memory as well.
            evidence['clips'][index]=current
            raise
