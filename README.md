# sslive

Live GPU slides for [SolveIt](https://solve.it.com) + [gpudev](https://github.com/rleyvasal/gpudev) / CRAFT.

**Do not paste `sslive.py` into a dialog cell** — load it from disk with `%run` (LLM context budget).

More detail: **[DOCS.md](DOCS.md)**.

## Quick start

```text
%local
%run /path/to/sslive/sslive.py    # host — registers %sslive
%gpu                              # optional: torch / Run target
%sslive                           # open deck
```

With CRAFT (same pattern):

```text
%local
%run /path/to/gpudev/CRAFT.py
%run /path/to/sslive/sslive.py
%gpu
%sslive
```

| Magic / call | Role |
|--------------|------|
| `%sslive` / `%sslive 800` | Open live deck |
| `%sslive_export talk.html` | Portable HTML snapshot |
| `await slive()` | Same as `%sslive` (Python API) |
| `register_sslive()` | Re-register magics if missing |

## How it fits

```text
%gpu mode
  ├─ normal cells / %pointcloud  →  remote GPU
  └─ %sslive (host-local magic)
         → iframe deck + layout
         → ▶ Run → CRAFT → same remote GPU
```

| Piece | Where |
|-------|--------|
| Deck UI, layout, export | SolveIt **host** |
| Code ▶ Run / Shift+Enter | **Remote GPU** via CRAFT |

## Deck content

Mark slides with a note cell:

```text
#| s
```

Notes and code cells **after** that marker become the deck. Layout is stored in a separate skipped note `#| sslive-layout` (not LLM context).

## Edit mode

- **`e`** or ✎ — enter/exit edit (leave edit **saves** layout)
- Drag / resize elements; **reset** restores flow for code+output together
- Code stays a **one-line bar**; click opens a floating editor (~6 lines)
- **`f`** — fullscreen (Esc leaves fullscreen only; not edit mode)

## Export

```text
%sslive_export talk.html
%sslive_export talk.html title=Demo
```

Portable file: frozen code + last outputs + layout. Use **Plotly** / matplotlib (or `%pointcloud_plotly` from pcviz) for viz that travels offline-ish; plain `%pointcloud` is live-only.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `dialoghelper not available` | `%local` → `%run sslive.py` first (not under bare `%gpu`) |
| `%sslive` not found | Re-`%run` on host, or `register_sslive()` |
| `_exec_mgr not found` | Load CRAFT on host, then `%gpu` |

## Repo layout

```text
sslive/
  sslive.py    # implementation
  README.md    # this file
  DOCS.md      # architecture, layout model, changelog
```
