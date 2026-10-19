"""Bind editable translations to a completed MKV and safely replace its text tracks."""
from __future__ import annotations

import copy
import base64
import hashlib
import json
import math
import os
import re
import shutil
import tempfile
import threading
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path

import mkv_fast_mux
import subtitle_tool_core as core
from subtitle_identity_guard import media_signature


MANIFEST = 'translation-output.json'
REVIEW_STORE = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'SubFlow' / 'translation-reviews'
LOCATION_FILE = REVIEW_STORE / 'locations.json'
_LOCATION_LOCK = threading.RLock()
FLAGS = {
    'default_track': '--default-track-flag', 'forced_track': '--forced-display-flag',
    'enabled_track': '--track-enabled-flag', 'hearing_impaired': '--hearing-impaired-flag',
    'visual_impaired': '--visual-impaired-flag', 'text_descriptions': '--text-descriptions-flag',
    'original': '--original-flag', 'commentary': '--commentary-flag',
}


def work_directory(output):
    return Path(str(Path(output).with_suffix('')) + '_work')


def _locations():
    try:
        value=json.loads(LOCATION_FILE.read_text(encoding='utf8'))
        entries=value.get('locations',[]) if value.get('schema_version')==1 else []
        return [entry for entry in entries if isinstance(entry,dict)] if isinstance(entries,list) else []
    except (OSError,ValueError,AttributeError):
        return []


def _location(output):
    path=Path(output).resolve()
    # A newly produced file at the old location owns its adjacent receipt.
    for entry in reversed(_locations()):
        try:
            # Registration already stores canonical paths. Avoid opening each
            # unrelated NAS path on every 400ms table refresh.
            old=Path(entry['original_output'])
            current=Path(entry['output'])
            store=Path(entry['work_dir'])
            if not all(p.is_absolute() for p in (old,current,store)): continue
            if store.parent!=REVIEW_STORE.resolve(): continue
            aliases=entry.get('aliases',[])
            if not isinstance(aliases,list): continue
            previous={old,*[Path(value) for value in aliases if isinstance(value,str)]}
            if path==current or (path in previous and not path.is_file()):
                return current,store
        except (KeyError,TypeError,ValueError,OSError):
            continue
    return path,work_directory(path)


def current_output(output):
    return _location(output)[0]


def register_moved_output(original_output,new_output):
    """Explicitly rebind a moved movie after size and subtitle-UID verification.

    Snapshot only small review artifacts. The movie and original processing
    directory are never modified, and later processing cannot overwrite this
    review's timing or translations.
    """
    import translation_review
    original,new=Path(original_output).resolve(),Path(new_output).resolve()
    if new.suffix.lower()!='.mkv' or not new.is_file():
        raise ValueError('请选择存在的 MKV 成品。')
    registered_original,original_work=_location(original)
    manifest=json.loads((original_work/MANIFEST).read_text(encoding='utf8'))
    if manifest.get('schema_version')!=1 or manifest.get('output')!=str(registered_original):
        raise ValueError('原成品记录不匹配，不能重新关联。')
    new_signature=media_signature(str(new))
    if new_signature['size']!=manifest['output_signature']['size']:
        raise ValueError('新位置影片大小与原成品不同，未关联。')
    tracks={t['id']:t for t in core.inspect_media(str(new)).get('tracks',[])}
    reports=[]
    for binding in manifest.get('bindings',[]):
        track=tracks.get(binding['track']['id'])
        if track is None or _track_identity(track)!=binding['track']:
            raise ValueError('新位置字幕轨的 UID 或属性不符，未关联。')
        report_path=Path(binding['report']).resolve()
        if report_path.parent!=original_work.resolve():
            raise ValueError('原翻译记录不属于本片。')
        report=translation_review.load_report(report_path)
        if Path(report['target_srt']).resolve()!=Path(binding['target_srt']).resolve():
            raise ValueError('原翻译记录与成品轨不一致。')
        reports.append((binding,report_path,report))
    if not reports: raise ValueError('原成品没有翻译修改记录。')
    with _LOCATION_LOCK:
        REVIEW_STORE.mkdir(parents=True,exist_ok=True)
        store=REVIEW_STORE/uuid.uuid4().hex
        store.mkdir()
        try:
            rebound=copy.deepcopy(manifest)
            rebound['output']=str(new)
            rebound['output_signature']=new_signature
            for new_binding,(_binding,old_report,report) in zip(rebound['bindings'],reports):
                record=copy.deepcopy(report)
                for key in ('source_srt','target_srt','cache_path'):
                    source=Path(record[key])
                    destination=store/source.name
                    if source.is_file(): shutil.copy2(source,destination)
                    elif key!='cache_path': raise ValueError('缺少原翻译字幕文件。')
                    record[key]=str(destination.resolve())
                destination=store/old_report.name
                record['report_path']=str(destination.resolve())
                _write(destination,record)
                translation_review.load_report(destination)
                new_binding['report']=str(destination.resolve())
                new_binding['target_srt']=record['target_srt']
            _write(store/MANIFEST,rebound)
            if media_signature(str(new))!=new_signature:
                raise ValueError('关联期间成品发生变化，未关联。')
            previous_entries=_locations()
            previous=next((entry for entry in reversed(previous_entries)
                           if entry.get('output')==str(registered_original)
                           and entry.get('work_dir')==str(original_work.resolve())),None)
            logical=previous['original_output'] if previous else str(original)
            aliases=list(dict.fromkeys([*(previous.get('aliases',[]) if previous else []),str(original),str(registered_original)]))
            entries=[entry for entry in previous_entries if entry.get('output')!=str(new)]
            entries.append(dict(original_output=logical,aliases=aliases,output=str(new),work_dir=str(store.resolve())))
            _write(LOCATION_FILE,dict(schema_version=1,locations=entries))
        except Exception:
            # Only files created in this unique, verified store may be removed.
            for file in store.iterdir():
                if file.is_file(): file.unlink()
            store.rmdir()
            raise
    return store


def _hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _write(path, value):
    pending = path.with_name(path.name + '.pending')
    pending.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    pending.replace(path)


def _track_identity(track):
    props = track.get('properties', {})
    return dict(id=track['id'], uid=props.get('uid'), codec=props.get('codec_id'),
                language=props.get('language_ietf') or props.get('language'),
                name=props.get('track_name', ''))


def _translation_artifact(work, report_path, report):
    """Only bind files emitted by our translation routes, never arbitrary SRTs."""
    target = report['target_language']
    target_path = Path(report['target_srt']).resolve()
    if target_path.parent != work or Path(report_path).resolve().parent != work:
        return None
    match = re.fullmatch(r'translated(?P<track>-track-(?:0|[1-9]\d*))?-'
                         + re.escape(target) + r'\.srt', target_path.name)
    if not match:
        return None
    cache = work / ('translation-cache' + (match['track'] or '') + '-' + target + '.jsonl')
    if (Path(report['cache_path']).resolve() != cache
            or Path(report_path).resolve() != cache.with_suffix('.issues.json')
            or Path(report['source_srt']).resolve() != target_path.with_suffix('.source.srt')):
        return None
    return target_path


def _validated_font_receipt(receipt, source_sha256, track=None):
    """Preserve the resolved font size; never rerun adaptive sizing on edits.

    The receipt must describe the exact header used in the completed track.
    Checking only its family name would allow a stale or edited receipt to
    silently replace the user's manual size or previously resolved auto size.
    """
    import subtitle_fonts
    try:
        if (not isinstance(receipt, dict) or receipt.get('source_sha256') != source_sha256
                or not re.fullmatch(r'[0-9a-f]{64}', source_sha256)):
            raise ValueError('字体来源记录不一致')
        style = receipt['style']
        size, family, color = style['size'], style['family'], style['color']
        if (isinstance(size, bool) or not isinstance(size, (int, float))
                or not math.isfinite(size) or not 0 < size <= 1000
                or not isinstance(family, str) or not family
                or any(c in family for c in ',\r\n')
                or not isinstance(color, str) or not re.fullmatch(r'#[0-9A-Fa-f]{6}', color)):
            raise ValueError('字体样式记录无效')
        encoded = receipt['private']
        if not isinstance(encoded, str) or len(encoded) > 1024 * 1024:
            raise ValueError('字体样式头无效')
        private = base64.b64decode(encoded, validate=True)
        expected = subtitle_fonts.header(style)
        accepted = {expected}
        # 2.0.73 emitted the same canonical header except for a fixed blue
        # SecondaryColour. Existing outputs must remain editable, but only
        # that exact legacy form is accepted, never arbitrary ASS headers.
        if not any(key in style for key in ('size_mode', 'canvas_width', 'canvas_height')):
            rgb = color[1:]
            bgr = (rgb[4:6] + rgb[2:4] + rgb[:2]).encode('ascii')
            accepted.add(expected.replace(b',&H00' + bgr + b',&H00' + bgr + b',&H00000000',
                b',&H00' + bgr + b',&H000000FF,&H00000000', 1))
        if not private or private not in accepted:
            raise ValueError('字体样式与记录不一致')
        if ('private_sha256' in receipt
                and receipt['private_sha256'] != hashlib.sha256(private).hexdigest()):
            raise ValueError('字体样式头校验不一致')
        if track is not None:
            props = track.get('properties', {})
            if (props.get('codec_id') != 'S_TEXT/ASS'
                    or bytes.fromhex(props.get('codec_private_data', '')) != private):
                raise ValueError('成品字体样式与记录不一致')
            if 'track_uid' in receipt and receipt['track_uid'] != props.get('uid'):
                raise ValueError('成品字体轨道与记录不一致')
        return receipt
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f'{exc}；未更改成品。') from exc


def register_output(output, work, targets, log=lambda message: None):
    """Called only after this processing run published its completed output."""
    import translation_review
    output, work = Path(output).resolve(), Path(work).resolve()
    if output.suffix.lower() != '.mkv': return
    path = work / MANIFEST
    # A new successful processing run supersedes bindings from an older run.
    path.unlink(missing_ok=True)
    path.with_name(path.name + '.pending').unlink(missing_ok=True)
    reports = {}
    for report_path in translation_review.discover_reports(work):
        report = translation_review.load_report(report_path)
        target = report['target_language']
        if target in targets and report.get('issues') and _translation_artifact(work, report_path, report):
            reports.setdefault(target, []).append((report_path, report))
    if not reports: return
    media = core.inspect_media(str(output))
    tracks = media.get('tracks', [])
    bindings = []
    for target, candidates in reports.items():
        label = core.LANGUAGES[target][2] + ' Auto'
        if len(candidates) != 1:
            log(f'{label}有多份同语言翻译记录；无法确定成品来源，暂不开放直接重封装。')
            continue
        report_path, report = candidates[0]
        language = core.LANGUAGES[target][0]
        matches = [track for track in tracks if track.get('type') == 'subtitles'
                   and track.get('properties', {}).get('codec_id') in {'S_TEXT/UTF8','S_TEXT/ASS'}
                   and track.get('properties', {}).get('language') == language
                   and track.get('properties', {}).get('track_name') == label]
        if len(matches) != 1:
            log(f'{label}无法唯一对应成品轨道；问题报告已保留，暂不开放直接重封装。')
            continue
        target_path = Path(report['target_srt']).resolve()
        import subtitle_fonts
        font_receipt = subtitle_fonts.styled_receipt(work,target_path)
        if matches[0].get('properties',{}).get('codec_id')=='S_TEXT/ASS':
            if not font_receipt:
                log(f'{label}缺少本次字体记录；保留问题报告，暂不开放直接重封装。')
                continue
            try:
                _validated_font_receipt(font_receipt, _hash(target_path), matches[0])
            except ValueError as exc:
                log(f'{label}{exc}暂不开放直接重封装。')
                continue
        else:
            # A stale font sidecar must not turn a plain UTF8 track into ASS.
            font_receipt = None
        bindings.append(dict(report=str(Path(report_path).resolve()),
                             target_srt=str(target_path), muxed_sha256=_hash(target_path),
                             pending_count=sum(issue.get('status') == 'needs_review'
                                               for issue in report['issues']),
                             track=_track_identity(matches[0]), font_style=font_receipt))
    if bindings:
        _write(path, dict(schema_version=1, output=str(output),
                          output_signature=media_signature(str(output)), bindings=bindings))
        count = sum(binding['pending_count'] for binding in bindings)
        if count:
            log(f'翻译有 {count} 条待检查；影片已完成，可在影片右键菜单中选择“检查/修改翻译”。')
        else:
            log('译文修改记录已保留；可在影片右键菜单中选择“检查/修改翻译”。')
        return count
    return 0


def available(output):
    output,work = _location(output)
    return output.is_file() and any((work / name).is_file()
        for name in (MANIFEST, MANIFEST + '.pending'))


def load_bound_reports(output):
    import translation_review
    output,work = _location(output)
    if output.suffix.lower() != '.mkv':
        raise ValueError('直接修改成品字幕目前仅支持 MKV 成品。')
    path = work / MANIFEST
    signature = media_signature(str(output))
    manifest = None
    # The pending receipt makes a successfully published output recoverable if
    # a later rename of the small receipt was interrupted.
    for candidate in (path, path.with_name(path.name + '.pending')):
        try: value = json.loads(candidate.read_text(encoding='utf-8'))
        except (OSError, ValueError): continue
        if (value.get('schema_version') == 1 and value.get('output') == str(output)
                and value.get('output_signature') == signature):
            manifest = value
            break
    if manifest is None:
        raise ValueError('成品已变化，或缺少本次翻译轨道记录；不能直接替换字幕。')
    reports = []
    used_ids = set()
    for binding in manifest.get('bindings', []):
        report_path = Path(binding['report']).resolve()
        if report_path.parent != path.parent.resolve():
            raise ValueError('翻译问题记录不属于本片工作目录。')
        report = translation_review.load_report(report_path)
        if (Path(report['target_srt']).resolve() != Path(binding['target_srt']).resolve()
                or binding['track']['id'] in used_ids):
            raise ValueError('翻译记录与成品字幕轨不一致。')
        used_ids.add(binding['track']['id'])
        reports.append((report_path, report))
    if not reports: raise ValueError('没有可修改的翻译问题记录。')
    return manifest, reports


def _verify_preserved(source_media, output_media, source_headers, output_headers, replaced):
    before, after = source_media.get('tracks', []), output_media.get('tracks', [])
    if len(before) != len(after): raise ValueError('重封装轨道数量不一致。')
    old_headers = {entry['number']: entry for entry in source_headers['tracks']}
    new_headers = {entry['number']: entry for entry in output_headers['tracks']}
    for old, new in zip(before, after):
        a, b = old.get('properties', {}), new.get('properties', {})
        if old['type'] != new['type'] or a.get('codec_id') != b.get('codec_id'):
            raise ValueError('重封装改变了原轨道的顺序或编码。')
        if a.get('uid') != b.get('uid'):
            raise ValueError('重封装改变了轨道 UID，章节或标签可能失去对应关系。')
        for key in ('track_name', 'language', 'language_ietf', *FLAGS):
            default = True if key == 'enabled_track' else False if key in FLAGS else ''
            if a.get(key, default) != b.get(key, default):
                raise ValueError(f'重封装改变了轨道语言或标记：{key}。')
        if old['id'] not in replaced:
            x, y = old_headers[a['number']], new_headers[b['number']]
            fields = ('codec', 'private', 'mappings', 'video', 'audio', 'uid',
                      'default_duration', 'codec_delay', 'seek_preroll', 'max_addition_id',
                      'timestamp_scale', 'timestamp_offset', 'content_encodings')
            if any(x[field] != y[field] for field in fields):
                raise ValueError('重封装改变了原音视频、杜比视界配置或保留字幕轨。')
        elif a.get('codec_id')=='S_TEXT/ASS':
            if old_headers[a['number']]['private'] != new_headers[b['number']]['private']:
                raise ValueError('重封装改变了译文字幕的字体样式。')
    if any(source_headers[key] != output_headers[key] for key in ('chapters', 'attachments')):
        raise ValueError('重封装改变了章节或附件。')


def _canonical_tags(path):
    """Account only for MKVToolNix's equivalent unspecified-language default.

    Do not discard languages, values, targets or children. Missing TagLanguage
    means und; adding an explicit IETF und is harmless only in that case.
    """
    tree=ET.parse(path)
    for simple in tree.getroot().iter('Simple'):
        if simple.findtext('TagLanguage', 'und')=='und':
            for node in list(simple.findall('TagLanguageIETF')):
                if node.text=='und': simple.remove(node)
    return ET.canonicalize(ET.tostring(tree.getroot(),encoding='unicode'),strip_text=True)


def _preserved_tags(output, destination, replaced_uids, log, cancel):
    core.run_command([core.MKVEXTRACT, str(output), 'tags', str(destination)],
                     log=log, cancel_event=cancel)
    tree = ET.parse(destination)
    statistics = {'BPS', 'DURATION', 'NUMBER_OF_BYTES', 'NUMBER_OF_FRAMES',
                  '_STATISTICS_WRITING_APP', '_STATISTICS_WRITING_DATE_UTC', '_STATISTICS_TAGS'}
    for tag in list(tree.getroot()):
        uids = {node.text for node in tag.findall('./Targets/TrackUID')}
        if not uids.intersection({str(uid) for uid in replaced_uids}): continue
        for simple in list(tag.findall('Simple')):
            if simple.findtext('Name') in statistics: tag.remove(simple)
        if not tag.findall('Simple'): tree.getroot().remove(tag)
    tree.write(destination, encoding='utf-8', xml_declaration=True)
    return _canonical_tags(destination)


def remux_corrected_output(output, log=lambda message: None, cancel=None):
    """No search, VAD, translation or trust mutation; old output survives failure."""
    import batch_core
    output,work = _location(output)
    with batch_core.claim_output_path(str(output)):
        manifest, reports = load_bound_reports(output)
        media = core.inspect_media(str(output), cancel_event=cancel)
        tracks = media.get('tracks', [])
        indexed = {track['id']: track for track in tracks}
        changed = []
        for binding, (report_path, report) in zip(manifest['bindings'], reports):
            track = indexed.get(binding['track']['id'])
            if track is None or _track_identity(track) != binding['track']:
                raise ValueError('成品字幕轨与本次翻译记录不一致，未更改原成品。')
            if binding['track']['codec'] == 'S_TEXT/ASS':
                _validated_font_receipt(binding.get('font_style'), binding['muxed_sha256'], track)
            if _hash(report['target_srt']) != binding['muxed_sha256']:
                changed.append((binding, report))
        if not changed:
            log('译文没有新修改，无需重新封装。')
            return str(output)
        core.check_cancel(cancel)
        original_headers = mkv_fast_mux.read_headers(output)
        core.ensure_mux_disk_space(str(output), str(output), log)
        manifest_path = work / MANIFEST
        with tempfile.TemporaryDirectory(prefix='.translation-remux-', dir=output.parent) as folder:
            temporary = Path(folder)
            partial = temporary / output.name
            editor = Path(core.MKVMERGE).with_name('mkvpropedit.exe')
            if not editor.is_file():
                raise ValueError('缺少 MKVToolNix 的 mkvpropedit，无法保留原轨道标识；未更改成品。')
            replacements = {}
            for index, (binding, report) in enumerate(changed, 1):
                snapshot = temporary / f'translation-{index}.srt'
                snapshot.write_bytes(Path(report['target_srt']).read_bytes())
                snapshot_hash=_hash(snapshot)
                if binding['track']['codec']=='S_TEXT/ASS':
                    import subtitle_fonts
                    receipt=binding.get('font_style')
                    _validated_font_receipt(receipt, binding['muxed_sha256'], indexed[binding['track']['id']])
                    styled=temporary/f'translation-{index}.mkv'
                    # Use the same inline-command policy as the first mux.
                    # <font> tags in an edited SRT must not override the user's
                    # chosen appearance. Size has already been resolved once;
                    # update() rewrites packets without adaptive recalculation.
                    from subtitle_style_engine import update
                    style = receipt['style']
                    private = base64.b64decode(receipt['private'])
                    events = [(start, end, f'{ordinal},0,Default,,0,0,0,,'
                               + subtitle_fonts.ass_text(body))
                              for ordinal, (start, end, body)
                              in enumerate(subtitle_fonts.srt_events(snapshot))]
                    events, _header, _ = update(private, events, style,
                        style.get('canvas_width', 1280), style.get('canvas_height', 720))
                    events = subtitle_fonts.layout_styled_events(events, style, private,
                        code=report['target_language'],input_path=output,media=media,work=temporary,
                        log=log,cancel=cancel)
                    events = subtitle_fonts.reapply_styled_fallbacks(events, style)
                    subtitle_fonts.write_subtitle_mkv(styled, events, style,
                        private=private, packets=True)
                    snapshot=styled
                replacements[binding['track']['id']] = (index, snapshot, snapshot_hash)
            tag_path = temporary / 'preserved-tags.xml'
            tags = _preserved_tags(output, tag_path,
                [indexed[track_id]['properties']['uid'] for track_id in replacements], log, cancel)
            subtitle_ids = [track['id'] for track in tracks if track['type'] == 'subtitles'
                            and track['id'] not in replacements]
            order = [f'{replacements[t["id"]][0]}:0' if t['id'] in replacements
                     else f'0:{t["id"]}' for t in tracks]
            args = [core.MKVMERGE, '-o', str(partial), '--track-order', ','.join(order)]
            if any(binding.get('font_style') for binding,report in changed):
                args += ['--enable-legacy-font-mime-types']
            args += ['--subtitle-tracks', ','.join(map(str, subtitle_ids))] if subtitle_ids else ['--no-subtitles']
            args.append(str(output))
            for binding, report in changed:
                track_id = binding['track']['id']
                props = indexed[track_id]['properties']
                args += ['--language', '0:' + (props.get('language_ietf') or props.get('language', 'und')),
                         '--track-name', '0:' + props.get('track_name', '')]
                for key, option in FLAGS.items():
                    if key in props or key in {'default_track', 'forced_track', 'enabled_track'}:
                        args += [option, '0:' + ('1' if props.get(key, key == 'enabled_track') else '0')]
                args.append(str(replacements[track_id][1]))
            log('正在写入修改后的译文；原成品保留到新文件检查通过。')
            core.run_command(args, log=log, cancel_event=cancel)
            merged_media = core.inspect_media(str(partial), cancel_event=cancel)
            if len(merged_media.get('tracks', [])) != len(tracks):
                raise ValueError('重封装轨道数量不一致。')
            edit_args = [str(editor), str(partial)]
            for old, new in zip(tracks, merged_media['tracks']):
                if old['id'] in replacements:
                    edit_args += ['--edit', f'track:={new["properties"]["uid"]}',
                                  '--set', f'track-uid={old["properties"]["uid"]}']
            edit_args += ['--tags', 'all:' + str(tag_path)]
            core.run_command(edit_args, log=log, cancel_event=cancel)
            audio = [track for track in tracks if track['type'] == 'audio']
            def validation_log(message):
                if message.startswith('输出验证通过：'):
                    message=message.replace('输出验证通过：','轨道和时长检查通过；仍需检查标签、章节及附件：',1)
                log(message)
            log('重新封装写入完成，正在检查轨道、标签、章节及附件；原成品尚未替换。')
            core.validate_media_output(partial, media, len(audio),
                sum(t['type'] == 'subtitles' for t in tracks), audio, validation_log,
                input_path=output, cancel_event=cancel)
            result_media = core.inspect_media(str(partial), cancel_event=cancel)
            _verify_preserved(media, result_media, original_headers,
                              mkv_fast_mux.read_headers(partial), replacements)
            check_tags = temporary / 'verified-tags.xml'
            core.run_command([core.MKVEXTRACT, str(partial), 'tags', str(check_tags)],
                             log=log, cancel_event=cancel)
            if _canonical_tags(check_tags) != tags:
                raise ValueError('重封装后标签未完整保留，原成品未更改。')
            core.check_cancel(cancel)
            if media_signature(str(output)) != manifest['output_signature']:
                raise ValueError('重封装期间原成品发生变化，未覆盖它。')
            next_manifest = copy.deepcopy(manifest)
            report_by_path = {str(path.resolve()): report for path, report in reports}
            final_tracks = {old['id']: new for old, new in zip(tracks, result_media['tracks'])}
            for binding in next_manifest['bindings']:
                old_id = binding['track']['id']
                if old_id in replacements:
                    if _hash(binding['target_srt']) != replacements[old_id][2]:
                        raise ValueError('重封装期间译文发生变化，请重试；原成品未更改。')
                    binding['muxed_sha256'] = replacements[old_id][2]
                    if binding.get('font_style'):
                        binding['font_style']['source_sha256'] = replacements[old_id][2]
                    binding['pending_count'] = sum(issue.get('status') == 'needs_review'
                        for issue in report_by_path[binding['report']]['issues'])
                binding['track'] = _track_identity(final_tracks[old_id])
            next_manifest['output_signature'] = media_signature(str(partial))
            pending = manifest_path.with_name(manifest_path.name + '.pending')
            pending.write_text(json.dumps(next_manifest, ensure_ascii=False, indent=2), encoding='utf-8')
            core.check_cancel(cancel)
            log('全部检查通过，正在替换成品。')
            os.replace(partial, output)
            try: pending.replace(manifest_path)
            except OSError: log('成品已更新；翻译轨道记录保留在待完成文件，下次打开时自动读取。')
        log(f'修改后的译文已写入成品：{output}')
        return str(output)
