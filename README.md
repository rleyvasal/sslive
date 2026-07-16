# sslive **0.1.0** (working)

RISE-like live GPU slides for [SolveIt](https://solve.it.com) + [gpudev](https://github.com/rleyvasal/gpudev) / CRAFT.

## What works

| Feature | Status |
|---------|--------|
| Edit code **in the slide** | ✅ |
| ▶ Run / Shift+Enter on GPU (CRAFT) | ✅ |
| Output updates under the cell | ✅ |
| Sync source → SolveIt dialog cell | ✅ |
| Stay on current slide in **fullscreen** | ✅ |
| Preview mode keep-focus after Run | ✅ pre-emptive focus guard (see below) |
| Move/size/font/order elements (S2-A API) | ✅ `await set_layout(...)` |
| Edit mode: drag/nudge **in the slide** (S2-B) | ✅ press `e` |

## Usage

```python
%local
%run sslive/sslive.py

%gpu

%local
await slive()
```

1. Click the **slide iframe**  
2. Edit the code textarea  
3. **▶ Run** or **Shift+Enter**  
4. Output updates; dialog code cell source updates  

```text
Slide edit → postMessage → Python bridge
  → CRAFT GPU execute
  → in-place output push
  → update_msg (dialog source)
  → refocus slide iframe
```

## Preview focus (pre-emptive guard)

After dialog write-back, SolveIt tries to **focus the updated dialog cell** (same class of issue as HTMX live-preview swaps focusing dialog content). `update_msg` has no opt-out, so in **inline preview** the parent bridge arms a short *focus guard* right before each write-back: for ~2 s, `focus()` / `scrollIntoView()` calls targeting anything outside `#sslive-frame` are swallowed, and a `focusin` backstop bounces stray focus back within the same tick — so focus never visibly leaves the slide. Real clicks/keys in the dialog always win (guard yields to user gestures), and fullscreen is untouched. A light reactive refocus remains as fallback for host paths the guard can't intercept.

Optional: `await sync_dialog()` to push all deck sources later.

## Commands

| Call | Role |
|------|------|
| `await slive()` | Start presentation |
| `await run_cell_index(i)` | Programmatic run |
| `await pump_slide_runs()` | Drain stuck Run queue |
| `await sync_dialog()` | Batch write sources to dialog |
| `refocus_presenter()` | Manually return focus to slides |
| `layout_ids()` | List element ids you can lay out |
| `await set_layout(el_id, x=, y=, w=, fs=, ff=, z=, order=, align=)` | Position/style an element (live + persisted) |
| `await clear_layout(el_id \| None)` | Reset one/all elements to flow layout |

Layout is design-space px on the 1920×1080 stage; overrides persist in a
hidden `#| sslive-layout` note in the dialog (auto-created, `skipped=1`).
Example: `await set_layout('el-output-_abc123', x=1000, y=400, w=700)` moves
that screenshot/output block live — no rebuild, focus stays put.

### Edit mode (in the slide)

Press **`e`** (or the ✎ nav button): elements get dashed outlines.
**Drag** notes/outputs anywhere; **code cells drag by their toolbar strip**
(textarea/Run keep working). Click selects; **arrows nudge 1px**
(Shift = 10px); **Esc** deselects, then exits. A plain click never changes
an element's layout mode — only actual movement pins it. Every drop/nudge
flows through the bridge into the overlay and persists to the dialog
(debounced, focus-guarded). First drag of a flow element freezes its
current width so the text keeps its wrap.

Keep driver cells under **`%local`**.
