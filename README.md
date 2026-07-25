# sslive

Live slides for [SolveIt](https://solve.it.com). Optional [gpudev](https://github.com/rleyvasal/gpudev) / CRAFT for GPU **▶ Run**.

**Do not paste `sslive.py` into a dialog cell** — load it from disk with `%run` (LLM context budget).

More detail: **[DOCS.md](DOCS.md)**.

## Quick start (slides only — no CRAFT)

```text
%local
%run /path/to/sslive/sslive.py    # host — registers %sslive
%sslive                           # open deck; ▶ Run uses host IPython
```

Works as a **standalone slides demo**. Pure Python and magics run on the SolveIt host.

## With CRAFT GPU

```text
%local
%run /path/to/gpudev/CRAFT.py
%run /path/to/sslive/sslive.py
%gpu
%sslive                           # ▶ Run → remote GPU when connected
```

| Magic / call | Role |
|--------------|------|
| `%sslive` / `%sslive 800` | Open live deck |
| `%sslive_export talk.html` | Portable HTML snapshot |
| `await sslive()` | Same as `%sslive` (Python API) |
| `register_sslive()` | Re-register magics if missing |

## How it fits

```text
%sslive (host-local magic)
  ├─ deck UI, layout, export     →  SolveIt host
  └─ ▶ Run / Shift+Enter
        ├─ CRAFT remote GPU      →  when %gpu + _exec_mgr live
        └─ host IPython          →  standalone / no CRAFT
```

| Piece | Where |
|-------|--------|
| Deck UI, layout, export | SolveIt **host** |
| Code ▶ Run (with CRAFT) | **Remote GPU** via CRAFT |
| Code ▶ Run (standalone) | **Host IPython** |

Status badge: `gpu · ready`, `local · ready`, or `offline · …`.

## Deck content

Start the slide region with a **note** cell (the note itself is not a slide):

```text
# sslive

This section defines slides for sslive.

Conventions:
- `# sslive` starts the slide region and is not itself a slide
- `# ...` creates a new slide
- `## ...` creates a subslide under the current slide
- cells after a slide/subslide belong to that heading until a new heading appears

Authoring intent:
- slide content is written at the end of the notebook
- it summarizes or reorganizes material introduced earlier
- content before `# sslive` should not be treated as slide structure
```

Notes and code cells **after** that marker become the deck. Layout is stored in a separate skipped note `#| sslive-layout` (not LLM context).

On `%run sslive.py`, this usage note is printed once (`SSLIVE_USAGE` / `print_usage()`) so the LLM has authoring context in the cell output. Legacy marker `#| s` is still recognized.

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
| Badge `local · ready` | Expected without CRAFT — ▶ Run still works on host |
| Want GPU Run | Load CRAFT on host, `%gpu`, re-open `%sslive` |
| Force GPU-only open | `await sslive(require_gpu=True)` |

## Repo layout

```text
sslive/
  sslive.py    # implementation
  README.md    # this file
  DOCS.md      # architecture, layout model, changelog
```
