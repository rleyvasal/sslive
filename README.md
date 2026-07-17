# sslive **0.1.0** (working baseline)

Live GPU slides for [SolveIt](https://solve.it.com) + [gpudev](https://github.com/rleyvasal/gpudev) / CRAFT.

**Working as of 2026-07-17** — see [CHANGELOG.md](CHANGELOG.md). Treat this as the freeze baseline before package split / CRAFT addon modularity.

Stay in **`%gpu` mode** for your usual work (`torch`, `%pointcloud`, …). Open the deck with **`%slive`** — a **local magic** (like other CRAFT host tools) that displays the slides in the cell output and runs slide code on the remote GPU.

### Addon model (CRAFT + sslive / pcviz / mojo)

- **CRAFT** owns connection + remote execution + local-magic registry.
- **sslive** (and peers) live on the **host disk** and are loaded with `%run` / `import` — **never paste addon source into the dialog** (LLM context).
- Same pattern for **pcviz** / **mojo**: one short host load line, then magics under `%gpu`.

## Architecture

```text
%gpu mode (dialog stays here)
    │
    ├─ normal cells / %pointcloud  →  remote GPU kernel
    │
    └─ %slive  (local magic on SolveIt host)
           → iframe deck + dialoghelper + layout
           → ▶ Run  →  CRAFT client (_exec_mgr)  →  same remote GPU
```

| Piece | Where |
|-------|--------|
| `%slive` / `await slive()`, layout, AI-hide | SolveIt **host** (local magic) |
| Code from ▶ Run / Shift+Enter | **Remote GPU** via CRAFT |

You do **not** need to flip back to `%local` for the deck if `%slive` is registered as a local magic (done automatically when you `%run` this file on the host).

## Usage (critical order)

**`%run sslive` must happen under `%local`.**  
That loads the host (dialoghelper) and auto-registers **`%slive`**. Then switch to `%gpu` for normal work.

```python
%local
%run sslive/sslive.py      # host only — auto-registers %slive

%gpu                       # torch / %pointcloud / slide Run target
%slive                     # open deck (local magic; no register_slive needed)
```

`register_slive()` is only a **recovery** helper if `%slive` is missing after a bad order.

| Symptom | Cause | Fix |
|---------|--------|-----|
| `dialoghelper not available` | `%run` / `await slive()` under `%gpu` (remote) | `%local` → `%run` → `%gpu` → `%slive` |
| `%slive` not found | Magic not registered / not local | `%local` → `%run` again, or `register_slive()` |
| `_exec_mgr not found` | CRAFT client missing on host | Load CRAFT on host, then `%gpu` |

1. Click the slide iframe  
2. Edit a code box  
3. **▶ Run** / **Shift+Enter** → remote GPU  
4. Output updates in place; dialog source syncs shortly after  

**Soft-start:** if the CRAFT client is not attached yet, the deck still opens (notes, layout, reveal). The badge shows `gpu · offline`; ▶ Run will explain until GPU is ready.

## What works

| Feature | Status |
|---------|--------|
| `%slive` local magic under `%gpu` | ✅ |
| Edit code in the slide | ✅ |
| ▶ Run on GPU (CRAFT) | ✅ |
| Soft-start without GPU (view/layout) | ✅ |
| Layout / floating toolbar / reveal | ✅ |
| Note split (title, bullets, display math, images) | ✅ |
| Preview cell AI-hidden (`skipped=1`) | ✅ |
| Markdown + LaTeX | ✅ mistletoe + latex2mathml |

## LLM context

| Message | In LLM? |
|---------|---------|
| Deck notes/code under `#\| s` | Yes |
| `#\| sslive-layout` | No (`skipped=1`) |
| `%slive` / `await slive()` cell (iframe) | No (auto-skip) |

```python
await hide_from_ai()            # if the eye stayed open
await hide_from_ai("_msg_id")
```

Do not paste `sslive.py` into a dialog cell — only `%run` it.

## Export portable HTML

Under **`%gpu`**, bare Python runs on the remote kernel — `export_html` is **not** defined there. Use the **local magic** (same idea as `%slive`):

```text
%gpu
%slive                      # open deck, run cells as needed
%slive_export talk.html     # host-local — works under %gpu
%slive_export talk.html title=Demo
```

If you prefer a Python call, flip host first:

```text
%local
export_html("talk.html")
%gpu
```

Open `talk.html` in any browser — **no SolveIt / CRAFT / GPU**. Snapshot only: re-export after you change slides or re-run cells.

Reload host code after pulling changes:

```text
%local
%run path/to/sslive.py
%gpu
```

| Included | Not included (v1) |
|----------|-------------------|
| Slides, layout, reveal | Live ▶ Run |
| Frozen code (collapsed bar; click → floating panel) | Layout editing |
| Syntax-highlighted expand (~6 lines, SE-resize, Esc) | Live in-slide syntax highlighting |
| Last-run outputs (PNG, Plotly, …) | Host-only viewers without snapshot |
| Nav / keyboard / print CSS | Offline-vendored Plotly / highlight.js (CDN) |

**Code in export:** collapsed to a one-line bar (same idea as live). Click opens a **standard floating panel** above the slide (~6 lines visible, scroll for more, highlight.js Python highlighting). Drag the ↘ corner to resize; Esc, second click, or click outside collapses. Edit-mode layout height does not size the expanded panel.

**`%pointcloud` / Three.js:** export tries to embed a localhost viewer page (needs that server still reachable). If not, a placeholder is shown — use matplotlib/Plotly for fully portable viz.

## Commands

| Call | Role |
|------|------|
| `%slive` / `%slive 800` | Open deck (local magic) |
| `await slive()` | Same (async API) |
| `export_html("out.html")` | Portable static HTML snapshot |
| `export_html_str()` | Same as string |
| `%slive_export out.html` | Magic for export |
| `session()` | Last `LiveSession` |
| `await hide_from_ai()` | Force AI-hide |
| `await sync_dialog()` | Batch source write-back |
| `await set_layout(...)` | Programmatic layout |
| `layout_ids()` | List element ids (`*` = has overlay) |
| `layout_status()` | Persistence diagnostics |
| `await save_layout()` / `await flush_layout_save()` | Write overlay now |

### Edit mode

**`e`** or ✎: select elements; toolbar floats next to the selection. **← / →** reveal then slides. Nav shows `n / N` only (no fragment counter).

### Layout persistence (positions after drag)

Edit-mode **move / resize / font / reveal** is stored in **one** hidden dialog note, placed **just below the `%slive` preview** (not at the top of the dialog):

```text
#| sslive-layout
{ "version": 1, "elements": { "el-code-_abc": {"x":120,"y":80,"w":900}, ... } }
```

- **Created automatically at the end of `%slive`** if none exists (empty overlay is fine).
- Also written on first drag/resize / leaving edit mode / `await save_layout()`.
- **`skipped=1`** (red eye — not in LLM / not a slide).
- Later edits **update the same note** (find includes skipped messages so we never spawn copies).
- If several layout notes exist, the next `%slive` **merges** them into one and retires the rest.
- Coordinates are design-space **1920×1080** px.

```python
await ensure_layout_note()   # create/find the single layout cell now
layout_status()              # see msg id / errors if creation failed
```

```python
layout_ids()           # see which elements have overrides
layout_status()        # msg id, pending save, orphan keys
await flush_layout_save()  # force write after a long edit session
```

**Note:** fine note pieces use ids `el-{index}-{cell_id}`. Editing the note text (add/remove bullets) can renumber indices so old keys become orphans — positions for those pieces may reset.

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `_exec_mgr not found` | CRAFT client not on **host** namespace — load CRAFT bootstrap in this dialog, then `%gpu`, then `%slive` |
| Magic not found under `%gpu` | `%run sslive.py` again on host so local magic registers |
| Eye not red | `await hide_from_ai()` |
