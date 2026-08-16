"""Regression coverage for desktop content-visibility (perf: long-conversation
scroll/paint cost, task description "Step 2").

User rows get `content-visibility: auto` on desktop unconditionally, mirroring
the already-shipped touch-device rule (style.css, `@media (pointer: coarse)`).
Assistant turns get the same treatment but gated behind a default-off
`cv-assistant-desktop` class on `<html>`, set only via
`window._setDesktopAssistantContentVisibility(true)` — see the KNOWN OPEN RISK
comment in static/ui.js above `_assistantRowIntrinsicHeightBySessionIdx` for
why this stays opt-in pending real-browser scroll-jump testing.

These tests cover (a) the CSS gating structure via source-text assertions,
consistent with this repo's existing CSS-testing convention (see
test_css_tooltips.py), and (b) the JS remembered-height/flag mechanics via
real execution under Node, extracted from static/ui.js.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from tests.js_source_extract import extract_function

ROOT = Path(__file__).resolve().parents[1]
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
STYLE_CSS = (ROOT / "static" / "style.css").read_text(encoding="utf-8")
NODE = shutil.which("node")


# ---------------------------------------------------------------------------
# CSS structure
# ---------------------------------------------------------------------------

def _desktop_media_block() -> str:
    marker = "@media (hover: hover) and (pointer: fine) {"
    start = STYLE_CSS.index(marker)
    depth = 0
    i = STYLE_CSS.index("{", start)
    j = i
    while j < len(STYLE_CSS):
        if STYLE_CSS[j] == "{":
            depth += 1
        elif STYLE_CSS[j] == "}":
            depth -= 1
            if depth == 0:
                return STYLE_CSS[start:j + 1]
        j += 1
    raise AssertionError("desktop content-visibility media block did not close")


def test_desktop_user_rows_get_content_visibility_unconditionally():
    block = _desktop_media_block()
    assert re.search(
        r'\.msg-row\[data-role="user"\]\s*\{[^}]*content-visibility:\s*auto',
        block,
    ), "desktop user rows must get content-visibility:auto unconditionally"


def test_desktop_assistant_rows_gated_behind_flag_class():
    block = _desktop_media_block()
    assert re.search(
        r'html\.cv-assistant-desktop\s+\.msg-row\.assistant-turn\s*\{[^}]*content-visibility:\s*auto',
        block,
    ), "assistant turns must only get content-visibility:auto under html.cv-assistant-desktop"
    # Must NOT be unconditional — i.e. no bare `.msg-row.assistant-turn {
    # content-visibility: auto }` selector outside the flag gate.
    assert not re.search(
        r'(?<!cv-assistant-desktop )(?<!cv-assistant-desktop)\n?\s*\.msg-row\.assistant-turn\s*\{[^}]*content-visibility:\s*auto',
        block,
    )


def test_desktop_block_keeps_live_turn_force_visible():
    block = _desktop_media_block()
    assert "#liveAssistantTurn" in block
    assert re.search(r'content-visibility:\s*visible', block)


def test_mobile_touch_block_unchanged_by_desktop_addition():
    # The pre-existing touch rule (@media (pointer: coarse)) must still exist,
    # unmodified in intent, alongside the new desktop block.
    assert "@media (pointer: coarse) {" in STYLE_CSS
    touch_start = STYLE_CSS.index("@media (pointer: coarse) {")
    desktop_start = STYLE_CSS.index("@media (hover: hover) and (pointer: fine) {")
    assert touch_start < desktop_start, "desktop block must come after the touch block, not replace it"


# ---------------------------------------------------------------------------
# JS mechanics (executed under Node)
# ---------------------------------------------------------------------------

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


class _FakeClassList:
    def __init__(self):
        self._classes = set()

    def toggle(self, name, force=None):
        pass  # replaced by JS Set-backed version in the driver; unused in Python


_ASSISTANT_HEIGHT_FUNCS = [
    "_clearAssistantRowIntrinsicHeightCache",
    "_rememberAssistantRowIntrinsicHeight",
    "_applyAssistantRowIntrinsicHeight",
]


def _assistant_height_block() -> str:
    start = UI_JS.index("const _assistantRowIntrinsicHeightBySessionIdx=Object.create(null);")
    last_fn = extract_function(UI_JS, "_setDesktopAssistantContentVisibility")
    end = UI_JS.index(last_fn) + len(last_fn)
    block = UI_JS[start:end]
    for required in (
        "const _assistantRowIntrinsicHeightBySessionIdx",
        "function _clearAssistantRowIntrinsicHeightCache()",
        "function _rememberAssistantRowIntrinsicHeight(sessionMsgIdx, height)",
        "function _applyAssistantRowIntrinsicHeight(row, sessionMsgIdx)",
        "function _rememberRenderedAssistantRowIntrinsicHeights()",
        "function _applyDesktopAssistantContentVisibilityFlag()",
        "function _setDesktopAssistantContentVisibility(enabled)",
    ):
        assert required in block, f"{required!r} missing from extracted assistant-row block"
    return block


def _driver(body: str) -> str:
    return f"""
'use strict';
const MESSAGE_VIRTUAL_DEFAULT_ROW_HEIGHTS = {{
  user: 120, process_wakeup: 96, assistant: 160, tool_call: 400, default: 140,
}};

class FakeClassList {{
  constructor() {{ this._set = new Set(); }}
  toggle(name, force) {{
    const has = this._set.has(name);
    const want = force === undefined ? !has : !!force;
    if (want) this._set.add(name); else this._set.delete(name);
    return want;
  }}
  contains(name) {{ return this._set.has(name); }}
}}
const htmlEl = {{ classList: new FakeClassList() }};
globalThis.document = {{ documentElement: htmlEl }};
globalThis.window = globalThis;

function $(id) {{ return globalThis.__els[id] || null; }}

{_assistant_height_block()}

{body}
"""


def test_flag_setter_toggles_html_class():
    script = _driver("""
_setDesktopAssistantContentVisibility(true);
const onAfterEnable = document.documentElement.classList.contains('cv-assistant-desktop');
_setDesktopAssistantContentVisibility(false);
const onAfterDisable = document.documentElement.classList.contains('cv-assistant-desktop');
console.log(JSON.stringify({onAfterEnable, onAfterDisable}));
""")
    result = json.loads(_run_node(script))
    assert result["onAfterEnable"] is True
    assert result["onAfterDisable"] is False


def test_flag_defaults_off():
    script = _driver("""
_applyDesktopAssistantContentVisibilityFlag();
console.log(JSON.stringify({
  flag: !!window._desktopAssistantContentVisibility,
  cls: document.documentElement.classList.contains('cv-assistant-desktop'),
}));
""")
    result = json.loads(_run_node(script))
    assert result["flag"] is False
    assert result["cls"] is False


def test_apply_height_falls_back_to_role_default_when_unmeasured():
    script = _driver("""
const row = { style: {} };
_applyAssistantRowIntrinsicHeight(row, 7);
console.log(JSON.stringify({containIntrinsicSize: row.style.containIntrinsicSize}));
""")
    result = json.loads(_run_node(script))
    assert result["containIntrinsicSize"] == "auto 160px"


def test_apply_height_uses_remembered_measurement_when_larger():
    script = _driver("""
_rememberAssistantRowIntrinsicHeight(7, 4200);
const row = { style: {} };
_applyAssistantRowIntrinsicHeight(row, 7);
console.log(JSON.stringify({containIntrinsicSize: row.style.containIntrinsicSize}));
""")
    result = json.loads(_run_node(script))
    assert result["containIntrinsicSize"] == "auto 4200px"


def test_remembered_height_never_regresses_below_role_default():
    script = _driver("""
_rememberAssistantRowIntrinsicHeight(7, 30);  // smaller than the 160px role default
const row = { style: {} };
_applyAssistantRowIntrinsicHeight(row, 7);
console.log(JSON.stringify({containIntrinsicSize: row.style.containIntrinsicSize}));
""")
    result = json.loads(_run_node(script))
    assert result["containIntrinsicSize"] == "auto 160px"


def test_clear_cache_removes_remembered_heights():
    script = _driver("""
_rememberAssistantRowIntrinsicHeight(7, 4200);
_clearAssistantRowIntrinsicHeightCache();
const row = { style: {} };
_applyAssistantRowIntrinsicHeight(row, 7);
console.log(JSON.stringify({containIntrinsicSize: row.style.containIntrinsicSize}));
""")
    result = json.loads(_run_node(script))
    assert result["containIntrinsicSize"] == "auto 160px", "cleared cache must not leave a stale remembered height"


def _fake_row(role_data, rect, dom_id=""):
    return f"""{{
      id: {json.dumps(dom_id)},
      dataset: {json.dumps(role_data)},
      style: {{}},
      getBoundingClientRect() {{ return {json.dumps(rect)}; }},
    }}"""


def test_remember_rendered_skips_out_of_view_rows_and_live_turn():
    # container: viewport is y in [0, 600]. margin = container height (600),
    # so "in view" spans roughly [-600, 1200].
    in_view_row = _fake_row({"sessionMsgIdx": "3"}, {"top": 100, "bottom": 500, "height": 400})
    far_off_row = _fake_row({"sessionMsgIdx": "9"}, {"top": 5000, "bottom": 8000, "height": 3000})
    live_row = _fake_row({"sessionMsgIdx": "11"}, {"top": 100, "bottom": 900, "height": 800}, dom_id="liveAssistantTurn")
    script = _driver(f"""
globalThis.__els = {{
  messages: {{ getBoundingClientRect() {{ return {{top:0,bottom:600,height:600}}; }} }},
  msgInner: {{
    querySelectorAll(sel) {{
      if (sel === '.msg-row.assistant-turn[data-session-msg-idx]') {{
        return [{in_view_row}, {far_off_row}, {live_row}];
      }}
      return [];
    }},
  }},
}};
_rememberRenderedAssistantRowIntrinsicHeights();
console.log(JSON.stringify({{
  inViewRemembered: _assistantRowIntrinsicHeightBySessionIdx[3] || null,
  outOfViewRemembered: _assistantRowIntrinsicHeightBySessionIdx[9] || null,
  liveTurnRemembered: _assistantRowIntrinsicHeightBySessionIdx[11] || null,
}}));
""")
    result = json.loads(_run_node(script))
    assert result["inViewRemembered"] == 400
    assert result["outOfViewRemembered"] is None, "an out-of-view row must not be persisted (unreliable measurement)"
    assert result["liveTurnRemembered"] is None, "the live streaming turn must never be persisted mid-stream"


def test_remember_rendered_floors_at_role_default():
    tiny_in_view_row = _fake_row({"sessionMsgIdx": "5"}, {"top": 10, "bottom": 40, "height": 30})
    script = _driver(f"""
globalThis.__els = {{
  messages: {{ getBoundingClientRect() {{ return {{top:0,bottom:600,height:600}}; }} }},
  msgInner: {{
    querySelectorAll(sel) {{
      if (sel === '.msg-row.assistant-turn[data-session-msg-idx]') return [{tiny_in_view_row}];
      return [];
    }},
  }},
}};
_rememberRenderedAssistantRowIntrinsicHeights();
console.log(JSON.stringify({{remembered: _assistantRowIntrinsicHeightBySessionIdx[5]}}));
""")
    result = json.loads(_run_node(script))
    assert result["remembered"] == 160, "a measured height below the role default must still floor at the role default"


def test_new_call_sites_wired_into_render_messages():
    assert "if(typeof _rememberRenderedAssistantRowIntrinsicHeights==='function') _rememberRenderedAssistantRowIntrinsicHeights();" in UI_JS
    assert "if(typeof _applyAssistantRowIntrinsicHeight==='function') _applyAssistantRowIntrinsicHeight(currentAssistantTurn, currentAssistantTurn.dataset.sessionMsgIdx);" in UI_JS
    assert "currentAssistantTurn.dataset.sessionMsgIdx=_messageSessionIndexForRawIdx(rawIdx);" in UI_JS


def test_session_switch_clears_assistant_height_cache_alongside_user_cache():
    fn = extract_function(UI_JS, "_clearMessageVirtualHeightCache")
    assert "_clearUserRowIntrinsicHeightCache" in fn
    assert "_clearAssistantRowIntrinsicHeightCache" in fn, (
        "the assistant remembered-height cache must be cleared on the same "
        "reset path as the user-row cache, or stale heights from a prior "
        "session could leak into a new one via colliding sessionMsgIdx keys"
    )
