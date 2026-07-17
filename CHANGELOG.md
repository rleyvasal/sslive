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

### Export (0.1.1)

- `export_html("talk.html")` / `export_html_str()` / `%slive_export` — static portable player
- Frozen code + last-run outputs, layout, reveal, keyboard nav; no live GPU

### Known follow-ups

- Further reduce first-open preview flash if SolveIt remounts on any `update_msg`
- Package split (`deck` / `execute` / `layout` / `presenter` / `bridge` / `entry`) behind thin `%run` entry
- Stable note fragment ids (S2-D renumber drift)
- Shared `craft_hostkit` + CRAFT thin addon loader for sslive / pcviz / mojo
- Export: offline Plotly bundle, image inlining, in-preview download button
