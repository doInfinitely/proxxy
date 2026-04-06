"""RemotePage — async Page-like interface that sends commands over WebSocket.

Mimics the subset of browser-use's Page API used by MobileAgent but dispatches
each call as a `browser_cmd` message to the iOS WKWebView client and awaits
the corresponding `browser_result`.
"""

import asyncio
import base64
import json
import logging
import uuid
from typing import Any

from fastapi import WebSocket

logger = logging.getLogger(__name__)

# How long to wait for a browser_result before giving up.
CMD_TIMEOUT = 30.0


class RemotePage:
    """A Page-like object backed by a remote iOS WKWebView over WebSocket."""

    def __init__(self, ws: WebSocket) -> None:
        self._ws = ws
        # Pending futures keyed by command id
        self._pending: dict[str, asyncio.Future] = {}

    # ------------------------------------------------------------------
    # Receiving results — called from the WebSocket message loop
    # ------------------------------------------------------------------

    def handle_browser_result(self, msg: dict) -> None:
        """Resolve a pending command future with the received result.

        Called by the main WebSocket handler when it receives a message
        with type ``browser_result``.
        """
        cmd_id = msg.get("id", "")
        fut = self._pending.pop(cmd_id, None)
        if fut and not fut.done():
            if msg.get("success"):
                fut.set_result(msg.get("data"))
            else:
                fut.set_exception(
                    RuntimeError(msg.get("error", "browser command failed"))
                )
        else:
            logger.warning("Unexpected browser_result id=%s (no pending future)", cmd_id)

    # ------------------------------------------------------------------
    # Sending commands
    # ------------------------------------------------------------------

    async def _send_cmd(self, cmd: str, **kwargs: Any) -> Any:
        """Send a ``browser_cmd`` and wait for the matching result."""
        cmd_id = f"cmd-{uuid.uuid4().hex[:8]}"
        payload: dict[str, Any] = {"type": "browser_cmd", "id": cmd_id, "cmd": cmd}
        payload.update(kwargs)

        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[cmd_id] = fut

        try:
            await self._ws.send_json(payload)
            return await asyncio.wait_for(fut, timeout=CMD_TIMEOUT)
        except asyncio.TimeoutError:
            self._pending.pop(cmd_id, None)
            raise TimeoutError(f"browser_cmd '{cmd}' (id={cmd_id}) timed out after {CMD_TIMEOUT}s")

    # ------------------------------------------------------------------
    # Page API surface (used by MobileAgent)
    # ------------------------------------------------------------------

    async def evaluate(self, js: str, *args: Any) -> Any:
        """Evaluate JavaScript in the remote WKWebView."""
        return await self._send_cmd("evaluate", js=js, args=list(args))

    async def goto(self, url: str) -> None:
        """Navigate the remote browser to *url*."""
        await self._send_cmd("navigate", url=url)

    async def screenshot(self, *, format: str = "jpeg", quality: int = 70) -> str:
        """Capture a screenshot and return it as a base64-encoded string."""
        data = await self._send_cmd("screenshot", format=format, quality=quality)
        return data or ""

    async def url(self) -> str:
        """Return the current page URL."""
        data = await self._send_cmd("get_url")
        return data or ""

    async def get_html(self, selector: str = "body") -> str:
        """Return the outerHTML of the first element matching *selector*."""
        data = await self._send_cmd("get_html", selector=selector)
        return data or ""

    async def current_url(self) -> str:
        return await self.url()

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cancel_all(self) -> None:
        """Cancel every pending command future (called on disconnect)."""
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()
