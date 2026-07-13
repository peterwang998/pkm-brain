# Brain UI browser fallback

Authority: `docs/specs/app-and-operations.md` and
`docs/specs/curation-and-review.md`.

- `index.html`, `tokens.css`, `app.css` define the fallback design. Do not add
  colors, shadows, fonts, or spacing values outside `tokens.css`; new visual patterns
  require an owning feature-spec update.
- `app.js`, `api.js`, `md.js`, `views/*.js` implement the six browser routes over
  the same API and mutation primitives as the native app.
- Serving: `ui_server.py` serves this directory at `/ui/*` (via `importlib.resources`,
  no path traversal, `Cache-Control: no-cache`) and `index.html` at `/`. The legacy
  embedded `ui_shell()` has been removed.
- Zero build step, zero external dependencies, zero network. If it needs npm, it's wrong.
- The browser is off by default in app-managed operation and remains a maintained
  portability/diagnostic fallback.
