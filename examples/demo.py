#!/usr/bin/env python3
"""Runnable demo: attach to a pilot Chrome and drive three verbs live.

Prereq — a Chrome launched with remote debugging on a DEDICATED profile
(see the README's "Chrome 136 gotcha"):

    chrome --remote-debugging-port=9222 --user-data-dir="$HOME/.pilot-chrome"

Then:

    python examples/demo.py

It navigates to example.com, prints the numbered actionable elements, follows
the "More information" link by its visible text, and prints the resulting page
as plain text. Every call returns a short human-readable string — that string
is exactly what a model driving this would read back.
"""
import asyncio
import sys

from handle import Browser


async def main() -> int:
    b = Browser()
    try:
        print("GOTO  ", await b.goto("example.com"))
    except Exception as e:  # noqa: BLE001 - the demo's whole job is to explain this
        print(f"\nCould not reach Chrome on {b.cdp}: {e}\n"
              "Launch a pilot Chrome first:\n"
              "  chrome --remote-debugging-port=9222 "
              '--user-data-dir="$HOME/.pilot-chrome"', file=sys.stderr)
        return 1
    print("LINKS \n" + await b.links())
    print("CLICK ", await b.click("More information"))
    text = await b.read()
    print("READ  \n" + text[:400])
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
