"""MobileAgent — LLM-powered agent loop for iOS WKWebView clients.

Unlike BrowserAgent which relies on browser-use + Playwright/Chromium,
MobileAgent calls an LLM API directly with tool definitions for browser
actions. Each tool call is translated into JS sent to the iOS client's
WKWebView via RemotePage.

Uses browser-use-style element indexing: before each LLM call, a JS
snippet extracts all interactive elements, assigns numeric indices, and
adds visual labels. The LLM references elements by index.
"""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from dataclasses import dataclass, field

from remote_page import RemotePage
from agent import AgentMessage, StepUpdate

logger = logging.getLogger(__name__)

MAX_AGENT_STEPS = 30

# ── Tool definitions (index-based, browser-use style) ─────────────────

_TOOL_DEFS = [
    ("navigate", "Navigate the browser to a URL.", {
        "type": "object",
        "properties": {"url": {"type": "string", "description": "The URL to navigate to"}},
        "required": ["url"],
    }),
    ("click_element", "Click an interactive element on the page by its index number from the element list.", {
        "type": "object",
        "properties": {"index": {"type": "integer", "description": "The index number of the element to click (from the interactive elements list)"}},
        "required": ["index"],
    }),
    ("type_text", "Type text into an input element identified by its index number.", {
        "type": "object",
        "properties": {
            "index": {"type": "integer", "description": "The index number of the input element"},
            "text": {"type": "string", "description": "The text to type"},
            "clear_first": {"type": "boolean", "description": "Whether to clear the field before typing", "default": True},
        },
        "required": ["index", "text"],
    }),
    ("scroll", "Scroll the page up or down.", {
        "type": "object",
        "properties": {
            "direction": {"type": "string", "enum": ["up", "down"], "description": "Scroll direction"},
            "amount": {"type": "integer", "description": "Pixels to scroll (default 500)", "default": 500},
        },
        "required": ["direction"],
    }),
    ("select_option", "Select an option from a dropdown/select element by its index.", {
        "type": "object",
        "properties": {
            "index": {"type": "integer", "description": "The index of the select element"},
            "value": {"type": "string", "description": "The value or visible text of the option to select"},
        },
        "required": ["index", "value"],
    }),
    ("go_back", "Go back to the previous page.", {
        "type": "object",
        "properties": {},
    }),
    ("wait", "Wait for a specified number of seconds (e.g. for page to load).", {
        "type": "object",
        "properties": {"seconds": {"type": "number", "description": "Seconds to wait (max 10)", "default": 2}},
    }),
    ("done", "Signal that the task is complete and provide a final summary to the user.", {
        "type": "object",
        "properties": {"summary": {"type": "string", "description": "Final message to show the user"}},
        "required": ["summary"],
    }),
]

# Anthropic format
ANTHROPIC_TOOLS = [
    {"name": n, "description": d, "input_schema": s} for n, d, s in _TOOL_DEFS
]

# OpenAI format
OPENAI_TOOLS = [
    {"type": "function", "function": {"name": n, "description": d, "parameters": s}}
    for n, d, s in _TOOL_DEFS
]


# ── JS: Element extraction (browser-use style indexing) ───────────────

EXTRACT_ELEMENTS_JS = """() => {
    // Remove any previous labels
    document.querySelectorAll('[data-agent-label]').forEach(el => el.remove());

    const INTERACTIVE = 'a, button, input, textarea, select, [role="button"], ' +
        '[role="link"], [role="tab"], [role="menuitem"], [role="checkbox"], ' +
        '[role="radio"], [role="switch"], [role="combobox"], [role="option"], ' +
        '[onclick], [tabindex]:not([tabindex="-1"]), label[for], summary, details';

    const els = Array.from(document.querySelectorAll(INTERACTIVE));
    const results = [];
    let idx = 0;

    for (const el of els) {
        // Skip hidden/invisible
        const style = getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') continue;
        if (el.offsetWidth === 0 && el.offsetHeight === 0) continue;
        if (el.closest('[aria-hidden="true"]')) continue;

        const rect = el.getBoundingClientRect();
        if (rect.width < 2 || rect.height < 2) continue;

        const tag = el.tagName.toLowerCase();
        const type = el.getAttribute('type') || '';
        const role = el.getAttribute('role') || '';
        const ariaLabel = el.getAttribute('aria-label') || '';
        const placeholder = el.getAttribute('placeholder') || '';
        const text = (el.innerText || el.textContent || '').trim().substring(0, 80);
        const value = el.value || '';
        const href = el.href || '';
        const name = el.getAttribute('name') || '';
        const id = el.id || '';

        let desc = '<' + tag;
        if (type) desc += ' type="' + type + '"';
        if (role) desc += ' role="' + role + '"';
        if (id) desc += ' id="' + id + '"';
        if (name) desc += ' name="' + name + '"';
        desc += '>';

        if (ariaLabel) desc += ' "' + ariaLabel + '"';
        else if (placeholder) desc += ' placeholder="' + placeholder + '"';
        else if (text && text.length <= 60) desc += ' "' + text + '"';

        if (tag === 'input' || tag === 'textarea') {
            desc += ' value="' + value.substring(0, 60) + '"';
        }
        if (tag === 'select') {
            const opts = Array.from(el.options).slice(0, 8).map(
                o => o.selected ? '[' + o.text.trim() + ']' : o.text.trim()
            );
            desc += ' options=[' + opts.join(', ') + ']';
        }
        if (href) {
            try {
                const u = new URL(href);
                desc += ' href="' + u.pathname.substring(0, 50) + '"';
            } catch(e) {}
        }

        el.setAttribute('data-agent-idx', idx);

        const label = document.createElement('div');
        label.setAttribute('data-agent-label', '');
        label.textContent = idx;
        const scrollX = window.scrollX || window.pageXOffset;
        const scrollY = window.scrollY || window.pageYOffset;
        label.style.cssText = 'position:absolute;z-index:2147483647;' +
            'background:#e8384f;color:#fff;font:bold 11px/1 monospace;' +
            'padding:1px 4px;border-radius:3px;pointer-events:none;' +
            'box-shadow:0 1px 3px rgba(0,0,0,.4);' +
            'left:' + Math.max(0, rect.left + scrollX - 2) + 'px;' +
            'top:' + Math.max(0, rect.top + scrollY - 14) + 'px;';
        document.body.appendChild(label);

        results.push({i: idx, d: desc});
        idx++;
        if (idx >= 200) break;
    }

    return JSON.stringify(results);
}"""

CLICK_BY_INDEX_JS = """(idx) => {
    const el = document.querySelector('[data-agent-idx="' + idx + '"]');
    if (!el) return 'Element not found for index ' + idx;
    el.scrollIntoView({block: 'center', behavior: 'instant'});
    el.focus();
    ['pointerdown','mousedown','pointerup','mouseup','click'].forEach(t => {
        el.dispatchEvent(new PointerEvent(t, {bubbles:true,cancelable:true,view:window}));
    });
    return 'clicked: ' + (el.innerText || el.tagName).substring(0, 60);
}"""

TYPE_BY_INDEX_JS = """(idx, text, clearFirst) => {
    const el = document.querySelector('[data-agent-idx="' + idx + '"]');
    if (!el) return 'Element not found for index ' + idx;
    el.scrollIntoView({block: 'center', behavior: 'instant'});
    el.focus();
    el.click();

    const tag = el.tagName.toLowerCase();
    const isInput = (tag === 'input' || tag === 'textarea');

    if (isInput) {
        if (clearFirst) {
            el.select();
            document.execCommand('delete');
        }
        document.execCommand('insertText', false, text);

        if (el.value !== text && clearFirst) {
            const proto = tag === 'textarea'
                ? window.HTMLTextAreaElement.prototype
                : window.HTMLInputElement.prototype;
            const nativeSetter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
            if (nativeSetter) nativeSetter.call(el, text);
            else el.value = text;
            el.dispatchEvent(new Event('input', {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
        }
    } else {
        if (clearFirst) el.textContent = '';
        el.textContent += text;
        el.dispatchEvent(new Event('input', {bubbles: true}));
    }
    return 'typed into: ' + (el.placeholder || el.name || el.id || tag);
}"""

SELECT_BY_INDEX_JS = """(idx, value) => {
    const el = document.querySelector('[data-agent-idx="' + idx + '"]');
    if (!el || el.tagName.toLowerCase() !== 'select') return 'Not a select element at index ' + idx;
    el.scrollIntoView({block: 'center', behavior: 'instant'});
    let found = false;
    for (const opt of el.options) {
        if (opt.value === value || opt.text.trim().toLowerCase() === value.toLowerCase()) {
            opt.selected = true;
            found = true;
            break;
        }
    }
    if (!found) {
        const lower = value.toLowerCase();
        for (const opt of el.options) {
            if (opt.text.trim().toLowerCase().includes(lower) || opt.value.toLowerCase().includes(lower)) {
                opt.selected = true;
                found = true;
                break;
            }
        }
    }
    if (!found) return 'Option "' + value + '" not found in select';
    el.dispatchEvent(new Event('change', {bubbles: true}));
    return 'selected: ' + value;
}"""

SCROLL_JS = """(direction, amount) => {
    const dy = direction === 'up' ? -amount : amount;
    window.scrollBy({top: dy, behavior: 'smooth'});
    return 'scrolled ' + direction + ' ' + amount + 'px';
}"""

GO_BACK_JS = """() => {
    window.history.back();
    return 'navigated back';
}"""

REMOVE_LABELS_JS = """() => {
    document.querySelectorAll('[data-agent-label]').forEach(el => el.remove());
    return 'labels removed';
}"""


SYSTEM_PROMPT = """\
You are Proxxy, a helpful AI assistant that uses the browser on the user's \
device to complete tasks. You can search the web, navigate websites, fill \
out forms, extract information, and perform any browser-based task.

You can also make phone calls to businesses using the make_phone_call tool. \
When the user asks you to call a business, first find the phone number by \
browsing the web, then use make_phone_call with the phone number and business \
name. The call will be handled by an AI voice agent.

The current date and time is: {current_datetime}

IMPORTANT RULES:
1. If a website requires login or authentication, STOP immediately and tell \
the user: "This site requires you to log in. Please complete the \
authentication in your browser, then let me know when you're done."
2. Never enter passwords, payment details, or other sensitive information.
3. Be thorough in your work — verify results before reporting them.
4. Always summarize your findings clearly at the end.\
"""


class MobileAgent:
    """Agent loop for iOS clients using RemotePage as the browser interface."""

    def __init__(self, page: RemotePage, *, about_me: str = "") -> None:
        self.page = page
        self._step_queue: asyncio.Queue[StepUpdate] = asyncio.Queue()
        self._running = False
        self._conversation: list[dict[str, str]] = []
        self._current_url: str = ""
        self.about_me: str = about_me
        # Extra tools injected externally (e.g. make_phone_call)
        self._extra_tools_openai: list[dict] = []
        self._extra_tools_anthropic: list[dict] = []
        self._extra_tool_handlers: dict[str, any] = {}

        # Pick provider
        anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
        openai_key = os.environ.get("OPENAI_API_KEY")
        if openai_key:
            self._provider = "openai"
            from openai import AsyncOpenAI
            self._openai = AsyncOpenAI(api_key=openai_key)
            self._anthropic = None
            logger.info("MobileAgent using OpenAI")
        elif anthropic_key:
            self._provider = "anthropic"
            import anthropic
            self._anthropic = anthropic.AsyncAnthropic(api_key=anthropic_key)
            self._openai = None
            logger.info("MobileAgent using Anthropic")
        else:
            raise RuntimeError("Neither OPENAI_API_KEY nor ANTHROPIC_API_KEY is set")

    def register_tool(self, name: str, description: str, parameters: dict, handler) -> None:
        """Register an extra tool (e.g. make_phone_call) for the agent to use."""
        self._extra_tools_openai.append({
            "type": "function",
            "function": {"name": name, "description": description, "parameters": parameters},
        })
        self._extra_tools_anthropic.append({
            "name": name, "description": description, "input_schema": parameters,
        })
        self._extra_tool_handlers[name] = handler

    # ------------------------------------------------------------------
    # Public interface (matches BrowserAgent's shape for main.py compat)
    # ------------------------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._running

    async def run_task(self, user_message: str) -> None:
        """Run a task using the LLM agent loop + remote WKWebView."""
        self._running = True
        self._conversation.append({"role": "user", "content": user_message})

        try:
            await self._agent_loop(user_message)
        except Exception as e:
            logger.error("MobileAgent error: %s", e)
            await self._step_queue.put(
                StepUpdate(
                    done=True,
                    messages=[AgentMessage("assistant", f"Something went wrong: {e}")],
                )
            )
        finally:
            self._running = False

    async def get_update(self, timeout: float = 30.0) -> StepUpdate | None:
        try:
            return await asyncio.wait_for(
                self._step_queue.get(), timeout=timeout
            )
        except asyncio.TimeoutError:
            return None

    async def stop_task(self) -> None:
        self._running = False

    async def shutdown(self) -> None:
        self._running = False
        self.page.cancel_all()

    # ------------------------------------------------------------------
    # Agent loop
    # ------------------------------------------------------------------

    async def _agent_loop(self, user_message: str) -> None:
        """Core loop: extract elements -> screenshot -> LLM -> tool calls -> repeat."""
        now = datetime.now(timezone.utc).strftime("%A, %B %d, %Y at %H:%M UTC")
        system_prompt = SYSTEM_PROMPT.format(current_datetime=now)

        if self.about_me:
            system_prompt += f"\n\nAbout the user:\n{self.about_me}"

        system_prompt += "\n\n" + ELEMENT_INDEX_INSTRUCTIONS

        messages = self._build_messages(user_message)

        # Build tool lists with extras
        openai_tools = OPENAI_TOOLS + self._extra_tools_openai
        anthropic_tools = ANTHROPIC_TOOLS + self._extra_tools_anthropic

        for step in range(MAX_AGENT_STEPS):
            if not self._running:
                break

            if step == 0:
                screenshot_b64 = ""
                current_url = ""
                element_text = ""
            else:
                try:
                    element_text = await self._extract_elements()
                except Exception as e:
                    logger.warning("Element extraction failed: %s", e)
                    element_text = "(element extraction failed)"

                try:
                    screenshot_b64 = await self.page.screenshot()
                    current_url = await self.page.url()
                except Exception as e:
                    logger.warning("Failed to get screenshot: %s", e)
                    screenshot_b64 = ""
                    current_url = self._current_url

            if current_url:
                self._current_url = current_url

            if step > 0:
                await self._step_queue.put(
                    StepUpdate(
                        screenshot=screenshot_b64,
                        url=current_url,
                        messages=[AgentMessage("status", f"Browsing: {current_url}")] if current_url else [],
                    )
                )

            # Build observation message for the LLM
            if step == 0:
                last = messages[-1]
                if isinstance(last.get("content"), str):
                    messages[-1] = {
                        "role": "user",
                        "content": last["content"] + "\n\n[IMPORTANT: You MUST use your browser tools to help the user. Use the navigate tool to go to the appropriate website. For general lookups, navigate to https://www.google.com/search?q=your+query+here. Do NOT respond with just text — always use tools.]",
                    }
            else:
                self._append_observation(messages, screenshot_b64, current_url, element_text)

            # Call LLM
            try:
                if self._provider == "openai":
                    assistant_text, tool_calls = await self._call_openai(
                        system_prompt, messages, openai_tools
                    )
                else:
                    assistant_text, tool_calls = await self._call_anthropic(
                        system_prompt, messages, anthropic_tools
                    )
            except Exception as e:
                logger.error("LLM API error: %s", e)
                await self._step_queue.put(
                    StepUpdate(done=True, messages=[AgentMessage("assistant", f"AI service error: {e}")])
                )
                return

            if assistant_text.strip():
                self._conversation.append({"role": "assistant", "content": assistant_text})
                if tool_calls:
                    await self._step_queue.put(
                        StepUpdate(messages=[AgentMessage("assistant", assistant_text)])
                    )

            if not tool_calls:
                await self._step_queue.put(
                    StepUpdate(done=True, final_result=assistant_text or "Task completed.")
                )
                return

            # Execute tool calls
            task_done = False
            for name, call_id, params in tool_calls:
                result = await self._execute_tool(name, params)
                self._append_tool_result(messages, name, call_id, result)
                if name == "done":
                    task_done = True
                    summary = params.get("summary", "Task completed.")
                    self._conversation.append({"role": "assistant", "content": summary})
                    await self._step_queue.put(
                        StepUpdate(done=True, final_result=summary)
                    )

            if task_done:
                return

        await self._step_queue.put(
            StepUpdate(done=True, final_result="I've reached the maximum number of steps. Here's what I found so far.")
        )

    # ------------------------------------------------------------------
    # Element extraction
    # ------------------------------------------------------------------

    async def _extract_elements(self) -> str:
        raw = await self.page.evaluate(EXTRACT_ELEMENTS_JS)
        if not raw:
            return "(no interactive elements found)"

        try:
            elements = json.loads(raw) if isinstance(raw, str) else raw
        except (json.JSONDecodeError, TypeError):
            return "(element extraction parse error)"

        if not elements:
            return "(no interactive elements found)"

        lines = []
        for el in elements:
            lines.append(f"[{el['i']}] {el['d']}")
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Message building
    # ------------------------------------------------------------------

    def _build_messages(self, user_message: str) -> list[dict]:
        messages = []
        for msg in self._conversation[:-1]:
            messages.append({"role": msg["role"], "content": msg["content"]})
        messages.append({"role": "user", "content": user_message})
        return messages

    def _append_observation(self, messages: list, b64: str, url: str, element_text: str) -> None:
        text_parts = []
        if url:
            text_parts.append(f"Current URL: {url}")
        if element_text:
            text_parts.append(f"\nInteractive elements:\n{element_text}")
        text_parts.append("\nWhat should I do next? Use element indices for click/type actions.")
        observation_text = "\n".join(text_parts)

        if self._provider == "openai":
            content = []
            if b64:
                content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{b64}", "detail": "low"},
                })
            content.append({"type": "text", "text": observation_text})
            messages.append({"role": "user", "content": content})
        else:
            content = []
            if b64:
                content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}})
            content.append({"type": "text", "text": observation_text})
            messages.append({"role": "user", "content": content})

    # ------------------------------------------------------------------
    # OpenAI provider
    # ------------------------------------------------------------------

    async def _call_openai(self, system_prompt: str, messages: list, tools: list):
        response = await self._openai.chat.completions.create(
            model="gpt-4o",
            max_tokens=4096,
            messages=[{"role": "system", "content": system_prompt}] + messages,
            tools=tools,
        )
        choice = response.choices[0]
        msg = choice.message

        messages.append(msg.model_dump(exclude_none=True))

        assistant_text = msg.content or ""
        tool_calls = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                try:
                    params = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    params = {}
                tool_calls.append((tc.function.name, tc.id, params))

        return assistant_text, tool_calls

    def _append_tool_result_openai(self, messages: list, name: str, call_id: str, result: str) -> None:
        messages.append({"role": "tool", "tool_call_id": call_id, "content": result})

    # ------------------------------------------------------------------
    # Anthropic provider
    # ------------------------------------------------------------------

    async def _call_anthropic(self, system_prompt: str, messages: list, tools: list):
        response = await self._anthropic.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=4096,
            system=system_prompt,
            tools=tools,
            messages=messages,
        )

        messages.append({"role": "assistant", "content": response.content})

        assistant_text = ""
        tool_calls = []
        for block in response.content:
            if block.type == "text":
                assistant_text += block.text
            elif block.type == "tool_use":
                tool_calls.append((block.name, block.id, block.input))

        return assistant_text, tool_calls

    def _append_tool_result_anthropic(self, messages: list, name: str, call_id: str, result: str) -> None:
        messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": call_id, "content": result}]})

    def _append_tool_result(self, messages: list, name: str, call_id: str, result: str) -> None:
        if self._provider == "openai":
            self._append_tool_result_openai(messages, name, call_id, result)
        else:
            self._append_tool_result_anthropic(messages, name, call_id, result)

    # ------------------------------------------------------------------
    # Tool execution
    # ------------------------------------------------------------------

    async def _execute_tool(self, name: str, params: dict) -> str:
        # Check extra tools first (e.g. make_phone_call)
        if name in self._extra_tool_handlers:
            try:
                return await self._extra_tool_handlers[name](params)
            except Exception as e:
                logger.warning("Extra tool '%s' failed: %s", name, e)
                return f"Error: {e}"

        try:
            if name == "navigate":
                url = params["url"]
                try:
                    await self.page.evaluate(REMOVE_LABELS_JS)
                except Exception:
                    pass
                await self.page.goto(url)
                await asyncio.sleep(2.0)
                return f"Navigated to {url}"

            elif name == "click_element":
                index = params["index"]
                result = await self.page.evaluate(CLICK_BY_INDEX_JS, index)
                await asyncio.sleep(0.8)
                return str(result)

            elif name == "type_text":
                index = params["index"]
                text = params["text"]
                clear = params.get("clear_first", True)
                result = await self.page.evaluate(TYPE_BY_INDEX_JS, index, text, clear)
                return str(result)

            elif name == "select_option":
                index = params["index"]
                value = params["value"]
                result = await self.page.evaluate(SELECT_BY_INDEX_JS, index, value)
                return str(result)

            elif name == "scroll":
                direction = params["direction"]
                amount = params.get("amount", 500)
                result = await self.page.evaluate(SCROLL_JS, direction, amount)
                await asyncio.sleep(0.3)
                return str(result)

            elif name == "go_back":
                result = await self.page.evaluate(GO_BACK_JS)
                await asyncio.sleep(1.5)
                return str(result)

            elif name == "wait":
                seconds = min(params.get("seconds", 2), 10)
                await asyncio.sleep(seconds)
                return f"Waited {seconds} seconds"

            elif name == "done":
                try:
                    await self.page.evaluate(REMOVE_LABELS_JS)
                except Exception:
                    pass
                return "Task complete"

            else:
                return f"Unknown tool: {name}"

        except Exception as e:
            logger.warning("Tool '%s' failed: %s", name, e)
            return f"Error: {e}"

    # ------------------------------------------------------------------
    # Compatibility helpers
    # ------------------------------------------------------------------

    async def take_screenshot(self) -> None:
        try:
            b64 = await self.page.screenshot()
            url = await self.page.url()
            await self._step_queue.put(StepUpdate(screenshot=b64, url=url))
        except Exception:
            pass


ELEMENT_INDEX_INSTRUCTIONS = """\
BROWSER INTERACTION:
You control a browser on the user's device. On each step you will see:
1. A screenshot of the current page
2. A list of interactive elements with numeric indices like:
   [0] <a> "Google Search" href="/search"
   [1] <input type="text" name="q"> placeholder="Search" value=""
   [2] <button> "Search"

When you want to interact with an element, use its index number:
- click_element(index=2) to click the "Search" button
- type_text(index=1, text="restaurants in Tokyo") to type into the search box
- select_option(index=5, value="2 guests") to select from a dropdown

IMPORTANT:
- Always use the index numbers from the element list, never guess CSS selectors
- The element list only shows elements currently visible in the viewport
- If you need to find elements below the fold, scroll down first
- After clicking or typing, wait for the next observation to see the updated page
- Use navigate() to go to a specific URL directly
- Use go_back() to return to the previous page
- Use done(summary="...") when you have the answer for the user
- TIP: For Google searches, navigate directly to https://www.google.com/search?q=your+search+query
  instead of trying to type into the search box — this is faster and more reliable
"""
