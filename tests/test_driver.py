"""Offline tests: no browser, no sockets. They pin the two things most likely
to break silently — the generated element-matching JS and the tab-selection
clamp — against a stub, so CI stays hermetic."""
import json

from handle.driver import MAX_LINKS, Browser, _LINKS_JS, _el_js


def test_links_js_has_max_substituted():
    # The %MAX% placeholder must be gone and the real cap present.
    assert "%MAX%" not in _LINKS_JS
    assert f">= {MAX_LINKS}" in _LINKS_JS
    assert "data-pilot-n" in _LINKS_JS


def test_el_js_quotes_target_safely():
    # A target with a quote in it must not break out of the JS string.
    js = _el_js('say "hi"', "return el.click();")
    assert json.dumps('say "hi"') in js
    assert "data-pilot-n" in js
    assert "return el.click();" in js


def test_el_js_numeric_target_takes_the_fast_path():
    js = _el_js("14", "return 1;")
    # A bare number is matched by the data-pilot-n attribute, not text search.
    assert r"/^\d+$/.test" in js
    assert "data-pilot-n" in js


def test_el_js_searches_fields_before_links():
    # Form fields must be concatenated before anchors so a query doesn't land
    # in a link.
    js = _el_js("email", "return 1;")
    fields_at = js.index("input, textarea, select")
    links_at = js.index("a[href], button, [role=button], [onclick]")
    assert fields_at < links_at


def test_page_ws_clamps_tab_index(monkeypatch):
    b = Browser()
    pages = [
        {"webSocketDebuggerUrl": "ws://one", "type": "page"},
        {"webSocketDebuggerUrl": "ws://two", "type": "page"},
    ]
    monkeypatch.setattr(b, "_targets", lambda: pages)
    assert b._page_ws(0) == "ws://one"     # 0 -> first
    assert b._page_ws(2) == "ws://two"     # exact
    assert b._page_ws(99) == "ws://two"    # over-range clamps to last
    assert b._page_ws(-5) == "ws://one"    # under-range clamps to first


def test_cdp_default_is_loopback():
    assert Browser().cdp.startswith("http://127.0.0.1")


def test_queries_walk_shadow_roots():
    """A page built from web components keeps its real <input> inside a shadow
    root, where document.querySelectorAll('input') finds nothing. Every query
    this driver generates has to walk open shadow roots or it reports an empty
    page on a form the user can plainly see."""
    assert "shadowRoot" in _LINKS_JS
    assert "deep(document," in _LINKS_JS
    js = _el_js("email", "return 1;")
    assert "shadowRoot" in js
    assert "deep(document, 'input, textarea, select')" in js


def test_numbered_lookup_also_walks_shadow_roots():
    """Numbering is useless if the number can only be found in the light DOM:
    links() can mark an element inside a shadow root that click() then cannot
    find."""
    js = _el_js("14", "return 1;")
    assert "deep(document, '[data-pilot-n=" in js


def test_type_measures_the_field_before_typing():
    """`type` clicks the field for real, which means it needs the box. The JS it
    generates must report coordinates and say NO BOX rather than guessing."""
    import inspect
    src = inspect.getsource(Browser.type)
    assert "Input.insertText" in src
    assert "Input.dispatchMouseEvent" in src
    assert "NO BOX" in src
    # and the setter idiom survives as the fallback, not the first move
    assert src.index("Input.insertText") < src.index("getOwnPropertyDescriptor")


def test_type_clears_with_a_real_backspace():
    """Clearing by assigning '' is the same untrusted write that made the
    typing fail in the first place."""
    import inspect
    src = inspect.getsource(Browser.type)
    assert "Backspace" in src
    assert "setSelectionRange" in src
