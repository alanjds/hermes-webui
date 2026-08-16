# Test-only vendored copies

`marked.umd.js` (marked v18.0.9) and `purify.min.js` (DOMPurify v3.4.13) here
are **test fixtures only** — they exist so the marked.js/DOMPurify renderer
tests (`tests/test_marked_renderer.py`) run deterministically offline,
without network access to a CDN.

They are byte-identical to the files `static/index.html` loads at runtime
from jsdelivr at the same pinned versions — see the `integrity=` (SRI)
attributes on the `<script>` tags in `static/index.html` for the exact
hashes, computed from these same bytes. **Production never loads from this
directory**; the app has no build step and does not bundle or self-host
these libraries (unlike KaTeX/js-yaml, which *are* self-hosted under
`static/vendor/` for CSP/reliability reasons — see ARCHITECTURE.md §9/§10
for why marked/DOMPurify instead follow the Prism.js CDN+SRI pattern).

If `static/index.html`'s pinned marked/DOMPurify versions change, re-fetch
matching copies here (e.g. `npm pack marked@<version>` /
`npm pack dompurify@<version>`, then take `lib/marked.umd.js` and
`dist/purify.min.js` from the extracted tarball) and update this note.
