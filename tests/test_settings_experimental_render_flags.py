"""Regression coverage for the two experimental, opt-in render-performance
settings added alongside the perf work in ARCHITECTURE.md Section 5.4:

- desktop_assistant_content_visibility: content-visibility:auto for
  assistant rows on desktop (see the KNOWN OPEN RISK note in static/ui.js
  above _assistantRowIntrinsicHeightBySessionIdx).
- use_marked_renderer: the marked.js + DOMPurify renderer (bug B8).

Both default OFF, are exposed as "(experimental)"-labeled checkboxes in the
Preferences settings pane (mirroring the existing virtualize_transcript
toggle's UX exactly — see tests/test_issue4325_virtualization_toggle.py),
persist server-side, and are applied both at boot (so the very first render
after a reload honors the saved preference) and hot-applied when toggled
from the open Settings panel.
"""
import json
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
INDEX = REPO_ROOT / "static" / "index.html"
PANELS = REPO_ROOT / "static" / "panels.js"
BOOT = REPO_ROOT / "static" / "boot.js"
UI = REPO_ROOT / "static" / "ui.js"
I18N = REPO_ROOT / "static" / "i18n.js"
CONFIG = REPO_ROOT / "api" / "config.py"

SETTINGS = [
    {
        "key": "desktop_assistant_content_visibility",
        "checkbox_id": "settingsDesktopAssistantContentVisibility",
        "js_var": "desktopAssistantCvCb",
        "setter": "_setDesktopAssistantContentVisibility",
    },
    {
        "key": "use_marked_renderer",
        "checkbox_id": "settingsUseMarkedRenderer",
        "js_var": "useMarkedRendererCb",
        "setter": "_setUseMarkedRenderer",
    },
]


@pytest.mark.parametrize("setting", SETTINGS, ids=lambda s: s["key"])
def test_setting_is_default_off_and_bool_allowlisted(setting):
    src = CONFIG.read_text(encoding="utf-8")
    assert f'"{setting["key"]}": False' in src, "must default OFF (experimental/opt-in)"
    assert f'"{setting["key"]}",' in src, "must be in _SETTINGS_BOOL_KEYS"


@pytest.mark.parametrize("setting", SETTINGS, ids=lambda s: s["key"])
def test_settings_preferences_expose_toggle_experimental_and_unchecked(setting):
    html = INDEX.read_text(encoding="utf-8")
    cb_id = setting["checkbox_id"]
    assert f'id="{cb_id}"' in html
    cb_line = next(l for l in html.splitlines() if f'id="{cb_id}"' in l)
    assert "checked" not in cb_line, "opt-in toggle must not be pre-checked"
    # The i18n label/desc keys referenced by data-i18n must exist (checked
    # against i18n.js separately below) and both must literally say
    # "(experimental)" in the default English string, matching the plain-
    # text convention virtualize_transcript already uses so a user can tell
    # at a glance this is not a fully-supported setting.
    label_key = f"settings_label_{setting['key']}"
    desc_key = f"settings_desc_{setting['key']}"
    assert f'data-i18n="{label_key}"' in html
    assert f'data-i18n="{desc_key}"' in html


@pytest.mark.parametrize("setting", SETTINGS, ids=lambda s: s["key"])
def test_english_label_says_experimental(setting):
    js = I18N.read_text(encoding="utf-8")
    label_key = f"settings_label_{setting['key']}"
    line = next(l for l in js.splitlines() if l.strip().startswith(f"{label_key}:"))
    assert "experimental" in line.lower()


@pytest.mark.parametrize("setting", SETTINGS, ids=lambda s: s["key"])
def test_i18n_all_locales(setting):
    js = I18N.read_text(encoding="utf-8")
    assert js.count(f"settings_label_{setting['key']}:") == 15
    assert js.count(f"settings_desc_{setting['key']}:") == 15


def test_boot_applies_saved_preferences_default_off():
    js = BOOT.read_text(encoding="utf-8")
    assert "window._desktopAssistantContentVisibility=s.desktop_assistant_content_visibility===true;" in js
    assert "window._useMarkedRenderer=s.use_marked_renderer===true;" in js
    # Settings-load-failed fallback also defaults OFF for both.
    assert "window._desktopAssistantContentVisibility=false;" in js
    assert "window._useMarkedRenderer=false;" in js


def test_boot_reapplies_content_visibility_class_after_settings_load():
    """ui.js initializes the cv-assistant-desktop <html> class to OFF at
    script-eval time, before settings are known (see
    _applyDesktopAssistantContentVisibilityFlag's own doc comment) — boot.js
    must re-apply it once the real persisted value arrives, or a user who
    enabled the setting would see it silently revert to off on every load."""
    js = BOOT.read_text(encoding="utf-8")
    idx = js.index("window._desktopAssistantContentVisibility=s.desktop_assistant_content_visibility===true;")
    tail = js[idx:idx + 300]
    assert "_applyDesktopAssistantContentVisibilityFlag()" in tail


@pytest.mark.parametrize("setting", SETTINGS, ids=lambda s: s["key"])
def test_panels_round_trip_and_hot_apply(setting):
    js = PANELS.read_text(encoding="utf-8")
    cb_id = setting["checkbox_id"]
    key = setting["key"]
    js_var = setting["js_var"]
    setter = setting["setter"]
    # Save side: _preferencesPayloadFromUi() must read the checkbox into the
    # persisted payload.
    assert f"const {js_var}=$('{cb_id}');" in js
    assert f"payload.{key}=" in js
    # Load side: checkbox state must come from the persisted setting.
    assert f"{js_var}.checked=settings.{key}===true;" in js
    # Hot-apply: the change listener must call the real ui.js setter (which
    # itself clears the stale render cache and re-renders), not just flip a
    # bare window global.
    assert f"if(typeof {setter}==='function') {setter}(" in js
    assert "_schedulePreferencesAutosave();" in js


def test_use_marked_renderer_load_does_not_trigger_redundant_rerender():
    """On initial settings load nothing has rendered yet, so applying
    use_marked_renderer via the full _setUseMarkedRenderer() setter (which
    clears the cache and calls renderMessages()) would be redundant — same
    reasoning as render_user_markdown's load path. Only the change handler
    (user-driven, after the transcript is already showing) should invoke the
    full setter. Anchored on the unique load-side "...===true;" assignment
    (the checkbox-lookup line itself is not unique — the same const also
    appears in the save-side _preferencesPayloadFromUi()).
    """
    js = PANELS.read_text(encoding="utf-8")
    start = js.index("useMarkedRendererCb.checked=settings.use_marked_renderer===true;")
    end = js.index("useMarkedRendererCb.addEventListener('change'", start)
    load_block = js[start:end]
    assert "window._useMarkedRenderer=useMarkedRendererCb.checked;" in load_block
    assert "_setUseMarkedRenderer(" not in load_block


def test_desktop_assistant_content_visibility_setter_exists_and_toggles_html_class():
    js = UI.read_text(encoding="utf-8")
    assert "function _setDesktopAssistantContentVisibility(enabled){" in js
    assert "function _applyDesktopAssistantContentVisibilityFlag(){" in js
    assert "window._setDesktopAssistantContentVisibility=_setDesktopAssistantContentVisibility;" in js


def test_use_marked_renderer_setter_exists_and_clears_cache():
    js = UI.read_text(encoding="utf-8")
    fn_start = js.index("function _setUseMarkedRenderer(enabled){")
    fn_end = js.index("\n}", fn_start)
    body = js[fn_start:fn_end]
    assert "window._useMarkedRenderer=!!enabled;" in body
    assert "clearMessageRenderCache" in body
    assert "renderMessages" in body
    assert "window._setUseMarkedRenderer=_setUseMarkedRenderer;" in js


# ── settings.json round-trip (load_settings behavior) ────────────────────────


@pytest.fixture
def _settings_env(tmp_path, monkeypatch):
    """Point load_settings at an isolated settings.json under tmp."""
    import api.config as config

    sf = tmp_path / "settings.json"
    monkeypatch.setattr(config, "SETTINGS_FILE", sf)
    return config, sf


def _write(sf, payload):
    sf.write_text(json.dumps(payload), encoding="utf-8")


@pytest.mark.parametrize("setting", SETTINGS, ids=lambda s: s["key"])
def test_fresh_install_defaults_off(_settings_env, setting):
    config, sf = _settings_env
    _write(sf, {"onboarding_completed": True})
    assert config.load_settings()[setting["key"]] is False


@pytest.mark.parametrize("setting", SETTINGS, ids=lambda s: s["key"])
def test_stored_true_is_honored(_settings_env, setting):
    """Unlike virtualize_transcript, these are brand-new settings with no
    prior default-flip history, so a stored True is honored directly — no
    opt-in migration marker needed."""
    config, sf = _settings_env
    _write(sf, {"onboarding_completed": True, setting["key"]: True})
    assert config.load_settings()[setting["key"]] is True


@pytest.mark.parametrize("setting", SETTINGS, ids=lambda s: s["key"])
def test_save_settings_coerces_non_bool_value(_settings_env, setting):
    """Bool coercion happens in save_settings() (the POST handler's write
    path), not in load_settings() (a plain read+defaults-merge) — a manually
    edited settings.json with a non-bool value round-trips as-is through
    load_settings(), so this exercises the actual coercion path via
    save_settings() instead."""
    config, sf = _settings_env
    _write(sf, {"onboarding_completed": True})
    config.save_settings({setting["key"]: 1})
    assert config.load_settings()[setting["key"]] is True
