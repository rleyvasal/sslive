# sslive **0.1.0** (working)

Live GPU slides for [SolveIt](https://solve.it.com) + [gpudev](https://github.com/rleyvasal/gpudev) / CRAFT.

Stay in **`%gpu` mode** for your usual work (`torch`, `%pointcloud`, …). Open the deck with **`%slive`** — a **local magic** (like other CRAFT host tools) that displays the slides in the cell output and runs slide code on the remote GPU.

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

## Commands

| Call | Role |
|------|------|
| `%slive` / `%slive 800` | Open deck (local magic) |
| `await slive()` | Same (async API) |
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

- Created on first drag (or first save); **`skipped=1`** (red eye — not in LLM / not a slide).
- Later edits **update the same note** (find includes skipped messages so we never spawn copies).
- If older sessions left many layout notes, the next `%slive` **merges** them into one and retires the rest.
- Coordinates are design-space **1920×1080** px.
- Leaving edit mode, or the next `%slive` / `reload_deck`, **flushes** any debounced write.

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
