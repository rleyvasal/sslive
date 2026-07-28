# sslive documentation

Companion to [README.md](README.md). Architecture, layout model, and history.

---

## Architecture

### Host vs execute backend

| Concern | Location |
|---------|----------|
| `%sslive`, layout, dialoghelper, export | SolveIt **host** (local magics) |
| Cell execution (▶ Run) | **CRAFT remote** when connected; else **host IPython** |

**One load** (CRAFT optional — auto-detected for ▶ Run):

```text
%local → %run sslive/sslive.py → %sslive
```

Optional GPU (same sslive load):

```text
%local → %run sslive/sslive.py → %run CRAFT.py → %gpu → %sslive
```

`craft_env_status()` returns `connected` | `present` | `absent`.
`LiveExecutor` / `exec_backend()` returns `gpu` | `local` | `offline`. Badge and spinner text follow that mode.

### Data model

```text
Deck
  slides: list[Slide]
  cells: dict[cell_id, Cell]
  elements: dict[el_id, Element]
  layout: { version, elements: { el_id: {x,y,w,h,z,fs,ff,reveal,…} } }
  theme: dict

Slide   — index, cell_ids, is_title
Cell    — id, kind note|code, source, element_ids, outputs
Element — id, cell_id, kind, html/content (layout via deck.layout overlay)
OutputPart — stream|error|image/png|text/html|text/plain
```

### Slide section range and headings

| Construct | Role |
|-----------|------|
| `# sslive` | Section **start** only — **not** a slide; content before ignored |
| `%sslive` / `%sslive_export` | Section **end** — first code cell whose first non-empty line is this magic; that cell and everything after are **out of the deck** |
| `# Title` | **Title slide** (`Slide.is_title`) |
| `## Heading` | **Regular slide** (usual form for body content) |
| following note/code | Belong to the most recent `#` / `##` until the next heading |
| `###` and deeper | Body text inside the current slide, not a break |

Legacy exact body `#| s` still starts the region. No `# /sslive` end marker.

```text
# sslive

# Demo Talk                 ← title slide
intro…

## Motivation               ← regular slide
…
## Method
…

%sslive                     ← present; terminator
```

Prefer `##` for most slides; reserve `#` for the main title. On `%run`, `SSLIVE_USAGE` spells this out for the LLM.

Note cells after the marker are split into fine pieces (`el-{idx}-{cell_id}`): headings, list items, display math, images, tables.

### Execute path

```text
▶ Run → postMessage sslive_run
     → host poll
     → if CRAFT live:  remote_kc.execute_interactive
       else:           get_ipython().run_cell (host)
     → push_slide_result → in-place #el-output-{id}
```

Magics (`%pointcloud`, …) always use host IPython. Prefer Plotly JSON MIME over fat HTML. After Run, live absolute layout on the output box is preserved (important for Plotly).

### Presenter

- Design space **1920×1080**, scaled to viewport
- Bridge: `postMessage` → parent queue → `js_eval` poll
- Soft-start: deck opens without CRAFT; badge `local · ready` and ▶ Run still works

---

## Layout editor

Google Slides–style authoring **in the live presenter**.

### Storage

One dialog note (auto-created under the preview):

```text
#| sslive-layout
{ "version": 1, "elements": { "el-code-_abc": {"x":120,"y":80,"w":900}, … },
  "deck": { "theme": "dark", "background": {…} } }
```

- `skipped=1` (not LLM / not a slide)
- Coordinates in design px (1920×1080)
- Absent keys → document flow
- `deck.theme` / `deck.background` — gear menu (see below)

### Interaction

| Action | Behavior |
|--------|----------|
| `e` / ✎ | Toggle edit mode |
| Leave edit | Flush layout to dialog (if dirty) |
| Drag first move | Pin **all** in-flow siblings (no reflow under the dragged box) |
| Reset | Clear layout for element; **code+output** reset together |
| Code height | Not used for bars; expand is floating panel |

### Save policy

While editing: DOM + in-memory `deck.layout` only.  
On leave edit / `%sslive` / `flush_layout_save`: write the layout note.

### Chrome: pencil vs gear

Nav: **`[✎] [⚙] [‹ n/N ›]`**

| Control | Role |
|---------|------|
| **✎** pencil | Freeform layout edit only (drag, multi-select, text edit). Saves on exit. |
| **⚙** gear | Deck settings: **Dark/Light** theme, **background** color/image/clear, status, shortcuts |

Theme + background persist in the layout note under `layout.deck`:

```json
{
  "version": 1,
  "elements": { "…": {} },
  "deck": {
    "theme": "dark",
    "background": { "color": "#1a1a2e", "image": "bg.jpg" }
  }
}
```

- Default open (`theme=None`) restores `layout.deck` (dark if unset).
- `await sslive(theme="light")` forces light for that open and writes it into the layout.
- **Background image is a path** (notebook / SolveIt data dir), not a base64 blob.
  Host resolves the file, downsizes (max edge 1920, JPEG ~q82), and inlines into
  the presenter / export only. Gear: type filename + **Apply** (no local OS picker).
- Huge `data:` backgrounds already saved into the layout note are **scrubbed** on open.
- Note images `![](photo.jpg)` work the same way: path stays in markdown; host
  inlines a downsized data URL into the slide HTML so srcdoc can show them.
- Logo and per-slide backgrounds are deferred.  
While fullscreen: dialog write may be deferred until FS ends (avoids iframe remount).

### API

```python
await set_layout(el_id, x=…, y=…, w=…, fs=…)
layout_ids()
layout_status()
await save_layout() / await flush_layout_save()
await ensure_layout_note()
```

Note piece ids are **content-stable**: `el-n-{cell_id}-{hash}` from kind + text
(image path for images). Inserting/reordering blocks no longer shifts layout.
Legacy `el-{index}-{cell_id}` keys are migrated on load (positional match).
A single-bullet text edit gets a new hash; layout is remapped by order among
unmatched pieces when possible.

---

## Code UI (live vs export)

| Mode | UI |
|------|-----|
| Live | One-line textarea bar → floating editor (~6 lines, SE-resize, Run) |
| Export | One-line bar → floating panel + highlight.js, SE-resize |

Live does **not** use Monaco (size / srcdoc / layout tradeoffs). Export uses CDN Plotly + highlight.js.

---

## Portable export

```text
%sslive_export out.html
```

Includes: slides, layout, reveal, frozen code, last-run outputs, keyboard nav.  
Does **not** include: live Run, layout edit, host-only viewers without embed.

| Viz | Portable? |
|-----|-----------|
| matplotlib PNG | Yes |
| Plotly | Yes (CDN) |
| `%pointcloud` (Three.js) | No (localhost) — use `%pointcloud_plotly` (pcviz) |

---

## LLM / dialog budget

| In LLM | Not in LLM |
|--------|------------|
| Short `%run` one-liner | Full `sslive.py` |
| User slides under `# sslive` | `#\| sslive-layout` (skipped) |
| Short CRAFT loader | `%sslive` preview iframe (skipped) |

---

## Changelog (condensed)

### 0.1.x baseline

- Host-local `%sslive` / `%sslive_export` under `%gpu`
- In-slide edit + GPU Run; soft-start without GPU
- Layout edit, floating toolbar, reveal steps
- Floating code editor (live + export); Plotly layout kept after Run
- Leave-edit layout save; code+output reset pair; pin siblings on first freeform drag
- Export: static player, highlight.js, pointcloud placeholder → prefer Plotly path
- Edit mode: `e` only (Esc does not exit edit / no FS flash hacks)

### Follow-ups

- Optional offline Plotly / HL bundles
- Thin package split if the single file grows too large
- Logo / per-slide backgrounds
- Asset path picker (list images in notebook dir)

### Standalone execute (no CRAFT)

- `exec_backend()` / `LiveExecutor.backend()` → `gpu` | `local` | `offline`
- ▶ Run falls back to host IPython when CRAFT is missing or not connected
- Badge: `local · ready · ▶ Run` for slides-only demos
- `require_gpu=True` still forces CRAFT for callers that need remote kernels only

---

## Design notes (foundation)

Originally adapted from static sslides ideas: dialog as source of truth, CRAFT for execute, no pure-srcdoc-only host for the live path. Layout overlay is a separate skipped note so positions travel with the dialog without polluting cell sources.
