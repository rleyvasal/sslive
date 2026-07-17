# Changelog

## 0.1.0 — working baseline (2026-07-17)

End-to-end working version for SolveIt + CRAFT. **Do not paste this file into the dialog** — load with `%run` on the host only.

### Working

- `%local` → `%run sslive.py` → `%gpu` → `%slive` (local magic under GPU mode)
- In-slide code edit; ▶ Run / Shift+Enter on CRAFT remote GPU
- Soft-start: deck opens if GPU offline; badge reflects status
- Layout edit mode (drag / resize / font / reveal); single `#| sslive-layout` note (`skipped=1`)
- `hold_dialog_focus()` — same focus pattern as dialog code write-back
- Plotly path hardened (JSON MIME preferred, fill host, no parent HTMX poison)
- Preview + layout notes AI-hidden; deck notes/code remain LLM context
- Note split: titles, bullets, display math, images, tables; inline math stays in bullets

### Load rule (LLM budget)

| In dialog / LLM | Not in LLM |
|-----------------|------------|
| Short `%run` / import one-liner | Full `sslive.py` source |
| User slides under `#\| s` | `#\| sslive-layout` (skipped) |
| CRAFT bootstrap (keep short) | `%slive` preview iframe (skipped) |

### Live code floating editor + reset fix

- Live code stays a one-line bar; click/focus opens floating ~6-line editor (editable, SE-resize, Run/Shift+Enter, Esc)
- Layout height no longer makes code chrome tall; edit-mode resize ignores height on code cells
- **reset** restores original **size and position** (back to document flow); code+output reset **together** so one does not jump while the other stays absolute

### Export (0.1.1)

- `export_html("talk.html")` / `export_html_str()` / `%slive_export` — static portable player
- Frozen code + last-run outputs, layout, reveal, keyboard nav; no live GPU
- Code expand: floating ~6-line panel (z-index above plots), SE-resize, Esc/outside collapse
- Export syntax highlighting via highlight.js CDN (Python); layout height no longer drives expand size

### Known follow-ups

- Further reduce first-open preview flash if SolveIt remounts on any `update_msg`
- Package split (`deck` / `execute` / `layout` / `presenter` / `bridge` / `entry`) behind thin `%run` entry
- Stable note fragment ids (S2-D renumber drift)
- Shared `craft_hostkit` + CRAFT thin addon loader for sslive / pcviz / mojo
- Export: offline Plotly / highlight.js bundles, image inlining, in-preview download button
