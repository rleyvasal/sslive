# sslive

Live slides for [SolveIt](https://solve.it.com). Optional [gpudev](https://github.com/rleyvasal/gpudev) / CRAFT for GPU **▶ Run**.

**Do not paste `sslive.py` into a dialog cell** — load it from disk with `%run` (LLM context budget).

More detail: **[DOCS.md](DOCS.md)**.

## Quick start — one command

```text
%local
%run /app/data/gpudevd/sslive/sslive.py   # or %run /path/to/sslive/sslive.py
%sslive                                   # open deck
```

Same load works **with or without CRAFT**. CRAFT is auto-detected:

| Environment | ▶ Run uses |
|-------------|------------|
| No CRAFT | Host IPython |
| CRAFT loaded, no `%gpu` yet | Host IPython (badge: CRAFT present) |
| CRAFT + `%gpu` connected | Remote GPU |

Optional GPU (no second sslive recipe):

```text
%local
%run /app/data/gpudevd/sslive/sslive.py
%run /path/to/gpudev/CRAFT.py
%gpu
%sslive
```

Aliases (all run the same loader):

```text
%run /path/to/sslive/load.py
%run /path/to/gpudev/addons/sslive.py
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
        └─ host IPython          →  standalone / CRAFT not connected
```

| Piece | Where |
|-------|--------|
| Deck UI, layout, export | SolveIt **host** |
| Code ▶ Run (with CRAFT) | **Remote GPU** via CRAFT |
| Code ▶ Run (standalone) | **Host IPython** |

Status badge: `gpu · ready`, `local · ready`, or `offline · …`.

On load you should see something like:

```text
sslive: environment = absent|present|connected · ▶ Run backend = local|gpu (…)
sslive 0.1.0 ready (one load · CRAFT …)
```

## Deck content

### Section range (start → present)

```text
# sslive                 ← start (not a slide)

# Title / ## Slide …
…

%sslive                  ← stop: this cell and everything after are out of the deck
```

- **`# sslive`** — section start only; content before it is ignored.  
- **`%sslive`** / **`%sslive_export`** — first such code cell **terminates** the region (launcher, not a slide). Stay at the bottom of the notebook; no `# /sslive` end marker.

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
```

Portable HTML snapshot of the deck (layout + content). See **DOCS.md** for architecture and layout model.
