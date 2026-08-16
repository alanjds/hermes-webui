"""Regression coverage for the per-message render cache hardening.

`_renderCache` (static/ui.js) memoizes `renderMd()`/`_renderUserFencedBlocks()`
output keyed by message content so `renderMessages()` doesn't re-run the
regex-based markdown pipeline for messages that haven't changed. Two gaps in
the original implementation are covered here:

1. Long-message cache keys were sampled (`length + first-20 + last-20 chars`)
   instead of hashed, so two distinct long messages with identical length and
   matching head/tail could collide and silently serve each other's rendered
   HTML.
2. Eviction cleared the *entire* cache once it crossed a fixed size, instead
   of evicting only the least-recently-used entry, which thrashes on long or
   multi-session conversations.

Both are fixed in `static/ui.js`'s `_renderCache`/`_renderCacheKey`/
`_getCachedRender`. These tests extract the real implementation out of
`static/ui.js` and execute it under Node so the assertions exercise actual
behavior, not just source-text presence.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from tests.js_source_extract import extract_function

ROOT = Path(__file__).resolve().parents[1]
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _cache_block() -> str:
    """The full render-cache implementation: the two consts plus all four
    functions, extracted from static/ui.js in source order so the test
    exercises the exact shipped code rather than a re-typed copy."""
    start = UI_JS.index("const _renderCache = new Map();")
    assert start != -1, "_renderCache declaration not found in static/ui.js"
    # Everything through the end of _getCachedRender's closing brace.
    get_cached_render = extract_function(UI_JS, "_getCachedRender")
    end = UI_JS.index(get_cached_render) + len(get_cached_render)
    block = UI_JS[start:end]
    for required in (
        "const _renderCache = new Map();",
        "function _clearRenderCache()",
        "function _fnv1aHash(str)",
        "function _renderCacheKey(text, isUser)",
        "function _getCachedRender(text, isUser)",
    ):
        assert required in block, f"{required!r} missing from extracted cache block"
    return block


def _run_node(script: str) -> str:
    if not NODE:
        pytest.skip("node executable is required for JavaScript behavior checks")
    with tempfile.NamedTemporaryFile("w", suffix=".js", encoding="utf-8", delete=False) as handle:
        handle.write(script)
        script_path = handle.name
    try:
        result = subprocess.run(
            [NODE, script_path],
            cwd=ROOT,
            text=True,
            capture_output=True,
            timeout=10,
        )
    finally:
        Path(script_path).unlink(missing_ok=True)
    if result.returncode:
        pytest.fail(
            "node behavior check failed"
            f"\nexit code: {result.returncode}"
            f"\nstdout:\n{result.stdout or '<empty>'}"
            f"\nstderr:\n{result.stderr or '<empty>'}",
        )
    return result.stdout.strip()


def _driver(body: str) -> str:
    """Wrap the extracted cache block with minimal stand-ins for its three
    external dependencies (renderMd/_renderUserFencedBlocks/
    _stripXmlToolCallsDisplay/window) plus a call counter so tests can
    assert on cache hits vs. real (re)renders."""
    return f"""
'use strict';
globalThis.window = globalThis;
let renderCalls = 0;
function renderMd(text) {{ renderCalls += 1; return 'MD:' + text; }}
function _renderUserFencedBlocks(text) {{ renderCalls += 1; return 'FENCED:' + text; }}
function _stripXmlToolCallsDisplay(text) {{ return text; }}

{_cache_block()}

{body}
"""


def test_long_messages_with_matching_head_and_tail_do_not_collide():
    """Two distinct long assistant messages of identical length whose first
    and last 20 characters match must render independently — the sampled
    key this replaces would have collided and served one message's HTML for
    the other."""
    head = "A" * 20
    tail = "Z" * 20
    middle_a = "one" + "x" * 600
    middle_b = "two" + "y" * 600
    msg_a = head + middle_a + tail
    msg_b = head + middle_b + tail
    assert len(msg_a) == len(msg_b) and msg_a[:20] == msg_b[:20] == head
    assert msg_a[-20:] == msg_b[-20:] == tail
    assert msg_a != msg_b

    script = _driver(f"""
const a = _getCachedRender({json.dumps(msg_a)}, false);
const b = _getCachedRender({json.dumps(msg_b)}, false);
console.log(JSON.stringify({{a, b, distinct: a !== b, renderCalls}}));
""")
    result = json.loads(_run_node(script))
    assert result["distinct"], "long messages sharing head/tail must not collide on cache key"
    assert result["a"] == "MD:" + msg_a
    assert result["b"] == "MD:" + msg_b
    assert result["renderCalls"] == 2


def test_repeated_render_of_same_message_is_a_cache_hit():
    script = _driver("""
_getCachedRender('hello world', false);
_getCachedRender('hello world', false);
_getCachedRender('hello world', false);
console.log(JSON.stringify({renderCalls}));
""")
    result = json.loads(_run_node(script))
    assert result["renderCalls"] == 1, "identical content must render once and hit cache thereafter"


def test_edited_message_is_not_served_stale_cached_html():
    """Simulates submitEdit()/regenerateResponse() truncating S.messages and
    re-sending different text for the same transcript position (#edit-
    invalidation). Because the key is content-derived, the new text must
    never read back the old rendering."""
    script = _driver("""
const before = _getCachedRender('original message text', false);
const after = _getCachedRender('edited message text', false);
console.log(JSON.stringify({before, after, distinct: before !== after}));
""")
    result = json.loads(_run_node(script))
    assert result["distinct"]
    assert result["before"] == "MD:original message text"
    assert result["after"] == "MD:edited message text"


def test_eviction_is_lru_not_clear_all():
    """Filling the cache past its cap must evict only the single
    least-recently-used entry, not discard the whole working set. Re-touch
    the first entry to make it recently-used, then overflow by one more —
    the touched entry must survive while an untouched one is evicted."""
    driver = _driver("""
// Fill to exactly the cap.
for (let i = 0; i < _renderCacheMax; i++) {
  _getCachedRender('msg-' + i, false);
}
const sizeAtCap = _renderCache.size;
// Touch the oldest entry (msg-0) so it becomes most-recently-used.
_getCachedRender('msg-0', false);
const rendersAfterTouch = renderCalls;
// One more distinct entry pushes the cache over the cap. This is itself a
// genuine miss, so it costs one render call — capture the count *after* it
// as the baseline for the two rechecks below.
_getCachedRender('msg-overflow', false);
const sizeAfterOverflow = _renderCache.size;
const rendersAfterOverflow = renderCalls;
// msg-0 was just touched -> must still be a hit (no new render call).
_getCachedRender('msg-0', false);
const rendersAfterMsg0Recheck = renderCalls;
// msg-1 was never touched -> should have been the one evicted.
_getCachedRender('msg-1', false);
const rendersAfterMsg1Recheck = renderCalls;
console.log(JSON.stringify({
  sizeAtCap, sizeAfterOverflow,
  msg0StayedCached: rendersAfterMsg0Recheck === rendersAfterOverflow,
  msg1WasEvicted: rendersAfterMsg1Recheck === rendersAfterOverflow + 1,
}));
""")
    result = json.loads(_run_node(driver))
    assert result["sizeAtCap"] == 500
    # Eviction happens on the entry that pushes size to cap+1, so size never
    # exceeds the cap.
    assert result["sizeAfterOverflow"] == 500
    assert result["msg0StayedCached"], "recently-touched entry must survive eviction"
    assert result["msg1WasEvicted"], "untouched oldest entry must be the one evicted"


def test_no_new_dependency_and_names_preserved():
    """The two existing call sites (renderMessages()'s user-row and
    assistant-segment loops) and clearMessageRenderCache() call
    _getCachedRender()/_clearRenderCache() by these exact names — the
    hardening must not rename or change the call signature of either."""
    assert "_getCachedRender(displayContent, isUser)" in UI_JS
    assert "_getCachedRender(partDisplayText,false);" in UI_JS
    assert "function _clearRenderCache(){ _renderCache.clear(); }" in UI_JS
