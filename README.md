# sslive

Live GPU presentations for [SolveIt](https://solve.it.com), driven by [gpudev](https://github.com/rleyvasal/gpudev) / CRAFT.

Sibling of [sslides](https://github.com/rleyvasal/sslides) (static snapshot decks). **sslive** is the live run path.

## Status

**Foundation (S1-A):** run-only. No write-back of slide edits to the SolveIt notebook yet. Stable `cell_id` / `el_id` so save can land later.

## Locked decisions

| ID | Choice |
|----|--------|
| Delivery | Single notebook-runnable script (`sslive.py`) — not a package yet |
| Backend | GPU only via CRAFT (`_exec_mgr`) |
| Execute | Remote kernel path + capture hook (never mutate SolveIt cells to run) |
| Source of truth | SolveIt dialog / `.ipynb` (load only in v0) |
| Sync | S1-A: no live edit → notebook write-back |
| Launcher | Skip/hide `slive()` cell after open (LLM context) |
| UI stack | TBD after reusing sslides patterns; custom DOM + element ids |

## Two pipes

1. **Execute:** slide `cell_id` → Deck source → CRAFT remote kernel → outputs on slide UI  
2. **Author (later):** slide edits → Deck → `update_msg` / notebook — **not in foundation**

## Pieces (in `sslive.py`)

1. Content loader  
2. Deck model (`cell_id` / `el_id`)  
3. GPU executor (capture)  
4. Live host (routes)  
5. Presenter UI  
6. `slive()` entry + skip launcher  

## Usage

```python
# 1) Load script on SolveIt kernel (not GPU)
%local
%run sslive/sslive.py

# 2) Connect CRAFT
%gpu

# 3) Live deck (dialoghelper is async)
%local
await slive()          # starts local server + embeds preview iframe
# optional: print(deck_summary()); run_cell_index(0)
```

Requires a note cell with exactly `#| s`, then `#` / `##` slide content below it.

**Presenter (click iframe first):** `←`/`→` slides · **Shift+Enter** run selected code · `f` fullscreen · ▶ Run on each code cell.

**Important:** keep `slive` / `run_cell_*` under `%local`. Only slide *source strings* run on the GPU via CRAFT.

`sstop()` stops the local presenter server.

## Local reference

Clone of sslides for inventory: `/Users/admin/sslides` (not a submodule).
