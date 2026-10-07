"""Isolate historical Whisper regressions from the active VAD public API."""
from contextlib import ExitStack
from unittest.mock import patch
import pro_core as p

def preferred(input_path, selected_audio_id, source_language):
    try:
        tracks=[t for t in p.legacy.inspect_tracks(input_path) if t.type=='audio']
    except Exception:
        return selected_audio_id
    lang=p.normalize_language_code(source_language)
    same=[t for t in tracks if lang!='und' and p.normalize_language_code(t.language)==lang
          and not any(w in (t.name or '').lower() for w in ('commentary','comment','director','解说','评论'))]
    if same:return max(same,key=lambda t:bool(t.default)).id
    return next((t.id for t in tracks if t.id==selected_audio_id),
                next((t.id for t in tracks if t.default),tracks[0].id if tracks else None))

def enter():
    scope=ExitStack()
    for name in ('prepare_shared_subtitle_content_audio','preflight_external_subtitle',
                 'preflight_online_subtitle','align_embedded_text_track'):
        scope.enter_context(patch.object(p,name,getattr(p,'_legacy_whisper_'+name)))
    scope.enter_context(patch.object(p,'_preferred_validation_audio_id',preferred))
    scope.enter_context(patch.object(p,'prepare_shared_alignment_audio',p._build_continuous_alignment_audio))
    return scope
