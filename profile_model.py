# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path


AUDIO_FORMATS = {
    "AAC": ("aac",),
    "E-AC-3": ("e-ac-3", "eac3", "dolby digital plus"),
    "AC-3": ("ac-3", "ac3", "dolby digital"),
    "Opus": ("opus",),
}

LANGUAGES = [
    ("zh-CN", "简体中文"), ("zh-TW", "繁体中文"), ("en", "英文"),
    ("es", "西班牙语"), ("ja", "日语"), ("ko", "韩语"),
    ("fr", "法语"), ("de", "德语"), ("pt", "葡萄牙语"),
    ("ru", "俄语"), ("hi", "印地语"),
]

LANGUAGE_LABELS = dict(LANGUAGES)


@dataclass
class PreferenceProfile:
    slot: int
    name: str
    audio_formats: list[str] = field(default_factory=lambda: ["AAC", "AC-3"])
    keep_original_audio: bool = True
    create_missing_audio: bool = False
    subtitle_languages: list[str] = field(default_factory=lambda: ["zh-CN", "en"])
    # Deprecated serialized field kept only so existing preference JSON files
    # continue to load after the obsolete three-way UI choice was removed.
    no_subtitle_action: str = "online"
    prefer_compatible_main_audio: bool = False
    lazy_audio_mode: bool = False
    replace_downloaded_subtitle: bool = False
    audio_policy: str = "universal"
    chinese_script_equivalent: bool = False
    subtitle_styles: dict[str, dict] = field(default_factory=dict)


@dataclass
class BatchPlan:
    path: str
    profile_slot: int
    audio_ids: list[int] = field(default_factory=list)
    subtitle_ids: list[int] = field(default_factory=list)
    missing_audio_formats: list[str] = field(default_factory=list)
    missing_subtitle_languages: list[str] = field(default_factory=list)
    local_chinese_conversion_sources: dict[str, int] = field(default_factory=dict)
    source_subtitle_id: int | None = None
    source_mode: str = "none"
    external_subtitle: str = ""
    external_language: str = ""
    external_subtitle_verified: bool = False
    external_subtitle_verification: str = ""
    external_subtitle_provider: str = ""
    external_subtitle_release: str = ""
    external_subtitle_identity_key: str = ""
    external_subtitle_seal: str = ""
    external_subtitle_origin: str = ""
    manual_confirmation_hash: str = ""
    # Keep the untouched candidate as the baseline when a user reopens review.
    manual_review_origin: dict[str, object] = field(default_factory=dict)
    manual_review_required: bool = False
    # Actual dialogue audio used for saved evidence, not the first output track.
    pending_review_audio_id: int | None = None
    # Only a saved-candidate failure from processing resumes after listening.
    manual_review_resume_processing: bool = False
    external_subtitle_media_signature: dict[str, int] = field(default_factory=dict)
    external_subtitle_content_hash: str = ""
    subtitle_report_path: str = ""
    subtitle_verification_states: list[dict] = field(default_factory=list)
    # An audio-stage checkpoint alone never means final candidate acceptance.
    audio_verified_source: dict = field(default_factory=dict)
    status: str = "pending"
    status_label: str = "待分析"
    task_state: str = "waiting"
    task_status_label: str = "等待处理"
    burned_subtitle: bool = False
    burned_subtitle_detail: str = ""
    has_complete_text: bool = False
    has_complete_english_text: bool = False
    has_complete_subtitle: bool = False
    has_retained_image_subtitles: bool = False
    has_image_subtitles: bool = False
    summary: str = ""
    detail: str = ""
    main_audio: str = ""
    audio_tracks: str = ""
    audio_codecs: str = ""
    generated_audio_source_id: int | None = None
    audio_passthrough_warning: bool = False
    audio_warning_detail: str = ""
    subtitles: str = ""
    media_specification: str = ""
    output_path: str = ""


def profile_file() -> Path:
    local_appdata = Path(os.environ.get("LOCALAPPDATA", str(Path.home())))
    root = local_appdata / "SubFlow"
    legacy_candidates = (
        local_appdata / "SubtitleTrackTool-Runtime" / "preference_profiles.json",
        local_appdata / "SubtitleTrackTool-ProMax" / "preference_profiles.json",
    )
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError:
        root = Path(__file__).resolve().parent / "runtime-data"
        root.mkdir(parents=True, exist_ok=True)
    path = root / "preference_profiles.json"
    if not path.exists():
        for legacy in legacy_candidates:
            if not legacy.is_file():
                continue
            try:
                path.write_bytes(legacy.read_bytes())
                break
            except OSError:
                continue
    return path


def default_profiles() -> list[PreferenceProfile]:
    return [
        PreferenceProfile(1, "常用：简中 + 英文", subtitle_languages=["zh-CN", "en"], audio_policy="universal"),
        PreferenceProfile(2, "通用兼容：简体中文", subtitle_languages=["zh-CN"], audio_policy="universal"),
        PreferenceProfile(3, "精简兼容：中英字幕", subtitle_languages=["zh-CN", "en"], audio_policy="compact"),
        PreferenceProfile(4, "原生音频：只整理轨道", subtitle_languages=[], audio_policy="native"),
    ]


def load_profiles() -> list[PreferenceProfile]:
    path = profile_file()
    if not path.exists():
        profiles = default_profiles()
        save_profiles(profiles)
        return profiles
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        for item in raw:
            # The removed image-discard preference must never silently delete PGS.
            item.pop("discard_image_subtitles", None)
            if "audio_policy" not in item:
                if item.get("lazy_audio_mode"):
                    item["audio_policy"] = "compact"
                elif item.get("prefer_compatible_main_audio") or item.get("create_missing_audio"):
                    item["audio_policy"] = "universal"
                elif item.get("slot") == 4:
                    item["audio_policy"] = "native"
                else:
                    item["audio_policy"] = "universal"
        profiles = [PreferenceProfile(**item) for item in raw]
        for profile in profiles:
            profile.subtitle_styles = normalize_subtitle_styles(profile.subtitle_styles)
            if not hasattr(profile, "prefer_compatible_main_audio"):
                profile.prefer_compatible_main_audio = False
            if not hasattr(profile, "lazy_audio_mode"):
                profile.lazy_audio_mode = False
        by_slot = {profile.slot: profile for profile in profiles}
        return [by_slot.get(slot) or default_profiles()[slot - 1] for slot in range(1, 5)]
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return default_profiles()


def save_profiles(profiles: list[PreferenceProfile]) -> None:
    path = profile_file()
    temp = path.with_suffix(".tmp")
    records = []
    for profile in profiles:
        record = asdict(profile)
        record["subtitle_styles"] = normalize_subtitle_styles(profile.subtitle_styles)
        records.append(record)
    temp.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp, path)


def normalize_language(value: str) -> str:
    code = (value or "und").strip().lower().replace("_", "-")
    aliases = {
        "chi": "zh-CN", "zho": "zh-CN", "chs": "zh-CN", "zh": "zh-CN", "zh-cn": "zh-CN",
        "cht": "zh-TW", "zh-tw": "zh-TW", "eng": "en", "english": "en",
        "spa": "es", "jpn": "ja", "kor": "ko", "fra": "fr", "fre": "fr",
        "deu": "de", "ger": "de", "por": "pt", "rus": "ru", "hin": "hi",
    }
    if code in aliases:
        return aliases[code]
    if code == "cmn" or code.startswith("cmn-hans") or code.startswith("zh-hans") or code.startswith("zh-cn") or code.startswith("zh-sg"):
        return "zh-CN"
    if code.startswith("cmn-hant") or code.startswith("zh-hant") or code.startswith("zh-tw") or code.startswith("zh-hk") or code.startswith("zh-mo"):
        return "zh-TW"
    for target, _label in LANGUAGES:
        if code == target.lower():
            return target
    return code[:2] if len(code) >= 2 and code != "und" else "und"


def normalize_subtitle_styles(styles: object) -> dict[str, dict]:
    """Normalize preferences, including the old automatically inserted 38.

    Older dialogs saved 38 without knowing whether it had been edited.  Only
    a recorded user operation proves that 38 is a manual choice.  Other valid
    legacy sizes preserve their deliberate deviation from that old default.
    This is a preference migration, never a completed-track receipt migration.
    """
    if not isinstance(styles, dict):
        return {}
    result = {}
    font_fields = ("font_id", "path", "sha256", "family", "ui_family")
    for code, item in styles.items():
        if not isinstance(code, str) or not isinstance(item, dict):
            continue
        if any(key in item and not isinstance(item[key], str) for key in font_fields):
            continue
        color = item.get("color", "keep")
        if not isinstance(color, str) or (color != "keep" and not re.fullmatch(r"#[0-9A-Fa-f]{6}", color)):
            continue
        size = item.get("size", 0)
        mode = item.get("size_mode", "manual" if size else "auto")
        if not isinstance(mode, str) or mode not in {"auto", "manual"}:
            continue
        if mode == "manual":
            if (isinstance(size, bool) or not isinstance(size, (int, float))
                    or not 1 <= size <= 200 or not float(size).is_integer()):
                continue
            if size == 38 and item.get("size_origin") != "user":
                mode = "auto"
        normalized = {key: item[key] for key in font_fields if key in item}
        normalized.setdefault("font_id", "")
        normalized.update(size_mode=mode, size=0 if mode == "auto" else int(size),
                          color=color if color == "keep" else color.upper())
        if mode == "manual":
            normalized["size_origin"] = "user"
        result[normalize_language(code)] = normalized
    return result


def effective_subtitle_styles(styles: object, languages) -> dict[str, dict]:
    """Return automatic settings for selected languages without a Save click."""
    normalized = normalize_subtitle_styles(styles)
    result = {}
    for code in languages:
        if not isinstance(code, str):
            continue
        code = normalize_language(code)
        result[code] = dict(normalized.get(code, dict(
            font_id="", size_mode="auto", size=0, color="keep")))
    return result


def audio_format(codec: str) -> str:
    value = (codec or "").lower()
    for label, tokens in AUDIO_FORMATS.items():
        if any(token in value for token in tokens):
            return label
    return codec or "未知"


def is_commentary(track) -> bool:
    import audio_roles
    return bool(audio_roles.excluded_reason(track))


def language_label(code: str) -> str:
    normalized = normalize_language(code)
    return LANGUAGE_LABELS.get(normalized, normalized or "未知")


def preset_english(slot: int, current_name: str = "") -> PreferenceProfile:
    return PreferenceProfile(slot, current_name or "常见英文影片", ["AAC", "AC-3", "E-AC-3"], True, False, ["zh-CN"], "online")


def preset_chinese(slot: int, current_name: str = "") -> PreferenceProfile:
    return PreferenceProfile(slot, current_name or "常见中文影片", ["AAC", "AC-3", "E-AC-3"], True, False, ["zh-CN"], "online")


def preset_compatible(slot: int, current_name: str = "") -> PreferenceProfile:
    return PreferenceProfile(
        slot,
        current_name or "兼容偏好：简中 + 保留重音轨",
        ["AAC"],
        True,
        True,
        ["zh-CN"],
        "online",
        True,
        False,
    )


def preset_lazy(slot: int, current_name: str = "") -> PreferenceProfile:
    return PreferenceProfile(
        slot,
        current_name or "懒人偏好：重音轨为主 + 中英字幕",
        ["AAC"],
        True,
        True,
        ["zh-CN", "en"],
        "online",
        False,
        True,
    )
