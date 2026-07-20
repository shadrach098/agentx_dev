# AgentX docs site

Static single-page site that bundles every markdown file under `docs/`
plus the top-level `README.md` and `AGENTX.md` into a browsable webapp.

## Open it

Just double-click `host/index.html` -- no server required. All docs are
inlined into `data.js` as strings so it works from `file://` URLs.

Optional: serve it (nicer URLs, faster hash routing):

```bash
python -m http.server -d host 8000
# then open http://localhost:8000
```

## What's inside

| File | Purpose |
|---|---|
| `index.html` | Landing page + SPA shell |
| `style.css` | Dark + light theme, sidebar layout |
| `app.js` | Hash router, marked+Prism rendering, search, theme toggle |
| `data.js` | Auto-generated bundle of every docs page (markdown + title + group) |
| `build_data.py` | Regenerates `data.js` from `docs/` |

## Features

- **Hash routing** -- links like `#concepts/models` and
  `#advanced/rag#persist` (page + anchor) work directly.
- **Sidebar nav** with animated active marker that slides between items.
- **Right-rail TOC** auto-generated from h2/h3 in the current article
  with scrollspy that highlights the visible section.
- **Search** -- `Ctrl+K` or click the search box. Case-insensitive
  substring match across every doc; results ranked by earliest hit.
- **Dark / light** -- toggle in the top right; persisted to
  `localStorage`.
- **Syntax highlighting** -- Python / Bash / YAML / JSON via Prism.
- **Copy-to-clipboard** button appears on code-block hover.
- **Prev / next** -- footer navigation follows the sidebar order.
- **Relative-link rewriting** -- markdown `[foo](../guides/x.md)`
  links auto-rewrite to hash routes.

## Design system

Dark-first, editorial monospace. Signature moves:

- **Type** -- JetBrains Mono for headings (unusual for docs; deliberate),
  Inter for body prose.
- **Accent** -- `#B8FF3E` electric lime. Reserved for active states,
  hero underscore, hover reveals. Never decorative.
- **Motion** -- deliberately restrained. Hero characters fade in
  left-to-right on first load; sidebar active-marker slides between
  items via `transform`; nav items shift 2px on hover; code blocks
  reveal a language label + copy button on hover; top bar's bottom
  border appears when you scroll past 12px. All motion respects
  `prefers-reduced-motion`.
- **Layout** -- three columns (sidebar / narrow content / TOC),
  collapses cleanly on mobile.

## Rebuild after editing docs

```bash
python host/build_data.py
```

Then reload the browser. `data.js` is small; regenerates in <1s.

## Dependencies

None at build time. Runtime loads two libraries from CDN:
- [marked](https://github.com/markedjs/marked) 11.1.1 for Markdown
- [Prism](https://prismjs.com) 1.29.0 for syntax highlighting

To use offline: download both scripts, put them in `host/vendor/`, and
update the `<script src=...>` tags in `index.html`.
