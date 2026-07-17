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

## Usage (GPU-mode first)

```python
%gpu
# CRAFT already loaded in this dialog (so host has _exec_mgr)

%run path/to/sslive.py    # registers %slive + marks it *local* for %gpu mode
# if you %run *before* %gpu, re-mark after:
#   register_slive()

%slive                    # open deck (like %pointcloud)
# or: await slive()
```

If you see **`%slive` not found** under `%gpu`:

```python
register_slive()   # re-register + mark local
%slive
```

Or skip the magic: `await slive()` on the host (same effect).

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
| `layout_ids()` | List element ids |

### Edit mode

**`e`** or ✎: select elements; toolbar floats next to the selection. **← / →** reveal then slides. Nav shows `n / N` only (no fragment counter).

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `_exec_mgr not found` | CRAFT client not on **host** namespace — load CRAFT bootstrap in this dialog, then `%gpu`, then `%slive` |
| Magic not found under `%gpu` | `%run sslive.py` again on host so local magic registers |
| Eye not red | `await hide_from_ai()` |
