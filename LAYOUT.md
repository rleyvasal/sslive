# Layout editor plan (S2)

Google Slides–style authoring on the **live** presenter: drag elements
(text blocks, code cells, outputs/screenshots), resize, font size/family,
ordering (flow, z-stack, later reveal-fragments) — persisted with the dialog.

## Where it lives

| Piece | Location | Why |
|-------|----------|-----|
| Code | `sslive.py` (same single file) | keeps `%run sslive/sslive.py` workflow |
| Layout data | hidden dialog note `#| sslive-layout` + JSON, `skipped=1` | dialog **is** the document — layout travels with it, survives rebuilds; no sidecar file, no pollution of cell sources |
| Editor UI | edit mode inside the existing presenter iframe | reuses the proven `postMessage → parent queue → js_eval poll` bridge (only browser→Python path in SolveIt); no second app |

## Data model

Overlay keyed by `el_id` (absent = flow layout, today's behavior):

```json
{
  "version": 1,
  "elements": {
    "el-note-_abc123": {"x": 120, "y": 80, "w": 800, "h": null,
                         "z": 2, "order": 3,
                         "fs": 28, "ff": "Georgia, serif", "align": "left"}
  },
  "deck": {"font_family": null, "font_scale": 1.0}
}
```

- Coordinates in **design space** (1920×1080) — `#stage` scale transform makes
  them viewport-independent for free.
- `Element` grows `w, h, z, style` (`x`, `y` already reserved).
- `x != null` → `position:absolute` in the slide; else flex flow sorted by `order`.
- Merge into deck in `build_deck` after elements exist; keep unknown ids
  (tolerates temporarily deleted cells).

## Render changes

- `.slide` becomes a `position:relative` canvas.
- Positioned elements: inline `left/top/width` px; flow elements unchanged.
- Per-element inline `font-size`/`font-family`; deck-level default font in CSS.

## Edit mode (presenter JS)

- Toggle: `e` key / ✎ button. Selection outline + floating toolbar.
- **Drag**: pointer events; delta ÷ current stage scale → design px; live DOM
  update. Code cells drag by a **grip handle only** (textarea keeps its clicks).
- **Resize**: Google Slides–style 8 handles (corners + edges) → `w`/`h`
  (and `x`/`y` when resizing from left/top). Note corner-drag scales `fs`
  with the box; Alt+corner forces font-scale on any element.
- **Nudge**: arrows 1px, Shift+arrows 10px.
- **Toolbar**: font size stepper, font family dropdown, free **z** / **order**
  number fields (any integer on the selected element) plus ±1 buttons,
  reset-to-flow.
- Debounced `postMessage {type:'sslive_layout', el_id, patch}` to parent queue.

## Python side

- Poll loop drains `sslive_run` **and** `sslive_layout` items.
- Apply patch to deck → debounce-persist whole overlay JSON to the layout
  message via `update_msg` — **behind `_arm_focus_guard()`** (S1 guard reused;
  no focus jump on layout saves).
- Discover layout msg via `find_msgs` (`#| sslive-layout` prefix); create with
  `add_msg(..., skipped=1)` if missing.
- Scripting API (also how S2-A is tested before any UI exists):
  `set_layout(el_id, x=, y=, w=, fs=, ff=, z=, order=)`, `clear_layout(...)`.

## Element granularity

- **v1**: move blocks as they exist today — whole note cell, code cell,
  output mount. Ids = dialog msg ids → stable.
- **v2 (S2-D)**: split note cells per markdown block via sslides'
  mistletoe `parse_markdown_to_elements` (emits `el-{idx}-{cid}`).
  Index-based ids: layout survives content edits only while block count/order
  is stable — acceptable, document it.

## Build order

1. **S2-A** overlay model + persistence + render merge + `set_layout` API — **done**
   (deviation: overlay lives on `Deck.layout` as a JSON-shaped dict rather than
   on `Element` fields — lossless round-trip, unknown el_ids survive; `Element.x/y`
   stay reserved-unused)
2. **S2-B** edit mode: select, drag, nudge; round-trip persist; reload survives — **done**
   (refinements: code cells drag by the whole toolbar strip, not just the ⠿ grip;
   flow→absolute conversion deferred to first real movement so plain clicks
   never change layout mode; nudge patches debounced 350 ms; no `_push_layout`
   echo for slide-originated patches — the DOM already shows the result)
3. **S2-C** toolbar: font size/family, resize, z-order, flow reorder — **done**
   (pure presenter UI — Python patch pipeline from S2-A/B needed zero changes;
   toolbar is viewport-fixed so it never scales down with the stage; free z/order
   number inputs on the selected element (not linear-only ±1); 8-handle resize
   persists `w`/`h`/`x`/`y`; reset sends an all-null patch which drops the spec
   entirely; selection survives output re-render after Run)
4. **S2-D** finer note-block elements (mistletoe path), image polish
5. later: reveal-order fragments (`fragment_step` reserved), undo/redo,
   multi-select, snap guides, per-slide backgrounds

## Risks / decisions

- Code-cell drag vs textarea selection → grip handle only.
- `update_msg` churn → one layout message, debounced whole-overlay writes.
- Full rebuild (`refresh_presenter`) must reflect overlay → guaranteed:
  deck updated before persist.
- Fragments ("ordering" could also mean reveal order) → reserved, not v1.
- Single-file `sslive.py` until it hurts; packaging still a non-goal.
