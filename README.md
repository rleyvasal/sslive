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

### Section marker (not a slide)

```text
# sslive
```

`# sslive` only marks where the slide section **starts**. It is **not** a slide. Content before it is ignored.

### After `# sslive`

| Heading | Role |
|---------|------|
| `# Talk title` | **Title slide** (main deck title — use sparingly) |
| `## Regular slide` | **Normal slide** — use this for almost all content slides |
| following notes/code | Belong to the **most recent** `#` / `##` until the next heading |

```text
# sslive

# My Demo Talk
agenda notes…

## Motivation
why this matters…
[code]

## Results
plots…
```

Do **not** make every slide a `#` heading — that produces only title-style slides. Prefer `##` for body slides.

**Math:** always use LaTeX with `$…$` (inline) or `$$…$$` (display). Plain ASCII formulas do not render.

Layout is stored in a separate skipped note `#| sslive-layout` (not LLM context). On `%run sslive.py`, `SSLIVE_USAGE` is printed for the LLM. Legacy marker `#| s` is still recognized.

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
