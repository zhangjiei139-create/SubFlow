# -*- coding: utf-8 -*-
"""Three short manual-review windows; no whole-film VAD or Whisper."""
from __future__ import annotations
import hashlib, json, math, os, shutil, threading, time, wave, zipfile
from pathlib import Path
import numpy as np
import manual_timeline
import pro_core
import subtitle_tool_core as core

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


def _load_short_audio(wav,slow,npz):
    duration=_pcm_duration(wav,WINDOW)
    _pcm_duration(slow,WINDOW*2)
    with np.load(npz,allow_pickle=False) as fingerprint:
        frames=fingerprint['speech']
        if frames.ndim!=1 or not len(frames) or not np.isfinite(frames).all() or abs(len(frames)/100-duration)>.25:
            raise ValueError('局部声音标记损坏或与短音频时长不符。')
        return vad_intervals(frames>=1)


def _artifact_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read_short_cache(folder,signature,wav,slow,npz):
    try:
        manifest=json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
        if manifest.get('signature')!=signature: return None
        cached=[folder/name for name in ('audio.wav','audio.half.wav','audio.npz')]
        if any(_artifact_hash(path)!=manifest.get('artifacts',{}).get(path.name) for path in cached): return None
        vad=_load_short_audio(*cached)
        for source,destination in zip(cached,(wav,slow,npz)):
            shutil.copy2(source,destination)
        return vad
    except (OSError,ValueError,KeyError,EOFError,wave.Error,zipfile.BadZipFile):
        return None


def _write_short_cache(folder,signature,wav,slow,npz):
    folder.mkdir(parents=True,exist_ok=True)
    hashes={}
    for source,name in zip((wav,slow,npz),('audio.wav','audio.half.wav','audio.npz')):
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

def shortlist_windows(events, duration, region, limit=3):
    lo=(region-1)*duration/3
    hi=min(duration,region*duration/3)
    first=math.ceil(lo/30)*30
    last=math.floor((hi-60)/30)*30
    scored=[]
    for start in range(int(first),int(last)+1,30):
        cues=nearby(events,start)
        if len(cues)<3: continue
        occupied=merged((max(start,seconds(e.start)),min(start+60,seconds(e.end))) for e in cues)
        gaps=sorted((b-a for (_,a),(b,_) in zip(occupied,occupied[1:]) if b-a>=3),reverse=True)
        score=7*min(3,len(gaps))+sum(min(10,g) for g in gaps[:3])+.3*min(14,len(cues))-.35*max(0,len(cues)-18)
        scored.append(dict(start=float(start),cue_count=len(cues),gap_count=len(gaps),score=score))
    # Large empty stretches are only useful if the same window still contains
    # enough dialogue to compare. On sparse films retain the original fallback.
    balanced=[row for row in scored if 9<=row['cue_count']<=18 and row['gap_count']>=2]
    if balanced: scored=balanced
    scored.sort(key=lambda row:(-row['score'],row['start']))
    chosen=[]
    for row in scored:
        if all(abs(row['start']-other['start'])>=120 for other in chosen): chosen.append(row)
        if len(chosen)>=limit: break
    return chosen

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

def prepare_window(movie,events,work,region,option,audio_index,engine,log,deadline,*,audio_id=None,audio_codec=''):
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
        vad=_read_short_cache(cache,signature,wav,slow,npz)
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
            run_limited([core.FFMPEG,'-y','-hide_banner','-nostdin','-loglevel','error',
                         '-i',str(wav),'-af','atempo=0.5','-ar',str(SHORT_AUDIO_RATE),'-ac','1',
                         '-c:a','pcm_s16le',str(slow)],deadline,40,'半速试听音频准备')
            vad=_load_short_audio(wav,slow,npz)
            core.check_cancel(deadline)
            try: _write_short_cache(cache,signature,wav,slow,npz)
            except OSError: log('人工核听短音频缓存写入未完成；本次已准备的核听材料仍可使用。')
        core.check_cancel(deadline)
    rows=[dict(start=round(seconds(e.start),3),end=round(seconds(e.end),3),
               text=e.text,zh='') for e in nearby(events,start,20)]
    return dict(region=region,movie_start=start,subtitle_time=start+30,
                duration=60.0,audio=str(wav),slow_audio=str(slow),vad=vad,
                spoken_seconds=round(sum(b-a for a,b in vad),2),rows=rows,
                cue_count=option['cue_count'],large_gap_count=option['gap_count'],
                initial_offset=0.0,audio_id=audio_id,audio_stream_index=audio_index,
                audio_codec=audio_codec,decode_mode=signature['decode_mode'],
                timeline='video-relative',short_audio_cache_hit=cache_hit)
def prepare_review(movie,subtitle,work_dir,selected_audio_id,log,source_language='en'):
    """One selected 60s window per film third; extra local probes only when sparse."""
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
    audio_id=pro_core._preferred_validation_audio_id(movie,selected_audio_id,source_language)
    audio_index=pro_core._audio_stream_index(movie,audio_id)
    if audio_index is None: raise RuntimeError('找不到供人工核听的对白音轨。')
    audio=next((track for track in core.inspect_tracks(movie) if track.type=='audio' and track.id==audio_id),None)
    if audio is None: raise RuntimeError('找不到供人工核听的对白音轨编码。')
    engine=pro_core._tool(pro_core.FFSUBSYNC_CANDIDATES,'字幕对齐引擎 ffsubsync')
    clips=[]
    for region in (1,2,3):
        options=shortlist_windows(events,duration,region)
        if not options: raise RuntimeError(f'第 {region} 区找不到至少 3 条字幕的 60 秒窗口。')
        probed=[]
        for option in options:
            clip=prepare_window(movie,events,work,region,option,audio_index,engine,log,deadline,
                audio_id=audio_id,audio_codec=audio.codec)
            probed.append(clip)
            # VAD only chooses a usable listening window; it never validates text identity.
            if clip['spoken_seconds']>=12: break
        selected=max(probed,key=lambda item:item['spoken_seconds'])
        clips.append(selected)
        log(f"人工时间轴第 {region} 区：{selected['movie_start']:.0f} 秒起，"
            f"字幕 {selected['cue_count']} 条，长空白 {selected['large_gap_count']} 处，"
            f"声音标记约 {selected['spoken_seconds']:.1f} 秒。")
    evidence=dict(movie=movie,source=str(source),review_dir=str(work),
        source_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        normalized_sha256=hashlib.sha256(normalized.read_bytes()).hexdigest(),
        cue_count=len(events),initial_offset=0.0,
        validation_audio_id=audio_id,audio_stream_index=audio_index,audio_codec=audio.codec,
        decode_mode=pro_core.vad_decode_mode(audio.codec),timeline='video-relative',
        elapsed_seconds=round(time.monotonic()-started,2),clips=clips,
        review_rule='manual-three-window-timeline-v3')
    (work/'manual-review.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
    return evidence
