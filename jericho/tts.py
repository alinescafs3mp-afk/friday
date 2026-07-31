"""Local text-to-speech — Jericho speaks a reply on request within a conversation.

piper-tts is folded into the same optional extra as the existing speech-to-text
exception (``jericho[voice]``), lazy-imported and gated behind
``JERICHO_TTS_ENABLED`` — the core system runs, and the agent's ``speak`` tool
stays unavailable, when it is absent or disabled. See :mod:`jericho.whisper` for
the sibling STT module this one deliberately mirrors.

Encoding to OGG/Opus (the format Telegram's ``sendVoice`` requires for a native
voice-message bubble) uses PyAV (``av``), which faster-whisper already pulls in
transitively — so this adds no new heavy dependency category on top of the
existing STT exception, only the lightweight onnxruntime-based piper-tts itself.

The pure helper (:func:`sanitize_text`) is kept free of model/file I/O so it can
be unit-tested without the heavy dependency.
"""

from __future__ import annotations

import io
import logging
import threading
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_OPUS_RATE = 48000  # only rate/channel-count libopus accepts for encoding


class TTSUnavailable(RuntimeError):
    """piper-tts (or PyAV) is not installed, or the voice model could not be loaded."""


@dataclass
class Speech:
    """Result of a synthesis, plus enough provenance to log or review it."""

    audio_bytes: bytes  # ogg container, opus codec, mono, 48kHz
    sample_rate: int
    duration_sec: float
    voice: str
    truncated: bool


def sanitize_text(text: str, *, max_chars: int) -> tuple[str, bool]:
    """Collapse whitespace and cap length before synthesis.

    Pure and model-free so it is unit-testable without piper installed. Returns
    ``(cleaned, truncated)`` — the caller decides whether to warn about
    truncation; a very long request must not turn into unbounded synthesis time.
    """
    cleaned = " ".join(text.split())
    truncated = len(cleaned) > max_chars
    return cleaned[:max_chars], truncated


_MODEL_LOCK = threading.Lock()
_MODEL_CACHE: dict[tuple[str, str], object] = {}


def _voice_paths(voice: str, download_root: str) -> tuple[Path, Path]:
    base = Path(download_root)
    return base / f"{voice}.onnx", base / f"{voice}.onnx.json"


def _load_voice(voice: str, download_root: str):
    """Load (and memoise) a PiperVoice. Model files are fetched on first use and
    cached under ``download_root``; synthesis is fully local afterwards."""
    key = (voice, download_root)
    cached = _MODEL_CACHE.get(key)
    if cached is not None:
        return cached
    with _MODEL_LOCK:
        cached = _MODEL_CACHE.get(key)
        if cached is not None:
            return cached
        try:
            from piper import PiperVoice  # type: ignore[import-untyped]  # optional dep
            from piper.download_voices import download_voice  # type: ignore[import-untyped]
        except ImportError as exc:  # pragma: no cover - exercised via TTSUnavailable
            raise TTSUnavailable("piper-tts is not installed; install the 'jericho[voice]' extra") from exc
        model_path, config_path = _voice_paths(voice, download_root)
        try:
            base = model_path.parent
            if not model_path.exists() or not config_path.exists():
                base.mkdir(parents=True, exist_ok=True)
                download_voice(voice, base)
            engine = PiperVoice.load(model_path, config_path, download_dir=base)
        except Exception as exc:  # noqa: BLE001 - surface any load failure uniformly
            raise TTSUnavailable(f"could not load Piper voice {voice!r}: {exc}") from exc
        _MODEL_CACHE[key] = engine
        logger.info("tts: loaded voice=%s", voice)
        return engine


def _encode_opus_ogg(pcm_bytes: bytes, *, source_rate: int) -> bytes:
    """Resample int16 mono PCM to 48kHz and mux it into an OGG/Opus container."""
    try:
        import av
        import numpy as np
    except ImportError as exc:  # pragma: no cover - exercised via TTSUnavailable
        raise TTSUnavailable("PyAV is not installed; install the 'jericho[voice]' extra") from exc

    buffer = io.BytesIO()
    container = av.open(buffer, mode="w", format="ogg")
    try:
        stream = container.add_stream("libopus", rate=_OPUS_RATE)
        stream.layout = "mono"
        resampler = av.audio.resampler.AudioResampler(format="s16", layout="mono", rate=_OPUS_RATE)

        samples = np.frombuffer(pcm_bytes, dtype=np.int16).reshape(1, -1)
        frame = av.AudioFrame.from_ndarray(samples, format="s16", layout="mono")
        frame.sample_rate = source_rate

        for resampled in resampler.resample(frame):
            for packet in stream.encode(resampled):
                container.mux(packet)
        for packet in stream.encode(None):
            container.mux(packet)
    finally:
        container.close()
    return buffer.getvalue()


def synthesize_speech(
    text: str,
    *,
    voice: str = "ru_RU-irina-medium",
    download_root: str = "",
    max_chars: int = 2000,
) -> Speech:
    """Synthesize ``text`` into an OGG/Opus voice clip, entirely locally.

    Raises :class:`TTSUnavailable` when the optional dependency, PyAV, or the
    voice model is missing, so callers can degrade to a text-only reply instead
    of failing the turn. Raises :class:`ValueError` for empty input after
    sanitization — that is a caller bug (nothing to speak), not a runtime
    availability problem.
    """
    cleaned, truncated = sanitize_text(text, max_chars=max_chars)
    if not cleaned:
        raise ValueError("nothing to speak: text is empty after sanitization")

    engine = _load_voice(voice, download_root)
    pcm_chunks: list[bytes] = []
    sample_rate = 22050
    try:
        for chunk in engine.synthesize(cleaned):
            sample_rate = chunk.sample_rate
            pcm_chunks.append(chunk.audio_int16_bytes)
    except Exception as exc:  # noqa: BLE001 - surface any inference failure uniformly
        raise TTSUnavailable(f"synthesis failed for voice {voice!r}: {exc}") from exc

    pcm = b"".join(pcm_chunks)
    if not pcm:
        # Found by adversarial review (G23): a voice with zero synthesized chunks
        # would otherwise reach the encoder with an empty buffer. Surfaced as the
        # same unavailability the caller already handles, not a silent empty clip.
        raise TTSUnavailable(f"voice {voice!r} produced no audio for the given text")
    duration_sec = round(len(pcm) / 2 / sample_rate, 2) if sample_rate else 0.0
    audio_bytes = _encode_opus_ogg(pcm, source_rate=sample_rate)
    return Speech(
        audio_bytes=audio_bytes,
        sample_rate=_OPUS_RATE,
        duration_sec=duration_sec,
        voice=voice,
        truncated=truncated,
    )
