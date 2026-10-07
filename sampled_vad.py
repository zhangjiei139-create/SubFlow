"""Pure coordinate and fingerprint helpers for a continuous sampled reference.

No media process is started here. A fingerprint frame covers 10 ms, on the
absolute video timeline. ffsubsync's NPZ has only ``speech`` in the currently
installed version; it contains neither an origin nor a sample rate. Keep the
origin/policy in a validated sidecar, never invent silent frames for unobserved
audio. Context frames may warm up a new VAD segment; they never replace already
observed middle frames. Concatenation correctness is tested here, not the VAD's
acoustic equivalence to a newly processed continuous window.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_HALF_UP
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any, Mapping, Sequence, TypeVar
from zipfile import BadZipFile

import numpy as np

FRAME_RATE = 100
FRAME_MILLISECONDS = 10
PCM_SAMPLE_RATE = 48000
PCM_SAMPLES_PER_FRAME = 480
METADATA_VERSION = "sampled-vad-v1"
SUPPORTED_COVERAGES = (Decimal("0.50"), Decimal("0.60"), Decimal("0.75"))
_TIME = re.compile(r"^(\d+):([0-5]\d):([0-5]\d)[,.](\d{3})$")
Event = TypeVar("Event")


class ReferenceValidationError(ValueError):
    """A coordinate, fingerprint or cache identity cannot be trusted."""


def _decimal(value: Any, name: str) -> Decimal:
    if isinstance(value, bool):
        raise ReferenceValidationError(f"{name} must be a finite number")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ReferenceValidationError(f"{name} must be a finite number") from exc
    if not result.is_finite():
        raise ReferenceValidationError(f"{name} must be finite")
    return result


def _integer(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, np.integer)):
        raise ReferenceValidationError(f"{name} must be an integer")
    return int(value)


def _grid_seconds(value: Any, name: str) -> int:
    frames = _decimal(value, name) * FRAME_RATE
    if frames < 0 or frames != frames.to_integral_value():
        raise ReferenceValidationError(f"{name} must be nonnegative and on the 10ms grid")
    return int(frames)


@dataclass(frozen=True)
class FrameRange:
    """Absolute 100Hz half-open interval [start_frame, end_frame)."""

    start_frame: int
    end_frame: int

    def __post_init__(self) -> None:
        start = _integer(self.start_frame, "start_frame")
        end = _integer(self.end_frame, "end_frame")
        if start < 0 or end <= start:
            raise ReferenceValidationError("Frame range must be positive and ordered")

    @property
    def frame_count(self) -> int:
        return int(self.end_frame - self.start_frame)

    @property
    def start_seconds(self) -> float:
        return self.start_frame / FRAME_RATE

    @property
    def end_seconds(self) -> float:
        return self.end_frame / FRAME_RATE

    @property
    def duration_seconds(self) -> float:
        return self.frame_count / FRAME_RATE

    @property
    def start_ms(self) -> int:
        return int(self.start_frame) * FRAME_MILLISECONDS

    @property
    def end_ms(self) -> int:
        return int(self.end_frame) * FRAME_MILLISECONDS

    @property
    def pcm_sample_count(self) -> int:
        return self.frame_count * PCM_SAMPLES_PER_FRAME

    def contains(self, other: "FrameRange") -> bool:
        return self.start_frame <= other.start_frame and other.end_frame <= self.end_frame


def movie_frame_count(duration_seconds: Any) -> int:
    """The engine emits ceil(PCM_samples/480) frames, including a partial tail."""
    duration = _decimal(duration_seconds, "duration_seconds")
    if duration <= 0:
        raise ReferenceValidationError("Movie duration must be positive")
    return int((duration * FRAME_RATE).to_integral_value(rounding=ROUND_CEILING))


def centered_range(duration_seconds: Any, coverage: Any = .50) -> FrameRange:
    """Round each movie-relative endpoint once to the nearest 10ms, ties up."""
    duration = _decimal(duration_seconds, "duration_seconds")
    fraction = _decimal(coverage, "coverage")
    if duration <= 0 or fraction not in SUPPORTED_COVERAGES:
        raise ReferenceValidationError("Centered coverage must be 50%, 60% or 75% of a positive movie")
    start = duration * (1 - fraction) / 2 * FRAME_RATE
    end = duration * (1 + fraction) / 2 * FRAME_RATE
    return FrameRange(int(start.to_integral_value(rounding=ROUND_HALF_UP)),
                      int(end.to_integral_value(rounding=ROUND_HALF_UP)))


@dataclass(frozen=True)
class ExtensionSegment:
    name: str
    owned_range: FrameRange
    decode_range: FrameRange

    def __post_init__(self) -> None:
        if self.name not in {"left", "right"} or not self.decode_range.contains(self.owned_range):
            raise ReferenceValidationError("Extension context must contain its owned left/right interval")

    @property
    def trim_start_frames(self) -> int:
        return self.owned_range.start_frame - self.decode_range.start_frame

    @property
    def trim_end_frames(self) -> int:
        return self.decode_range.end_frame - self.owned_range.end_frame


@dataclass(frozen=True)
class ExtensionPlan:
    base_range: FrameRange
    target_range: FrameRange
    segments: tuple[ExtensionSegment, ...]

    def __post_init__(self) -> None:
        if not self.target_range.contains(self.base_range):
            raise ReferenceValidationError("An extension target must contain the existing reference")
        names = [part.name for part in self.segments]
        if len(names) != len(set(names)):
            raise ReferenceValidationError("Duplicate extension segment")
        owned = sorted([self.base_range, *(part.owned_range for part in self.segments)],
                       key=lambda span: span.start_frame)
        cursor = self.target_range.start_frame
        for span in owned:
            if span.start_frame != cursor or not self.target_range.contains(span):
                raise ReferenceValidationError("Extension ownership has a gap or overlap")
            cursor = span.end_frame
        if cursor != self.target_range.end_frame:
            raise ReferenceValidationError("Extension does not cover the target")


def extension_plan(base_range: FrameRange, target_range: FrameRange,
                   duration_seconds: Any, context_seconds: Any = 2) -> ExtensionPlan:
    """Decode just new sides plus context; keep existing middle frames intact."""
    context = _grid_seconds(context_seconds, "context_seconds")
    movie_end = movie_frame_count(duration_seconds)
    if target_range.end_frame > movie_end or not target_range.contains(base_range):
        raise ReferenceValidationError("Target must contain base and stay inside the movie")
    parts = []
    for name, start, end in (("left", target_range.start_frame, base_range.start_frame),
                             ("right", base_range.end_frame, target_range.end_frame)):
        if start == end:
            continue
        owned = FrameRange(start, end)
        decoded = FrameRange(max(0, start - context), min(movie_end, end + context))
        parts.append(ExtensionSegment(name, owned, decoded))
    return ExtensionPlan(base_range, target_range, tuple(parts))


def expected_vad_frames(pcm_sample_count: Any, sample_rate: Any = PCM_SAMPLE_RATE) -> int:
    """Exact engine frame-count logic, including an incomplete final frame."""
    samples = _integer(pcm_sample_count, "pcm_sample_count")
    if _integer(sample_rate, "sample_rate") != PCM_SAMPLE_RATE or samples <= 0:
        raise ReferenceValidationError("Positive 48000Hz PCM sample count is required")
    return (samples + PCM_SAMPLES_PER_FRAME - 1) // PCM_SAMPLES_PER_FRAME


def validate_pcm(span: FrameRange, sample_count: Any, sample_rate: Any = PCM_SAMPLE_RATE,
                 channels: Any = 1, sample_width: Any = 2) -> None:
    """Do not disguise missing/extra samples with a matching ceil frame count."""
    if (_integer(sample_rate, "sample_rate") != PCM_SAMPLE_RATE
            or _integer(channels, "channels") != 1
            or _integer(sample_width, "sample_width") != 2):
        raise ReferenceValidationError("Expected 48kHz mono signed-16 PCM")
    if _integer(sample_count, "sample_count") != span.pcm_sample_count:
        raise ReferenceValidationError("PCM sample count differs from the exact requested grid interval")


def validate_arrays(arrays: Mapping[str, np.ndarray], span: FrameRange) -> None:
    if "speech" not in arrays or not arrays:
        raise ReferenceValidationError("NPZ must contain speech")
    for name, array in arrays.items():
        if not isinstance(name, str) or not isinstance(array, np.ndarray) or array.dtype.hasobject:
            raise ReferenceValidationError("NPZ arrays must be named, non-object numpy arrays")
    speech = arrays["speech"]
    if speech.ndim != 1 or speech.dtype.kind not in "biuf" or not np.isfinite(speech).all():
        raise ReferenceValidationError("Speech must be finite, real, one-dimensional numerical frames")
    if len(speech) != span.frame_count:
        raise ReferenceValidationError(f"NPZ has {len(speech)} frames, expected exactly {span.frame_count}; not padded or silently trimmed")


def read_npz(path: str | Path, span: FrameRange, *, metadata: Mapping[str, Any] | None = None,
             identity: Mapping[str, Any] | None = None) -> dict[str, np.ndarray]:
    try:
        with np.load(Path(path), allow_pickle=False) as stored:
            arrays = {name: stored[name].copy() for name in stored.files}
    except (OSError, ValueError, TypeError, AttributeError, EOFError, BadZipFile) as exc:
        raise ReferenceValidationError("Cannot read a safe NPZ fingerprint") from exc
    validate_arrays(arrays, span)
    if metadata is not None:
        if identity is None:
            raise ReferenceValidationError("Expected cache identity is required with metadata")
        validate_metadata(metadata, span, identity, arrays)
    return arrays


def write_npz(path: str | Path, arrays: Mapping[str, np.ndarray], span: FrameRange) -> Path:
    """Atomically preserve actual NPZ keys/dtypes; the sidecar stays separate."""
    validate_arrays(arrays, span)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + ".", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            np.savez_compressed(stream, **arrays)
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()
    return path


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                          allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ReferenceValidationError("Cache metadata must be finite JSON data") from exc


def _array_schema(arrays: Mapping[str, np.ndarray]) -> dict[str, Any]:
    return {name: {"dtype": array.dtype.str, "shape": list(array.shape)}
            for name, array in sorted(arrays.items())}


def _payload_digest(arrays: Mapping[str, np.ndarray]) -> str:
    digest = hashlib.sha256()
    for name, array in sorted(arrays.items()):
        digest.update(_canonical({"name": name, "dtype": array.dtype.str, "shape": list(array.shape)}))
        digest.update(array.tobytes(order="C"))
    return digest.hexdigest()


def make_metadata(span: FrameRange, identity: Mapping[str, Any],
                  arrays: Mapping[str, np.ndarray]) -> dict[str, Any]:
    """Identity should include source signature, audio track and decoding/VAD policy."""
    validate_arrays(arrays, span)
    if not isinstance(identity, Mapping) or not identity:
        raise ReferenceValidationError("A nonempty source/audio/policy identity is required")
    frozen_identity = json.loads(_canonical(dict(identity)))
    return {"version": METADATA_VERSION, "frame_rate_hz": FRAME_RATE,
            "start_frame": int(span.start_frame), "end_frame": int(span.end_frame),
            "frame_count": span.frame_count, "identity": frozen_identity,
            "array_schema": _array_schema(arrays), "payload_sha256": _payload_digest(arrays)}


def validate_metadata(metadata: Mapping[str, Any], span: FrameRange,
                      identity: Mapping[str, Any], arrays: Mapping[str, np.ndarray]) -> None:
    expected = make_metadata(span, identity, arrays)
    if not isinstance(metadata, Mapping) or _canonical(dict(metadata)) != _canonical(expected):
        raise ReferenceValidationError("Cache metadata, origin, schema, content or source/audio/policy identity differs")


def trim_context(arrays: Mapping[str, np.ndarray], decode_range: FrameRange,
                 owned_range: FrameRange) -> dict[str, np.ndarray]:
    validate_arrays(arrays, decode_range)
    if not decode_range.contains(owned_range):
        raise ReferenceValidationError("Owned frames are outside decoded context")
    start = owned_range.start_frame - decode_range.start_frame
    end = owned_range.end_frame - decode_range.start_frame
    result = {name: array.copy() for name, array in arrays.items()}
    result["speech"] = arrays["speech"][start:end].copy()
    validate_arrays(result, owned_range)
    return result


def stitch_reference(base_arrays: Mapping[str, np.ndarray], plan: ExtensionPlan,
                     segment_arrays: Mapping[str, Mapping[str, np.ndarray]]) -> dict[str, np.ndarray]:
    """Every target frame must have one observed owner; never zero-fill a gap."""
    validate_arrays(base_arrays, plan.base_range)
    if set(segment_arrays) != {part.name for part in plan.segments}:
        raise ReferenceValidationError("Missing or unexpected extension segment")
    pieces = [(plan.base_range.start_frame, base_arrays["speech"])]
    for part in plan.segments:
        payload = segment_arrays[part.name]
        validate_arrays(payload, part.decode_range)
        if set(payload) != set(base_arrays):
            raise ReferenceValidationError("NPZ schema keys differ between segments")
        for name, original in base_arrays.items():
            candidate = payload[name]
            if candidate.dtype != original.dtype:
                raise ReferenceValidationError("NPZ dtype differs between segments")
            if name != "speech" and (candidate.shape != original.shape or candidate.tobytes() != original.tobytes()):
                raise ReferenceValidationError("Unknown auxiliary NPZ data differs; cannot guess how to stitch it")
        trimmed = trim_context(payload, part.decode_range, part.owned_range)
        pieces.append((part.owned_range.start_frame, trimmed["speech"]))
    result = {name: array.copy() for name, array in base_arrays.items()}
    result["speech"] = np.concatenate([frames for _, frames in sorted(pieces, key=lambda item: item[0])])
    validate_arrays(result, plan.target_range)
    return result


def _time_ms(text: str) -> int:
    matched = _TIME.fullmatch(text)
    if not matched:
        raise ReferenceValidationError("Subtitle time must be a nonnegative SRT timestamp")
    h, m, s, ms = map(int, matched.groups())
    return ((h * 60 + m) * 60 + s) * 1000 + ms


def _format_ms(value: int) -> str:
    if value < 0:
        raise ReferenceValidationError("A full subtitle shift would create negative times; no cue is silently dropped or clipped")
    hours, rem = divmod(value, 3600000)
    minutes, rem = divmod(rem, 60000)
    seconds, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def _event_times(event: Event) -> tuple[int, int]:
    try:
        start, end = _time_ms(event.start), _time_ms(event.end)
    except (AttributeError, TypeError) as exc:
        raise ReferenceValidationError("Expected core.SubtitleEvent start/end/text") from exc
    if end <= start or not isinstance(event.text, str):
        raise ReferenceValidationError("Subtitle cue must contain text and a positive interval")
    return start, end


def crop_subtitles(events: Sequence[Event], span: FrameRange, guard_seconds: Any = 10,
                   *, guard: Any | None = None) -> tuple[Event, ...]:
    """Only complete guarded cues; same movie origin as the local fingerprint."""
    if guard is not None:
        if guard_seconds != 10 and guard_seconds != guard:
            raise ReferenceValidationError("Conflicting subtitle guard values")
        guard_seconds = guard
    guard_ms = _grid_seconds(guard_seconds, "guard_seconds") * FRAME_MILLISECONDS
    kept = []
    for event in events:
        start, end = _event_times(event)
        if start >= span.start_ms + guard_ms and end <= span.end_ms - guard_ms:
            kept.append(replace(event, start=_format_ms(start - span.start_ms),
                                end=_format_ms(end - span.start_ms)))
    return tuple(kept)


def offset_milliseconds(offset_seconds: Any) -> int:
    """Convert the engine estimate once; apply the same integer to both ends."""
    return int((_decimal(offset_seconds, "offset_seconds") * 1000).to_integral_value(rounding=ROUND_HALF_UP))


def shift_full_subtitles(events: Sequence[Event], offset_ms: Any) -> tuple[Event, ...]:
    """Shift the complete original cues, preserving order, text and duration."""
    delta = _integer(offset_ms, "offset_ms")
    shifted = []
    for event in events:
        start, end = _event_times(event)
        shifted.append(replace(event, start=_format_ms(start + delta), end=_format_ms(end + delta)))
    return tuple(shifted)
