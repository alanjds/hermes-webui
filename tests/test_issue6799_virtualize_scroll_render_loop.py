"""Regression tests for #6799: virtualize_transcript scroll -> re-render feedback loop.

With "Virtualize long transcripts" (virtualize_transcript) enabled, a
programmatic scrollTop write made by a virtualized re-render's own
scroll-compensation logic re-entered the scroll listener before the
_programmaticScroll guard could block it: the `messages` element's
`scroll` listener in static/ui.js called `_scheduleMessageVirtualizedRender()`
BEFORE checking `_freshProgrammaticScrollActive()`. Compounding this, the
common (non-drag) branch of `_scheduleMessageVirtualizedRender()` never
wrote `_messageVirtualWindowKey` back after rendering, so the "nothing
changed, skip it" fast path (`nextKey===_messageVirtualWindowKey`) could
never fire for a scroll event landing outside the short guard window.
Together these produced an unbroken re-render loop (~5-6/sec while idle,
CPU 100%+, and broken transcript text selection since the DOM kept
getting wiped and rebuilt under the cursor).

The fix has two parts:
1. Check the _programmaticScroll guard BEFORE scheduling the virtualized
   render in the scroll listener, so a render's own scrollTop write
   cannot re-enter the scheduler while still marked programmatic.
2. Write _messageVirtualWindowKey back after the common (non-drag)
   branch renders, mirroring the pre-existing _scrollbarDragActive
   branch, so a subsequent scroll event with an unchanged window
   short-circuits instead of unconditionally re-rendering.
"""
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent.resolve()
UI_JS_PATH = REPO_ROOT / "static" / "ui.js"
UI_JS = UI_JS_PATH.read_text(encoding="utf-8")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node not on PATH")


def _run_node(source: str) -> str:
    with tempfile.NamedTemporaryFile(
        "w", suffix=".cjs", encoding="utf-8", dir=REPO_ROOT, delete=False
    ) as script:
        script.write(source)
        script_path = Path(script.name)
    try:
        result = subprocess.run(
            [NODE, str(script_path)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=30,
        )
    finally:
        script_path.unlink(missing_ok=True)
    if result.returncode != 0:
        raise RuntimeError(result.stderr)
    return result.stdout.strip()


def _extract_func_script(js: str) -> str:
    return f"""
const src = {js!r};
function extractFunc(name) {{
  const re = new RegExp('function\\\\s+' + name + '\\\\s*\\\\(');
  const start = src.search(re);
  if (start < 0) throw new Error(name + ' not found');
  let i = src.indexOf('{{', start);
  let depth = 1; i++;
  while (depth > 0 && i < src.length) {{
    if (src[i] === '{{') depth++;
    else if (src[i] === '}}') depth--;
    i++;
  }}
  return src.slice(start, i);
}}
"""


def _messages_scroll_listener_body() -> str:
    start = UI_JS.index("el.addEventListener('scroll',()=>{")
    end = UI_JS.index("});", start)
    return UI_JS[start:end]


def test_scroll_listener_checks_programmatic_guard_before_scheduling_virtualized_render():
    """The _programmaticScroll guard must run before _scheduleMessageVirtualizedRender()
    so a render's own scrollTop write cannot re-enter the virtualized-render
    trigger while still marked programmatic (#6799)."""
    body = _messages_scroll_listener_body()
    guard_idx = body.index("if(_freshProgrammaticScrollActive()) return;")
    schedule_idx = body.index("_scheduleMessageVirtualizedRender();")
    assert guard_idx < schedule_idx, (
        "_scheduleMessageVirtualizedRender() must be called AFTER the "
        "_freshProgrammaticScrollActive() guard, not before -- otherwise a "
        "programmatic scroll from the virtualized re-render's own scroll "
        "compensation re-triggers the scheduler before the guard can stop "
        "it (#6799)."
    )


def test_scheduled_virtualized_render_updates_window_key_so_repeat_calls_noop():
    """After the common (non-drag) branch renders, _messageVirtualWindowKey
    must be updated to match what was just rendered -- mirroring the
    pre-existing _scrollbarDragActive branch -- so that a second call with
    an unchanged window short-circuits at the top-level guard instead of
    scheduling another render (#6799)."""
    js = UI_JS_PATH.read_text(encoding="utf-8")
    source = _extract_func_script(js) + """
let _messageVirtualScrollRaf = 0;
let _messageVirtualWindowKey = '';
let _msgNodeRecycleEnabled = false;
let _scrollbarDragActive = false;
let renderCalls = 0;
function $(id){ return (id === 'messages' || id === 'msgInner') ? {} : null; }
function _getVisibleMessagesWithIdx(){ return []; }
function _messageVirtualKeepTailCount(){ return 50; }
// A fixed virtualized window: the "window key" a real scroll-driven
// re-render would recompute after settling on an unchanged scroll position.
function _currentMessageVirtualWindow(){ return {virtualized: true, start: 0, end: 10, tailStart: 0}; }
function _messageVirtualWindowKeyFor(w){ return 'fixed-window-key'; }
function renderMessages(opts){ renderCalls++; }
function _compensateScrollForMeasurementDelta(fn){ fn(); }
function requestAnimationFrame(fn){ fn(); return 1; }

eval(extractFunc('_scheduleMessageVirtualizedRender'));

_scheduleMessageVirtualizedRender();
const afterFirst = renderCalls;
const keyAfterFirst = _messageVirtualWindowKey;

// Second call with an IDENTICAL window (nothing actually changed) --
// simulates the render-induced scroll event that re-enters the listener.
_scheduleMessageVirtualizedRender();
const afterSecond = renderCalls;

console.log(JSON.stringify({afterFirst, keyAfterFirst, afterSecond}));
"""
    metrics = json.loads(_run_node(source))
    assert metrics["afterFirst"] == 1, "first call must render once (window is new)"
    assert metrics["keyAfterFirst"] == "fixed-window-key", (
        "_messageVirtualWindowKey must be updated to the rendered window's key "
        "after the common (non-drag) branch renders (#6799)"
    )
    assert metrics["afterSecond"] == 1, (
        "a second call with an unchanged window must be a no-op (no additional "
        "render) -- if _messageVirtualWindowKey is never updated after a "
        "render, this guard can never fire and every scroll event re-renders "
        "unconditionally, producing the #6799 feedback loop"
    )
