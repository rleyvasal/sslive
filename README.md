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
| Preview mode keep-focus after Run | ⚠️ best-effort (see below) |

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

## Known limitation (preview focus)

After dialog write-back, SolveIt often **focuses the updated dialog cell** (same class of issue as HTMX live-preview swaps focusing dialog content). Fullscreen hides this; in **inline preview** we re-focus `#sslive-frame` after sync (blur active element + scrollIntoView + multi-tick focus). Host UI may still briefly steal focus; click the slide if needed.

Optional: `await sync_dialog()` to push all deck sources later.

## Commands

| Call | Role |
|------|------|
| `await slive()` | Start presentation |
| `await run_cell_index(i)` | Programmatic run |
| `await pump_slide_runs()` | Drain stuck Run queue |
| `await sync_dialog()` | Batch write sources to dialog |
| `refocus_presenter()` | Manually return focus to slides |

Keep driver cells under **`%local`**.
