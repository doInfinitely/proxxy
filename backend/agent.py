"""General-purpose browser agent — browser-use Agent for any web task."""

import asyncio
import base64
import logging
import os
from datetime import datetime, timezone
from dataclasses import dataclass, field

from browser_use import Agent, Browser, Controller
from browser_use.browser.profile import ProxySettings

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """\
You are Proxxy, a helpful AI assistant that uses the browser to complete tasks \
for the user. You can search the web, navigate websites, fill out forms, \
extract information, and perform any browser-based task.

You can also make phone calls using the make_phone_call action. \
When the user asks you to call someone, use make_phone_call with the phone \
number and the name of the person or business. If the user doesn't provide a \
number, try to find it by browsing the web. The call will be handled by an AI \
voice agent. The call result and transcript will be returned to you when the \
call ends.

The current date and time is: {current_datetime}

IMPORTANT RULES:
1. If a website requires login or authentication, STOP immediately and tell \
the user: "This site requires you to log in. Please complete the \
authentication in your browser, then let me know when you're done."
2. Never enter passwords, payment details, or other sensitive information.
3. Be thorough in your work — verify results before reporting them.
4. Always summarize your findings clearly at the end.\
"""


@dataclass
class AgentMessage:
    role: str  # "assistant", "status", "action", "auth_required"
    content: str


@dataclass
class StepUpdate:
    screenshot: str | None = None  # base64 JPEG
    url: str | None = None
    tabs: list[dict] | None = None
    messages: list[AgentMessage] = field(default_factory=list)
    done: bool = False
    final_result: str | None = None


class BrowserAgent:
    """Wraps browser-use Agent with a persistent browser session across messages."""

    def __init__(self) -> None:
        self.browser: Browser | None = None
        self._step_queue: asyncio.Queue[StepUpdate] = asyncio.Queue()
        self._running = False
        self._current_url: str = ""
        self._step_count: int = 0
        self._conversation: list[dict[str, str]] = []  # {role, content}
        self._agent: Agent | None = None
        self.controller: Controller | None = None  # set externally with custom actions
        self.about_me: str = ""  # user's about me info

    async def _ensure_browser(self) -> Browser:
        """Create browser if not already open.

        Supports three modes via environment variables:
          - USE_CLOUD_BROWSER=true  → Browser Use Cloud (different IPs via proxy)
            Optional: CLOUD_PROXY_COUNTRY=us (us,uk,fr,it,jp,au,de,fi,ca,in)
          - CDP_URL=wss://...       → Any remote CDP browser (Browserless, Steel, etc.)
          - (default)               → Local headless Chromium
        """
        if self.browser is None:
            cdp_url = os.environ.get("CDP_URL", "").strip()
            use_cloud = os.environ.get("USE_CLOUD_BROWSER", "").strip().lower() in ("true", "1", "yes")

            if use_cloud:
                proxy_country = os.environ.get("CLOUD_PROXY_COUNTRY", "").strip() or None
                logger.info(f"Using Browser Use Cloud (proxy={proxy_country or 'default'})")
                self.browser = Browser(
                    use_cloud=True,
                    cloud_proxy_country_code=proxy_country,
                    keep_alive=True,
                )
            elif cdp_url:
                logger.info(f"Connecting to remote browser: {cdp_url[:60]}...")
                self.browser = Browser(
                    cdp_url=cdp_url,
                    keep_alive=True,
                )
            else:
                headless = os.environ.get("HEADLESS", "false").strip().lower() in ("true", "1", "yes")
                kwargs: dict = dict(
                    headless=headless,
                    keep_alive=True,
                    chromium_sandbox=False,
                    args=[
                        "--disable-dev-shm-usage",
                        "--disable-gpu",
                        "--no-zygote",
                        "--single-process",
                    ],
                )
                # ScraperAPI residential proxy (disable with DISABLE_SCRAPER_PROXY=1)
                scraper_key = os.environ.get("SCRAPERAPI_KEY")
                proxy_disabled = os.environ.get("DISABLE_SCRAPER_PROXY", "").strip() in ("1", "true", "yes")
                if scraper_key and not proxy_disabled:
                    logger.info("ScraperAPI proxy enabled (key length=%d)", len(scraper_key))
                    kwargs["proxy"] = ProxySettings(
                        server="http://proxy-server.scraperapi.com:8001",
                        username="scraperapi.ultra_premium=true",
                        password=scraper_key,
                    )
                    kwargs["disable_security"] = True  # proxy does SSL interception
                else:
                    logger.info("ScraperAPI proxy not active (key=%s, disabled=%s)",
                                "set" if scraper_key else "missing", proxy_disabled)
                self.browser = Browser(**kwargs)
        return self.browser

    def _build_task_with_context(self, user_message: str) -> str:
        """Build the agent task string including conversation history."""
        self._conversation.append({"role": "user", "content": user_message})

        if len(self._conversation) <= 1:
            return user_message

        parts = ["CONVERSATION HISTORY:"]
        for msg in self._conversation[:-1]:
            prefix = "User" if msg["role"] == "user" else "Assistant"
            parts.append(f"{prefix}: {msg['content']}")
        parts.append("")
        parts.append(f"CURRENT REQUEST: {user_message}")
        parts.append("")
        parts.append(
            "Continue from the current browser state. "
            "The browser is already open — do NOT navigate away unless needed."
        )
        return "\n".join(parts)

    def _system_prompt(self) -> str:
        now = datetime.now(timezone.utc).strftime("%A, %B %d, %Y at %H:%M UTC")
        prompt = SYSTEM_PROMPT.format(current_datetime=now)
        if self.about_me:
            prompt += f"\n\nAbout the user:\n{self.about_me}"
        return prompt

    async def run_task(self, user_message: str) -> None:
        """Run a task using the persistent browser session, with retry on timeout."""
        self._running = True
        self._step_count = 0
        max_retries = 3

        task = self._build_task_with_context(user_message)

        for attempt in range(1, max_retries + 1):
            try:
                browser = await self._ensure_browser()

                agent_kwargs = dict(
                    task=task,
                    browser=browser,
                    extend_system_message=self._system_prompt(),
                    register_new_step_callback=self._on_step_start,
                    register_done_callback=self._on_done,
                    max_actions_per_step=3,
                    use_judge=False,
                )
                if self.controller:
                    agent_kwargs["controller"] = self.controller
                self._agent = Agent(**agent_kwargs)

                history = await self._agent.run(max_steps=50)
                final = history.final_result()

                if final:
                    self._conversation.append({"role": "assistant", "content": final})

                await self._step_queue.put(
                    StepUpdate(done=True, final_result=final)
                )
                return

            except Exception as e:
                logger.error(f"Agent error (attempt {attempt}/{max_retries}): {e}")
                err_str = str(e).lower()
                is_timeout = "timed out" in err_str or "timeout" in err_str
                is_browser_err = "browser" in err_str and (
                    "start" in err_str or "launch" in err_str or "connect" in err_str
                )

                if (is_timeout or is_browser_err) and attempt < max_retries:
                    logger.info(f"Retrying task (attempt {attempt + 1}/{max_retries})...")
                    await self._step_queue.put(
                        StepUpdate(messages=[AgentMessage("status", f"Retrying... (attempt {attempt + 1})")])
                    )
                    # Reset browser on connection errors
                    if is_browser_err and self.browser:
                        try:
                            await asyncio.wait_for(self.browser.stop(), timeout=5.0)
                        except Exception:
                            pass
                        self.browser = None
                    await asyncio.sleep(2)
                    continue

                if is_timeout:
                    user_msg = "The browser took too long to respond after multiple retries."
                elif is_browser_err:
                    user_msg = "Failed to start the browser after multiple retries."
                else:
                    user_msg = f"Something went wrong: {e}"
                await self._step_queue.put(
                    StepUpdate(
                        done=True,
                        messages=[AgentMessage("assistant", user_msg)],
                    )
                )
                return
            finally:
                if attempt == max_retries or not self._running:
                    self._running = False

    # ── Browser access (all async) ───────────────────────────────────

    async def _get_page(self):
        """Get the active Playwright page, or None."""
        if not self.browser:
            return None
        try:
            return await self.browser.get_current_page()
        except Exception as e:
            logger.debug(f"get_current_page failed: {e}")
            return None

    async def _get_tabs(self) -> list[dict]:
        """Get list of open tabs with url, title, and active flag."""
        if not self.browser or not self.browser.session_manager:
            return []
        try:
            targets = self.browser.session_manager.get_all_page_targets()
            active_id = self.browser.agent_focus_target_id
            tabs = []
            for t in targets:
                tabs.append({
                    "id": t.target_id,
                    "url": t.url,
                    "title": t.title,
                    "active": t.target_id == active_id,
                })
            return tabs
        except Exception as e:
            logger.debug(f"get_tabs failed: {e}")
            return []

    async def _get_browser_snapshot(self) -> tuple[str, str, str]:
        """Get current browser screenshot (b64), URL, and page title."""
        if not self.browser:
            return "", "", ""
        try:
            page = await self._get_page()
            if page:
                b64 = await page.screenshot(format="jpeg", quality=70)
            else:
                raw = await self.browser.take_screenshot()
                b64 = base64.b64encode(raw).decode()
            url = await self.browser.get_current_page_url()
            title = await self.browser.get_current_page_title()
            return b64, url, title
        except Exception as e:
            logger.debug(f"Browser snapshot failed: {e}")
        return "", "", ""

    async def handle_mouse_move(self, x: int, y: int) -> tuple[str, str]:
        """Dispatch mousemove to the browser so hover effects trigger,
        query the cursor style, and return (cursor, screenshot_b64)."""
        page = await self._get_page()
        if not page:
            return "default", ""
        try:
            dpr = await self._get_dpr(page)
            css_x = int(x / dpr)
            css_y = int(y / dpr)

            mouse = await page.mouse
            await mouse.move(css_x, css_y)

            cursor = await page.evaluate(
                """(x, y) => {
                  const el = document.elementFromPoint(x, y);
                  if (!el) return 'default';
                  const cs = window.getComputedStyle(el).cursor;
                  if (cs && cs !== 'auto') return cs;
                  const tag = el.tagName;
                  if (tag === 'A' || el.closest('a')) return 'pointer';
                  if (tag === 'BUTTON' || tag === 'SUMMARY' || el.closest('button'))
                    return 'pointer';
                  const role = el.getAttribute('role');
                  if (role === 'button' || role === 'link' || role === 'tab'
                      || role === 'menuitem' || role === 'option')
                    return 'pointer';
                  if (tag === 'INPUT' || tag === 'TEXTAREA' || el.isContentEditable)
                    return 'text';
                  if (tag === 'SELECT') return 'pointer';
                  if (tag === 'LABEL') return 'pointer';
                  return 'default';
                }""",
                css_x, css_y,
            )

            b64 = await page.screenshot(format="jpeg", quality=70)
            return cursor or "default", b64
        except Exception as e:
            logger.debug(f"handle_mouse_move failed: {e}")
            return "default", ""

    async def take_screenshot(self) -> None:
        """Capture and queue a screenshot of the current browser state."""
        b64, url, _ = await self._get_browser_snapshot()
        if b64:
            await self._step_queue.put(StepUpdate(screenshot=b64, url=url))

    async def switch_tab(self, target_id: str) -> None:
        """Switch the browser's active tab."""
        if not self.browser:
            return
        from browser_use.browser.events import SwitchTabEvent
        await self.browser.event_bus.dispatch(
            SwitchTabEvent(target_id=target_id)
        )
        await asyncio.sleep(0.2)
        b64, url, _ = await self._get_browser_snapshot()
        tabs = await self._get_tabs()
        if b64:
            await self._step_queue.put(
                StepUpdate(screenshot=b64, url=url, tabs=tabs)
            )

    async def close_tab(self, target_id: str) -> None:
        """Close a browser tab."""
        if not self.browser:
            return
        from browser_use.browser.events import CloseTabEvent
        await self.browser.event_bus.dispatch(
            CloseTabEvent(target_id=target_id)
        )
        await asyncio.sleep(0.3)
        b64, url, _ = await self._get_browser_snapshot()
        tabs = await self._get_tabs()
        if b64:
            await self._step_queue.put(
                StepUpdate(screenshot=b64, url=url, tabs=tabs)
            )

    # ── User browser interaction (forwarded from frontend) ───────────

    async def _get_dpr(self, page) -> float:
        """Get the device pixel ratio from the browser."""
        try:
            result = await page.evaluate("() => window.devicePixelRatio")
            return float(result) if result else 1.0
        except Exception:
            return 1.0

    async def execute_browser_action(self, action: dict) -> None:
        """Execute a user browser action (click, type, keydown, scroll),
        and queue an updated screenshot."""
        page = await self._get_page()
        if not page:
            logger.warning("execute_browser_action: no page available")
            return

        action_type = action.get("action", "")

        try:
            dpr = await self._get_dpr(page)

            if action_type == "mousedown":
                raw_x, raw_y = action.get("x", 0), action.get("y", 0)
                x = int(raw_x / dpr)
                y = int(raw_y / dpr)
                logger.info(f"MouseDown: css=({x},{y})")
                sid = await page.session_id
                await page._client.send.Input.dispatchMouseEvent(
                    {"type": "mouseMoved", "x": x, "y": y},
                    session_id=sid,
                )
                await page._client.send.Input.dispatchMouseEvent(
                    {"type": "mousePressed", "x": x, "y": y,
                     "button": "left", "clickCount": 1},
                    session_id=sid,
                )

            elif action_type == "mouseup":
                raw_x, raw_y = action.get("x", 0), action.get("y", 0)
                x = int(raw_x / dpr)
                y = int(raw_y / dpr)
                logger.info(f"MouseUp: css=({x},{y})")
                sid = await page.session_id
                await page._client.send.Input.dispatchMouseEvent(
                    {"type": "mouseReleased", "x": x, "y": y,
                     "button": "left", "clickCount": 1},
                    session_id=sid,
                )

            elif action_type == "click":
                raw_x, raw_y = action.get("x", 0), action.get("y", 0)
                x = int(raw_x / dpr)
                y = int(raw_y / dpr)
                mouse = await page.mouse
                await mouse.click(x, y)

            elif action_type == "type":
                text = action.get("text", "")
                session_id = await page.session_id
                await page._client.send.Input.insertText(
                    {"text": text}, session_id=session_id
                )

            elif action_type == "keydown":
                key = action.get("key", "")
                await page.press(key)

            elif action_type == "paste":
                text = action.get("text", "")
                if text:
                    try:
                        session_id = await page.session_id
                        await page._client.send.Input.insertText(
                            {"text": text}, session_id=session_id
                        )
                    except Exception:
                        await page.evaluate(
                            "(t) => document.execCommand('insertText', false, t)",
                            text,
                        )

            elif action_type == "copy":
                selected = await page.evaluate(
                    "() => window.getSelection()?.toString() || ''"
                )
                self._last_copy = selected or ""

            elif action_type == "selectAll":
                await page.evaluate("() => document.execCommand('selectAll')")

            elif action_type == "scroll":
                raw_x, raw_y = action.get("x", 0), action.get("y", 0)
                x = int(raw_x / dpr)
                y = int(raw_y / dpr)
                dx = action.get("deltaX", 0)
                dy = action.get("deltaY", 0)
                mouse = await page.mouse
                await mouse.scroll(x, y, delta_x=int(dx), delta_y=int(dy))
            else:
                return

        except Exception as e:
            logger.warning(f"Browser action '{action_type}' failed: {e}")
            return

        await asyncio.sleep(0.15)
        b64, url, _ = await self._get_browser_snapshot()
        tabs = await self._get_tabs()

        if url:
            self._current_url = url
        if b64:
            await self._step_queue.put(StepUpdate(screenshot=b64, url=url, tabs=tabs))

    # ── Agent step callbacks ─────────────────────────────────────────

    async def _on_step_start(self, browser_state, agent_output, step_number) -> None:
        """Called at the start of each agent step."""
        messages: list[AgentMessage] = []
        screenshot: str | None = None

        if browser_state.screenshot:
            screenshot = browser_state.screenshot

        url = browser_state.url or ""
        if url:
            self._current_url = url
            messages.append(AgentMessage("status", f"Browsing: {url}"))

        self._step_count += 1
        tabs = await self._get_tabs()
        await self._step_queue.put(
            StepUpdate(screenshot=screenshot, url=url, tabs=tabs, messages=messages)
        )

    async def _on_done(self, history) -> None:
        pass

    async def get_update(self, timeout: float = 30.0) -> StepUpdate | None:
        try:
            return await asyncio.wait_for(
                self._step_queue.get(), timeout=timeout
            )
        except asyncio.TimeoutError:
            return None

    async def stop_task(self) -> None:
        """Stop the current task but keep the browser open."""
        self._running = False
        if self._agent:
            self._agent.stop()
            self._agent = None

    async def shutdown(self) -> None:
        """Fully shut down — close browser and clean up."""
        self._running = False
        if self.browser:
            try:
                await asyncio.wait_for(self.browser.stop(), timeout=10.0)
            except Exception as e:
                logger.warning(f"Browser stop error: {e}")
            self.browser = None

    @property
    def is_running(self) -> bool:
        return self._running
