# sslive

RISE-like live GPU slides for [SolveIt](https://solve.it.com), driven by [gpudev](https://github.com/rleyvasal/gpudev) / CRAFT.

## Goal

Edit and re-run code **on the slide** (like [RISE](https://rise.readthedocs.io/)), keep **one source** shared with the SolveIt dialog cell, execute on the **remote GPU**.

```text
Slide textarea (edit)
      │  ▶ Run / Shift+Enter
      ▼
postMessage → SolveIt parent page queue
      │  Python poll (js_eval)
      ▼
update_msg → SolveIt cell (unified source)
      +
CRAFT → GPU execute → refresh slide outputs
```

## Usage

```python
%local
%run sslive/sslive.py

%gpu

%local
await slive()
```

1. Click the **slide iframe** so it has focus  
2. **Edit** the code in the slide textarea  
3. Press **▶ Run** or **Shift+Enter**  
4. Source is written back to the SolveIt code cell and runs on the GPU  

If Run seems stuck (bridge):

```python
await pump_slide_runs()
```

### Fallback

```python
await run_cell_index(0)              # deck source → GPU
await run_cell_index(0, reload_from_dialog=True)  # re-read dialog first
await reload_deck()                  # rebuild after structural changes
```

Requires `#| s` then `#` / `##` slides in the dialog.

Keep `slive` / `run_cell*` under **`%local`**.

## Status

| Feature | Status |
|---------|--------|
| In-slide edit | yes (textarea) |
| In-slide Run | yes (postMessage bridge) |
| Sync to SolveIt cell | yes (`update_msg`) |
| GPU execute | yes (CRAFT) |
| Navigation | ←/→, f fullscreen |
