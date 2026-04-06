"""Configurable STT backends for the phone call service.

Selected via CALL_STT_BACKEND env var:
  - "deepgram" (default): Deepgram streaming WS with built-in endpointing
  - "deepgram+silero": Deepgram streaming WS + local Silero VAD for turn detection
  - "whisper": Local Silero VAD + Modal Whisper endpoint for STT
"""

import asyncio
import base64
import logging
import os
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


class STTBackend(ABC):
    """Abstract base class for speech-to-text backends."""

    def __init__(self):
        self._transcript_queue: asyncio.Queue[str] = asyncio.Queue()

    @abstractmethod
    async def start(self) -> None:
        """Initialize the backend (connect to services, load models)."""

    @abstractmethod
    async def feed_audio(self, pcm16_chunk: bytes) -> None:
        """Feed a chunk of PCM16 audio to the STT pipeline."""

    async def get_transcript(self, timeout: float = 0.1) -> str | None:
        """Get the next available transcript, or None if no result ready."""
        try:
            return await asyncio.wait_for(self._transcript_queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    @abstractmethod
    async def close(self) -> None:
        """Clean up resources."""


class DeepgramSTT(STTBackend):
    """Deepgram streaming STT with utterance-level turn detection.

    Accumulates is_final fragments and only emits a full transcript when
    Deepgram fires UtteranceEnd (speaker has stopped talking), preventing
    the AI from jumping in during brief mid-sentence pauses.
    """

    def __init__(self):
        super().__init__()
        self._ws = None
        self._listen_task = None
        self._utterance_buf: list[str] = []  # accumulates is_final fragments

    async def start(self) -> None:
        import websockets

        api_key = os.environ.get("DEEPGRAM_API_KEY", "")
        if not api_key:
            raise RuntimeError("DEEPGRAM_API_KEY not set")

        url = (
            "wss://api.deepgram.com/v1/listen"
            "?encoding=linear16&sample_rate=16000&channels=1"
            "&punctuate=true&interim_results=true"
            "&endpointing=500&utterance_end_ms=1200"
            "&model=nova-2&language=en"
        )
        headers = {"Authorization": f"Token {api_key}"}
        try:
            self._ws = await websockets.connect(url, additional_headers=headers)
        except Exception as e:
            # Extract dg-error header from any websockets rejection
            dg_error = ""
            resp = getattr(e, "response", None)
            if resp:
                dg_error = getattr(resp, "headers", {}).get("dg-error", "")
                status = getattr(resp, "status_code", "?")
                body = getattr(resp, "body", b"")
                if isinstance(body, bytes):
                    body = body.decode(errors="replace")[:200]
                logger.error(f"Deepgram rejected: HTTP {status}, dg-error={dg_error}, body={body}")
                raise RuntimeError(f"Deepgram HTTP {status}: {dg_error or body or 'check API key'}") from e
            raise
        self._listen_task = asyncio.create_task(self._listen())
        logger.info("DeepgramSTT started")

    async def _listen(self) -> None:
        """Listen for transcription results from Deepgram.

        is_final fragments are buffered. The full utterance is emitted only
        when UtteranceEnd fires (1.2s of silence after last speech).
        """
        import json

        try:
            async for message in self._ws:
                data = json.loads(message)
                msg_type = data.get("type")

                if msg_type == "Results":
                    channel = data.get("channel", {})
                    alternatives = channel.get("alternatives", [])
                    if alternatives:
                        transcript = alternatives[0].get("transcript", "").strip()
                        if transcript and data.get("is_final"):
                            self._utterance_buf.append(transcript)

                elif msg_type == "UtteranceEnd":
                    # Speaker finished — emit the full accumulated utterance
                    if self._utterance_buf:
                        full = " ".join(self._utterance_buf)
                        self._utterance_buf.clear()
                        await self._transcript_queue.put(full)

        except Exception as e:
            logger.debug(f"DeepgramSTT listen ended: {e}")

    async def feed_audio(self, pcm16_chunk: bytes) -> None:
        if self._ws:
            try:
                await self._ws.send(pcm16_chunk)
            except Exception as e:
                logger.debug(f"DeepgramSTT feed error: {e}")

    async def close(self) -> None:
        if self._listen_task:
            self._listen_task.cancel()
        if self._ws:
            try:
                # Send close signal to Deepgram
                await self._ws.send(b"")
                await self._ws.close()
            except Exception:
                pass
        logger.info("DeepgramSTT closed")


class SileroDeepgramSTT(STTBackend):
    """Local Silero VAD for turn detection + Deepgram streaming for STT."""

    def __init__(self):
        super().__init__()
        self._deepgram = DeepgramSTT()
        self._vad_model = None
        self._is_speaking = False
        self._silence_frames = 0
        self._silence_threshold = 15  # ~300ms at 50fps (20ms chunks)

    async def start(self) -> None:
        import torch

        self._vad_model, _ = torch.hub.load(
            "snakers4/silero-vad", "silero_vad", force_reload=False
        )
        await self._deepgram.start()
        logger.info("SileroDeepgramSTT started")

    async def feed_audio(self, pcm16_chunk: bytes) -> None:
        import torch
        import numpy as np

        # Run VAD on the chunk
        audio = np.frombuffer(pcm16_chunk, dtype=np.int16).astype(np.float32) / 32768.0
        tensor = torch.from_numpy(audio)
        speech_prob = self._vad_model(tensor, 16000).item()

        if speech_prob > 0.5:
            self._is_speaking = True
            self._silence_frames = 0
            await self._deepgram.feed_audio(pcm16_chunk)
        elif self._is_speaking:
            self._silence_frames += 1
            # Still feed audio during short silences
            await self._deepgram.feed_audio(pcm16_chunk)
            if self._silence_frames > self._silence_threshold:
                self._is_speaking = False
                self._silence_frames = 0

    async def get_transcript(self, timeout: float = 0.1) -> str | None:
        return await self._deepgram.get_transcript(timeout)

    async def close(self) -> None:
        await self._deepgram.close()
        logger.info("SileroDeepgramSTT closed")


class WhisperModalSTT(STTBackend):
    """Local Silero VAD + Modal Whisper endpoint for STT."""

    def __init__(self):
        super().__init__()
        self._vad_model = None
        self._is_speaking = False
        self._silence_frames = 0
        self._silence_threshold = 15
        self._audio_buffer = bytearray()
        self._modal_url = os.environ.get("MODAL_WHISPER_URL", os.environ.get("MODEL_URL", ""))

    async def start(self) -> None:
        import torch

        self._vad_model, _ = torch.hub.load(
            "snakers4/silero-vad", "silero_vad", force_reload=False
        )
        logger.info("WhisperModalSTT started")

    async def feed_audio(self, pcm16_chunk: bytes) -> None:
        import torch
        import numpy as np

        audio = np.frombuffer(pcm16_chunk, dtype=np.int16).astype(np.float32) / 32768.0
        tensor = torch.from_numpy(audio)
        speech_prob = self._vad_model(tensor, 16000).item()

        if speech_prob > 0.5:
            self._is_speaking = True
            self._silence_frames = 0
            self._audio_buffer.extend(pcm16_chunk)
        elif self._is_speaking:
            self._silence_frames += 1
            self._audio_buffer.extend(pcm16_chunk)
            if self._silence_frames > self._silence_threshold:
                self._is_speaking = False
                self._silence_frames = 0
                # Send buffered audio to Modal Whisper
                if len(self._audio_buffer) > 0:
                    audio_data = bytes(self._audio_buffer)
                    self._audio_buffer.clear()
                    asyncio.create_task(self._transcribe(audio_data))

    async def _transcribe(self, pcm16_data: bytes) -> None:
        """Send audio to Modal Whisper endpoint for transcription."""
        import httpx

        if not self._modal_url:
            logger.warning("No Modal Whisper URL configured")
            return

        try:
            audio_b64 = base64.b64encode(pcm16_data).decode()
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    self._modal_url,
                    json={
                        "audio": audio_b64,
                        "encoding": "pcm16",
                        "sample_rate": 16000,
                    },
                )
                if resp.status_code == 200:
                    result = resp.json()
                    transcript = result.get("text", "").strip()
                    if transcript:
                        await self._transcript_queue.put(transcript)
        except Exception as e:
            logger.debug(f"Whisper transcription error: {e}")

    async def close(self) -> None:
        self._audio_buffer.clear()
        logger.info("WhisperModalSTT closed")


def create_stt_backend() -> STTBackend:
    """Factory: create STT backend based on CALL_STT_BACKEND env var."""
    mode = os.environ.get("CALL_STT_BACKEND", "deepgram").lower()

    if mode == "deepgram":
        return DeepgramSTT()
    elif mode == "deepgram+silero":
        return SileroDeepgramSTT()
    elif mode == "whisper":
        return WhisperModalSTT()
    else:
        logger.warning(f"Unknown STT backend '{mode}', falling back to deepgram")
        return DeepgramSTT()
