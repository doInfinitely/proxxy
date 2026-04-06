"""Audio format conversion utilities for the Twilio ↔ STT/TTS pipeline.

Twilio sends/receives mulaw 8kHz mono.
Deepgram expects PCM16 16kHz mono.
ElevenLabs outputs PCM16 24kHz mono (mp3_44100_128 or pcm_24000).
"""

import audioop
import struct

import numpy as np


def mulaw_to_pcm16(data: bytes) -> bytes:
    """Convert mulaw-encoded bytes to signed 16-bit PCM."""
    return audioop.ulaw2lin(data, 2)


def pcm16_to_mulaw(data: bytes) -> bytes:
    """Convert signed 16-bit PCM bytes to mulaw."""
    return audioop.lin2ulaw(data, 2)


def resample(data: bytes, from_rate: int, to_rate: int) -> bytes:
    """Resample PCM16 mono audio between sample rates.

    Uses linear interpolation via numpy for quality.
    """
    if from_rate == to_rate:
        return data

    # Decode PCM16 to numpy int16 array
    samples = np.frombuffer(data, dtype=np.int16).astype(np.float64)
    if len(samples) == 0:
        return data

    # Compute output length
    out_len = int(len(samples) * to_rate / from_rate)
    if out_len == 0:
        return data

    # Linear interpolation
    indices = np.linspace(0, len(samples) - 1, out_len)
    resampled = np.interp(indices, np.arange(len(samples)), samples)

    # Clip and convert back to int16 bytes
    resampled = np.clip(resampled, -32768, 32767).astype(np.int16)
    return resampled.tobytes()


def mulaw_to_pcm16_16k(data: bytes) -> bytes:
    """Convert Twilio mulaw 8kHz to PCM16 16kHz (for Deepgram)."""
    pcm = mulaw_to_pcm16(data)
    return resample(pcm, 8000, 16000)


def pcm16_24k_to_mulaw_8k(data: bytes) -> bytes:
    """Convert ElevenLabs PCM16 24kHz to mulaw 8kHz (for Twilio)."""
    pcm_8k = resample(data, 24000, 8000)
    return pcm16_to_mulaw(pcm_8k)


def pcm16_16k_to_mulaw_8k(data: bytes) -> bytes:
    """Convert PCM16 16kHz to mulaw 8kHz (for Twilio)."""
    pcm_8k = resample(data, 16000, 8000)
    return pcm16_to_mulaw(pcm_8k)


# ─── DTMF tone generation ───

# Standard DTMF frequency pairs (row_freq, col_freq) for each key
_DTMF_FREQS: dict[str, tuple[int, int]] = {
    "1": (697, 1209), "2": (697, 1336), "3": (697, 1477), "A": (697, 1633),
    "4": (770, 1209), "5": (770, 1336), "6": (770, 1477), "B": (770, 1633),
    "7": (852, 1209), "8": (852, 1336), "9": (852, 1477), "C": (852, 1633),
    "*": (941, 1209), "0": (941, 1336), "#": (941, 1477), "D": (941, 1633),
}


def generate_dtmf_mulaw(digits: str, sample_rate: int = 8000,
                         tone_ms: int = 150, gap_ms: int = 100) -> bytes:
    """Generate DTMF tones as mulaw audio for sending via Twilio media stream.

    Args:
        digits: String of DTMF digits (0-9, *, #, A-D, 'w' for 0.5s pause)
        sample_rate: Output sample rate (8000 for Twilio)
        tone_ms: Duration of each tone in milliseconds
        gap_ms: Silence gap between tones in milliseconds

    Returns:
        mulaw-encoded audio bytes
    """
    tone_samples = int(sample_rate * tone_ms / 1000)
    gap_samples = int(sample_rate * gap_ms / 1000)
    pause_samples = int(sample_rate * 0.5)  # 'w' = 500ms pause

    t_tone = np.arange(tone_samples) / sample_rate
    all_samples = []

    for digit in digits.upper():
        if digit == "W":
            # 500ms pause
            all_samples.append(np.zeros(pause_samples))
            continue

        freqs = _DTMF_FREQS.get(digit)
        if not freqs:
            continue

        f1, f2 = freqs
        # Generate dual-tone signal (each at half amplitude to avoid clipping)
        signal = 0.5 * (np.sin(2 * np.pi * f1 * t_tone) +
                         np.sin(2 * np.pi * f2 * t_tone))
        # Scale to int16 range at ~70% volume
        signal = (signal * 0.7 * 32767).astype(np.float64)
        all_samples.append(signal)

        # Add inter-digit gap
        all_samples.append(np.zeros(gap_samples))

    if not all_samples:
        return b""

    combined = np.concatenate(all_samples)
    pcm16 = np.clip(combined, -32768, 32767).astype(np.int16).tobytes()
    return pcm16_to_mulaw(pcm16)
