"""Persistent, separate review records for usable but questionable translations."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import threading
import uuid

SCHEMA_VERSION = 1
_LOCK = threading.RLock()


class QualityWarningError(ValueError):
    def __init__(self, issues):
        self.issues = issues
        super().__init__('修订仍有质量提醒，请确认后再保存：' + '；'.join(
            f"第 {item['index']} 条：{item['reason']}" for item in issues))


def report_path(cache_path):
    return Path(cache_path).with_suffix('.issues.json')


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _rows(events):
    return [dict(index=index, start=event.start, end=event.end, source=event.text.strip())
            for index, event in enumerate(events, 1) if event.text.strip()]


def _fingerprint(events):
    return _digest(json.dumps(_rows(events), ensure_ascii=False, sort_keys=True).encode('utf-8'))


def validate_text(text):
    if not isinstance(text, str) or not text.strip():
        raise ValueError('译文为空，不能生成有效字幕。')
    text = text.strip().replace('\r\n', '\n').replace('\r', '\n')
    if (re.search(r'[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff]', text)
            or re.search(r'\n[ \t]*\n', text)
            or re.search(r'^\s*\d+:\d\d:\d\d[,.]\d+\s*-->', text, re.M)):
        raise ValueError('译文含无效字幕结构或控制字符。')
    return text


def _srt_bytes(rows, translations):
    lines = []
    for ordinal, row in enumerate(rows, 1):
        text = validate_text(translations[row['index']])
        lines.extend((str(ordinal), f"{row['start']} --> {row['end']}", text, ''))
    return '\n'.join(lines).encode('utf-8-sig')


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, indent=2).encode('utf-8')


def _replace_files(files):
    """Stage all content first; restore prior files if publishing fails."""
    token = uuid.uuid4().hex
    staged, previous, published = {}, {}, []
    try:
        for path, data in files.items():
            previous[path] = path.read_bytes() if path.exists() else None
            temporary = path.with_name(path.name + '.' + token + '.tmp')
            staged[path] = temporary
            temporary.write_bytes(data)
        for path, temporary in staged.items():
            temporary.replace(path)
            published.append(path)
    except Exception:
        for path in reversed(published):
            if previous[path] is None:
                path.unlink(missing_ok=True)
            else:
                rollback = path.with_name(path.name + '.' + token + '.rollback')
                rollback.write_bytes(previous[path])
                rollback.replace(path)
        raise
    finally:
        for temporary in staged.values():
            temporary.unlink(missing_ok=True)


def _bound_paths(path, saved):
    path = Path(path).resolve()
    if not isinstance(saved, dict) or saved.get('schema_version') != SCHEMA_VERSION or saved.get('report_path') != str(path):
        raise ValueError('翻译问题记录版本或所属目录不符。')
    paths = {key: Path(saved[key]).resolve() for key in ('source_srt', 'target_srt', 'cache_path')}
    if any(value.parent != path.parent for value in paths.values()):
        raise ValueError('翻译问题记录不能指向其他影片目录。')
    if report_path(paths['cache_path']) != path or len(set(paths.values())) != 3 or path in paths.values():
        raise ValueError('翻译问题文件路径无效。')
    return paths


def restore_issues(events, target, cache_path, output_srt=None):
    """Read separate review rows for exactly this source and artifact location."""
    path = report_path(cache_path).resolve()
    try:
        saved = json.loads(path.read_text(encoding='utf-8'))
        paths = _bound_paths(path, saved)
        if (paths['cache_path'] != Path(cache_path).resolve()
                or (output_srt is not None and paths['target_srt'] != Path(output_srt).resolve())
                or saved.get('target_language') != target
                or saved.get('source_fingerprint') != _fingerprint(events)):
            return {}
        rows = {row['index']: row for row in _rows(events)}
        result = {}
        for issue in saved['issues']:
            if not isinstance(issue, dict):
                continue
            row = rows.get(issue['index'])
            if row is None or any(issue[key] != row[key] for key in ('start', 'end', 'source')):
                continue
            validate_text(issue['translation'])
            if issue.get('status') in ('needs_review', 'resolved', 'user_approved'):
                result[issue['index']] = dict(issue)
        return result
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def save_progress(events, target, source_language, cache_path, output_srt, issues, *, complete=False,
                  translations=None):
    """Persist warnings independently of the accepted translation cache."""
    path = report_path(cache_path).resolve()
    output = Path(output_srt).resolve()
    if output.parent != path.parent:
        raise ValueError('译文和翻译缓存必须位于同一工作目录。')
    source = output.with_suffix('.source.srt')
    rows = _rows(events)
    source_bytes = _srt_bytes(rows, {row['index']: row['source'] for row in rows})
    target_bytes = _srt_bytes(rows, translations) if complete else None
    payload = dict(schema_version=SCHEMA_VERSION, report_path=str(path),
                   source_srt=str(source), target_srt=str(output), cache_path=str(Path(cache_path).resolve()),
                   target_language=target, source_language=source_language,
                   source_fingerprint=_fingerprint(events), source_cues=rows,
                   source_sha256=_digest(source_bytes), target_sha256=_digest(target_bytes) if complete else '',
                   complete=bool(complete), issues=[issues[index] for index in sorted(issues)])
    files = {source: source_bytes}
    if complete:
        files[output] = target_bytes
    files[path] = _json_bytes(payload)
    with _LOCK:
        _replace_files(files)
    return payload


def _target_translations(path, rows):
    """Read our SRT without stripping user markup, whitespace or line breaks."""
    blocks = re.split(r'\n[ \t]*\n', Path(path).read_text(encoding='utf-8-sig').strip())
    if len(blocks) != len(rows):
        raise ValueError('译文时间轴或条目数量发生变化。')
    translations = {}
    for ordinal, (block, row) in enumerate(zip(blocks, rows), 1):
        parts = block.split('\n', 2)
        if (len(parts) != 3 or parts[0] != str(ordinal)
                or parts[1] != f"{row['start']} --> {row['end']}"):
            raise ValueError('译文时间轴或条目数量发生变化。')
        translations[row['index']] = validate_text(parts[2])
    return translations


def load_report(path):
    """Return a complete report only when its source and target still match."""
    path = Path(path).resolve()
    saved = json.loads(path.read_text(encoding='utf-8'))
    paths = _bound_paths(path, saved)
    if not saved.get('complete'):
        raise ValueError('翻译尚未完成，暂不能编辑成品字幕。')
    for kind in ('source', 'target'):
        if _digest(paths[kind + '_srt'].read_bytes()) != saved[kind + '_sha256']:
            raise ValueError('字幕文件已变化，请重新载入当前翻译记录。')
    rows = saved['source_cues']
    if not isinstance(rows, list) or not rows or any(not isinstance(row, dict) for row in rows):
        raise ValueError('原字幕记录无效。')
    indices = [row['index'] for row in rows]
    if any(isinstance(index, bool) or not isinstance(index, int) or index < 1 for index in indices) or indices != sorted(set(indices)):
        raise ValueError('原字幕条目编号无效。')
    expected_source = _srt_bytes(rows, {row['index']: row['source'] for row in rows})
    if _digest(expected_source) != saved['source_sha256']:
        raise ValueError('翻译问题记录与原字幕不一致。')
    translations = _target_translations(paths['target_srt'], rows)
    by_index = {row['index']: row for row in rows}
    for issue in saved['issues']:
        if not isinstance(issue, dict):
            raise ValueError('翻译问题条目无效。')
        row = by_index[issue['index']]
        if any(issue[key] != row[key] for key in ('source', 'start', 'end')) or issue['translation'] != translations[issue['index']]:
            raise ValueError('问题条目与当前字幕不一致。')
    return saved


def discover_reports(work_dir):
    found = []
    for path in sorted(Path(work_dir).glob('*.issues.json')):
        try:
            if load_report(path)['issues']:
                found.append(path)
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return found


def save_corrections(path, updates, *, allow_quality_warning=False):
    """Edit problem rows without changing timing; never silently waive warnings."""
    import subtitle_tool_core as core
    path = Path(path).resolve()
    with _LOCK:
        saved = load_report(path)
        paths = _bound_paths(path, saved)
        issues = {item['index']: dict(item) for item in saved['issues']}
        normalized, warnings = {}, []
        for index, value in updates.items():
            if isinstance(index, bool) or not isinstance(index, int) or index not in issues:
                raise ValueError('只能修改当前记录中的问题字幕。')
            text = validate_text(value)
            reason = core.translation_quality_issue(issues[index]['source'], text, saved['target_language'])
            normalized[index] = (text, reason)
            if reason:
                warnings.append(dict(index=index, reason=reason))
        if warnings and not allow_quality_warning:
            raise QualityWarningError(warnings)
        if not normalized:
            return saved
        rows = saved['source_cues']
        translations = _target_translations(paths['target_srt'], rows)
        clean_records = []
        for index, (text, reason) in normalized.items():
            issues[index].update(translation=text, reason=reason,
                                 status='user_approved' if reason else 'resolved')
            translations[index] = text
            if not reason:
                clean_records.append(dict(target=saved['target_language'], index=index,
                    source=issues[index]['source'], translation=text, parser_version=core.TRANSLATION_PARSER_VERSION))
        target_bytes = _srt_bytes(rows, translations)
        saved.update(target_sha256=_digest(target_bytes), issues=[issues[index] for index in sorted(issues)])
        # Remove superseded records for edited rows, including a formerly good
        # revision that the user now explicitly chooses to keep with a warning.
        old_lines = paths['cache_path'].read_text(encoding='utf-8').splitlines() if paths['cache_path'].exists() else []
        retained = []
        for line in old_lines:
            try:
                item = json.loads(line)
                if isinstance(item, dict) and item.get('target') == saved['target_language'] and item.get('index') in normalized:
                    continue
            except (ValueError, TypeError):
                pass
            retained.append(line)
        cache_bytes = ('\n'.join(retained + [json.dumps(item, ensure_ascii=False) for item in clean_records]) + '\n').encode('utf-8')
        _replace_files({paths['target_srt']: target_bytes, paths['cache_path']: cache_bytes, path: _json_bytes(saved)})
        return saved
