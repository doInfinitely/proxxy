"""FastAPI backend for browser agent with WebSocket streaming."""

import asyncio
import json
import logging
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from agent import BrowserAgent, StepUpdate

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Browser Agent")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

FRONTEND_DIST = Path(__file__).resolve().parent.parent / "frontend" / "dist"


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

    agent = BrowserAgent()
    agent_task: asyncio.Task | None = None
    stream_task: asyncio.Task | None = None
    poll_task: asyncio.Task | None = None

    try:
        poll_task = asyncio.create_task(_poll_screenshots(ws, agent))

        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)

            if msg.get("type") == "message":
                user_text = msg["content"]
                logger.info(f"User: {user_text}")

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

            elif msg.get("type") == "browser_action":
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

            elif msg.get("type") == "cursor_query":
                cursor, b64 = await agent.handle_mouse_move(
                    msg.get("x", 0), msg.get("y", 0)
                )
                await ws.send_json({"type": "cursor", "cursor": cursor})
                if b64:
                    await ws.send_json({"type": "screenshot", "data": b64})

            elif msg.get("type") == "switch_tab":
                target_id = msg.get("target_id", "")
                if target_id:
                    await agent.switch_tab(target_id)
                    while not agent._step_queue.empty():
                        update = await agent.get_update(timeout=1.0)
                        if update:
                            await _send_update(ws, update)

            elif msg.get("type") == "close_tab":
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

    except WebSocketDisconnect:
        logger.info("Client disconnected")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        if stream_task and not stream_task.done():
            stream_task.cancel()
        if agent_task and not agent_task.done():
            agent_task.cancel()
        if poll_task:
            poll_task.cancel()
        await agent.shutdown()
        logger.info("Session cleaned up — browser closed")


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
    ws: WebSocket, agent: BrowserAgent, task: asyncio.Task
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
    import os

    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
