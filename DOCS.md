# sslive documentation

Companion to [README.md](README.md). Architecture, layout model, and history.

---

## Architecture

### Host vs GPU

| Concern | Location |
|---------|----------|
| `%sslive`, layout, dialoghelper, export | SolveIt **host** (local magics) |
| Cell execution (▶ Run) | CRAFT **remote** kernel via `_exec_mgr` |

Load order is always:

```text
%local → %run sslive.py → %gpu → %sslive
```

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

Note cells after `#| s` are split into fine pieces (`el-{idx}-{cell_id}`): headings, list items, display math, images, tables.

### Execute path

```text
▶ Run → postMessage sslive_run
     → host poll → remote_kc.execute_interactive
     → push_slide_result → in-place #el-output-{id}
```

Prefer Plotly JSON MIME over fat HTML. After Run, live absolute layout on the output box is preserved (important for Plotly).

### Presenter

- Design space **1920×1080**, scaled to viewport
- Bridge: `postMessage` → parent queue → `js_eval` poll
- Soft-start: deck opens if GPU offline (badge shows status)

---

## Layout editor

Google Slides–style authoring **in the live presenter**.

### Storage

One dialog note (auto-created under the preview):

```text
#| sslive-layout
{ "version": 1, "elements": { "el-code-_abc": {"x":120,"y":80,"w":900}, … } }
```

- `skipped=1` (not LLM / not a slide)
- Coordinates in design px (1920×1080)
- Absent keys → document flow

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
While fullscreen: dialog write may be deferred until FS ends (avoids iframe remount).

### API

```python
await set_layout(el_id, x=…, y=…, w=…, fs=…)
layout_ids()
layout_status()
await save_layout() / await flush_layout_save()
await ensure_layout_note()
```

Fragment ids `el-{index}-{cell_id}` can orphan after note re-split if block count changes.

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
| User slides under `#\| s` | `#\| sslive-layout` (skipped) |
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

- Less first-open preview flash
- Stable note fragment ids
- Optional offline Plotly / HL bundles
- Thin package split if the single file grows too large

---

## Design notes (foundation)

Originally adapted from static sslides ideas: dialog as source of truth, CRAFT for execute, no pure-srcdoc-only host for the live path. Layout overlay is a separate skipped note so positions travel with the dialog without polluting cell sources.
