# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import re
import threading
from pathlib import Path


_ALIAS_LOCK = threading.RLock()
TITLE_SETTINGS_LOCK = _ALIAS_LOCK
_PROPOSAL_LOCK = threading.RLock()
_MODEL_PROPOSALS: dict[tuple[str, str, tuple[str, ...]], str] = {}
_ROMAN_SEQUELS = {"I": "1", "II": "2", "III": "3", "IV": "4", "V": "5",
                  "VI": "6", "VII": "7", "VIII": "8", "IX": "9", "X": "10"}


def sequel_number(title: str) -> str:
    """Read explicit installment labels; never infer a number from a subtitle."""
    title = re.sub(r"\b(?:19|20)\d{2}\b", "", title or "").strip()
    match = re.search(r"(?:第\s*([1-9]\d?)\s*[部集章]|"
                      r"\b(?:episode|ep\.?|part|chapter)\s*[:._ -]*([1-9]\d?|VIII|VII|VI|IV|IX|III|II|I|V|X)\b)",
                      title, flags=re.I)
    if match:
        value = next(value for value in match.groups() if value)
        return _ROMAN_SEQUELS.get(value.upper(), value)
    match = re.search(r"(?:[\u3400-\u9fff]\s*([1-9]\d?)\s*$|"
                      r"\b([1-9]\d?)\b|\b(VIII|VII|VI|IV|IX|III|II|V|X)\s*$)", title, flags=re.I)
    if match:
        value = next(value for value in match.groups() if value)
        return _ROMAN_SEQUELS.get(value.upper(), value)
    return ""


def sequel_title_conflict(original: str, candidates: tuple[str, ...]) -> bool:
    expected = sequel_number(original)
    return bool(expected and any(
        actual and actual != expected for title in candidates
        if (actual := sequel_number(title))
    ))


def _settings_payload(settings_file: Path) -> dict:
    with _ALIAS_LOCK:
        try:
            payload = json.loads(settings_file.read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}


def canonical_title_source(title: str, year: str, settings_file: Path) -> str:
    key = _cache_key(title, year)
    payload = _settings_payload(settings_file)
    aliases = payload.get("title_aliases") or {}
    rejected_titles = rejected_canonical_titles(title, year, settings_file)
    if (isinstance(aliases, dict) and aliases.get(key)
            and str(aliases[key]).casefold() not in {value.casefold() for value in rejected_titles}):
        return "缓存"
    if _series_title_proposal(title, year, settings_file, rejected_titles):
        return "已确认系列别名与续集编号"
    return "已否决候选记录；排除错误片名后重新解析" if rejected_titles else "模型"


def _rejection_records(value: object) -> list[dict[str, str]]:
    """Read the old single rejection and the newer per-proposal blacklist."""
    raw = value.get("candidates") if isinstance(value, dict) else value
    if not isinstance(raw, list):
        raw = [value] if isinstance(value, dict) else []
    records = []
    for item in raw:
        if isinstance(item, dict) and (name := _clean_model_title(str(item.get("title") or ""))):
            records.append({"title": name, "reason": str(item.get("reason") or "")[:240]})
    return records


def rejected_canonical_titles(title: str, year: str, settings_file: Path) -> tuple[str, ...]:
    rejected = _settings_payload(settings_file).get("title_alias_rejections") or {}
    record = rejected.get(_cache_key(title, year)) if isinstance(rejected, dict) else None
    return tuple(item["title"] for item in _rejection_records(record))


def reject_canonical_title(title: str, year: str, canonical: str, reason: str,
                           settings_file: Path) -> None:
    """Share a proven conflict across providers without touching service keys."""
    canonical = _clean_model_title(canonical)
    if not canonical:
        return
    key = _cache_key(title, year)
    with _ALIAS_LOCK:
        payload = _settings_payload(settings_file)
        aliases = payload.get("title_aliases") or {}
        if isinstance(aliases, dict) and aliases.get(key) == canonical:
            aliases.pop(key, None)
            payload["title_aliases"] = aliases
        rejected = payload.get("title_alias_rejections")
        if not isinstance(rejected, dict):
            rejected = {}
        records = _rejection_records(rejected.get(key))
        records = [item for item in records if item["title"].casefold() != canonical.casefold()]
        records.append({"title": canonical, "reason": reason[:240]})
        # Retain the legacy fields for compatibility, but reject the proposal,
        # never the entire localized film. Confirming a good name keeps this
        # blacklist so an old bad alias cannot be adopted again later.
        rejected[key] = {"title": canonical, "reason": reason[:240], "candidates": records}
        payload["title_alias_rejections"] = rejected
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        settings_file.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def needs_canonical_title(title: str) -> bool:
    """Return True when a localized title has no usable Latin title tokens."""
    return bool(re.search(r"[\u3400-\u9fff]", title)) and not bool(
        re.search(r"[A-Za-z]{2,}", title)
    )


def _cache_key(title: str, year: str) -> str:
    normalized = re.sub(r"\s+", " ", title).strip().casefold()
    return f"{normalized}|{year.strip()}"


def _read_aliases(settings_file: Path) -> dict[str, str]:
    with _ALIAS_LOCK:
        payload = _settings_payload(settings_file)
    aliases = payload.get("title_aliases") or {}
    return {
        str(key): str(value).strip()
        for key, value in aliases.items()
        if str(value).strip()
    } if isinstance(aliases, dict) else {}


def _save_alias(settings_file: Path, key: str, value: str) -> None:
    with _ALIAS_LOCK:
        payload = _settings_payload(settings_file)
        aliases = payload.get("title_aliases")
        if not isinstance(aliases, dict):
            aliases = {}
        aliases[key] = value
        payload["title_aliases"] = aliases
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        settings_file.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def _clean_model_title(response: str) -> str:
    first = next((line.strip() for line in response.splitlines() if line.strip()), "")
    first = re.sub(r"^(?:title|official title|english title)\s*:\s*", "", first, flags=re.I)
    first = first.strip(" `\"'。")
    if not first or first.upper() in {"UNKNOWN", "UNSURE", "NOT SURE"}:
        return ""
    if len(first) > 120 or not re.search(r"[A-Za-z]{2,}", first):
        return ""
    if re.search(r"[\u3400-\u9fff]", first):
        return ""
    return first


def remember_canonical_title(title: str, year: str, canonical: str, settings_file: Path,
                             *, confirmed_year: str = "", confirmed_identity: str = "") -> None:
    """Only a provider-confirmed original year and film ID may create an alias."""
    canonical = _clean_model_title(canonical)
    if (canonical and year and str(confirmed_year).strip() == year.strip()
            and str(confirmed_identity).strip()
            and not sequel_title_conflict(title, (canonical,))
            and canonical.casefold() not in {value.casefold() for value in
                                            rejected_canonical_titles(title, year, settings_file)}):
        _save_alias(settings_file, _cache_key(title, year), canonical)


def _series_base(title: str) -> str:
    title = re.sub(r"\b(?:19|20)\d{2}\b", "", title or "").strip()
    title = re.sub(r"(?:第\s*[1-9]\d?\s*[部集章]|"
                   r"\b(?:episode|ep\.?|part|chapter)\s*[:._ -]*(?:[1-9]\d?|VIII|VII|VI|IV|IX|III|II|I|V|X))\s*$",
                   "", title, flags=re.I)
    title = re.sub(r"(?:[1-9]\d?|\b(?:VIII|VII|VI|IV|IX|III|II|I|V|X))\s*$", "", title, flags=re.I)
    return re.sub(r"\s+", " ", title).strip(" :：._-").casefold()


def _series_title_proposal(title: str, year: str, settings_file: Path,
                           excluded: tuple[str, ...]) -> str:
    number = sequel_number(title)
    if not number:
        return ""
    excluded_keys = {value.casefold() for value in excluded}
    proposals = set()
    for alias_key, english in _read_aliases(settings_file).items():
        source_title, _, source_year = alias_key.rpartition("|")
        if _series_base(source_title) != _series_base(title) or alias_key == _cache_key(title, year):
            continue
        if year.isdigit() and source_year.isdigit() and int(source_year) >= int(year):
            continue
        english = _clean_model_title(english)
        if not english:
            continue
        base = re.sub(r"(?:\s+([1-9]\d?|VIII|VII|VI|IV|IX|III|II|I|V|X))\s*$", "", english, flags=re.I)
        proposal = f"{base} {number}"
        if proposal.casefold() not in excluded_keys and not sequel_title_conflict(title, (proposal,)):
            proposals.add(proposal)
    # Competing confirmed series names require model/site resolution; don't
    # silently choose one by cache order.
    return next(iter(proposals)) if len(proposals) == 1 else ""


def resolve_canonical_title(title: str, year: str, settings_file: Path,
                            *, excluded_titles: tuple[str, ...] = ()) -> str:
    """Resolve a localized movie title locally; never use literal translation as truth."""
    if not needs_canonical_title(title):
        return title

    key = _cache_key(title, year)
    excluded = tuple(dict.fromkeys((*rejected_canonical_titles(title, year, settings_file), *excluded_titles)))
    excluded_keys = {value.casefold() for value in excluded}
    cached = _read_aliases(settings_file).get(key, "")
    if cached and cached.casefold() not in excluded_keys:
        return _clean_model_title(cached)
    if series := _series_title_proposal(title, year, settings_file, excluded):
        return series

    import subtitle_tool_core as core

    prompt = f"""Identify the exact official original English title of this movie.
Localized Chinese title: {title}
Release year: {year or "unknown"}
Explicit installment number: {sequel_number(title) or "not specified"}
Excluded wrong/unconfirmed proposals: {json.dumps(excluded, ensure_ascii=False)}

Rules:
- Return only the official English movie title, with no year and no explanation.
- Do not translate the Chinese words literally.
- Use the release year to disambiguate remakes and similarly named films.
- Preserve the explicit installment number, and never return an excluded proposal.
- If you are not confident, return exactly UNKNOWN.
"""
    cache_key = (str(settings_file.resolve()), key, tuple(sorted(excluded_keys)))
    with _PROPOSAL_LOCK:
        if cache_key in _MODEL_PROPOSALS:
            return _MODEL_PROPOSALS[cache_key]
        core.begin_ollama_lease()
        try:
            try:
                core.ensure_ollama_running()
                response = core.call_ollama_retry(
                    prompt, model=core.DEFAULT_MODEL, host=core.OLLAMA_HOST,
                    timeout=90, attempts=1,
                )
                proposal = _clean_model_title(response)
            except (OSError, RuntimeError):
                proposal = ""
        finally:
            if core.end_ollama_lease():
                core.unload_ollama_model()
        if proposal.casefold() in excluded_keys or sequel_title_conflict(title, (proposal,)):
            proposal = ""
        if len(_MODEL_PROPOSALS) >= 128:
            _MODEL_PROPOSALS.clear()
        _MODEL_PROPOSALS[cache_key] = proposal
        return proposal
