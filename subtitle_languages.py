# -*- coding: utf-8 -*-
"""Interpret subtitle metadata without guessing a Chinese script from ``chi``."""
from __future__ import annotations

import re

from profile_model import normalize_language


CHINESE_SCRIPTS = {"zh-CN", "zh-TW"}


def _chinese_metadata(language: str, track_name: str) -> bool:
    code = (language or "und").strip().lower().replace("_", "-")
    name = (track_name or "").lower()
    return (
        code in {"chi", "zho", "chs", "cht", "zh", "cmn", "yue"}
        or code.startswith(("zh-", "cmn-", "yue-"))
        or (code == "und" and bool(re.search(
            r"\b(?:chinese|mandarin|cantonese)\b|中文|普通话|普通話|粤语|粵語", name
        )))
    )


def subtitle_language(language: str, track_name: str = "") -> str:
    """Return a language/script code, ``zh`` if unknown, or ``yue`` for Cantonese.

    Track names help old MKVs which only carry the broad ``chi`` language tag.
    Cantonese is kept distinct from Mandarin even when its writing is Chinese.
    Explicit script metadata wins over a conflicting script in the name.
    """
    code = (language or "und").strip().lower().replace("_", "-")
    name = (track_name or "").lower()
    if not _chinese_metadata(code, name):
        return normalize_language(code)
    if code == "yue" or code.startswith("yue-") or "-yue" in code or re.search(
        r"\bcantonese\b|粤语|粵語|粤文|粵文", name
    ):
        return "yue"
    if code in {"chs", "zh-cn", "zh-sg"} or re.search(r"(?:^|-)hans(?:-|$)", code):
        return "zh-CN"
    if code in {"cht", "zh-tw", "zh-hk", "zh-mo"} or re.search(r"(?:^|-)hant(?:-|$)", code):
        return "zh-TW"
    simplified = bool(re.search(r"\b(?:simplified|chs|hans)\b|简体|簡體|简中|簡中", name))
    traditional = bool(re.search(r"\b(?:traditional|cht|hant)\b|繁体|繁體|繁中", name))
    if simplified != traditional:
        return "zh-CN" if simplified else "zh-TW"
    return "zh"


def chinese_dialect_rank(language: str, track_name: str = "") -> int:
    """Rank ordinary Chinese subtitles ahead of a separately identified dialect."""
    code = (language or "und").strip().lower().replace("_", "-")
    name = (track_name or "").lower()
    if subtitle_language(code, name) == "yue":
        return 2
    if code == "cmn" or code.startswith("cmn-") or re.search(r"\bmandarin\b|普通话|普通話|国语|國語", name):
        return 0
    return 1
