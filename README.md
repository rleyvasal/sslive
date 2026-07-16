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

Keep driver cells under **`%local`**.
