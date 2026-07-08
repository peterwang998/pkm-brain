# Brain UI v2 — scaffold

Spec: `docs/brain-ui-v2-spec.md` (design is fixed there; implementation is Codex's).

- `index.html`, `tokens.css`, `app.css` — **the design, shipped complete.** Do not add
  colors, shadows, fonts, or spacing values outside `tokens.css`; new visual patterns
  require a spec change.
- `app.js`, `api.js`, `md.js`, `views/*.js` — **contracts to implement.** JSDoc headers are
  normative: endpoints, layout composition, exact keyboard behavior, empty states.
- Serving: `ui_server.py` serves this directory at `/ui/*` (via `importlib.resources`,
  no path traversal, `Cache-Control: no-cache`) and `index.html` at `/`. The legacy
  embedded `ui_shell()` has been removed.
- Zero build step, zero external dependencies, zero network. If it needs npm, it's wrong.

Build order and acceptance gates: spec §6 (P1 shell+Today → P2 Queue → P3 Wiki+Entities →
P4 Ask → P5 Ops+palette+legacy removal). Backend additions required per phase: spec §5.1.
