"""Local audio transcription (§9) — voice notes become text.

Telegram voice notes (ogg/opus) currently land as un-extractable media that
waits in the Inbox with no content. This module turns them into text entirely
on the owner's own hardware (faster-whisper / CTranslate2, no network at
inference), so the transcript can flow through the normal review-gated pipeline.

faster-whisper is the project's single deliberate exception to "zero new
dependencies". It is an OPTIONAL extra (``jericho[voice]``), lazy-imported and
gated behind ``FRIDAY_WHISPER_ENABLED`` — the core system runs, and audio stays
un-extractable inbox-first, when it is absent or disabled.

The pure helpers (:func:`looks_like_audio`, :func:`assemble_transcript`) are kept
free of model/file I/O so they can be unit-tested without the heavy dependency.
"""

from __future__ import annotations

import io
import logging
import math
import os
import threading
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Extensions and mime hints we treat as transcribable audio (and audio-bearing
# containers). Telegram voice notes arrive as audio/ogg with an .oga/.opus name.
_AUDIO_EXTENSIONS = frozenset(
    {
        ".ogg",
        ".oga",
        ".opus",
        ".mp3",
        ".mpga",
        ".wav",
        ".m4a",
        ".aac",
        ".flac",
        ".webm",
        ".mp4",
        ".mpeg",
        ".amr",
        ".wma",
    }
)
_AUDIO_MIME_HINTS = ("audio/", "video/ogg", "video/webm")


class WhisperUnavailable(RuntimeError):
    """faster-whisper is not installed, or the model could not be loaded."""


@dataclass
class Transcript:
    """Result of a transcription, plus enough provenance to review it."""

    text: str
    language: str | None
    language_probability: float
    confidence: float  # mean per-segment confidence in 0..1
    duration: float  # audio length in seconds
    segment_count: int
    model: str

    @property
    def is_empty(self) -> bool:
        return not self.text.strip()


def looks_like_audio(*, content_type: str | None, filename: str | None) -> bool:
    """Whether a file should be routed to transcription instead of left as
    un-extractable media. Matches on mime hint first, then extension."""
    ct = (content_type or "").lower()
    if any(hint in ct for hint in _AUDIO_MIME_HINTS):
        return True
    _, ext = os.path.splitext((filename or "").lower())
    return ext in _AUDIO_EXTENSIONS


def _segment_confidence(avg_logprob: float) -> float:
    """Map a segment's ``avg_logprob`` (~[-1, 0], higher is better) to 0..1.

    ``exp(avg_logprob)`` is the geometric-mean token probability — a reasonable,
    monotonic proxy for how sure the model was about that segment.
    """
    try:
        return max(0.0, min(1.0, math.exp(avg_logprob)))
    except (OverflowError, ValueError):
        return 0.0


def assemble_transcript(segments, info, *, model: str) -> Transcript:
    """Fold faster-whisper's ``(segments, info)`` into a :class:`Transcript`.

    Segments is any iterable of objects exposing ``text`` and ``avg_logprob``;
    ``info`` exposes ``language``, ``language_probability`` and ``duration``.
    Kept pure (no model, no file) so it is unit-testable with lightweight fakes.
    """
    texts: list[str] = []
    confidences: list[float] = []
    count = 0
    for seg in segments:
        piece = (getattr(seg, "text", "") or "").strip()
        if piece:
            texts.append(piece)
        confidences.append(_segment_confidence(float(getattr(seg, "avg_logprob", -10.0))))
        count += 1
    text = " ".join(texts).strip()
    confidence = round(sum(confidences) / len(confidences), 4) if confidences else 0.0
    return Transcript(
        text=text,
        language=getattr(info, "language", None),
        language_probability=round(float(getattr(info, "language_probability", 0.0) or 0.0), 4),
        confidence=confidence,
        duration=round(float(getattr(info, "duration", 0.0) or 0.0), 2),
        segment_count=count,
        model=model,
    )


_MODEL_LOCK = threading.Lock()
_MODEL_CACHE: dict[tuple[str, str, str, str | None], object] = {}


def _load_model(model: str, *, device: str, compute_type: str, download_root: str | None):
    """Load (and memoise) a WhisperModel. The model weights are fetched from
    HuggingFace on first use and cached under ``download_root``; inference is
    fully local afterwards."""
    key = (model, device, compute_type, download_root)
    cached = _MODEL_CACHE.get(key)
    if cached is not None:
        return cached
    with _MODEL_LOCK:
        cached = _MODEL_CACHE.get(key)
        if cached is not None:
            return cached
        try:
            from faster_whisper import WhisperModel  # type: ignore[import-untyped]  # optional dep
        except ImportError as exc:  # pragma: no cover - exercised via WhisperUnavailable
            raise WhisperUnavailable(
                "faster-whisper is not installed; install the 'jericho[voice]' extra"
            ) from exc
        try:
            engine = WhisperModel(
                model,
                device=device,
                compute_type=compute_type,
                download_root=download_root,
            )
        except Exception as exc:  # noqa: BLE001 - surface any load failure uniformly
            raise WhisperUnavailable(f"could not load Whisper model {model!r}: {exc}") from exc
        _MODEL_CACHE[key] = engine
        logger.info("whisper: loaded model=%s device=%s compute_type=%s", model, device, compute_type)
        return engine


def _transcribe_source(
    audio,
    *,
    model: str,
    language: str | None,
    device: str,
    compute_type: str,
    beam_size: int,
    download_root: str | None,
) -> Transcript:
    engine = _load_model(model, device=device, compute_type=compute_type, download_root=download_root)
    segments, info = engine.transcribe(
        audio,
        language=language,
        beam_size=beam_size,
        vad_filter=True,
    )
    return assemble_transcript(segments, info, model=model)


def transcribe_file(
    path: str,
    *,
    model: str = "small",
    language: str | None = None,
    device: str = "cpu",
    compute_type: str = "int8",
    beam_size: int = 5,
    download_root: str | None = None,
) -> Transcript:
    """Transcribe an audio file (by path) to text, entirely locally.

    ``language=None`` auto-detects — required, since voice notes are Russian by
    default but not exclusively. Raises :class:`WhisperUnavailable` when the
    optional dependency or the model is missing, so callers can fall back to the
    un-extractable-media path instead of failing ingestion.
    """
    return _transcribe_source(
        path,
        model=model,
        language=language,
        device=device,
        compute_type=compute_type,
        beam_size=beam_size,
        download_root=download_root,
    )


def transcribe_bytes(
    content: bytes,
    *,
    model: str = "small",
    language: str | None = None,
    device: str = "cpu",
    compute_type: str = "int8",
    beam_size: int = 5,
    download_root: str | None = None,
) -> Transcript:
    """Transcribe raw audio bytes to text without touching the filesystem.

    faster-whisper decodes any container it understands (ogg/opus, mp3, wav, …)
    straight from a seekable file object, so ingestion can hand over the received
    bytes directly. Same failure contract as :func:`transcribe_file`.
    """
    return _transcribe_source(
        io.BytesIO(content),
        model=model,
        language=language,
        device=device,
        compute_type=compute_type,
        beam_size=beam_size,
        download_root=download_root,
    )
