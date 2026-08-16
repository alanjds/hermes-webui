"""Real-browser behavior tests for the flag-gated marked.js + DOMPurify
renderer (Step 3a — ARCHITECTURE.md §5.4/§9/§10, bug B8, roadmap Phase E).

`renderMdViaMarked()` (static/ui.js) is a from-scratch renderer alongside the
existing hand-rolled `renderMd()`, default OFF (`window._useMarkedRenderer`).
Since it depends on two real third-party libraries and a real DOM (DOMPurify
requires one — it cannot run under plain Node the way the regex-only
`renderMd()` tests do), these tests drive it in an actual headless Chromium
page via Playwright, loading the exact vendored library builds
(tests/vendor/marked.umd.js, tests/vendor/purify.min.js — see
tests/vendor/README.md) alongside the real functions extracted from
static/ui.js, so the assertions exercise the shipped implementation rather
than a re-typed mirror of it.

Gracefully skips (does not fail) when playwright isn't installed or no
launchable Chromium is available — same posture as tests/browser_smoke.py.
Set PLAYWRIGHT_CHROMIUM_EXECUTABLE to point at a specific browser binary if
the environment's pre-installed Chromium doesn't match the installed
playwright package's expected build (this sandbox needs it; a normal
`playwright install` environment does not).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

try:
    from playwright.sync_api import sync_playwright
    HAS_PLAYWRIGHT = True
except ImportError:
    HAS_PLAYWRIGHT = False

pytestmark = pytest.mark.skipif(not HAS_PLAYWRIGHT, reason="playwright not installed")

ROOT = Path(__file__).resolve().parents[1]
UI_JS = (ROOT / "static" / "ui.js").read_text(encoding="utf-8")
MARKED_JS = (ROOT / "tests" / "vendor" / "marked.umd.js").read_text(encoding="utf-8")
PURIFY_JS = (ROOT / "tests" / "vendor" / "purify.min.js").read_text(encoding="utf-8")


def _extract_function(src: str, name: str) -> str:
    marker = f"function {name}("
    start = src.find(marker)
    assert start >= 0, f"{name} not found in static/ui.js"
    brace = src.find("{", start)
    depth = 1
    i = brace + 1
    while depth > 0 and i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
        i += 1
    return src[start:i]


# Functions renderMdViaMarked() needs, in dependency order. Mirrors exactly
# what a real page load provides (these are all real top-level functions in
# static/ui.js — nothing here is a test-only stand-in).
_FUNCS = [
    "_isBacktickFenceClose", "_matchBacktickFenceLine",
    "_isSafeDataImageUri",
    "_fencedCodeBlockHtml",
    "_safeAttrValue", "_markdownHref", "_isInternalSessionHref", "_isSafeUrl",
    "renderMd",
    "_installMarkedDomPurifyHooks", "_markedDomPurifyConfig", "_getMarkedInstance",
    "_stashMathForMarked", "_restoreMathStash", "renderMdViaMarked",
]
_CONSTS = ["_DATA_IMAGE_RE", "_DATA_IMAGE_SVG_RE", "_DATA_IMAGE_MAX_LEN"]


def _extract_esc() -> str:
    # esc() is a one-line const arrow function, not `function name(...)` —
    # extracted directly from the real source (line-anchored) rather than
    # hand-retyped, since its body is a JS object literal mixing single- and
    # double-quoted string keys that's easy to mis-escape when copied by hand.
    m = re.search(r"^const esc=.*$", UI_JS, re.MULTILINE)
    assert m, "esc() not found in static/ui.js"
    return m.group(0)


def _glue_js() -> str:
    pieces = [
        _extract_esc(),
        "window._sessionUrlForSid = function(sid) { return '/app/session/' + encodeURIComponent(String(sid||'')); };",
        "let _markedInstance=null;",
        "let _markedDomPurifyHooksInstalled=false;",
    ]
    for c in _CONSTS:
        m = re.search(rf"const {re.escape(c)}\s*=.*", UI_JS)
        assert m, f"{c} not found in static/ui.js"
        pieces.append(m.group(0))
    for fn in _FUNCS:
        pieces.append(_extract_function(UI_JS, fn))
    pieces.append("window.__marked = function(text) { return renderMdViaMarked(text); };")
    pieces.append("window.__old = function(text) { return renderMd(text); };")
    return "\n".join(pieces)


@pytest.fixture(scope="module")
def page(tmp_path_factory):
    # Loaded as external <script src> files (page.goto against a real
    # file:// URL), not inlined into an HTML string via set_content()/
    # document.write() — the vendored minified libraries can contain
    # sequences (e.g. inside a string literal or regex) that an HTML
    # tokenizer would misread as closing an inline <script> tag early, since
    # document.write()-based injection HTML-parses everything including
    # script bodies. External files are fetched as raw text instead, which
    # sidesteps that class of embedding bug entirely.
    harness_dir = tmp_path_factory.mktemp("marked_renderer_harness")
    (harness_dir / "marked.umd.js").write_text(MARKED_JS, encoding="utf-8")
    (harness_dir / "purify.min.js").write_text(PURIFY_JS, encoding="utf-8")
    (harness_dir / "glue.js").write_text(_glue_js(), encoding="utf-8")
    html = (
        '<!doctype html><html><head><meta charset="utf-8"></head><body>'
        '<script src="marked.umd.js"></script>'
        '<script src="purify.min.js"></script>'
        '<script src="glue.js"></script>'
        "</body></html>"
    )
    (harness_dir / "index.html").write_text(html, encoding="utf-8")

    pw = sync_playwright().start()
    try:
        kwargs = {"headless": True}
        exe = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE")
        if exe:
            kwargs["executable_path"] = exe
        browser = pw.chromium.launch(**kwargs)
    except Exception as exc:  # noqa: BLE001 - environment probe, not a real failure
        pw.stop()
        pytest.skip(f"no launchable Chromium available: {exc}")
    pg = browser.new_page()
    errors = []
    pg.on("pageerror", lambda exc: errors.append(str(exc)))
    pg.goto((harness_dir / "index.html").as_uri())
    if errors:
        browser.close()
        pw.stop()
        pytest.fail(f"harness page threw on load: {errors}")
    yield pg
    browser.close()
    pw.stop()


def render(page, text: str) -> str:
    return page.evaluate("(t) => window.__marked(t)", text)


def render_old(page, text: str) -> str:
    return page.evaluate("(t) => window.__old(t)", text)


class TestParityWithRenderMd:
    """Cases where the two renderers should agree exactly (or up to
    insignificant inter-tag whitespace)."""

    def test_headings_bold_italic(self, page):
        md = "# Hello\n\nThis is **bold** and *italic* text."
        # marked terminates block-level output with a trailing newline;
        # renderMd() doesn't. Insignificant for HTML — strip before compare.
        assert render(page, md).strip() == render_old(page, md).strip()

    def test_mermaid_block_shares_helper_and_matches_shape(self, page):
        md = "```mermaid\ngraph TD\nA-->B\n```"
        out = render(page, md)
        assert 'class="mermaid-block"' in out
        assert re.search(r'data-mermaid-id="mermaid-[a-z0-9]+"', out)
        assert "graph TD" in out and "A--&gt;B" in out

    def test_diff_block_exact_match(self, page):
        md = "```diff\n@@ -1,2 +1,2 @@\n-old\n+new\n```"
        assert render(page, md) == render_old(page, md)

    def test_json_tree_block_same_content_not_wrapped_in_paragraph(self, page):
        md = '```json\n{"a": 1}\n```'
        out = render(page, md)
        assert 'class="code-tree-wrap"' in out
        assert 'data-lang="json"' in out
        assert not out.strip().startswith("<p>"), "block content must not be wrapped in <p> (renderMd() has the same exclusion)"

    def test_csv_block_renders_table(self, page):
        md = "```csv\na,b\n1,2\n3,4\n```"
        out = render(page, md)
        assert 'class="csv-table"' in out
        assert "<td>1</td>" in out and "<td>2</td>" in out

    def test_katex_inline_delimiter(self, page):
        md = "The value is $x^2 + 1$ here."
        out = render(page, md)
        assert '<span class="katex-inline" data-katex="inline">x^2 + 1</span>' in out

    def test_katex_display_delimiter_not_wrapped_in_paragraph(self, page):
        md = "Equation:\n\n$$E = mc^2$$"
        out = render(page, md)
        assert '<div class="katex-block" data-katex="display">E = mc^2</div>' in out
        assert "<p><div" not in out, "display math must not be nested inside <p> (invalid HTML renderMd() avoids)"

    def test_dollar_signs_inside_code_fence_are_not_treated_as_math(self, page):
        md = "```bash\necho $HOME $1\n```"
        out = render(page, md)
        assert "katex" not in out
        assert "$HOME $1" in out

    def test_workspace_link_rewritten_to_hash_anchor(self, page):
        md = "[open file](workspace://src/main.py)"
        out = render(page, md)
        assert 'href="#workspace=src%2Fmain.py"' in out

    def test_session_link_rewritten_to_internal_anchor(self, page):
        md = "[go](session://abc123)"
        out = render(page, md)
        assert 'class="session-link"' in out
        assert 'href="/app/session/abc123"' in out
        assert "target=" not in out.split("</a>")[0].split("<a")[-1] or "target=\"_blank\"" not in out

    def test_https_link_gets_target_blank(self, page):
        md = "[site](https://example.com)"
        out = render(page, md)
        assert 'href="https://example.com"' in out
        assert 'target="_blank"' in out
        assert 'rel="noopener"' in out

    def test_autolink_bare_url(self, page):
        md = "Visit https://example.com/page now."
        out = render(page, md)
        assert 'href="https://example.com/page"' in out

    def test_strikethrough(self, page):
        assert render(page, "~~gone~~") == "<p><del>gone</del></p>\n"

    def test_blockquote_single_newline_becomes_br(self, page):
        md = "> quoted **text**\n> more"
        out = render(page, md)
        assert "quoted <strong>text</strong><br>more" in out

    def test_multiline_paragraph_single_newline_becomes_br(self, page):
        """renderMd()'s paragraph-wrap step turns every remaining single
        newline into <br> (chat-style, not CommonMark's soft-break-collapses-
        to-a-space default) — marked must be configured (breaks:true) to
        match, or ordinary multi-line LLM output would visibly reflow."""
        md = "line one\nline two"
        out = render(page, md)
        assert "<br>" in out

    def test_data_image_png_renders_inline_with_media_class(self, page):
        md = "![alt](data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=)"
        assert render(page, md).strip() == render_old(page, md).strip()


class TestDeliberateImprovementsOverRenderMd:
    """Cases where marked's output differs from renderMd() on purpose —
    documented, known gaps in the old regex renderer (ARCHITECTURE.md §5.4:
    "Nested lists: single regex pass, multi-level indentation not handled")
    that marked's real CommonMark/GFM parser fixes as a side effect of the
    replacement, not something this renderer set out to preserve."""

    def test_nested_lists_render_as_true_nested_structure(self, page):
        md = "- a\n  - b\n    - c\n- d"
        out = render(page, md)
        # True nesting: an inner <ul> inside the <li> it belongs to, not a
        # flat sibling with a margin-left style hack (renderMd()'s approach).
        assert out.count("<ul>") >= 2
        assert 'style="margin-left' not in out

    def test_task_list_renders_real_checkbox_input(self, page):
        md = "- [ ] todo\n- [x] done"
        out = render(page, md)
        assert '<input disabled="" type="checkbox">' in out
        assert '<input checked="" disabled="" type="checkbox">' in out


class TestSanitizationAtLeastAsStrict:
    """DOMPurify replaces the old SAFE_TAGS/_tag() allowlist entirely for
    this renderer (task requirement: "confirm the resulting sanitization is
    at least as strict"). These assert no script ever survives to execute,
    matching or exceeding renderMd()'s own guarantees for the same inputs."""

    def test_script_tag_is_stripped(self, page):
        out = render(page, "Hello <script>alert(1)</script> world")
        assert "<script" not in out
        assert "</script>" not in out

    def test_img_onerror_attribute_is_stripped(self, page):
        out = render(page, '<img src=x onerror="alert(1)">')
        assert "onerror" not in out

    def test_javascript_href_is_stripped(self, page):
        out = render(page, "[click me](javascript:alert(1))")
        assert "javascript:" not in out

    def test_vbscript_href_is_stripped(self, page):
        out = render(page, "[click me](vbscript:msgbox(1))")
        assert "vbscript:" not in out

    def test_data_text_html_is_not_treated_as_safe_image(self, page):
        out = render(page, '<img src="data:text/html;base64,PHNjcmlwdD5hbGVydCgxKTwvc2NyaXB0Pg==">')
        assert "data:text/html" not in out

    def test_svg_data_uri_without_base64_is_rejected(self, page):
        """_isSafeDataImageUri requires base64 encoding even for SVG — a raw
        (non-base64) data:image/svg+xml URI can carry literal <script> markup
        and must not pass through as a "safe image"."""
        out = render(page, '<img src="data:image/svg+xml,<svg onload=alert(1)>">')
        assert "onload" not in out
        assert "<svg" not in out

    def test_event_handler_attribute_on_allowed_tag_is_stripped(self, page):
        out = render(page, '<p onclick="alert(1)">hi</p>')
        assert "onclick" not in out

    def test_iframe_is_not_in_allowed_tags(self, page):
        out = render(page, '<iframe src="https://evil.example"></iframe>')
        assert "<iframe" not in out


class TestFlagGating:
    """window._useMarkedRenderer defaults off; only renderMdViaMarked() calls
    itself use the new pipeline. This module doesn't load boot.js, so it
    can't exercise _getCachedRender()'s dispatch directly — that's covered
    by tests/test_render_cache_lru_hash.py's key-prefix test — but confirms
    the source wiring is present and the fallback path is real."""

    def test_source_wires_flag_into_render_cache_key(self):
        assert "window._useMarkedRenderer ? 'm' : ''" in UI_JS

    def test_source_dispatches_to_marked_only_when_flag_is_set(self):
        assert "const useMarked = !!window._useMarkedRenderer;" in UI_JS
        assert "useMarked ? renderMdViaMarked(text) : renderMd(text)" in UI_JS

    def test_renderer_falls_back_to_renderMd_when_marked_unavailable(self, page):
        # Simulate the defer-race / library-unavailable case directly rather
        # than reloading the page without the library scripts.
        out = page.evaluate("""() => {
            const saved = window.marked;
            window.marked = undefined;
            try { return renderMdViaMarked('**hi**'); }
            finally { window.marked = saved; }
        }""")
        assert out == "<p><strong>hi</strong></p>\n"
