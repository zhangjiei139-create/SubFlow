"""Active continuous-VAD route. Legacy Whisper verifiers stay dormant in pro_core."""
from __future__ import annotations
import hashlib
import json
import math
from pathlib import Path
import threading
import time
from contextlib import contextmanager

# Acceptance rules and acoustic preparation have independent cache versions.
# A peak-acceptance correction must recheck the verdict without decoding again.
FINGERPRINT_RULE_VERSION = 'continuous-vad-v4-sampled-stereo-preserve-20261002'
RULE_VERSION = 'continuous-vad-v6-outside-range-peak-20261003'
PROPOSAL_REPORT_PREFIX = '连续VAD时间轴核验通过：提议固定偏移'
LEGACY_VERIFIED_REPORT_PREFIX = '连续VAD确认固定偏移'
_PENDING_ACCEPTANCE_NOTE = '；待候选其余验收'
_locks_guard = threading.Lock()
_locks = {}
_EVIDENCE_REASONS = {
    'too_few_dialogue_cues': '有效对白字幕过少',
    'too_little_speech': '可用声音标记过少',
    'too_little_quiet': '缺少可区分的无声间隔',
    'dialogue_concentrated': '对白时间证据过于集中',
    'no_distinct_peak': '没有明显相关峰',
    'alternative_peak': '存在几乎同强的其他偏移',
    'broad_peak': '相关峰过宽，偏移不明确',
    'reference_invalid': '声音指纹无效',
    'cue_coordinate_invalid': '取样字幕时间坐标无效',
    'tool_coordinate_invalid': '引擎偏移与取样坐标不一致',
    'range_limited': '偏移触及搜索边界',
    'outside_range_peak': '自动范围外存在明显更强的时间匹配',
}


def _proposal_report(offset, shared):
    return (f'{PROPOSAL_REPORT_PREFIX} {offset:+.2f} 秒；音轨 '
            f'{shared.validation_audio_id}（{shared.audio_language}）'
            f'{_PENDING_ACCEPTANCE_NOTE}；未运行Whisper正文核验')


def adopted_alignment_report(report):
    """Describe adoption only when the caller has finished all acceptance gates."""
    for prefix in (PROPOSAL_REPORT_PREFIX, LEGACY_VERIFIED_REPORT_PREFIX):
        if report.startswith(prefix):
            return ('已采用连续VAD固定偏移' + report[len(prefix):]).replace(
                _PENDING_ACCEPTANCE_NOTE, '')
    return ''


def is_dts_codec(codec):
    value = (codec or '').lower()
    return 'dts' in value or value.strip() == 'dca'


def _decode_cost(track):
    codec = (track.codec or '').lower()
    if 'truehd' in codec or 'mlp' in codec:
        return 3
    if is_dts_codec(codec):
        # HD uses core-only decoding; prefer an explicitly ordinary DTS stream.
        return 2 if any(tag in codec for tag in ('hd', 'master', 'ma', 'xll', 'dts:x', 'dts-x')) else 1
    if any(tag in codec for tag in ('aac', 'ac-3', 'ac3', 'pcm', 'flac', 'mp3', 'mpeg', 'opus')):
        return 0
    return 2


def audio_track(tracks, selected_id=None):
    import pro_core as p
    audio = [t for t in tracks if t.type == 'audio' and not any(
        word in (t.name or '').lower() for word in
        ('commentary', 'director', 'comment', '解说', '解說', '评论', '評論',
         'audio description', 'descriptive', '口述影像', 'isolated score', 'music only',
         'partial', 'sample only', '片段音轨', '片段音軌'))]
    if not audio:
        raise RuntimeError('未找到正常对白音轨，不能使用评论或片段音轨纠偏。')
    english = [t for t in audio if p.normalize_language_code(t.language) == 'en']
    base = next((t for t in audio if t.id == selected_id), None) or next((t for t in audio if t.default), audio[0])
    language = p.normalize_language_code(base.language)
    # Unknown labels are not evidence that two tracks have the same language.
    pool = english or ([t for t in audio if p.normalize_language_code(t.language) == language]
                       if language != 'und' else [base])
    return min(pool, key=lambda t: (_decode_cost(t), t.id != base.id, not t.default, t.id))


def cache_directory(movie):
    movie = Path(movie).resolve()
    return movie.parent / (movie.stem + '_pro_work') / 'continuous-vad'


def preparation_budget_seconds(duration_seconds):
    """Cumulative film audio preparation ceiling; candidate matching stays at 20s."""
    if not isinstance(duration_seconds, (int, float)) or not math.isfinite(duration_seconds) or duration_seconds <= 0:
        return 180.0
    return 300.0 if duration_seconds > 3 * 3600 else 180.0


def image_preparation_budget_seconds(duration_seconds):
    """Keep the existing PGS scan budget independent of audio decoder policy."""
    if not isinstance(duration_seconds, (int, float)) or not math.isfinite(duration_seconds) or duration_seconds <= 0:
        return 180.0
    return float(min(300, max(60, math.ceil(duration_seconds / 60.0 + 30.0))))


def shared_audio(movie, selected_id, log, cancel=None, budget=None):
    import pro_core as p
    import subtitle_tool_core as core
    audio = audio_track(core.inspect_tracks(movie), selected_id)
    media = core.inspect_media(movie)
    ns = core.video_track_duration_ns(media) or core.media_duration_ns(media)
    duration = ns / 1e9 if ns > 0 else 0.0
    # None opts into the film-length policy. Explicit diagnostic/test limits
    # remain hard limits; never silently enlarge a caller's requested budget.
    limit = preparation_budget_seconds(duration) if budget is None else float(budget)
    if not math.isfinite(limit) or limit <= 0:
        raise ValueError('影片音频准备上限必须是有限正秒数。')
    deadline = p._DeadlineCancel(cancel, time.monotonic() + limit)
    started = time.monotonic()
    root = cache_directory(movie)
    key = (str(Path(movie).resolve()).casefold(), audio.id)
    with _locks_guard:
        lock = _locks.setdefault(key, threading.Lock())
    acquired = False
    try:
        while not acquired:
            core.check_cancel(deadline)
            acquired = lock.acquire(timeout=.1)
        core.check_cancel(deadline)
        log(f'影片音频准备：片长{duration / 60:.1f}分钟，参照音轨{audio.id}（{audio.codec}）；'
            f'本次最多{limit:g}秒，完成后立即继续，后续候选共用。')
        reference = p._build_continuous_alignment_audio(movie, audio.id, root / f'audio-{audio.id}', log, deadline)
    except core.CancelledError as exc:
        core.check_cancel(cancel)
        if deadline.timed_out:
            raise p.SubtitlePreflightTimeoutError(
                f'影片连续VAD准备达到{limit:g}秒上限；音频准备未完成，候选尚未核验。') from exc
        raise
    finally:
        if acquired:
            lock.release()
    return p.SharedSubtitleContentAudio(
        duration_seconds=ns / 1e9 if ns > 0 else 0,
        validation_audio_id=audio.id,
        audio_stream_index=p._audio_stream_index(movie, audio.id) or 0,
        audio_language=p.normalize_language_code(audio.language),
        fingerprint=None,
        vad_reference=reference,
        vad_preparation_state=dict(limit=limit, spent=time.monotonic() - started,
                                   references={.5: reference}),
    )


@contextmanager
def _pause_candidate_clocks(cancel):
    """Film-level expansion spends its own budget, never a candidate's 20s."""
    clocks = []
    current = cancel
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if getattr(current, 'candidate_match_clock', False):
            clocks.append((current, current.deadline))
            current.deadline = None
        current = getattr(current, 'cancel_event', None)
    started = time.monotonic()
    try:
        yield
    finally:
        elapsed = time.monotonic() - started
        for clock, previous in clocks:
            clock.deadline = previous + elapsed if previous is not None else None


def _extend_reference(movie, shared, reference, fraction, log, cancel):
    import pro_core as p
    import subtitle_tool_core as core
    state = getattr(shared, 'vad_preparation_state', {})
    key = (str(Path(movie).resolve()).casefold(), shared.validation_audio_id)
    with _locks_guard:
        lock = _locks.setdefault(key, threading.Lock())
    acquired = False
    try:
        while not acquired:
            core.check_cancel(cancel)
            acquired = lock.acquire(timeout=.1)
        core.check_cancel(cancel)
        references = state.setdefault('references', {})
        ready = references.get(fraction)
        if ready:
            try:
                if p._sampled_reference_info(ready):
                    return ready
            except p.SubtitleVerificationToolError:
                references.pop(fraction,None)
        limit = state.get('limit', preparation_budget_seconds(shared.duration_seconds))
        remaining = limit - state.get('spent', 0)
        if remaining <= 0:
            raise p.SubtitlePreflightTimeoutError(f'影片取样准备已用完累计{limit:g}秒上限。')
        preparation_cancel = p._DeadlineCancel(cancel, time.monotonic() + remaining)
        started = time.monotonic()
        try:
            result = p._build_continuous_alignment_audio(
                movie, shared.validation_audio_id, Path(reference).parent, log, preparation_cancel,
                fraction=fraction, previous_reference=Path(reference))
        except core.CancelledError as exc:
            core.check_cancel(cancel)
            if preparation_cancel.timed_out:
                raise p.SubtitlePreflightTimeoutError(
                    f'影片取样扩展达到累计{limit:g}秒上限，未应用字幕偏移。') from exc
            raise
        finally:
            state['spent'] = state.get('spent', 0) + time.monotonic() - started
            log(f'影片取样准备累计耗时 {state["spent"]:.2f}秒 / {limit:g}秒。')
        references[fraction] = result
        return result
    finally:
        if acquired:
            lock.release()


def preflight(movie, subtitle, work_dir, selected_id, log, cancel=None, *, shared=None,
              source_language='', candidate_label='字幕', budget=20.0, output_name='online-verified.srt'):
    import pro_core as p
    import subtitle_tool_core as core
    source=Path(subtitle); work=Path(work_dir); work.mkdir(parents=True,exist_ok=True)
    # Film-level VAD preparation has its own visible budget, before the per-file
    # matching clock starts. It is never repeated for every candidate.
    core.check_cancel(cancel)
    if shared is not None:
        duration = shared.duration_seconds
    else:
        media = core.inspect_media(movie)
        duration = (core.video_track_duration_ns(media) or core.media_duration_ns(media)) / 1e9
    events=core.parse_subtitle(source)
    if not events: raise p.SubtitleContentMismatchError('字幕没有有效正文和时间轴。')
    profile=p.subtitle_completeness(events,duration)
    if duration >= 900 and not profile.accepted:
        raise p.SubtitleContentMismatchError('字幕不完整，不能作为时间标杆：'+profile.report)
    end=max(p._subtitle_time_seconds(e.end) for e in events)
    if duration and end > duration+max(180,duration*.12):
        raise p.SubtitleContentMismatchError('字幕时间轴明显长于影片，请检查版本。')
    if p.normalize_language_code(source_language)=='en' and p.detect_subtitle_language(events) not in ('en','und'):
        raise p.SubtitleContentMismatchError('要求英文候选，但实际正文不是英文。')
    shared = shared or shared_audio(movie, selected_id, log, cancel)
    deadline=p._DeadlineCancel(cancel,time.monotonic()+max(.01,budget))
    deadline.candidate_match_clock = True
    core.check_cancel(deadline)
    video=Path(movie).resolve(); stat=video.stat()
    signature=dict(rule=RULE_VERSION,video=str(video),size=stat.st_size,mtime_ns=stat.st_mtime_ns,
        audio=shared.validation_audio_id,subtitle_sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        max_offset=p.MAX_AUTOMATIC_OFFSET_SECONDS,reference_sha256=hashlib.sha256(Path(shared.vad_reference).read_bytes()).hexdigest())
    marker=work/'vad-verdict.json'; output=work/output_name
    def checked_reference_info(path):
        info=p._sampled_reference_info(path)
        if info:
            import sampled_vad as sample
            identity=info['signature']
            current_stat=video.stat()
            if (identity.get('source')!=str(video)
                    or identity.get('size')!=current_stat.st_size
                    or identity.get('mtime_ns')!=current_stat.st_mtime_ns
                    or identity.get('audio_id')!=shared.validation_audio_id):
                raise p.SubtitleVerificationToolError('取样指纹不属于当前影片和音轨，或影片已变化。')
            span=sample.FrameRange(info['range']['start_frame'],info['range']['end_frame'])
            if span!=sample.centered_range(shared.duration_seconds,info['range']['fraction']):
                raise p.SubtitleVerificationToolError('取样指纹起点与当前影片范围不一致。')
        return info
    try:
        saved=json.loads(marker.read_text(encoding='utf-8'))
        if saved['signature']==signature and hashlib.sha256(output.read_bytes()).hexdigest()==saved['output_sha256']:
            matched_reference=saved.get('matched_reference')
            if matched_reference and (not Path(matched_reference).is_file() or
                    hashlib.sha256(Path(matched_reference).read_bytes()).hexdigest()!=saved.get('matched_reference_sha256')):
                raise ValueError('Matched reference changed')
            if matched_reference:
                checked_reference_info(matched_reference)
                sidecar=Path(matched_reference).with_suffix('.json')
                sidecar_hash=hashlib.sha256(sidecar.read_bytes()).hexdigest() if sidecar.is_file() else None
                if sidecar_hash!=saved.get('matched_reference_sidecar_sha256'):
                    raise ValueError('Matched reference coordinates changed')
            # Historical verdicts said "confirmed" before the candidate's
            # remaining acceptance gates ran. Reuse their numerical result,
            # while presenting it as a proposal instead of replaying that claim.
            cached_offset=saved.get('proposal_offset', saved.get('applied_offset'))
            if (isinstance(cached_offset,(int,float)) and not isinstance(cached_offset,bool)
                    and math.isfinite(cached_offset)
                    and abs(cached_offset)<=p.MAX_AUTOMATIC_OFFSET_SECONDS):
                report=_proposal_report(cached_offset,shared)
                log('复用相同字幕、音轨和规则的连续VAD时间轴预检结果；候选仍需完成其余验收。')
                log(candidate_label+'：'+report)
                return output,report
    except (OSError,ValueError,KeyError):pass
    normalized=p.subtitle_events_to_srt(source,work/'vad-original.srt')
    normalized_events=core.parse_subtitle(normalized)
    reference=Path(shared.vad_reference)
    matched_fraction=None
    evidence=None
    try:
        import sampled_vad as sample
        import vad_evidence
        while True:
            core.check_cancel(deadline)
            info=checked_reference_info(reference)
            diagnostics={}
            local=normalized
            local_events=normalized_events
            matching_output=output
            if info:
                span=sample.FrameRange(info['range']['start_frame'],info['range']['end_frame'])
                matched_fraction=info['range']['fraction']
                local_events=sample.crop_subtitles(normalized_events,span,
                    guard=vad_evidence.subtitle_guard_seconds(span.duration_seconds))
                local=work/f'vad-local-{round(matched_fraction*100)}.srt'
                core.write_srt(local,local_events,{i:e.text for i,e in enumerate(local_events,1)})
                matching_output=work/f'vad-match-{round(matched_fraction*100)}.srt'
                evidence=vad_evidence.evaluate_reference(reference,local_events,raw_offset=None)
            else:
                evidence=None
            if evidence is None or evidence['sufficient']:
                match_started=time.monotonic()
                p._alignment_candidate(movie,local,matching_output,shared.validation_audio_id,'audio-continuous',
                    log,deadline,diagnostics=diagnostics,shared_audio_reference=reference,
                    audio_timeline_in_video_coordinates=bool(info))
                log(f'{candidate_label}：本次时间轴匹配耗时 {time.monotonic()-match_started:.2f}秒。')
                core.check_cancel(deadline)
                raw=diagnostics.get('raw_offset_seconds')
                offset=diagnostics.get('offset_seconds')
                score=diagnostics.get('score')
                if not all(isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v) for v in (offset,raw,score)):
                    raise p.SubtitleVerificationToolError('连续VAD未返回有效偏移。')
                if abs(raw)>=p.MAX_AUTOMATIC_OFFSET_SECONDS-.1 or abs(offset)>p.MAX_AUTOMATIC_OFFSET_SECONDS:
                    raise p.SubtitleContentMismatchError('连续VAD结果触及自动±10秒范围，请使用手动核听。')
                if info:
                    evidence=vad_evidence.evaluate_reference(reference,local_events,raw_offset=raw)
                    if evidence.get('coordinate_error') or evidence.get('tool_coordinate_invalid'):
                        raise p.SubtitleVerificationToolError('取样字幕与声音坐标不一致，未应用偏移。')
                    if evidence.get('outside_range_peak'):
                        outside=evidence['diagnostic_offset_seconds']
                        message=(f'连续VAD：±10秒内提议 {raw:+.2f} 秒，'
                                 f'范围外 {outside:+.2f} 秒的匹配明显更强；'
                                 '本候选超出自动纠偏范围，未采用窗内提议；请换字幕或手动处理。')
                        log(candidate_label+'：'+message)
                        raise p.SubtitleContentMismatchError(message)
                    # The engine correlates uncentered +/-1 signals. Different
                    # speech/subtitle occupancies can make a valid best score
                    # negative; the independent peak contrast/shape and support
                    # checks above decide whether the evidence is sufficient.
                elif score<=0:
                    raise p.SubtitleVerificationToolError('连续VAD未返回有效偏移。')
            if evidence is None or evidence['sufficient']:
                if info:
                    # Only the estimate comes from the crop; the final output
                    # contains every intact original cue, not the sampled cues.
                    shifted=sample.shift_full_subtitles(normalized_events,int(round(offset*1000)))
                    core.write_srt(output,shifted,{i:e.text for i,e in enumerate(shifted,1)})
                    log(f'{candidate_label}：中间{matched_fraction:.0%}证据足够，停止扩展；固定偏移用于全片字幕。')
                break
            reason='；'.join(_EVIDENCE_REASONS.get(item,item) for item in evidence['reasons'])
            if not evidence.get('expandable',False):
                raise p.SubtitleVerificationToolError(f'取样时间轴资料无效，不能通过扩展修复：{reason}')
            if matched_fraction is None or matched_fraction>=.75:
                raise p.SubtitleContentMismatchError(f'中间75%仍缺少可用时间轴证据，请手动核听或换字幕：{reason}')
            next_fraction=.6 if matched_fraction<.6 else .75
            log(f'{candidate_label}：中间{matched_fraction:.0%}证据不足（{reason}），才向两侧扩至{next_fraction:.0%}。')
            with _pause_candidate_clocks(deadline):
                reference=_extend_reference(movie,shared,reference,next_fraction,log,deadline)
        core.check_cancel(deadline)
    except sample.ReferenceValidationError as exc:
        raise p.SubtitleContentMismatchError(f'全片字幕不能保持完整固定平移：{exc}') from exc
    except core.CancelledError as exc:
        if deadline.timed_out:raise p.SubtitlePreflightTimeoutError('候选连续VAD匹配达到20秒预算，未应用结果。') from exc
        raise
    offset=diagnostics.get('offset_seconds'); raw=diagnostics.get('raw_offset_seconds'); score=diagnostics.get('score')
    log(f'{candidate_label}：连续VAD提议固定偏移 {offset:+.2f} 秒；待候选核验，尚未采用。')
    if abs(raw)>=p.MAX_AUTOMATIC_OFFSET_SECONDS-.01 or abs(offset)>p.MAX_AUTOMATIC_OFFSET_SECONDS:
        raise p.SubtitleContentMismatchError('连续VAD结果触及自动±10秒范围，请使用手动核听。')
    actual=core.parse_subtitle(output)
    log(f'字幕正文完整性：原始{len(events)}条，有效{len(normalized_events)}条，纠偏后{len(actual)}条。')
    if len(actual)!=len(normalized_events):
        raise p.SubtitleVerificationToolError('纠偏前后有效字幕条目数量不一致，未采用结果。')
    if [e.text for e in actual] != [e.text for e in normalized_events]:
        raise p.SubtitleVerificationToolError('纠偏前后字幕正文或顺序发生变化，未采用结果。')
    delta=int(round(offset*1000))
    for before,after in zip(normalized_events,actual):
        old_start=round(p._subtitle_time_seconds(before.start)*1000)
        old_end=round(p._subtitle_time_seconds(before.end)*1000)
        new_start=round(p._subtitle_time_seconds(after.start)*1000)
        new_end=round(p._subtitle_time_seconds(after.end)*1000)
        if (new_start-old_start,new_end-old_end)!=(delta,delta):
            raise p.SubtitleVerificationToolError('纠偏改变了单条字幕时长，或并非统一平移，未采用结果。')
    report=_proposal_report(offset,shared)
    marker.write_text(json.dumps(dict(signature=signature,output_sha256=hashlib.sha256(output.read_bytes()).hexdigest(),
        # applied_offset is retained for existing audit readers: the temporary
        # output was shifted, but this marker is not a final adoption verdict.
        applied_offset=offset,proposal_offset=offset,
        matched_fraction=matched_fraction,evidence=evidence,raw_score=score,
        matched_reference=str(reference),matched_reference_sha256=hashlib.sha256(reference.read_bytes()).hexdigest(),
        matched_reference_sidecar_sha256=(hashlib.sha256(reference.with_suffix('.json').read_bytes()).hexdigest()
                                          if reference.with_suffix('.json').is_file() else None),
        alignment_status='timeline-preflight-passed-candidate-pending',report=report),ensure_ascii=False,indent=2),encoding='utf-8')
    log(candidate_label+'：'+report)
    return output,report
