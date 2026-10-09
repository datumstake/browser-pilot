"""A Chrome driver over CDP with one small, flat verb vocabulary.

Designed to be driven by a language model: a real, logged-in Chrome is steered
through a handful of one-word verbs (`goto`, `read`, `links`, `click`, `type`,
`press`, `submit`, `fetch`, `screenshot`), each taking plain strings. The point
is reliability under a small model — every argument is a string, and elements
are addressed by the *number* a prior `links` call printed, never a CSS selector
the model would have to compose.

WHY A DEDICATED PROFILE, NOT YOUR EVERYDAY ONE: since Chrome 136 the browser
silently refuses `--remote-debugging-port` on the default user-data-dir — the
exact capability this uses is the one abused in the wild to lift logged-in
sessions, so Google closed it on the default profile. Launch Chrome with a
dedicated `--user-data-dir` (see the README) and it works and persists: a site
you log into once stays logged in, and what the driver can reach is exactly what
you chose to give that profile.

No model code lives here; this file speaks CDP and nothing else. Headless is a
launch flag on Chrome, not a different driver.
"""
from __future__ import annotations

import asyncio
import base64
import json
import re
import urllib.request
from typing import Any

import aiohttp

CDP = "http://127.0.0.1:9222"
TIMEOUT_S = 30
MAX_TEXT = 12_000
MAX_LINKS = 40

#: Every query in this file goes through `deep()`, which walks open shadow roots.
#: WHY: a page built from web components (Reddit's `faceplate-*` elements, most
#: design-system widgets, anything Lit- or Stencil-based) keeps its real <input>
#: inside a shadow root, where `document.querySelectorAll('input')` finds exactly
#: nothing. A driver that cannot see those fields reports an empty page on a form
#: that is plainly visible to the user — measured 2026-10-09 against a live
#: signup flow, where `links()` listed five anchors and none of the four inputs.
#: Closed shadow roots stay invisible; nothing in the platform can reach those.
_DEEP = r"""
  const deep = (root, sel) => {
    let out = [...root.querySelectorAll(sel)];
    for (const el of root.querySelectorAll('*'))
      if (el.shadowRoot) out = out.concat(deep(el.shadowRoot, sel));
    return out;
  };
"""

#: JS that lists what a user could act on, numbered, visible elements only.
#: Numbers rather than selectors on purpose: a small model quoting "14" back
#: is reliable; a small model composing a CSS selector is not.
_LINKS_JS = r"""
(() => {
  %DEEP%
  const els = deep(document,
    'a[href], button, input, select, textarea, [role=button], [onclick]');
  const out = [];
  for (const el of els) {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    const label = (el.innerText || el.value || el.placeholder ||
                   el.getAttribute('aria-label') || '').trim()
                  .replace(/\s+/g, ' ').slice(0, 80);
    const kind = el.tagName.toLowerCase() +
                 (el.type ? ':' + el.type : '');
    out.push({ n: out.length + 1, kind, label,
               href: (el.getAttribute('href') || '').slice(0, 120) });
    el.setAttribute('data-pilot-n', out.length);
    if (out.length >= %MAX%) break;
  }
  return JSON.stringify(out);
})()
""".replace("%MAX%", str(MAX_LINKS)).replace("%DEEP%", _DEEP)


def _el_js(target: str, action: str) -> str:
    """JS acting on a numbered element from the last `links` call, or on the
    first element whose visible label contains the target text."""
    quoted = json.dumps(target)
    return f"""
(() => {{
  {_DEEP}
  let el = null;
  if (/^\\d+$/.test({quoted}))
    el = deep(document, '[data-pilot-n="' + {quoted} + '"]')[0];
  if (!el) {{
    const want = {quoted}.toLowerCase();
    // Form fields first: "q" or "email" names an input's name= far more
    // often than a link's text, and a model will type a search query into an
    // anchor if links are scanned first.
    const fields = deep(document, 'input, textarea, select');
    const rest = deep(document, 'a[href], button, [role=button], [onclick]');
    for (const cand of fields.concat(rest)) {{
      const label = ((cand.innerText || '') + ' ' + (cand.value || '') + ' ' +
                     (cand.placeholder || '') + ' ' + (cand.name || '') + ' ' +
                     (cand.id || '') + ' ' +
                     (cand.getAttribute('aria-label') || '')).toLowerCase();
      if (label.includes(want)) {{ el = cand; break; }}
    }}
  }}
  if (!el) return 'NO MATCH for ' + {quoted};
  {action}
}})()
"""


class Browser:
    """One attached Chrome page, driven verb by verb."""

    def __init__(self, cdp: str = CDP) -> None:
        self.cdp = cdp
        self._id = 0

    # -- plumbing --------------------------------------------------------------

    def _targets(self) -> list[dict]:
        with urllib.request.urlopen(f"{self.cdp}/json/list", timeout=10) as r:
            return [t for t in json.loads(r.read())
                    if t.get("type") == "page"]

    def _page_ws(self, tab: int = 0) -> str:
        pages = self._targets()
        if not pages:
            with urllib.request.urlopen(f"{self.cdp}/json/new", timeout=10) as r:
                return json.loads(r.read())["webSocketDebuggerUrl"]
        tab = max(1, min(tab or 1, len(pages)))
        return pages[tab - 1]["webSocketDebuggerUrl"]

    async def _cmd(self, ws, method: str, **params) -> dict:
        self._id += 1
        await ws.send_str(json.dumps(
            {"id": self._id, "method": method, "params": params}))
        while True:
            msg = await asyncio.wait_for(ws.receive(), timeout=TIMEOUT_S)
            if msg.type != aiohttp.WSMsgType.TEXT:
                raise RuntimeError(f"CDP socket closed: {msg.type}")
            data = json.loads(msg.data)
            if data.get("id") == self._id:
                if "error" in data:
                    raise RuntimeError(f"CDP {method}: {data['error']}")
                return data.get("result", {})

    async def _eval(self, ws, js: str) -> Any:
        got = await self._cmd(ws, "Runtime.evaluate", expression=js,
                              returnByValue=True, awaitPromise=True)
        return (got.get("result") or {}).get("value")

    async def _run(self, tab: int, fn) -> Any:
        async with aiohttp.ClientSession() as session:
            async with session.ws_connect(self._page_ws(tab),
                                          max_msg_size=32 * 1024 * 1024) as ws:
                return await fn(ws)

    # -- verbs -----------------------------------------------------------------

    async def goto(self, url: str, tab: int = 0) -> str:
        if not re.match(r"https?://", url, re.IGNORECASE):
            url = "https://" + url

        async def go(ws):
            await self._cmd(ws, "Page.enable")
            await self._cmd(ws, "Page.navigate", url=url)
            for _ in range(60):
                state = await self._eval(ws, "document.readyState")
                if state == "complete":
                    break
                await asyncio.sleep(0.5)
            title = await self._eval(ws, "document.title")
            here = await self._eval(ws, "location.href")
            return f"at {here} - {title}"

        return await self._run(tab, go)

    async def read(self, tab: int = 0) -> str:
        async def r(ws):
            title = await self._eval(ws, "document.title")
            here = await self._eval(ws, "location.href")
            text = await self._eval(
                ws, "document.body ? document.body.innerText : ''") or ""
            text = re.sub(r"\n{3,}", "\n\n", text).strip()
            if len(text) > MAX_TEXT:
                text = text[:MAX_TEXT] + f"\n... truncated at {MAX_TEXT} chars"
            return f"{title}\n{here}\n\n{text}"

        return await self._run(tab, r)

    async def fetch(self, url: str, tab: int = 0) -> str:
        """Navigate to a URL in the logged-in session and return its raw body text.

        Used to read API/JSON endpoints THROUGH the real browser: the request
        carries the session's cookies, so it reaches endpoints that refuse
        anonymous clients (many sites 403 an unauthenticated `.json` but serve
        it to a signed-in browser). Navigation — not an in-page `fetch()` —
        because a cross-origin fetch is CORS-blocked from whatever page is open;
        navigating makes the request same-origin. Read-only (a GET navigation).

        Note: this drives the given tab's navigation. Pass a dedicated tab index
        if you don't want to disturb the active page.
        """
        if not re.match(r"https?://", url, re.IGNORECASE):
            url = "https://" + url

        async def f(ws):
            await self._cmd(ws, "Page.enable")
            await self._cmd(ws, "Page.navigate", url=url)
            for _ in range(60):
                if await self._eval(ws, "document.readyState") == "complete":
                    break
                await asyncio.sleep(0.5)
            # A .json endpoint renders as text in the body; innerText is the
            # JSON string. textContent of <pre> is the raw shape; cover both.
            body = await self._eval(
                ws, "(document.body ? (document.querySelector('pre') "
                    "? document.querySelector('pre').textContent "
                    ": document.body.innerText) : '')")
            return body or "fetch error: empty body"

        return await self._run(tab, f)

    async def links(self, tab: int = 0) -> str:
        async def l(ws):  # noqa: E743
            raw = await self._eval(ws, _LINKS_JS) or "[]"
            rows = json.loads(raw)
            if not rows:
                return "nothing clickable found"
            return "\n".join(
                f"[{r['n']}] {r['kind']}: {r['label'] or r['href'] or '?'}"
                for r in rows)

        return await self._run(tab, l)

    async def click(self, target: str, tab: int = 0) -> str:
        async def c(ws):
            out = await self._eval(ws, _el_js(
                target,
                "el.scrollIntoView({block:'center'}); el.click(); "
                "return 'clicked ' + (el.innerText || el.value || "
                "el.tagName).trim().slice(0, 60);"))
            await asyncio.sleep(1.0)        # let a navigation start
            here = await self._eval(ws, "location.href")
            return f"{out} | now at {here}"

        return await self._run(tab, c)

    async def type(self, target: str, text: str, tab: int = 0) -> str:
        """Type into a field the way a person does: a real click, then real text.

        2026-10-09, measured on a live signup form: the native-setter idiom below
        — the one every "type into a React input" answer recommends — put the
        right characters in the box and the form still refused to advance. Its
        web component tracks its own value from trusted input events, so a
        scripted `.value =` plus a synthetic `input` event is a value the
        component never agreed to. A CDP mouse click at the field's centre
        followed by `Input.insertText` produces events Chrome marks trusted, and
        the same form accepted it immediately.

        So: real events first, the setter kept only as the fallback for a field
        with no box to click (hidden, zero-sized, scrolled out of a container).
        """
        quoted = json.dumps(text)

        async def t(ws):
            where = await self._eval(ws, _el_js(
                target,
                "el.scrollIntoView({block:'center'});"
                "const r = el.getBoundingClientRect();"
                "window.__pilotEl = el;"
                "if (r.width < 2 || r.height < 2) return 'NO BOX';"
                "return JSON.stringify({x: Math.round(r.x + r.width / 2),"
                " y: Math.round(r.y + r.height / 2),"
                " name: (el.name || el.placeholder || el.tagName).slice(0, 60)});"))
            if isinstance(where, str) and where.startswith("NO MATCH"):
                return where

            if where != "NO BOX":
                spot = json.loads(where)
                for kind in ("mousePressed", "mouseReleased"):
                    await self._cmd(ws, "Input.dispatchMouseEvent", type=kind,
                                    x=spot["x"], y=spot["y"], button="left",
                                    clickCount=1)
                await asyncio.sleep(0.15)
                # Clear through a real Backspace on a real selection, so the
                # component sees a deletion rather than a value that changed
                # behind its back.
                had = await self._eval(
                    ws, "(() => { const el = window.__pilotEl; el.focus();"
                        " if (el.setSelectionRange) el.setSelectionRange(0,"
                        " el.value.length); return el.value.length; })()")
                if had:
                    for kind in ("keyDown", "keyUp"):
                        await self._cmd(ws, "Input.dispatchKeyEvent", type=kind,
                                        key="Backspace", code="Backspace",
                                        windowsVirtualKeyCode=8,
                                        nativeVirtualKeyCode=8)
                    await asyncio.sleep(0.1)
                await self._cmd(ws, "Input.insertText", text=text)
                await asyncio.sleep(0.2)
                got = await self._eval(
                    ws, "(() => { const el = window.__pilotEl;"
                        " return el ? el.value : ''; })()")
                if got == text:
                    return (f"typed into {spot['name']} = "
                            f"{str(got)[:40]} (real events)")

            # Fallback: the native-setter idiom. A controlled input ignores a
            # bare el.value= because the framework's state never saw the write;
            # going through the prototype's own setter and dispatching 'input'
            # is the write the framework believes.
            return await self._eval(ws, _el_js(
                target,
                f"el.focus(); "
                f"const proto = el.tagName === 'TEXTAREA' ? "
                f"window.HTMLTextAreaElement.prototype : "
                f"window.HTMLInputElement.prototype; "
                f"const setter = Object.getOwnPropertyDescriptor(proto, 'value'); "
                f"if (setter && setter.set) setter.set.call(el, {quoted}); "
                f"else el.value = {quoted}; "
                f"el.dispatchEvent(new Event('input', {{bubbles: true}})); "
                f"el.dispatchEvent(new Event('change', {{bubbles: true}})); "
                f"return 'typed into ' + (el.name || el.placeholder || "
                f"el.tagName).slice(0, 60) + ' = ' + el.value.slice(0, 40) + "
                f"' (setter fallback)';"))

        return await self._run(tab, t)

    async def press(self, key: str = "Enter", tab: int = 0) -> str:
        # The full raw/char/up sequence, because a JS app listens on keydown
        # and a two-event dispatch can miss a search box's handler entirely.
        vk = {"Enter": 13, "Tab": 9, "Escape": 27}.get(key, 0)

        async def p(ws):
            await self._cmd(ws, "Input.dispatchKeyEvent", type="rawKeyDown",
                            key=key, code=key, windowsVirtualKeyCode=vk,
                            nativeVirtualKeyCode=vk)
            if key == "Enter":
                await self._cmd(ws, "Input.dispatchKeyEvent", type="char",
                                text="\r", key=key)
            await self._cmd(ws, "Input.dispatchKeyEvent", type="keyUp",
                            key=key, code=key, windowsVirtualKeyCode=vk,
                            nativeVirtualKeyCode=vk)
            await asyncio.sleep(1.5)
            here = await self._eval(ws, "location.href")
            return f"pressed {key} | now at {here}"

        return await self._run(tab, p)

    async def submit(self, target: str, tab: int = 0) -> str:
        """Submit the form the target sits in — the road that works when a
        page's Enter handler is unreachable by synthetic keys."""
        async def s(ws):
            out = await self._eval(ws, _el_js(
                target,
                "const f = el.form || el.closest('form');"
                "if (!f) return 'NO FORM around ' + (el.name || el.tagName);"
                "if (f.requestSubmit) f.requestSubmit(); else f.submit();"
                "return 'submitted form ' + (f.action || '').slice(0, 80);"))
            await asyncio.sleep(1.5)
            here = await self._eval(ws, "location.href")
            return f"{out} | now at {here}"

        return await self._run(tab, s)

    async def tabs(self) -> str:
        pages = self._targets()
        if not pages:
            return "no tabs open"
        return "\n".join(f"[{i + 1}] {p.get('title') or '?'} - "
                         f"{(p.get('url') or '')[:100]}"
                         for i, p in enumerate(pages))

    async def screenshot(self, tab: int = 0) -> bytes:
        """PNG bytes of the visible page — the vision verb. The caller decides
        whether a model can see it; this just captures."""
        async def s(ws):
            await self._cmd(ws, "Page.enable")
            got = await self._cmd(ws, "Page.captureScreenshot", format="png")
            return base64.b64decode(got["data"])

        return await self._run(tab, s)
