# -*- coding: utf-8 -*-
from __future__ import annotations

import concurrent.futures
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import burned_subtitle_detector as legacy
import subtitle_tool_core as core


SAMPLE_STAGES = (
    (0.18, 0.48, 0.75),
    (0.32, 0.62, 0.84),
    (0.10, 0.40, 0.69),
    (0.25, 0.55, 0.80),
)
GROUP_FRAME_INTERVAL_SECONDS = 0.4
GROUP_DURATION_SECONDS = 1.05
MAX_EXTRACT_WORKERS = 1
PRIMARY_PAGE_MODE = "6"
SECONDARY_PAGE_MODE = "11"
STABILITY_CONFIRM_SECONDS = 0.8
LOCAL_CONFIRM_SECONDS = 3.0
REPLACEMENT_RATIOS = (0.12, 0.22, 0.28, 0.36, 0.44, 0.58, 0.66, 0.73, 0.82)


@dataclass(frozen=True)
class VisualScreen:
    state: str
    score: float = 0.0
    reason: str = ""


@dataclass(frozen=True)
class FrameSample:
    path: Path
    second: float
    screen: VisualScreen


@dataclass(frozen=True)
class OcrAttempt:
    completed: bool
    lines: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExperimentalResult:
    detected: bool
    status: str
    evidence_count: int
    valid_positions: int
    attempted_positions: int
    extracted_frames: int
    suspicious_frames: int
    ocr_calls: int
    detail: str
    extract_seconds: float = 0.0
    visual_seconds: float = 0.0
    ocr_seconds: float = 0.0
    replacement_positions: int = 0
    evidence: tuple[dict[str, object], ...] = ()
    runtime_dir: str = ""


def _remaining(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


def _extract_frame_group(
    video: Path,
    second: float,
    destination: Path,
    deadline: float,
) -> list[Path]:
    remaining = _remaining(deadline)
    if remaining <= 0.5:
        return []
    destination.mkdir(parents=True, exist_ok=True)
    filter_graph = (
        f"fps={1.0 / GROUP_FRAME_INTERVAL_SECONDS:g},"
        "crop=iw:ih*0.42:0:ih*0.58,"
        "scale=1280:-2:force_original_aspect_ratio=decrease"
    )
    try:
        completed = subprocess.run(
            [
                core.FFMPEG,
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{max(0.0, second):.3f}",
                "-i",
                str(video),
                "-t",
                f"{GROUP_DURATION_SECONDS:.2f}",
                "-vf",
                filter_graph,
                "-frames:v",
                "3",
                "-c:v",
                "bmp",
                "-f",
                "image2pipe",
                "pipe:1",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=max(0.5, min(6.0, remaining)),
            creationflags=core.EXTERNAL_PROCESS_CREATION_FLAGS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if completed.returncode != 0:
        return []
    try:
        import cv2
        import numpy as np

        payload = completed.stdout
        offset = 0
        paths: list[Path] = []
        while len(paths) < 3 and offset + 6 <= len(payload):
            if payload[offset:offset + 2] != b"BM":
                break
            size = int.from_bytes(payload[offset + 2:offset + 6], "little")
            if size <= 14 or offset + size > len(payload):
                break
            frame = cv2.imdecode(
                np.frombuffer(payload[offset:offset + size], dtype=np.uint8),
                cv2.IMREAD_COLOR,
            )
            if frame is None or frame.size == 0:
                break
            path = destination / f"frame-{len(paths) + 1:02d}.jpg"
            if not cv2.imwrite(str(path), frame, [cv2.IMWRITE_JPEG_QUALITY, 88]):
                break
            paths.append(path)
            offset += size
        return paths
    except Exception:
        return []


def _extract_single_color(
    video: Path,
    second: float,
    destination: Path,
    deadline: float,
    use_hwaccel: bool = False,
    output_width: int = 1280,
) -> bool:
    remaining = _remaining(deadline)
    if remaining <= 0.5:
        return False
    filter_graph = (
        "crop=iw:ih*0.42:0:ih*0.58,"
        f"scale={max(320, output_width)}:-2:force_original_aspect_ratio=decrease"
    )
    try:
        completed = subprocess.run(
            [
                core.FFMPEG,
                "-hide_banner",
                "-loglevel",
                "error",
                *(["-hwaccel", "auto"] if use_hwaccel else []),
                "-ss",
                f"{max(0.0, second):.3f}",
                "-i",
                str(video),
                "-frames:v",
                "1",
                "-vf",
                filter_graph,
                "-q:v",
                "4",
                "-y",
                str(destination),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=max(0.5, min(5.0, remaining)),
            creationflags=core.EXTERNAL_PROCESS_CREATION_FLAGS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 0 and destination.is_file() and destination.stat().st_size > 0


def _extract_primary_frame(
    video: Path,
    second: float,
    destination: Path,
    deadline: float,
) -> list[Path]:
    remaining = _remaining(deadline)
    if remaining <= 0.5:
        return []
    destination.mkdir(parents=True, exist_ok=True)
    path = destination / "primary.png"
    try:
        extracted = legacy._extract_frame(
            video,
            second,
            path,
            timeout_seconds=max(0.5, min(6.0, remaining)),
            keyframes_only=False,
        )
    except (OSError, subprocess.SubprocessError):
        extracted = False
    return [path] if extracted else []


def _visual_screen(image_path: Path) -> VisualScreen:
    try:
        import cv2
        import numpy as np

        image = cv2.imread(str(image_path), cv2.IMREAD_COLOR)
        if image is None or image.size == 0:
            return VisualScreen("unknown", reason="image-unreadable")
        height, width = image.shape[:2]
        if height < 40 or width < 160:
            return VisualScreen("unknown", reason="image-too-small")

        channels = list(cv2.split(image))
        channels.append(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY))
        best_score = 0.0
        best_reason = ""
        for channel_index, channel in enumerate(channels):
            channel = cv2.GaussianBlur(channel, (3, 3), 0)
            edges = cv2.Canny(channel, 35, 110)
            contours, _hierarchy = cv2.findContours(
                edges,
                cv2.RETR_LIST,
                cv2.CHAIN_APPROX_SIMPLE,
            )
            components: list[tuple[int, int, int, int]] = []
            for contour in contours:
                x, y, w, h = cv2.boundingRect(contour)
                if not (4 <= h <= max(14, int(height * 0.22))):
                    continue
                if not (2 <= w <= max(24, int(width * 0.12))):
                    continue
                aspect = w / max(1.0, h)
                if not (0.08 <= aspect <= 8.0):
                    continue
                if w * h < 16:
                    continue
                components.append((x, y, w, h))

            if len(components) < 6:
                continue

            # A subtitle line normally contains several similarly sized glyphs
            # sitting on a tight baseline. Scene textures may span the frame,
            # but their component heights and vertical centres vary much more.
            for anchor in components:
                anchor_height = anchor[3]
                anchor_center = anchor[1] + anchor_height / 2.0
                row_components = [
                    component for component in components
                    if 0.55 * anchor_height <= component[3] <= 1.8 * anchor_height
                    and abs(
                        component[1] + component[3] / 2.0 - anchor_center
                    ) <= max(4.0, anchor_height * 0.55)
                ]
                if len(row_components) < 6:
                    continue

                row_components.sort(key=lambda item: (item[0], item[2], item[1]))
                unique: list[tuple[int, int, int, int]] = []
                for component in row_components:
                    if unique and (
                        abs(component[0] - unique[-1][0]) <= 2
                        and abs(component[2] - unique[-1][2]) <= 2
                    ):
                        continue
                    unique.append(component)
                if len(unique) < 8:
                    continue

                heights = np.asarray([item[3] for item in unique], dtype=np.float32)
                centres = np.asarray(
                    [item[1] + item[3] / 2.0 for item in unique], dtype=np.float32
                )
                median_height = float(np.median(heights))
                height_cv = float(np.std(heights) / max(1.0, np.mean(heights)))
                baseline_spread = float(np.std(centres) / max(1.0, median_height))
                left = min(item[0] for item in unique)
                right = max(item[0] + item[2] for item in unique)
                span = right - left
                span_ratio = span / max(1.0, width)
                if not (0.07 <= span_ratio <= 0.88):
                    continue
                if height_cv > 0.28 or baseline_spread > 0.18:
                    continue

                count = len(unique)
                score = min(1.0, count / 20.0) * 0.45
                score += min(1.0, span_ratio / 0.25) * 0.25
                score += max(0.0, 1.0 - height_cv / 0.28) * 0.15
                score += max(0.0, 1.0 - baseline_spread / 0.18) * 0.15
                if score > best_score:
                    best_score = score
                    best_reason = f"regular-text-line-channel-{channel_index}"
                if score >= 0.62:
                    return VisualScreen(
                        "suspicious",
                        score,
                        f"regular-text-line-channel-{channel_index}",
                    )
        if best_score >= 0.52:
            return VisualScreen("suspicious", best_score, best_reason or "regular-text-line")
        return VisualScreen("clear", best_score, "no-aligned-text-structure")
    except Exception as exc:
        return VisualScreen("unknown", reason=f"screen-error:{exc}")


def _ocr_lines_one_mode(
    image: Path,
    languages: str,
    page_mode: str,
    deadline: float,
) -> OcrAttempt:
    remaining = _remaining(deadline)
    if remaining <= 0.5:
        return OcrAttempt(False)
    environment = os.environ.copy()
    environment["TESSDATA_PREFIX"] = str(Path(core.TESSERACT).resolve().parent / "tessdata")
    try:
        completed = subprocess.run(
            [
                core.TESSERACT,
                str(image),
                "stdout",
                "-l",
                languages,
                "--psm",
                page_mode,
                "tsv",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=max(0.5, min(5.0, remaining)),
            creationflags=core.EXTERNAL_PROCESS_CREATION_FLAGS,
            env=environment,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return OcrAttempt(False)
    if completed.returncode != 0:
        return OcrAttempt(False)
    rows = completed.stdout.decode("utf-8", errors="replace").splitlines()[1:]
    grouped: dict[tuple[str, str, str, str], list[str]] = {}
    for row in rows:
        columns = row.split("\t", 11)
        if len(columns) != 12:
            continue
        try:
            confidence = float(columns[10])
        except ValueError:
            continue
        text = columns[11].strip()
        if confidence < 45 or not text:
            continue
        key = (columns[1], columns[2], columns[3], columns[4])
        grouped.setdefault(key, []).append(text)
    return OcrAttempt(
        True,
        tuple(" ".join(words) for words in grouped.values() if words),
    )


def _first_meaningful(lines: tuple[str, ...]) -> str:
    return next((line for line in lines if legacy._meaningful_line(line)), "")


def _independent_evidence(evidence: list[dict[str, object]]) -> bool:
    for index, first in enumerate(evidence):
        for second in evidence[index + 1:]:
            first_text = legacy._normalized_evidence_text(str(first.get("text", "")))
            second_text = legacy._normalized_evidence_text(str(second.get("text", "")))
            if not first_text or not second_text or first_text == second_text:
                continue
            delta = abs(float(first.get("second", 0.0)) - float(second.get("second", 0.0)))
            same_position = first.get("position") == second.get("position")
            if delta >= 12.0:
                return True
            if (
                same_position
                and 1.5 <= delta <= 6.0
                and bool(first.get("stable", True))
                and bool(second.get("stable", True))
            ):
                return True
    return False


def detect_experimental(
    video_path: str,
    media_data: dict | None = None,
    audio_languages: list[str] | None = None,
    time_budget_seconds: float = 45.0,
) -> ExperimentalResult:
    video = Path(video_path)
    if not video.is_file():
        return ExperimentalResult(False, "uncertain", 0, 0, 0, 0, 0, 0, "影片文件不存在")
    if not Path(core.FFMPEG).is_file() or not Path(core.TESSERACT).is_file():
        return ExperimentalResult(False, "uncertain", 0, 0, 0, 0, 0, 0, "烧录字幕检测组件不可用")
    media_data = media_data or core.inspect_media(str(video))
    duration = legacy._duration_seconds(video, media_data)
    languages = legacy._available_ocr_languages(audio_languages or [])
    if duration <= 30 or not languages:
        return ExperimentalResult(False, "uncertain", 0, 0, 0, 0, 0, 0, "影片过短或 OCR 语言库不可用")

    deadline = time.monotonic() + max(5.0, time_budget_seconds)
    root = legacy._runtime_dir()
    evidence: list[dict[str, object]] = []
    valid_positions = 0
    attempted_positions = 0
    extracted_frames = 0
    suspicious_frames = 0
    ocr_calls = 0
    extract_seconds = 0.0
    visual_seconds = 0.0
    ocr_seconds = 0.0
    replacement_positions = 0
    unresolved = False

    def recognize(path: Path) -> str:
        nonlocal ocr_calls, ocr_seconds, unresolved
        started = time.monotonic()
        primary_attempt = _ocr_lines_one_mode(
            path, languages, PRIMARY_PAGE_MODE, deadline
        )
        ocr_calls += 1
        text = _first_meaningful(primary_attempt.lines)
        if text:
            ocr_seconds += time.monotonic() - started
            return text
        secondary_attempt = _ocr_lines_one_mode(
            path, languages, SECONDARY_PAGE_MODE, deadline
        )
        ocr_calls += 1
        text = _first_meaningful(secondary_attempt.lines)
        if not primary_attempt.completed or not secondary_attempt.completed:
            unresolved = True
        ocr_seconds += time.monotonic() - started
        return text

    replacement_batches = tuple(
        tuple(REPLACEMENT_RATIOS[index:index + 3])
        for index in range(0, len(REPLACEMENT_RATIOS), 3)
    )
    batches = SAMPLE_STAGES + replacement_batches
    try:
        for batch_index, stage in enumerate(batches):
            is_replacement = batch_index >= len(SAMPLE_STAGES)
            if is_replacement and valid_positions >= 12:
                break
            if _remaining(deadline) <= 0.5:
                unresolved = True
                break
            ratios = stage
            if is_replacement:
                ratios = stage[:max(0, 12 - valid_positions)]
                replacement_positions += len(ratios)
            if not ratios:
                break
            extract_started = time.monotonic()
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=MAX_EXTRACT_WORKERS,
                thread_name_prefix="burned-v8-extract",
            ) as executor:
                futures = []
                for ratio in ratios:
                    position = attempted_positions + 1
                    attempted_positions += 1
                    futures.append((
                        position,
                        duration * ratio,
                        executor.submit(
                            _extract_primary_frame,
                            video,
                            duration * ratio,
                            root / f"position-{position:02d}",
                            deadline,
                        ),
                    ))
                groups = [(position, second, future.result()) for position, second, future in futures]
            extract_seconds += time.monotonic() - extract_started

            for position, base_second, paths in groups:
                if not paths:
                    continue
                screen_started = time.monotonic()
                samples = [
                    FrameSample(
                        path,
                        base_second + index * GROUP_FRAME_INTERVAL_SECONDS,
                        _visual_screen(path),
                    )
                    for index, path in enumerate(paths)
                ]
                visual_seconds += time.monotonic() - screen_started
                extracted_frames += len(samples)
                if any(sample.screen.state == "unknown" for sample in samples):
                    unresolved = True
                    continue
                valid_positions += 1
                candidates = sorted(
                    (sample for sample in samples if sample.screen.state == "suspicious"),
                    key=lambda sample: sample.screen.score,
                    reverse=True,
                )
                suspicious_frames += len(candidates)
                if not candidates:
                    continue

                primary = candidates[0]
                text = recognize(primary.path)
                if not text:
                    continue

                stable_visual = False
                stability_second = min(
                    duration - 0.2,
                    primary.second + STABILITY_CONFIRM_SECONDS,
                )
                stability_dir = root / f"position-{position:02d}" / "stability"
                stability_started = time.monotonic()
                stability_paths = _extract_primary_frame(
                    video,
                    stability_second,
                    stability_dir,
                    deadline,
                )
                extract_seconds += time.monotonic() - stability_started
                if stability_paths:
                    extracted_frames += 1
                    screen_started = time.monotonic()
                    stability_screen = _visual_screen(stability_paths[0])
                    visual_seconds += time.monotonic() - screen_started
                    if stability_screen.state == "unknown":
                        unresolved = True
                    elif stability_screen.state == "suspicious":
                        suspicious_frames += 1
                        stable_visual = True
                evidence.append({
                    "position": position,
                    "second": primary.second,
                    "text": text[:160],
                    "stable": stable_visual,
                })
                if _independent_evidence(evidence):
                    return ExperimentalResult(
                        True,
                        "detected",
                        len(evidence),
                        valid_positions,
                        attempted_positions,
                        extracted_frames,
                        suspicious_frames,
                        ocr_calls,
                        "两条独立正文证据确认存在烧录字幕",
                        round(extract_seconds, 3),
                        round(visual_seconds, 3),
                        round(ocr_seconds, 3),
                        replacement_positions,
                        tuple(evidence),
                        str(root),
                    )

                # The short group establishes visual persistence. Only after
                # a real OCR text hit do we pay for another random seek about
                # three seconds later to look for a changed subtitle line.
                if stable_visual and _remaining(deadline) > 0.5:
                    neighbour_second = min(duration - 0.2, primary.second + LOCAL_CONFIRM_SECONDS)
                    neighbour_dir = root / f"position-{position:02d}" / "local-confirm"
                    nearby_extract_started = time.monotonic()
                    neighbour_paths = _extract_primary_frame(
                        video, neighbour_second, neighbour_dir, deadline
                    )
                    extract_seconds += time.monotonic() - nearby_extract_started
                    if not neighbour_paths:
                        continue
                    neighbour_path = neighbour_paths[0]
                    extracted_frames += 1
                    screen_started = time.monotonic()
                    neighbour_screen = _visual_screen(neighbour_path)
                    visual_seconds += time.monotonic() - screen_started
                    if neighbour_screen.state == "unknown":
                        unresolved = True
                        continue
                    if neighbour_screen.state != "suspicious":
                        continue
                    suspicious_frames += 1
                    neighbour_text = recognize(neighbour_path)
                    if neighbour_text:
                        evidence.append({
                            "position": position,
                            "second": neighbour_second,
                            "text": neighbour_text[:160],
                            "stable": True,
                        })
                        if _independent_evidence(evidence):
                            return ExperimentalResult(
                                True,
                                "detected",
                                len(evidence),
                                valid_positions,
                                attempted_positions,
                                extracted_frames,
                                suspicious_frames,
                                ocr_calls,
                                "同一局部区域出现变化后的第二条字幕正文",
                                round(extract_seconds, 3),
                                round(visual_seconds, 3),
                                round(ocr_seconds, 3),
                                replacement_positions,
                                tuple(evidence),
                                str(root),
                            )

        if evidence or unresolved or valid_positions < 12:
            detail = (
                f"取得 {len(evidence)} 条正文证据、{valid_positions} 个有效位置，"
                "尚不足以确认是否存在烧录字幕"
            )
            status = "uncertain"
        else:
            detail = "12 个分散位置均完成视觉筛查，未检测到烧录字幕"
            status = "not_detected"
        return ExperimentalResult(
            False,
            status,
            len(evidence),
            valid_positions,
            attempted_positions,
            extracted_frames,
            suspicious_frames,
            ocr_calls,
            detail,
            round(extract_seconds, 3),
            round(visual_seconds, 3),
            round(ocr_seconds, 3),
            replacement_positions,
            tuple(evidence),
            str(root),
        )
    finally:
        import shutil

        if os.environ.get("SUBFLOW_BURN_V8_KEEP", "").strip() != "1":
            shutil.rmtree(root, ignore_errors=True)
