"""FastAPI backend for Proxxy with WebSocket streaming."""

from dotenv import load_dotenv
load_dotenv()

import asyncio
import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response, JSONResponse

from agent import BrowserAgent, StepUpdate, AgentMessage
from browser_use import Controller
from mobile_agent import MobileAgent
from remote_page import RemotePage
from call_service import CallService
from pydantic import BaseModel
from twilio.jwt.access_token import AccessToken
from twilio.jwt.access_token.grants import VoiceGrant
from call_billing import (
    verify_firebase_token,
    get_minutes_balance,
    create_checkout_session,
    handle_checkout_webhook,
    MINUTE_PACKS,
    FREE_MINUTES_PER_MONTH,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Proxxy")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"


@app.get("/health")
async def health():
    return JSONResponse({"status": "ok"})


# ─── Session registry ───


@dataclass
class SessionState:
    session_id: str
    owner_ws: WebSocket
    agent: BrowserAgent | MobileAgent
    active_call: CallService | None = None
    uid: str | None = None  # Firebase user ID (set via auth message)
    voice_id: str | None = None  # ElevenLabs voice ID (set via settings message)
    about_me: str = ""  # User's "about me" info for agent context
    is_mobile: bool = False  # True when client is iOS WKWebView
    remote_page: RemotePage | None = None  # Set for mobile sessions


sessions: dict[str, SessionState] = {}


def _create_call_controller(session: SessionState) -> Controller:
    """Create a browser-use Controller with a make_phone_call action."""
    controller = Controller()

    class PhoneCallParams(BaseModel):
        phone_number: str
        business_name: str

    @controller.action("Make a phone call to a business. Use this when the user asks you to call somewhere. First find the phone number by browsing, then call.", param_model=PhoneCallParams)
    async def make_phone_call(params: PhoneCallParams):
        ws = session.owner_ws

        # Check auth
        if not session.uid:
            return "Cannot make calls: user is not signed in."

        # Check minutes
        balance = get_minutes_balance(session.uid)
        if balance["total"] <= 0:
            return "Cannot make calls: no call minutes remaining."

        # Create call service
        call_svc = CallService(
            ws, session.agent._conversation,
            {"phone": params.phone_number, "name": params.business_name},
            uid=session.uid,
            voice_id=session.voice_id,
            about_me=session.about_me,
        )
        session.active_call = call_svc

        await ws.send_json({"type": "minutes_update", **balance})
        await ws.send_json({
            "type": "call_status",
            "status": "ringing",
            "business": params.business_name,
            "phone": params.phone_number,
        })

        # Start the call and wait for it to complete
        try:
            await call_svc.start_call(params.phone_number)

            # Wait for call to end (poll call_active flag)
            while call_svc._call_active and not call_svc._call_ended:
                await asyncio.sleep(1)

            # Collect transcript
            transcript_lines = []
            for entry in call_svc.phone_transcript:
                speaker = "Business" if entry["role"] == "business" else "Agent"
                transcript_lines.append(f"{speaker}: {entry['content']}")

            _inject_call_transcript(session)
            session.active_call = None

            if transcript_lines:
                return f"Call completed. Transcript:\n" + "\n".join(transcript_lines)
            else:
                return "Call completed but no transcript was captured."

        except Exception as e:
            logger.error(f"Phone call error: {e}", exc_info=True)
            session.active_call = None
            return f"Call failed: {e}"

    return controller


def _register_phone_call_tool(agent: MobileAgent, session: SessionState) -> None:
    """Register the make_phone_call tool on a MobileAgent."""

    async def handle_phone_call(params: dict) -> str:
        ws = session.owner_ws
        phone_number = params.get("phone_number", "")
        business_name = params.get("business_name", "")

        if not session.uid:
            return "Cannot make calls: user is not signed in."

        balance = get_minutes_balance(session.uid)
        if balance["total"] <= 0:
            return "Cannot make calls: no call minutes remaining."

        call_svc = CallService(
            ws, session.agent._conversation,
            {"phone": phone_number, "name": business_name},
            uid=session.uid,
            voice_id=session.voice_id,
            about_me=session.about_me,
        )
        session.active_call = call_svc

        await ws.send_json({"type": "minutes_update", **balance})
        await ws.send_json({
            "type": "call_status",
            "status": "ringing",
            "business": business_name,
            "phone": phone_number,
        })

        try:
            await call_svc.start_call(phone_number)

            while call_svc._call_active and not call_svc._call_ended:
                await asyncio.sleep(1)

            transcript_lines = []
            for entry in call_svc.phone_transcript:
                speaker = "Business" if entry["role"] == "business" else "Agent"
                transcript_lines.append(f"{speaker}: {entry['content']}")

            _inject_call_transcript(session)
            session.active_call = None

            if transcript_lines:
                return f"Call completed. Transcript:\n" + "\n".join(transcript_lines)
            else:
                return "Call completed but no transcript was captured."

        except Exception as e:
            logger.error(f"Phone call error: {e}", exc_info=True)
            session.active_call = None
            return f"Call failed: {e}"

    agent.register_tool(
        name="make_phone_call",
        description=(
            "Make a phone call to a business. Use this when the user asks you to "
            "call somewhere. First find the phone number by browsing, then call."
        ),
        parameters={
            "type": "object",
            "properties": {
                "phone_number": {"type": "string", "description": "The phone number to call"},
                "business_name": {"type": "string", "description": "The name of the business"},
            },
            "required": ["phone_number", "business_name"],
        },
        handler=handle_phone_call,
    )


def _inject_call_transcript(session: SessionState) -> None:
    """Append the phone call transcript into the agent's conversation memory."""
    call = session.active_call
    if not call or not call.phone_transcript:
        return
    lines = []
    for entry in call.phone_transcript:
        speaker = "Business" if entry["role"] == "business" else "Agent (you, on the phone)"
        lines.append(f"{speaker}: {entry['content']}")
    business_name = call.business.get("name", "the business")
    summary = (
        f"[Phone call with {business_name} ended. Transcript:]\n"
        + "\n".join(lines)
    )
    session.agent._conversation.append({"role": "assistant", "content": summary})


async def _send_update(ws: WebSocket, update: StepUpdate) -> None:
    """Send a StepUpdate's data to the WebSocket client."""
    if update.screenshot:
        await ws.send_json({"type": "screenshot", "data": update.screenshot})
    if update.tabs is not None:
        await ws.send_json({"type": "tabs", "tabs": update.tabs})
    if update.url:
        await ws.send_json({"type": "url_update", "url": update.url})
    for msg in update.messages:
        await ws.send_json({"type": msg.role, "content": msg.content})
    if update.done:
        await ws.send_json({
            "type": "done",
            "content": update.final_result or "Task completed.",
        })
        await ws.send_json({"type": "status", "status": "idle"})


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    logger.info("Client connected")

    agent: BrowserAgent | MobileAgent = BrowserAgent()
    agent_task: asyncio.Task | None = None
    stream_task: asyncio.Task | None = None
    poll_task: asyncio.Task | None = None
    remote_page: RemotePage | None = None
    is_mobile = False

    session_id = uuid.uuid4().hex[:8]
    session = SessionState(
        session_id=session_id, owner_ws=ws, agent=agent,
    )
    sessions[session_id] = session

    # Give the agent a controller with phone call capability (web clients)
    agent.controller = _create_call_controller(session)

    try:
        await ws.send_json({"type": "session_id", "session_id": session_id})
        poll_task = asyncio.create_task(_poll_screenshots(ws, agent))

        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)

            # ── iOS hello handshake — upgrade to MobileAgent ──
            if msg.get("type") == "hello" and msg.get("client") == "ios":
                logger.info("iOS client connected (v%s)", msg.get("version", "?"))
                remote_page = RemotePage(ws)
                mobile_agent = MobileAgent(remote_page, about_me=session.about_me)
                # Register phone call tool on mobile agent
                _register_phone_call_tool(mobile_agent, session)
                # Replace the agent in the session
                session.agent = mobile_agent
                session.is_mobile = True
                session.remote_page = remote_page
                agent = mobile_agent
                is_mobile = True
                # Cancel screenshot polling — not needed for mobile
                if poll_task:
                    poll_task.cancel()
                    poll_task = None
                await ws.send_json({"type": "hello_ack", "status": "ok"})
                continue

            # ── iOS browser_result — forward to RemotePage ──
            if msg.get("type") == "browser_result" and remote_page:
                remote_page.handle_browser_result(msg)
                continue

            if msg.get("type") == "message":
                user_text = msg["content"]
                logger.info(f"User: {user_text}")

                # During active call, route messages to call service
                if session.active_call:
                    await session.active_call.receive_user_response(user_text)
                    continue

                if stream_task and not stream_task.done():
                    stream_task.cancel()
                if agent.is_running:
                    await agent.stop_task()
                    if agent_task and not agent_task.done():
                        agent_task.cancel()

                await ws.send_json(
                    {"type": "status", "status": "thinking"}
                )

                agent_task = asyncio.create_task(agent.run_task(user_text))
                stream_task = asyncio.create_task(
                    _stream_updates(ws, agent, agent_task)
                )

            elif msg.get("type") == "browser_action" and not is_mobile:
                logger.info(f"Browser action: {msg.get('action')} at ({msg.get('x')},{msg.get('y')})")
                try:
                    await agent.execute_browser_action(msg)
                except Exception as e:
                    logger.error(f"Browser action error: {e}", exc_info=True)
                if msg.get("action") == "copy" and getattr(agent, "_last_copy", ""):
                    await ws.send_json({"type": "clipboard", "text": agent._last_copy})
                    agent._last_copy = ""
                while not agent._step_queue.empty():
                    update = await agent.get_update(timeout=1.0)
                    if update:
                        await _send_update(ws, update)

            elif msg.get("type") == "cursor_query" and not is_mobile:
                cursor, b64 = await agent.handle_mouse_move(
                    msg.get("x", 0), msg.get("y", 0)
                )
                await ws.send_json({"type": "cursor", "cursor": cursor})
                if b64:
                    await ws.send_json({"type": "screenshot", "data": b64})

            elif msg.get("type") == "switch_tab" and not is_mobile:
                target_id = msg.get("target_id", "")
                if target_id:
                    await agent.switch_tab(target_id)
                    while not agent._step_queue.empty():
                        update = await agent.get_update(timeout=1.0)
                        if update:
                            await _send_update(ws, update)

            elif msg.get("type") == "close_tab" and not is_mobile:
                target_id = msg.get("target_id", "")
                if target_id:
                    await agent.close_tab(target_id)
                    while not agent._step_queue.empty():
                        update = await agent.get_update(timeout=1.0)
                        if update:
                            await _send_update(ws, update)

            elif msg.get("type") == "stop":
                if stream_task and not stream_task.done():
                    stream_task.cancel()
                if agent.is_running:
                    await agent.stop_task()
                    if agent_task and not agent_task.done():
                        agent_task.cancel()

                await agent.take_screenshot()
                while not agent._step_queue.empty():
                    update = await agent.get_update(timeout=1.0)
                    if update:
                        await _send_update(ws, update)

                await ws.send_json(
                    {"type": "status", "status": "idle"}
                )

            elif msg.get("type") == "auth":
                token = msg.get("token", "")
                uid = verify_firebase_token(token)
                if uid:
                    session.uid = uid
                    await ws.send_json({"type": "auth_ok", "uid": uid})
                else:
                    await ws.send_json({"type": "auth_error", "content": "Invalid token"})

            elif msg.get("type") == "settings":
                if "voice_id" in msg:
                    session.voice_id = msg["voice_id"]
                if "about_me" in msg:
                    session.about_me = msg["about_me"]
                    session.agent.about_me = msg["about_me"]

            elif msg.get("type") == "start_call":
                if not session.uid:
                    await ws.send_json({"type": "call_ended", "content": "Sign in required to make calls."})
                    continue

                balance = get_minutes_balance(session.uid)
                if balance["total"] <= 0:
                    await ws.send_json({
                        "type": "call_ended",
                        "content": "No call minutes remaining. Purchase more minutes to continue.",
                    })
                    await ws.send_json({"type": "minutes_update", **balance})
                    continue

                business = msg.get("business", {})
                voice_id = msg.get("voice_id") or session.voice_id
                call_svc = CallService(ws, agent._conversation, business, uid=session.uid, voice_id=voice_id, about_me=session.about_me)
                session.active_call = call_svc
                await ws.send_json({"type": "minutes_update", **balance})
                await call_svc.start_call(business.get("phone", ""))

            elif msg.get("type") == "call_response":
                if session.active_call:
                    await session.active_call.receive_user_response(msg.get("content", ""))

            elif msg.get("type") == "end_call":
                if session.active_call:
                    await session.active_call.end_call()
                    try:
                        _inject_call_transcript(session)
                    except Exception as e:
                        logger.warning(f"Failed to inject call transcript: {e}")
                    session.active_call = None

            elif msg.get("type") == "call_takeover":
                if session.active_call:
                    await session.active_call.set_takeover(True)

            elif msg.get("type") == "call_handback":
                if session.active_call:
                    await session.active_call.set_takeover(False)

    except WebSocketDisconnect:
        logger.info("Client disconnected")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        if session.active_call:
            try:
                await session.active_call.end_call()
            except Exception:
                pass
            try:
                _inject_call_transcript(session)
            except Exception:
                pass
            session.active_call = None

        sessions.pop(session_id, None)

        if stream_task and not stream_task.done():
            stream_task.cancel()
        if agent_task and not agent_task.done():
            agent_task.cancel()
        if poll_task:
            poll_task.cancel()
        if remote_page:
            remote_page.cancel_all()
        await agent.shutdown()
        logger.info("Session cleaned up — %s", "mobile disconnected" if is_mobile else "browser closed")


@app.websocket("/ws/mobile")
async def mobile_websocket_endpoint(ws: WebSocket) -> None:
    """Dedicated WebSocket endpoint for iOS/mobile clients — uses MobileAgent directly."""
    await ws.accept()
    logger.info("Mobile client connected")

    remote_page = RemotePage(ws)
    agent = MobileAgent(remote_page)
    agent_task: asyncio.Task | None = None
    stream_task: asyncio.Task | None = None

    session_id = uuid.uuid4().hex[:8]
    session = SessionState(
        session_id=session_id, owner_ws=ws, agent=agent,
        is_mobile=True, remote_page=remote_page,
    )
    sessions[session_id] = session
    _register_phone_call_tool(agent, session)

    try:
        await ws.send_json({"type": "session_id", "session_id": session_id})
        await ws.send_json({"type": "hello_ack", "status": "ok"})

        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)

            # Forward browser results to RemotePage
            if msg.get("type") == "browser_result":
                remote_page.handle_browser_result(msg)
                continue

            if msg.get("type") == "message":
                user_text = msg["content"]
                logger.info(f"[mobile] User: {user_text}")

                if session.active_call:
                    await session.active_call.receive_user_response(user_text)
                    continue

                if stream_task and not stream_task.done():
                    stream_task.cancel()
                if agent.is_running:
                    await agent.stop_task()
                    if agent_task and not agent_task.done():
                        agent_task.cancel()

                await ws.send_json({"type": "status", "status": "thinking"})

                agent_task = asyncio.create_task(agent.run_task(user_text))
                stream_task = asyncio.create_task(
                    _stream_updates(ws, agent, agent_task)
                )

            elif msg.get("type") == "stop":
                if stream_task and not stream_task.done():
                    stream_task.cancel()
                if agent.is_running:
                    await agent.stop_task()
                    if agent_task and not agent_task.done():
                        agent_task.cancel()
                await ws.send_json({"type": "status", "status": "idle"})

            elif msg.get("type") == "auth":
                token = msg.get("token", "")
                uid = verify_firebase_token(token)
                if uid:
                    session.uid = uid
                    await ws.send_json({"type": "auth_ok", "uid": uid})
                else:
                    await ws.send_json({"type": "auth_error", "content": "Invalid token"})

            elif msg.get("type") == "settings":
                if "voice_id" in msg:
                    session.voice_id = msg["voice_id"]
                if "about_me" in msg:
                    session.about_me = msg["about_me"]
                    agent.about_me = msg["about_me"]

            elif msg.get("type") == "start_call":
                if not session.uid:
                    await ws.send_json({"type": "call_ended", "content": "Sign in required to make calls."})
                    continue
                balance = get_minutes_balance(session.uid)
                if balance["total"] <= 0:
                    await ws.send_json({"type": "call_ended", "content": "No call minutes remaining."})
                    await ws.send_json({"type": "minutes_update", **balance})
                    continue
                business = msg.get("business", {})
                voice_id = msg.get("voice_id") or session.voice_id
                call_svc = CallService(ws, agent._conversation, business, uid=session.uid, voice_id=voice_id, about_me=session.about_me)
                session.active_call = call_svc
                await ws.send_json({"type": "minutes_update", **balance})
                await call_svc.start_call(business.get("phone", ""))

            elif msg.get("type") == "call_response":
                if session.active_call:
                    await session.active_call.receive_user_response(msg.get("content", ""))

            elif msg.get("type") == "end_call":
                if session.active_call:
                    await session.active_call.end_call()
                    try:
                        _inject_call_transcript(session)
                    except Exception as e:
                        logger.warning(f"Failed to inject call transcript: {e}")
                    session.active_call = None

            elif msg.get("type") == "call_takeover":
                if session.active_call:
                    await session.active_call.set_takeover(True)

            elif msg.get("type") == "call_handback":
                if session.active_call:
                    await session.active_call.set_takeover(False)

    except WebSocketDisconnect:
        logger.info("Mobile client disconnected")
    except Exception as e:
        logger.error(f"Mobile WebSocket error: {e}", exc_info=True)
    finally:
        if session.active_call:
            try:
                await session.active_call.end_call()
            except Exception:
                pass
            try:
                _inject_call_transcript(session)
            except Exception:
                pass
            session.active_call = None

        sessions.pop(session_id, None)

        if stream_task and not stream_task.done():
            stream_task.cancel()
        if agent_task and not agent_task.done():
            agent_task.cancel()
        remote_page.cancel_all()
        await agent.shutdown()
        logger.info("Mobile session cleaned up")


async def _poll_screenshots(ws: WebSocket, agent: BrowserAgent) -> None:
    """Poll browser screenshots when agent is idle so the UI stays in sync."""
    try:
        while True:
            await asyncio.sleep(2)
            if not agent.is_running and agent.browser is not None:
                b64, url, _ = await agent._get_browser_snapshot()
                tabs = await agent._get_tabs()
                if b64:
                    await ws.send_json({"type": "screenshot", "data": b64})
                    if tabs:
                        await ws.send_json({"type": "tabs", "tabs": tabs})
                    if url:
                        await ws.send_json(
                            {"type": "url_update", "url": url}
                        )
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.debug(f"Poll stopped: {e}")


async def _stream_updates(
    ws: WebSocket, agent: BrowserAgent | MobileAgent, task: asyncio.Task
) -> None:
    """Stream agent step updates to the WebSocket client."""
    try:
        while not task.done():
            update = await agent.get_update(timeout=5.0)
            if update is None:
                continue
            await _send_update(ws, update)
            if update.done:
                return

        while not agent._step_queue.empty():
            update = await agent.get_update(timeout=1.0)
            if update:
                await _send_update(ws, update)

        await ws.send_json({"type": "status", "status": "idle"})

    except Exception as e:
        logger.error(f"Stream error: {e}")
        try:
            await ws.send_json({"type": "status", "status": "idle"})
        except Exception:
            pass


# ─── Phone call endpoints ───


@app.api_route("/api/calls/twiml/{call_id}", methods=["GET", "POST"])
async def call_twiml(call_id: str):
    """Return TwiML that connects the call to our media stream WebSocket."""
    host = os.environ.get("PUBLIC_DOMAIN", os.environ.get("RAILWAY_PUBLIC_DOMAIN", "localhost:8080"))
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Connect>
        <Stream url="wss://{host}/ws/media/{call_id}" />
    </Connect>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


@app.websocket("/ws/media/{call_id}")
async def media_stream(ws: WebSocket, call_id: str):
    """Twilio media stream WebSocket — receives/sends call audio."""
    await ws.accept()

    # Find the session that owns this call
    session = None
    for s in sessions.values():
        if s.active_call and s.active_call.call_id == call_id:
            session = s
            break

    if not session or not session.active_call:
        logger.warning(f"No session found for call {call_id}")
        await ws.close(code=4004, reason="No active call")
        return

    logger.info(f"Media stream connected for call {call_id}")
    try:
        await session.active_call.handle_media_stream(ws)
    except Exception as e:
        logger.error(f"Media stream error for call {call_id}: {e}", exc_info=True)
    finally:
        logger.info(f"Media stream closed for call {call_id}")


@app.post("/api/calls/status/{call_id}")
async def call_status_webhook(call_id: str, request: Request):
    """Twilio status callback — track call state changes."""
    form = await request.form()
    status = form.get("CallStatus")
    logger.info(f"Call {call_id} status: {status}")

    # Forward call status to the client
    for s in sessions.values():
        if s.active_call and s.active_call.call_id == call_id:
            try:
                await s.owner_ws.send_json({
                    "type": "call_status",
                    "status": status,
                    "call_id": call_id,
                })
            except Exception:
                pass
            break

    if status in ("completed", "failed", "busy", "no-answer", "canceled"):
        # Find session and clean up
        for s in sessions.values():
            if s.active_call and s.active_call.call_id == call_id:
                summary = None if status == "completed" else f"Call {status}."
                await s.active_call.end_call(summary)
                try:
                    _inject_call_transcript(s)
                except Exception:
                    pass
                s.active_call = None
                break

    return JSONResponse({"ok": True})


# ─── Conference bridge endpoints (for user takeover) ───


@app.get("/api/voice-token")
async def voice_token(request: Request):
    """Generate a Twilio Access Token with VoiceGrant for the Client SDK."""
    identity = None

    # Try Firebase auth first
    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if token:
        identity = verify_firebase_token(token)

    # Fall back to session ID
    if not identity:
        session_id = request.query_params.get("session")
        if session_id and session_id in sessions:
            identity = f"session-{session_id}"

    if not identity:
        return JSONResponse({"error": "auth or session required"}, status_code=401)

    access_token = AccessToken(
        os.environ["TWILIO_ACCOUNT_SID"],
        os.environ["TWILIO_API_KEY_SID"],
        os.environ["TWILIO_API_KEY_SECRET"],
        identity=identity,
    )
    voice_grant = VoiceGrant(
        outgoing_application_sid=os.environ["TWILIO_TWIML_APP_SID"],
    )
    access_token.add_grant(voice_grant)

    return JSONResponse({"token": access_token.to_jwt()})


@app.api_route("/api/calls/conf-twiml/{call_id}", methods=["GET", "POST"])
async def conf_twiml(call_id: str):
    """TwiML: business joins conference room with a <Start><Stream> for STT."""
    host = os.environ.get("PUBLIC_DOMAIN", os.environ.get("RAILWAY_PUBLIC_DOMAIN", "localhost:8080"))
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Start>
        <Stream url="wss://{host}/ws/conf-stream/{call_id}" track="both_tracks"/>
    </Start>
    <Dial>
        <Conference beep="false" startConferenceOnEnter="true"
                    endConferenceOnExit="false">room-{call_id}</Conference>
    </Dial>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


@app.api_route("/api/calls/conf-browser-twiml", methods=["GET", "POST"])
async def conf_browser_twiml(request: Request):
    """TwiML App voice URL: browser client joins the conference room."""
    form = await request.form()
    room = form.get("room", "default")
    logger.info(f"Browser joining conference room: {room}")
    twiml = f"""<?xml version="1.0" encoding="UTF-8"?>
<Response>
    <Dial>
        <Conference beep="false">{room}</Conference>
    </Dial>
</Response>"""
    return Response(content=twiml, media_type="application/xml")


@app.websocket("/ws/conf-stream/{call_id}")
async def conf_stream(ws: WebSocket, call_id: str):
    """Twilio <Start><Stream> WebSocket from conference leg — feeds STT."""
    await ws.accept()

    # Find the session that owns this call
    session = None
    for s in sessions.values():
        if s.active_call and s.active_call.call_id == call_id:
            session = s
            break

    if not session or not session.active_call:
        logger.warning(f"No session found for conf-stream {call_id}")
        await ws.close(code=4004, reason="No active call")
        return

    logger.info(f"Conference stream connected for call {call_id}")
    try:
        await session.active_call.handle_conference_stream(ws)
    except Exception as e:
        logger.error(f"Conference stream error for call {call_id}: {e}", exc_info=True)
    finally:
        logger.info(f"Conference stream closed for call {call_id}")


# ─── Call billing endpoints ───


@app.get("/api/call-minutes")
async def get_call_minutes(request: Request):
    """Get the user's call minute balance. Requires Firebase ID token."""
    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if not token:
        return JSONResponse({"error": "auth required"}, status_code=401)

    uid = verify_firebase_token(token)
    if not uid:
        return JSONResponse({"error": "invalid token"}, status_code=401)

    balance = get_minutes_balance(uid)
    return JSONResponse({
        **balance,
        "free_per_month": FREE_MINUTES_PER_MONTH,
        "packs": {k: {"minutes": v["minutes"], "price_cents": v["price_cents"],
                       "label": v["label"]} for k, v in MINUTE_PACKS.items()},
    })


@app.post("/api/call-minutes/checkout")
async def create_call_minutes_checkout(request: Request):
    """Create a Stripe Checkout Session for purchasing a minute pack."""
    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    if not token:
        return JSONResponse({"error": "auth required"}, status_code=401)

    uid = verify_firebase_token(token)
    if not uid:
        return JSONResponse({"error": "invalid token"}, status_code=401)

    body = await request.json()
    pack_id = body.get("pack_id")
    if pack_id not in MINUTE_PACKS:
        return JSONResponse({"error": "invalid pack_id"}, status_code=400)

    return_url = body.get("return_url", request.headers.get("origin", "http://localhost:8000"))
    checkout_url = create_checkout_session(uid, pack_id, return_url)
    if not checkout_url:
        return JSONResponse({"error": "checkout failed"}, status_code=500)

    return JSONResponse({"url": checkout_url})


@app.post("/api/call-minutes/webhook")
async def stripe_webhook(request: Request):
    """Stripe webhook — credits minutes after successful payment."""
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")

    success = handle_checkout_webhook(payload, sig)
    if not success:
        return JSONResponse({"error": "webhook failed"}, status_code=400)

    return JSONResponse({"ok": True})


# ─── Voice & TTS endpoints ───

_voice_cache: dict = {"voices": None, "fetched_at": 0}


@app.get("/api/voices")
async def list_voices():
    """Return available ElevenLabs voices (cached for 1 hour)."""
    import time
    import httpx

    now = time.time()
    if _voice_cache["voices"] and now - _voice_cache["fetched_at"] < 3600:
        return JSONResponse({"voices": _voice_cache["voices"]})

    api_key = os.environ.get("ELEVENLABS_API_KEY", "")
    if not api_key:
        return JSONResponse({"voices": []})

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                "https://api.elevenlabs.io/v1/voices",
                headers={"xi-api-key": api_key},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
            voices = [
                {"voice_id": v["voice_id"], "name": v["name"]}
                for v in data.get("voices", [])
            ]
            _voice_cache["voices"] = voices
            _voice_cache["fetched_at"] = now
            return JSONResponse({"voices": voices})
    except Exception as e:
        logger.error(f"Failed to fetch voices: {e}")
        return JSONResponse({"voices": []})


@app.post("/api/tts")
async def text_to_speech(request: Request):
    """Stream ElevenLabs TTS audio as MP3."""
    import httpx

    body = await request.json()
    text = body.get("text", "")
    voice_id = body.get("voice_id", "iP95p4xoKVk53GoZ742B")

    if not text:
        return JSONResponse({"error": "text required"}, status_code=400)

    api_key = os.environ.get("ELEVENLABS_API_KEY", "")
    if not api_key:
        return JSONResponse({"error": "ElevenLabs not configured"}, status_code=500)

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                f"https://api.elevenlabs.io/v1/text-to-speech/{voice_id}",
                headers={
                    "xi-api-key": api_key,
                    "Content-Type": "application/json",
                    "Accept": "audio/mpeg",
                },
                json={
                    "text": text,
                    "model_id": "eleven_turbo_v2_5",
                    "voice_settings": {"stability": 0.5, "similarity_boost": 0.75},
                },
                timeout=30,
            )
            resp.raise_for_status()
            return Response(content=resp.content, media_type="audio/mpeg")
    except Exception as e:
        logger.error(f"TTS error: {e}")
        return JSONResponse({"error": "TTS failed"}, status_code=500)


# ─── Template endpoints ───


@app.post("/api/templates/generate-questions")
async def generate_template_questions(request: Request):
    """Use LLM to generate natural questions for template parameters."""
    import openai

    body = await request.json()
    prompt_text = body.get("prompt", "")
    parameters = body.get("parameters", [])

    if not prompt_text or not parameters:
        return JSONResponse({"error": "prompt and parameters required"}, status_code=400)

    client = openai.AsyncOpenAI()
    system = (
        "You generate friendly, natural questions for a form. "
        "Given a template prompt with {parameter} placeholders, generate one clear question "
        "for each parameter. Return ONLY a JSON array of strings."
    )
    user_msg = (
        f"Template: {prompt_text}\n"
        f"Parameters: {', '.join(parameters)}\n"
        f"Generate one natural question per parameter."
    )

    try:
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.3,
        )
        content = resp.choices[0].message.content.strip()
        # Parse JSON array from response
        if content.startswith("```"):
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
        questions = json.loads(content)
        return JSONResponse({"questions": questions})
    except Exception as e:
        logger.error(f"Question generation error: {e}")
        # Fallback: generate simple questions
        questions = [f"What is the {p}?" for p in parameters]
        return JSONResponse({"questions": questions})


@app.post("/api/templates/extract-params")
async def extract_template_params(request: Request):
    """Use LLM to extract parameter values from natural language input."""
    import openai

    body = await request.json()
    prompt_text = body.get("prompt", "")
    parameters = body.get("parameters", [])
    user_input = body.get("input", "")

    if not parameters or not user_input:
        return JSONResponse({"error": "parameters and input required"}, status_code=400)

    client = openai.AsyncOpenAI()
    system = (
        "Extract parameter values from user input. "
        "Return ONLY a JSON object mapping parameter names to extracted values."
    )
    user_msg = (
        f"Template: {prompt_text}\n"
        f"Parameters: {', '.join(parameters)}\n"
        f"User input: {user_input}\n"
        f"Extract the value for each parameter from the user's input."
    )

    try:
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.1,
        )
        content = resp.choices[0].message.content.strip()
        if content.startswith("```"):
            content = content.split("```")[1]
            if content.startswith("json"):
                content = content[4:]
        params = json.loads(content)
        return JSONResponse({"params": params})
    except Exception as e:
        logger.error(f"Param extraction error: {e}")
        return JSONResponse({"params": {}})


# ─── Session endpoints ───


@app.get("/api/sessions")
async def list_sessions(request: Request):
    """List active sessions (for authenticated users)."""
    token = request.headers.get("Authorization", "").removeprefix("Bearer ").strip()
    uid = verify_firebase_token(token) if token else None

    result = []
    for sid, s in sessions.items():
        if uid and s.uid != uid:
            continue
        result.append({
            "session_id": sid,
            "message_count": len(s.agent._conversation) if hasattr(s.agent, '_conversation') else 0,
            "has_call": s.active_call is not None,
        })
    return JSONResponse(result)


@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str):
    """Return session conversation history."""
    session = sessions.get(session_id)
    if not session:
        return JSONResponse({"error": "session not found"}, status_code=404)

    conversation = []
    if hasattr(session.agent, '_conversation'):
        for msg in session.agent._conversation:
            conversation.append({
                "role": msg.get("role", ""),
                "content": msg.get("content", ""),
            })

    return JSONResponse({
        "session_id": session_id,
        "conversation": conversation,
    })


# ─── Static file serving ───


if FRONTEND_DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="assets")

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        file = FRONTEND_DIST / full_path
        if file.is_file():
            return FileResponse(file)
        return FileResponse(FRONTEND_DIST / "index.html")


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
