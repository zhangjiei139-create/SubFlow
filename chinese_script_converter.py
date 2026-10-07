# -*- coding: utf-8 -*-
from __future__ import annotations

import copy
import sys
from pathlib import Path

CHINESE_SCRIPT_CODES = frozenset({"zh-CN", "zh-TW"})


def is_script_conversion(source_code: str, target_code: str) -> bool:
    return source_code in CHINESE_SCRIPT_CODES and target_code in CHINESE_SCRIPT_CODES and source_code != target_code


def _opencc_class():
    try:
        from opencc import OpenCC
    except ImportError:
        vendor = Path(__file__).resolve().parent / "vendor" / "python"
        if str(vendor) not in sys.path:
            sys.path.insert(0, str(vendor))
        from opencc import OpenCC
    return OpenCC


def convert_subtitle_events(events, output_path: str | Path, source_code: str, target_code: str) -> Path:
    if not is_script_conversion(source_code, target_code):
        raise ValueError(f"不支持的本地简繁转换：{source_code} -> {target_code}")
    config = "tw2sp" if source_code == "zh-TW" else "s2twp"
    converter = _opencc_class()(config)
    converted = copy.deepcopy(events)
    for item in converted:
        item.text = converter.convert(item.text)
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if hasattr(converted, "save"):
        converted.save(str(destination), encoding="utf-8")
    else:
        lines: list[str] = []
        for index, item in enumerate(converted, 1):
            lines.extend((
                str(index),
                f"{item.start} --> {item.end}",
                item.text,
                "",
            ))
        destination.write_text("\n".join(lines), encoding="utf-8-sig")
    return destination
