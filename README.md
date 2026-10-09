# browser-pilot

[![ci](https://github.com/datumstake/browser-pilot/actions/workflows/ci.yml/badge.svg)](https://github.com/datumstake/browser-pilot/actions/workflows/ci.yml)
[![tests](https://img.shields.io/badge/tests-10%20passing%20(no%20browser)-success)](tests)
[![python](https://img.shields.io/badge/python-3.10%2B-blue)](pyproject.toml)
[![license](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

A tiny driver that lets a program — or a language model — steer a **real,
logged-in Chrome** over the DevTools Protocol, through one small vocabulary of
one-word verbs. No Selenium, no Playwright, no WebDriver: just CDP and the
standard library plus `aiohttp`.

```python
import asyncio
from browser_pilot import Browser

async def main():
    b = Browser()                       # attaches to Chrome on :9222
    print(await b.goto("example.com"))
    print(await b.links())              # numbered list of clickable things
    print(await b.click("More info"))   # by visible text, or by that number
    print(await b.read())               # the page as plain text

asyncio.run(main())
```

## Why it exists

Most browser automation is built for *testing your own app* with selectors you
wrote. This is built for the opposite case: **driving sites you didn't build,
from an agent that can't be trusted to compose a CSS selector**, inside a
session that's already authenticated.

Two design choices fall out of that:

- **Elements are addressed by number, not selector.** `links()` numbers every
  visible, actionable element and tags it in the DOM; the model acts on `"14"`.
  A small model quoting a number back is reliable; a small model composing
  `div.card:nth-child(3) > a.btn` is not. Text matching is the fallback —
  `click("Sign in")`, `type("email", "...")` — with form fields searched before
  links so a search query doesn't get typed into an anchor.

- **The verbs are few and flat.** `goto read links click type press submit fetch
  tabs screenshot` — every argument is a string, every verb is one word. That's
  what a 7B-class model can drive without tool-call gymnastics.

## The Chrome 136 gotcha (read this before it bites you)

Since Chrome 136, the browser **silently refuses `--remote-debugging-port` on
your default profile.** The exact capability this driver needs is the one being
abused in the wild to steal logged-in sessions, so Google closed it on the
default `user-data-dir`. The fix is a *dedicated* profile:

```bash
# one time: launch a pilot Chrome on a separate, persistent profile
chrome --remote-debugging-port=9222 --user-data-dir="$HOME/.pilot-chrome"
```

Log into whatever sites you want the pilot to reach, once, in *that* window —
the profile is persistent, so the logins stick, and the driver can only ever
reach what you signed that profile into. Everyday Chrome stays untouched.

## The verbs

| verb | does |
|---|---|
| `goto(url)` | navigate; waits for `readyState == complete` |
| `read()` | the page as plain text (capped) |
| `links()` | numbered, visible, actionable elements (walks open shadow roots) |
| `click(target)` | click element number *or* first label match |
| `type(target, text)` | real click + `Input.insertText` (trusted events; setter fallback) |
| `press(key)` | synthetic Enter/Tab/Escape via full key-event sequence |
| `submit(target)` | submit the form the target sits in |
| `fetch(url)` | GET a URL *in-session* and return the body (reaches cookie-gated JSON) |
| `tabs()` | list open pages |
| `screenshot()` | PNG bytes of the visible page |

A few of these encode scars worth keeping:

- **Every query walks open shadow roots.** A page built from web components —
  Reddit's `faceplate-*` elements, most design systems, anything Lit or Stencil
  — keeps its real `<input>` inside a shadow root, and
  `document.querySelectorAll('input')` finds *nothing* there. On a one-field
  test page with the input in a shadow root, the plain query finds **0**
  actionable elements and the shadow-aware one finds **1**; against a live
  signup form the difference was five anchors and none of the four inputs.
  (Closed shadow roots stay invisible — nothing in the platform can reach them.)
- **`type` clicks for real, then inserts text.** The native-setter idiom below
  is the standard answer for React inputs, and it is not enough for a component
  that tracks its own value from *trusted* events: measured 2026-10-09, the
  characters appeared in the box and the form still refused to advance. A CDP
  mouse click at the field's centre followed by `Input.insertText` produces
  events Chrome marks `isTrusted`, and the same form accepted them. Clearing
  goes the same road — a real `Backspace` over a real selection.
- **The native-setter idiom is the fallback.** When a field has no box to click
  (hidden, zero-sized, inside a scrolled container), `type` falls back to
  setting through `HTMLInputElement.prototype`'s own setter and dispatching
  `input` — the write a framework's state will at least believe.
- **`press` sends the full raw→char→up sequence.** A JS app listening on
  `keydown` will miss a two-event dispatch; search boxes in particular.
- **`fetch` navigates rather than `fetch()`-ing.** A cross-origin in-page fetch
  is CORS-blocked; navigating makes the request same-origin so it carries the
  session's cookies.

## Try it

```bash
pip install -e ".[dev]"
# with a pilot Chrome running on :9222 (see above):
python examples/demo.py          # goto -> links -> read, live
pytest -q                        # offline: parses/plumbing against a fake CDP
```

The test suite runs **without a browser** — it exercises the element-matching JS
generation and the CDP message plumbing against a stub, so CI stays hermetic.

## License

MIT. See [LICENSE](LICENSE).

---

Built by **[datumstake](https://github.com/datumstake)**. The rest of the set:

[adapt-engine](https://github.com/datumstake/adapt-engine) — the rule-driven proposer ·
[self-verifying-ratchet](https://github.com/datumstake/self-verifying-ratchet) — the measure-or-roll-back loop ·
[focus-three](https://github.com/datumstake/focus-three) — a one-file offline focus tool
