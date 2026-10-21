"""Per-film stage records. Reports do not grant subtitle acceptance."""
from contextlib import contextmanager
from contextvars import ContextVar
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import time
from urllib.parse import unquote


_current = ContextVar("subtitle_film_audit", default=None)
STAGES = dict(selection='候选选择', purpose='字幕用途', identity='影片身份', release='版本/语言',
              structure='正文筛选', download_structure='下载/正文', audio_timing='音频时间轴',
              retained_tracks='保留字幕轨', acceptance='最终准入', timeline='本轨时间轴', replacement='替换原轨')
STATES = dict(not_examined='未核验', eligible='用途可用', filtered='排除', queued='待检查',
              duplicate='重复跳过', downloaded='已下载', verified='通过', audio_verified='音频对时通过',
              insufficient_evidence='证据不足', retained_track_unverified='保留轨未通过',
              unverified='尚未核验', timing_conflict='时间轴冲突', rejected='拒绝', timeout='超时',
              infrastructure='工具或准备异常', identity_pending='影片身份待确认',
              replacement_planned='已排除，待补齐', replaced='已用可靠来源补齐')


_REDACTED = '[REDACTED]'
_SECRET_NAMES = frozenset(('apikey', 'token', 'accesstoken', 'authtoken', 'bearertoken',
    'refreshtoken', 'authorization', 'password', 'passwd', 'clientsecret',
    'secret', 'privatekey', 'signature'))
# Match raw and percent-encoded delimiters without decoding or rewriting a
# candidate URL. The report is only a display projection of internal state.
_ENCODED_DELIMITER = r'%(?:25){0,2}'
_QUERY_DELIMITER = rf'(?:&amp;|[?&;]|{_ENCODED_DELIMITER}(?:3[fFbB]|26))'
_QUERY_VALUE = re.compile(
    rf'(?P<prefix>{_QUERY_DELIMITER})(?P<key>(?:(?!=|{_ENCODED_DELIMITER}3[dD]|{_QUERY_DELIMITER})'
    rf'(?:[A-Za-z0-9_.-]|%[A-Fa-f0-9]{{2}})){{1,120}})'
    rf'(?P<equals>=|{_ENCODED_DELIMITER}3[dD])'
    rf'(?P<value>(?:(?!{_QUERY_DELIMITER}|{_ENCODED_DELIMITER}23|[\s\"\'<>|]).)*)')
_NAMED_SECRET = re.compile(
    r'''(?i)(?<![A-Za-z0-9_.-])["']?(?:--?)?(?P<name>[A-Za-z][A-Za-z0-9_.-]{0,119})["']?\s*[:=]\s*''')
_SECRET_VALUE = re.compile(r'''"[^"\r\n]*(?:"|(?=[\r\n])|$)|'[^'\r\n]*(?:'|(?=[\r\n])|$)|[^\s"',;&<>|]+''')


def _secret_name(value, *, query=False):
    name = str(value)
    for _ in range(3):
        decoded = unquote(name)
        if decoded == name:
            break
        name = decoded
    name = re.sub(r'(?<=[a-z0-9])(?=[A-Z])', '_', name)
    parts = tuple(part for part in re.split(r'[^a-z0-9]+', name.casefold()) if part)
    compact = ''.join(parts)
    # A prefixed token/password is still a credential. A media/source signature
    # is verification metadata; only a standalone or query signature is secret.
    return (compact in _SECRET_NAMES or compact.endswith(('apikey', 'accesstoken',
            'authtoken', 'bearertoken', 'refreshtoken', 'clientsecret', 'privatekey'))
            or bool(parts and parts[-1] in _SECRET_NAMES - {'signature'})
            or bool(query and parts and parts[-1] in {'key', 'signature'}))


def _redact_named_values(text):
    # Scan bounded labels once. Consuming only secret values lets an unrelated
    # "reason: api_key=..." retain its reason while still finding the inner key.
    # A leading CLI -- is part of the same label; hyphenated garbage is not
    # retried at every component boundary.
    result, position = [], 0
    for match in _NAMED_SECRET.finditer(text):
        if match.start() < position or not _secret_name(match['name']):
            continue
        value = _SECRET_VALUE.match(text, match.end())
        if value is None:
            continue
        result.append(text[position:value.start()])
        raw = value.group(0)
        quote = raw[0] if raw[0] in '\"\'' else ''
        result.append(quote + _REDACTED + (quote if quote and raw.endswith(quote) else ''))
        position = value.end()
    result.append(text[position:])
    return ''.join(result)


def redacted_text(value):
    """Remove labelled credentials only at a human-readable output boundary."""
    text = str(value)
    text = _QUERY_VALUE.sub(lambda match: (
        match['prefix'] + match['key'] + match['equals'] + _REDACTED
        if _secret_name(match['key'], query=True) else match.group(0)), text)
    text = re.sub(r'(?i)(https?://)[^\s/@]+(?::[^\s/@]*)?@',
                  lambda match: match.group(1) + _REDACTED + '@', text)
    text = re.sub(r'''(?i)(\bauthorization["']?\s*[:=]\s*["']?(?:Bearer|Basic)\s+)[A-Za-z0-9._~+/-]+=*''',
                  lambda match: match.group(1) + _REDACTED, text)
    text = re.sub(r'(?i)(\bBearer\s+)[A-Za-z0-9._~+/-]+=*',
                  lambda match: match.group(1) + _REDACTED, text)
    return _redact_named_values(text)


def report_projection(value):
    """Copy and redact nested reports; never mutate source keys or receipts."""
    if isinstance(value, dict):
        return {(redacted_text(key) if isinstance(key, str) else key):
                (_REDACTED if _secret_name(key) and item not in (None, '')
                      else report_projection(item)) for key, item in value.items()}
    if isinstance(value, list):
        return [report_projection(item) for item in value]
    if isinstance(value, tuple):
        return tuple(report_projection(item) for item in value)
    return redacted_text(value) if isinstance(value, str) else value


def redacted_logger(log):
    """Sanitize before both UI callbacks and persistent process log writers."""
    return lambda message: log(redacted_text(message))


def current():
    return _current.get()


def failure_kind(error):
    name, text = type(error).__name__, str(error)
    if name == "RetainedSubtitleVerificationError":
        return "retained_track_unverified"
    if name == "SubtitleInsufficientRegionalSupportError" or "证据不足" in text or "缺少可用时间轴证据" in text:
        return "insufficient_evidence"
    if "Timeout" in name or "超时" in text or "时间上限" in text:
        return "timeout"
    if "ToolError" in name or "PreparationError" in name:
        return "infrastructure"
    if "偏移冲突" in text or "前后趋势" in text:
        return "timing_conflict"
    return "rejected"


def timing_conflict(value):
    if isinstance(value, dict):
        return any(value.get(key) is True for key in ('regional_offset_conflict', 'timing_conflict', 'conflict')) or any(
            timing_conflict(item) for item in value.values())
    return isinstance(value, (list, tuple)) and any(timing_conflict(item) for item in value)


class FilmAudit:
    def __init__(self, movie, directory):
        self.movie = str(Path(movie).resolve())
        self.directory = Path(directory)
        self.records = []
        self.windows = []
        self.searches = []
        self.active_record = None
        self.output_subtitles = []
        self.created = time.time_ns()
        source = Path(movie)
        try:
            stat = source.stat() if source.is_file() else None
        except OSError:
            stat = None
        self.signature = {"size": stat.st_size, "mtime_ns": stat.st_mtime_ns} if stat else {}

    def add(self, label, **fields):
        row = dict(label=str(label), state="not_examined", stages={}, attempted=False, **fields)
        self.records.append(row)
        return row

    def update(self, row, stage, state, reason="", **fields):
        row["stages"][stage] = dict(state=state, reason=str(reason), **fields)
        row.update(state=state, stage=stage, reason=str(reason))

    def evidence(self, subtitle, reference, evidence, **fields):
        self.windows.append(dict(subtitle=str(subtitle), reference=str(reference),
                                 evidence=evidence, **fields))
        if self.active_record is not None:
            self.active_record['offset_seconds'] = evidence.get('offset_seconds')
            self.active_record.setdefault('window_evidence', []).append(evidence)

    def check_output_purposes(self, media):
        """Check actual output headers before publication, without claiming semantics."""
        import subtitle_roles
        rows = []
        for track in media.get('tracks', []):
            if track.get('type') != 'subtitles':
                continue
            props = track.get('properties') or {}
            role = subtitle_roles.classify(props.get('track_name', ''), forced=props.get('forced_track', False))
            row = dict(id=track.get('id'), name=props.get('track_name', ''),
                       language=props.get('language_ietf') or props.get('language', 'und'), role=role.code,
                       purpose_accepted=role.main_dialogue, content_state='not_sampled')
            rows.append(row)
            if not role.main_dialogue:
                self.output_subtitles = rows
                raise RuntimeError(f"成品字幕用途检查失败：轨{track.get('id')} {role.reason}；未发布成品。")
        self.output_subtitles = rows

    def track_result(self, track_id, kind, accepted, reason="", **fields):
        row = next((r for r in self.records if r.get("track_id") == track_id and r.get("kind") == kind), None)
        if row is None:
            row = self.add(f"字幕轨 {track_id}", track_id=track_id, kind=kind)
        self.update(row, "timeline", "verified" if accepted else "unverified", reason, **fields)
        row['content_state'] = 'not_sampled'
        evidence = fields.get('evidence', {})
        row['offset_seconds'] = fields.get('offset_seconds', evidence.get('proposed_offset_seconds'))

    def write(self, outcome, reason, log):
        log = redacted_logger(log)
        replacements = [row for row in self.records if row['state'] == 'replacement_planned']
        if outcome == 'completed':
            for row in replacements:
                self.update(row, 'replacement', 'replaced', row.get('reason', '原轨无法纠偏'))
                log(f"内嵌字幕替换完成：{row['label']}已从成品剔除；所选字幕语言已补齐。")
        elif replacements:
            labels = '、'.join(row['label'] for row in replacements)
            log(f'内嵌字幕替换未完成：{labels}；本片处理未完成，原因：{reason}。')
        # Unstarted rows are reported as such, never falsely called rejected.
        for row in self.records:
            if row["state"] in {"queued", "downloaded", "checking", "not_examined"}:
                row.setdefault("reason", "本轮因限额、止损或提前结束未完成核验")
                row['state'] = 'not_examined'
        counts = Counter(row["state"] for row in self.records)
        attempted = sum(bool(row.get("attempted")) for row in self.records)
        mixed = any(row.get("state") == "timing_conflict" for row in self.records)
        mixed = mixed or timing_conflict(self.records) or timing_conflict(self.windows)
        data = dict(rule="subtitle-stage-audit-v1", movie=self.movie, media_signature=self.signature,
                    outcome=outcome, reason=str(reason), attempted_candidates=attempted,
                    state_counts=dict(counts), suspected_mixed_timeline=mixed,
                    fixed_offset_warning=any(w.get('evidence', {}).get('fixed_offset_warning') for w in self.windows),
                    mixed_timeline_note="区域时间冲突，需核听区分漂移、跳变或错版" if mixed else "未取得可信混轴证据",
                    records=self.records, windows=self.windows, searches=self.searches,
                    output_subtitles=self.output_subtitles)
        lines = [f"影片：{Path(self.movie).name}", f"结果：{outcome}；{reason}",
                 f"正式尝试 {attempted} 个候选；疑似混轴：{'是' if mixed else '未确认'}"]
        for i, row in enumerate(self.records, 1):
            lines.append(f"{i}. {row['label']} [{STAGES.get(row.get('stage'), '候选选择')}/{STATES.get(row['state'], row['state'])}]：{row.get('reason', '')}")
            offset = row.get('offset_seconds')
            lines.append(f"  计算偏移：{offset:+.2f} 秒" if isinstance(offset, (int, float)) else '  计算偏移：未得到有效偏移')
            for stage, result in row['stages'].items():
                if stage != row.get('stage'):
                    lines.append(f"  {STAGES.get(stage, stage)}/{STATES.get(result['state'], result['state'])}：{result['reason']}")
            replacement = row.get('replacement')
            if replacement:
                lines.append('  替换来源：' + (replacement.get('source_label') or '见已验证标轴')
                             + ('；文件：' + replacement['source_path'] if replacement.get('source_path') else ''))
                targets = replacement.get('target_labels', [])
                lines.append('  补齐语言：' + ('、'.join(targets) if targets else '可靠原文或正常保留轨，无需新增翻译')
                             + '；沿用标轴时间。')
            if row.get('content_state') == 'not_sampled':
                lines.append('  本轨对白内容抽检：未执行；时间轴通过不等同于内容已确认。')
        for search in self.searches:
            lines.append(f"站点 {search['provider']}：加载 {search['loaded']}；总数 {search['total']}；分页截断 {search['truncated']}；{search['identity_result']}")
            if 'candidate_recovery_requests' in search:
                lines.append(f"  候选级来源补证：本次 {search['candidate_recovery_requests']} 次，"
                             f"耗时 {search.get('candidate_recovery_seconds', 0.0):.2f} 秒；"
                             f"本片共享累计 {search.get('shared_evidence_requests', 0)} 次；"
                             '仅具体来源继续核验，全球作品ID状态保持。')
        first_review = next((row for row in self.records if row['state'] == 'insufficient_evidence'), None)
        if '参照音轨或字幕版本待确认' in str(reason):
            advice = '建议：在核听窗口检查并选择参照音轨，再确认字幕版本；无需重复搜索已下载的候选。'
        elif '字体' in str(reason) or '缺字' in str(reason):
            advice = '建议：检查该语言的字体及缺字提示；已通过的字幕和时间轴可继续使用，无需重新搜索字幕。'
        elif any(word in str(reason) for word in ('翻译', '译文', '本地模型')):
            advice = '建议：查看具体翻译问题并重试未解决条目，或使用译文修改；已通过的字幕来源可继续使用。'
        elif any(row['state'] == 'identity_pending' for row in self.records) and not attempted:
            advice = '建议：字幕选择中确认正确影片及年份；本次作品身份未确认，候选尚未下载或进行时间轴核验，不代表字幕错误。'
        else:
            advice = ('建议先核听：' + first_review['label'] if first_review else
                      '建议：先查看保留轨核验或用途/身份拒绝原因，再选择完整正片字幕。')
        lines.append(advice)
        for window in self.windows:
            evidence = window.get("evidence", {})
            fraction = evidence.get('sample_fraction')
            lines.append(f"分区证据：{Path(window['subtitle']).name}"
                         + (f"，中间 {fraction:.0%} 样本" if isinstance(fraction, (int, float)) else '，已保存取证'))
            origin = evidence.get('movie_origin_seconds', 0.)
            for region in evidence.get("region_observations", evidence.get("regional_offsets", [])):
                start = region.get('start_seconds')
                position = f"影片 {start+origin:.2f} 秒起" if start is not None else '位置见分区记录'
                lines.append(f"  分区 {region['region']}（{position}）：偏移 {region.get('offset_seconds')} 秒；可信 {region.get('trusted', True)}；{region.get('reason', '')}")
            if evidence.get('fixed_offset_warning'):
                lines.append('  人工三点偏移不同，已使用各点渐变修正；不把三点确认扩散为其他轨道的确认。')
        stem = self.directory / f"subtitle-report-{self.created}"
        data = report_projection(data)
        lines = [redacted_text(line) for line in lines]
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            json_path, text_path = stem.with_suffix(".json"), stem.with_suffix(".txt")
            json_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            text_path.write_text("\n".join(lines), encoding="utf-8")
        except OSError as error:
            log(f"字幕报告保存失败：{error}；以下保留完整报告。")
            text_path = None
        if outcome == "failed":
            for line in lines:
                log("字幕失败报告：" + line)
        elif any(row.get('kind', '').startswith('retained_') and row['state'] == 'unverified' for row in self.records):
            log('字幕验收提示：封装流程完成，但部分保留轨尚未核验；字幕来源通过不能代替这些轨道通过，详见逐轨报告。')
        if text_path:
            log(f"字幕{'失败' if outcome == 'failed' else '处理'}报告：{text_path}")
        return data, str(text_path or "")


@contextmanager
def scope(audit):
    token = _current.set(audit)
    try:
        yield audit
    finally:
        _current.reset(token)


def file_hash(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()
