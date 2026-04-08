"""Phone call service: manages the full Twilio ↔ STT ↔ GPT ↔ TTS call lifecycle.

The AI acts as a proxy on the phone with a business. The user communicates via
the Agent chat UI. If the AI needs information, it injects a question into the
chat and waits for the user to respond before continuing.
"""

import audioop
import asyncio
import base64
import json
import logging
import math
import os
import re
import time
import uuid

from openai import AsyncOpenAI
from twilio.rest import Client as TwilioClient

from audio_utils import (
    mulaw_to_pcm16, mulaw_to_pcm16_16k, pcm16_24k_to_mulaw_8k,
    generate_dtmf_mulaw,
)
from stt_backend import create_stt_backend
from call_billing import deduct_minutes

logger = logging.getLogger(__name__)

# Markers the GPT model uses to signal special actions
ASK_USER_RE = re.compile(r"\[ASK_USER:\s*(.+?)\]", re.DOTALL)
CALL_COMPLETE_RE = re.compile(r"\[CALL_COMPLETE:\s*(.+?)\]", re.DOTALL)
PRESS_RE = re.compile(r"\[PRESS:\s*([0-9*#A-Dw]+)\]", re.IGNORECASE)

SYSTEM_PROMPT = """You are Proxxy, an AI assistant on a live phone call. You are NOT a human.
You are calling {contact_name} on behalf of your user.

IDENTITY: You are Proxxy, an AI. Do NOT pretend to be the user or use the user's name as
your own. If asked who you are, say "I'm Proxxy, an AI assistant calling on behalf of
[the user]." If asked for the user's name, you may share it from the context below.

The user asked you to call because: {user_purpose}

Who you are calling:
- Name: {name}
- Address: {address}
- Phone: {phone}

RULES:
1. SPEAK NATURALLY. You are on a voice call — just talk like a normal person.
   Your response text will be spoken aloud via text-to-speech. Keep it concise.
2. NEVER use [PRESS: ...] unless you hear an ACTUAL automated phone menu that
   explicitly says "Press 1 for ..., press 2 for ..." with numbered options.
   If a real person is talking to you, NEVER press buttons — just respond verbally.
3. If you hear an automated menu (IVR), select the best option:
   [PRESS: 1]  — you can press multiple digits [PRESS: 12] or pause [PRESS: 1w2]
4. If you need information from the user that you don't have:
   [ASK_USER: your question here]
5. When the call objective is achieved or the call is ending:
   [CALL_COMPLETE: brief summary of the outcome]
6. You CANNOT send emails, texts, links, or any other follow-up during the call.
   Everything must be done verbally on this call."""


class CallService:
    """Manages a single phone call lifecycle."""

    def __init__(
        self,
        session_ws,
        chat_history: list[dict],
        business_info: dict,
        uid: str | None = None,
        voice_id: str | None = None,
        about_me: str = "",
    ):
        self.session_ws = session_ws
        self.chat_history = chat_history
        self.business = business_info
        self.uid = uid
        self.voice_id = voice_id or os.environ.get("ELEVENLABS_VOICE_ID", "iP95p4xoKVk53GoZ742B")
        self.about_me = about_me
        self.call_id = uuid.uuid4().hex[:12]
        self.call_sid: str | None = None

        # Audio/STT pipeline
        self.stt = create_stt_backend()
        self.openai = AsyncOpenAI()
        self.twilio = TwilioClient(
            os.environ["TWILIO_ACCOUNT_SID"],
            os.environ["TWILIO_AUTH_TOKEN"],
        )

        # Call state
        self.phone_transcript: list[dict] = []  # [{role: "business"|"agent", content: "..."}]
        self.user_purpose: str = ""
        self._media_ws = None
        self._stream_sid: str | None = None
        self._pending_user_response: asyncio.Event = asyncio.Event()
        self._user_response_text: str = ""
        self._call_active = False
        self._call_ended = False
        self._stt_task: asyncio.Task | None = None
        self._respond_task: asyncio.Task | None = None
        self._transcript_queue: asyncio.Queue = asyncio.Queue()
        self._tts_lock = asyncio.Lock()
        self._speaking = False  # True while TTS audio is being sent
        self._interrupted = False  # Set when barge-in detected during TTS
        self._vad = self._init_vad()
        self._vad_window: list[bool] = []  # sliding window for barge-in (last 20 frames)

        # User takeover: when True, AI pauses and user speaks via conference bridge
        self._user_takeover = False

        # Conference bridge state (for user takeover)
        self._conf_room: str | None = None
        self._conf_stream_ws = None
        # Two STT backends: inbound = business mic, outbound = user voice to business
        self._conf_stt_business = None
        self._conf_stt_user = None
        self._conf_stt_tasks: list[asyncio.Task] = []
        self._switching_to_conference = False

        # Billing
        self._call_start_time: float | None = None

    @staticmethod
    def _init_vad():
        """Initialize WebRTC VAD for barge-in detection (telephony-optimized)."""
        try:
            import webrtcvad
            vad = webrtcvad.Vad()
            vad.set_mode(3)  # Most aggressive — best at rejecting phone line noise
            return vad
        except ImportError:
            logger.warning("webrtcvad not installed — falling back to energy-based barge-in")
            return None

    def _is_speech(self, pcm16_8k: bytes) -> bool:
        """Check if a PCM16 8kHz frame contains speech using WebRTC VAD + RMS.

        RMS >= 500 cleanly separates real speech (typically 1000-11000 RMS)
        from echo/noise (peaks at ~150 RMS). No time-based echo suppression
        needed — the amplitude gap is large enough.
        """
        if not self._vad:
            return False
        try:
            frame_size = 320  # 20ms at 8kHz, 16-bit
            for i in range(0, len(pcm16_8k) - frame_size + 1, frame_size):
                frame = pcm16_8k[i:i + frame_size]
                if self._vad.is_speech(frame, 8000):
                    rms = audioop.rms(frame, 2)
                    if rms >= 500:
                        return True
            return False
        except Exception as e:
            logger.warning(f"VAD error: {e} (data len={len(pcm16_8k)})")
            return False

    async def start_call(self, phone_number: str) -> None:
        """Initiate a Twilio outbound call."""
        # Extract user purpose from chat history
        for msg in reversed(self.chat_history):
            if msg.get("role") == "user":
                self.user_purpose = msg.get("content", "")
                break

        host = os.environ.get("PUBLIC_DOMAIN", os.environ.get("RAILWAY_PUBLIC_DOMAIN", "localhost:8080"))
        twiml_url = f"https://{host}/api/calls/twiml/{self.call_id}"
        status_url = f"https://{host}/api/calls/status/{self.call_id}"

        try:
            call = self.twilio.calls.create(
                to=phone_number,
                from_=os.environ["TWILIO_PHONE_NUMBER"],
                url=twiml_url,
                status_callback=status_url,
                status_callback_event=["initiated", "ringing", "answered", "completed"],
                status_callback_method="POST",
            )
            self.call_sid = call.sid
            self._call_active = True

            await self._send_to_chat({
                "type": "call_starting",
                "contact_name": self.business.get("name", ""),
                "call_id": self.call_id,
            })

            logger.info(f"Call initiated: {self.call_sid} to {phone_number}")
        except Exception as e:
            logger.error(f"Failed to start call: {e}")
            await self._send_to_chat({
                "type": "call_ended",
                "content": f"Failed to start call: {e}",
            })

    async def handle_media_stream(self, ws) -> None:
        """Process Twilio media stream WebSocket — the core audio loop."""
        self._media_ws = ws
        self._call_start_time = time.monotonic()
        try:
            await self.stt.start()
        except Exception as e:
            logger.error(f"STT start failed for call {self.call_id}: {e}", exc_info=True)
            await self._send_to_chat({
                "type": "call_ended",
                "content": f"Call failed: speech recognition could not start ({e})",
            })
            self._call_active = False
            return

        await self._send_to_chat({
            "type": "call_connected",
            "contact_name": self.business.get("name", ""),
        })

        # STT loop sends transcript bubbles in real-time; respond loop
        # processes them through GPT/TTS without blocking transcript display
        self._stt_task = asyncio.create_task(self._process_stt_loop())
        self._respond_task = asyncio.create_task(self._respond_loop())

        try:
            async for raw in ws.iter_text():
                msg = json.loads(raw)
                event = msg.get("event")

                if event == "media":
                    payload = msg["media"]["payload"]
                    mulaw_bytes = base64.b64decode(payload)

                    # Forward inbound audio to frontend for live listening
                    await self._send_to_chat({
                        "type": "call_audio",
                        "audio": payload,  # already base64 mulaw
                    })

                    # Barge-in detection: VAD mode 3 + RMS >= 500 separates real
                    # speech from echo/noise without needing time-based suppression.
                    if self._speaking and not self._interrupted:
                        pcm16_8k = mulaw_to_pcm16(mulaw_bytes)
                        is_speech = self._is_speech(pcm16_8k)
                        self._vad_window.append(is_speech)
                        if len(self._vad_window) > 20:
                            self._vad_window.pop(0)
                        speech_count = sum(self._vad_window)
                        if speech_count >= 5:
                            self._interrupted = True
                            self._vad_window.clear()
                            logger.info("Barge-in detected — stopping TTS")
                            await self._flush_twilio_audio()

                    # Always feed business audio to STT (transcribe during takeover too)
                    pcm16 = mulaw_to_pcm16_16k(mulaw_bytes)
                    await self.stt.feed_audio(pcm16)

                elif event == "start":
                    self._stream_sid = msg.get("start", {}).get("streamSid")
                    logger.info(f"Media stream started: {self._stream_sid}")

                elif event == "stop":
                    logger.info("Media stream stopped")
                    break

        except Exception as e:
            logger.debug(f"Media stream ended: {e}")
        finally:
            if not self._switching_to_conference:
                self._call_active = False
            self._switching_to_conference = False
            if self._stt_task:
                self._stt_task.cancel()
            if self._respond_task:
                self._respond_task.cancel()
            await self.stt.close()

    async def _process_stt_loop(self) -> None:
        """Poll STT and send transcript bubbles to frontend in real-time.

        Never blocks on GPT/TTS — just queues transcripts for _respond_loop.
        """
        try:
            while self._call_active:
                transcript = await self.stt.get_transcript(timeout=0.2)
                if transcript is None:
                    continue

                # Send bubble immediately so it appears in correct order
                self.phone_transcript.append({"role": "business", "content": transcript})
                await self._send_to_chat({
                    "type": "call_transcript",
                    "speaker": "business",
                    "content": transcript,
                })

                # Queue for GPT response (unless user is speaking)
                if not self._user_takeover:
                    await self._transcript_queue.put(transcript)

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"STT processing error: {e}")

    async def _respond_loop(self) -> None:
        """Process queued transcripts through GPT + TTS (separate from STT polling)."""
        try:
            while self._call_active:
                try:
                    transcript = await asyncio.wait_for(
                        self._transcript_queue.get(), timeout=0.5
                    )
                except asyncio.TimeoutError:
                    continue

                if self._user_takeover:
                    continue

                response = await self._get_gpt_response(transcript)
                should_end = await self._handle_response(response)
                if should_end:
                    return

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"Respond loop error: {e}")

    async def _handle_response(self, response: str) -> bool:
        """Parse GPT response for markers and act on them.

        Handles [PRESS: ...], [ASK_USER: ...], [CALL_COMPLETE: ...] markers,
        and speaks any plain text. Returns True if the call should end.
        """
        # Check for DTMF press markers first — there can be multiple
        press_matches = list(PRESS_RE.finditer(response))
        if press_matches:
            # Speak any plain text in the response via TTS so the other
            # party hears it (not just the chat UI)
            narration = PRESS_RE.sub("", response).strip()
            narration = ASK_USER_RE.sub("", narration).strip()
            narration = CALL_COMPLETE_RE.sub("", narration).strip()
            if narration:
                await self._speak(narration)

            # Send each DTMF sequence
            for m in press_matches:
                digits = m.group(1).strip()
                await self._send_dtmf(digits)

            # Check if response also has CALL_COMPLETE after the PRESS
            if CALL_COMPLETE_RE.search(response):
                summary = CALL_COMPLETE_RE.search(response).group(1).strip()
                await self.end_call(summary)
                return True

            # Check if response also has ASK_USER after the PRESS
            if ASK_USER_RE.search(response):
                question = ASK_USER_RE.search(response).group(1).strip()
                await self.inject_question(question)
                await self._pending_user_response.wait()
                self._pending_user_response.clear()
                followup = await self._get_gpt_response(
                    f"[User responded: {self._user_response_text}]"
                )
                return await self._handle_response(followup)

            return False

        # Check for ASK_USER
        ask_match = ASK_USER_RE.search(response)
        if ask_match:
            question = ask_match.group(1).strip()
            await self.inject_question(question)
            await self._speak("One moment please, let me check on that.")
            await self._pending_user_response.wait()
            self._pending_user_response.clear()
            followup = await self._get_gpt_response(
                f"[User responded: {self._user_response_text}]"
            )
            return await self._handle_response(followup)

        # Check for CALL_COMPLETE
        complete_match = CALL_COMPLETE_RE.search(response)
        if complete_match:
            summary = complete_match.group(1).strip()
            clean = CALL_COMPLETE_RE.sub("", response).strip()
            if clean:
                await self._speak(clean)
            await self.end_call(summary)
            return True

        # No markers — just speak the response
        await self._speak(response)
        return False

    async def _send_dtmf(self, digits: str) -> None:
        """Generate DTMF tones and send them through the Twilio media stream."""
        await self._send_to_chat({
            "type": "call_transcript",
            "speaker": "agent",
            "content": f"[Pressing: {digits}]",
        })

        mulaw_audio = generate_dtmf_mulaw(digits)
        if mulaw_audio:
            await self._send_audio_to_twilio(mulaw_audio)
            # Brief pause after DTMF to let the IVR process
            await asyncio.sleep(0.5)

    async def _get_gpt_response(self, latest_input: str) -> str:
        """Build GPT messages and get a response."""
        system = SYSTEM_PROMPT.format(
            contact_name=self.business.get("name", "Unknown"),
            user_purpose=self.user_purpose,
            name=self.business.get("name", "Unknown"),
            address=self.business.get("address", "N/A"),
            phone=self.business.get("phone", "N/A"),
        )
        if self.about_me:
            system += f"\n\nAbout the user:\n{self.about_me}"

        messages = [{"role": "system", "content": system}]

        # Add phone transcript context
        for entry in self.phone_transcript:
            role = "assistant" if entry["role"] == "agent" else "user"
            messages.append({"role": role, "content": entry["content"]})

        # Add the latest input
        messages.append({"role": "user", "content": latest_input})

        try:
            resp = await self.openai.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages,
                max_tokens=200,
                temperature=0.7,
            )
            text = resp.choices[0].message.content or ""
            self.phone_transcript.append({"role": "agent", "content": text})
            return text
        except Exception as e:
            logger.error(f"GPT error: {e}")
            return "I'm sorry, could you repeat that?"

    async def _speak(self, text: str) -> None:
        """Convert text to speech via ElevenLabs and send audio to Twilio."""
        if not self._call_active:
            return
        async with self._tts_lock:
            self._speaking = True
            self._interrupted = False
            self._vad_window.clear()

            await self._send_to_chat({
                "type": "call_transcript",
                "speaker": "agent",
                "content": text,
            })

            try:
                import websockets

                api_key = os.environ["ELEVENLABS_API_KEY"]
                voice_id = self.voice_id
                uri = (
                    f"wss://api.elevenlabs.io/v1/text-to-speech/{voice_id}/stream-input"
                    f"?model_id=eleven_turbo_v2_5"
                    f"&output_format=pcm_24000"
                )

                async with websockets.connect(
                    uri,
                    additional_headers={"xi-api-key": api_key},
                ) as tts_ws:
                    # Send initial config
                    await tts_ws.send(json.dumps({
                        "text": " ",
                        "voice_settings": {
                            "stability": 0.5,
                            "similarity_boost": 0.75,
                        },
                    }))

                    # Send the text
                    await tts_ws.send(json.dumps({"text": text + " "}))

                    # Signal end of input
                    await tts_ws.send(json.dumps({"text": ""}))

                    # Collect audio chunks and send to both Twilio and frontend
                    async for msg in tts_ws:
                        # Stop sending if interrupted by barge-in or call ended
                        if self._interrupted or not self._call_active:
                            logger.info("TTS interrupted, stopping playback")
                            break
                        data = json.loads(msg)
                        audio_b64 = data.get("audio")
                        if audio_b64:
                            pcm24k = base64.b64decode(audio_b64)
                            mulaw = pcm16_24k_to_mulaw_8k(pcm24k)
                            await self._send_audio_to_twilio(mulaw)
                            # Forward to frontend so user hears AI side
                            mulaw_b64 = base64.b64encode(mulaw).decode("ascii")
                            await self._send_to_chat({
                                "type": "call_audio",
                                "audio": mulaw_b64,
                            })

            except Exception as e:
                logger.error(f"TTS error: {e}")
            finally:
                self._speaking = False

    async def _send_audio_to_twilio(self, mulaw_data: bytes) -> None:
        """Send mulaw audio to the Twilio media stream."""
        if not self._media_ws or not self._stream_sid:
            return

        # Twilio expects base64-encoded mulaw in 20ms chunks (160 bytes at 8kHz)
        chunk_size = 160
        for i in range(0, len(mulaw_data), chunk_size):
            chunk = mulaw_data[i : i + chunk_size]
            payload = base64.b64encode(chunk).decode("ascii")
            try:
                await self._media_ws.send_json({
                    "event": "media",
                    "streamSid": self._stream_sid,
                    "media": {"payload": payload},
                })
            except Exception as e:
                logger.debug(f"Twilio send error: {e}")
                break

    async def _cleanup_conf_stt(self) -> None:
        """Cancel conference STT tasks and close backends."""
        for task in self._conf_stt_tasks:
            task.cancel()
        self._conf_stt_tasks = []
        if self._conf_stt_business:
            await self._conf_stt_business.close()
            self._conf_stt_business = None
        if self._conf_stt_user:
            await self._conf_stt_user.close()
            self._conf_stt_user = None

    async def _flush_twilio_audio(self) -> None:
        """Clear any buffered audio in the Twilio media stream."""
        if self._media_ws and self._stream_sid:
            try:
                await self._media_ws.send_json({
                    "event": "clear",
                    "streamSid": self._stream_sid,
                })
            except Exception:
                pass

    async def inject_question(self, question: str) -> None:
        """Send a question from the call agent to the user via the chat UI."""
        await self._send_to_chat({
            "type": "call_question",
            "content": question,
        })

    async def receive_user_response(self, text: str) -> None:
        """Handle a user's response to a call question."""
        self._user_response_text = text
        self._pending_user_response.set()

    async def set_takeover(self, active: bool) -> None:
        """Toggle user takeover mode via Twilio Conference bridge."""
        self._user_takeover = active
        mode = "user" if active else "AI"

        # Stop any in-flight TTS immediately and flush Twilio audio buffer
        if active:
            self._interrupted = True
            await self._flush_twilio_audio()

        await self._send_to_chat({
            "type": "call_transcript",
            "speaker": "agent",
            "content": f"[{mode} is now speaking]",
        })
        if active:
            await self._enter_conference()
        else:
            await self._leave_conference()

    async def _enter_conference(self) -> None:
        """Redirect the business call leg into a Twilio Conference room."""
        self._switching_to_conference = True
        self._conf_room = f"room-{self.call_id}"

        host = os.environ.get("PUBLIC_DOMAIN", os.environ.get("RAILWAY_PUBLIC_DOMAIN", "localhost:8080"))
        conf_twiml_url = f"https://{host}/api/calls/conf-twiml/{self.call_id}"

        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None, lambda: self.twilio.calls(self.call_sid).update(url=conf_twiml_url, method="POST")
            )
            logger.info(f"Business redirected to conference {self._conf_room}")
        except Exception as e:
            logger.error(f"Failed to redirect to conference: {e}")
            self._switching_to_conference = False
            self._conf_room = None
            return

        # Tell frontend to join the conference via Twilio Client SDK
        logger.info(f"Sending conference_ready to frontend: room={self._conf_room}")
        await self._send_to_chat({
            "type": "conference_ready",
            "room": self._conf_room,
        })

    async def _leave_conference(self) -> None:
        """Redirect the business call leg back to the AI media stream."""
        # Stop conference STT tasks
        await self._cleanup_conf_stt()

        # Redirect business back to media stream
        host = os.environ.get("PUBLIC_DOMAIN", os.environ.get("RAILWAY_PUBLIC_DOMAIN", "localhost:8080"))
        stream_twiml_url = f"https://{host}/api/calls/twiml/{self.call_id}"

        try:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None, lambda: self.twilio.calls(self.call_sid).update(url=stream_twiml_url, method="POST")
            )
            logger.info(f"Business redirected back to media stream")
        except Exception as e:
            logger.error(f"Failed to redirect back to stream: {e}")

        # Add context note for AI
        self.phone_transcript.append({
            "role": "agent",
            "content": "[User spoke directly on the call, now handing back to AI]",
        })

        self._conf_room = None

    async def handle_conference_stream(self, ws) -> None:
        """Process Twilio <Start><Stream> WebSocket from the conference leg for STT.

        The stream is on the business call leg with track="both_tracks":
        - inbound = business mic (business speaking)
        - outbound = audio sent to business (user speaking via WebRTC)
        """
        self._conf_stream_ws = ws
        try:
            self._conf_stt_business = create_stt_backend()
            self._conf_stt_user = create_stt_backend()
            await self._conf_stt_business.start()
            await self._conf_stt_user.start()
            self._conf_stt_tasks = [
                asyncio.create_task(self._process_conf_stt_loop(self._conf_stt_business, "business")),
                asyncio.create_task(self._process_conf_stt_loop(self._conf_stt_user, "user")),
            ]
            logger.info(f"Conference STT stream started for {self.call_id} (2 tracks)")

            async for raw in ws.iter_text():
                msg = json.loads(raw)
                event = msg.get("event")

                if event == "media":
                    payload = msg["media"]["payload"]
                    track = msg["media"].get("track", "inbound")
                    mulaw_bytes = base64.b64decode(payload)
                    pcm16 = mulaw_to_pcm16_16k(mulaw_bytes)
                    # Route to the correct STT based on track
                    if track == "outbound":
                        await self._conf_stt_user.feed_audio(pcm16)
                    else:
                        await self._conf_stt_business.feed_audio(pcm16)

                elif event == "start":
                    logger.info(f"Conference stream started: {msg.get('start', {}).get('streamSid')}")

                elif event == "stop":
                    logger.info("Conference stream stopped")
                    break

        except Exception as e:
            logger.debug(f"Conference stream ended: {e}")
        finally:
            self._conf_stream_ws = None

    async def _process_conf_stt_loop(self, stt, speaker: str) -> None:
        """Poll a conference STT backend for transcripts during user takeover."""
        try:
            while self._user_takeover and self._call_active:
                transcript = await stt.get_transcript(timeout=0.2)
                if transcript is None:
                    continue
                self.phone_transcript.append({"role": speaker, "content": transcript})
                await self._send_to_chat({
                    "type": "call_transcript",
                    "speaker": speaker,
                    "content": transcript,
                })
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug(f"Conference STT loop ({speaker}) ended: {e}")

    async def end_call(self, summary: str | None = None) -> None:
        """Hang up the Twilio call and send a summary to the chat. Idempotent."""
        if self._call_ended:
            return
        self._call_ended = True
        self._call_active = False
        self._interrupted = True  # Stop any in-flight TTS
        await self._flush_twilio_audio()  # Clear buffered audio immediately

        # Cancel the respond loop (but not if we're being called from within it)
        if self._respond_task and self._respond_task is not asyncio.current_task():
            self._respond_task.cancel()
            self._respond_task = None

        # Clean up conference STT if takeover was active
        await self._cleanup_conf_stt()
        self._conf_room = None

        # Hang up via Twilio REST API (sync call — run in executor to avoid blocking)
        if self.call_sid:
            try:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(
                    None, lambda: self.twilio.calls(self.call_sid).update(status="completed")
                )
            except Exception as e:
                logger.debug(f"Error hanging up call: {e}")

        # Deduct minutes based on call duration
        duration_min = 0.0
        if self._call_start_time:
            duration_min = (time.monotonic() - self._call_start_time) / 60.0
            self._call_start_time = None

        if self.uid and duration_min > 0:
            balance = deduct_minutes(self.uid, duration_min)
            await self._send_to_chat({"type": "minutes_update", **balance})
            logger.info(f"Deducted {math.ceil(duration_min)} min from {self.uid}. Balance: {balance}")

        if not summary:
            summary = "Call ended."

        if duration_min > 0:
            summary += f"\n\n*Call duration: {math.ceil(duration_min)} min*"

        await self._send_to_chat({
            "type": "call_ended",
            "content": summary,
        })

        logger.info(f"Call ended: {self.call_id}")

    async def _send_to_chat(self, data: dict) -> None:
        """Send a message to the user's chat via the session WebSocket."""
        try:
            await self.session_ws.send_json(data)
        except Exception as e:
            logger.debug(f"Failed to send to chat: {e}")
