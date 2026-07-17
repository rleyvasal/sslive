# sslive **0.1.0** (working)

Live GPU slides for [SolveIt](https://solve.it.com) + [gpudev](https://github.com/rleyvasal/gpudev) / CRAFT: edit and run **in the slide**, layout and reveal like a deck, keep the dialog as the source of truth.

## Architecture

| Layer | Where | Role |
|-------|--------|------|
| **Host** | SolveIt **`%local`** | `await slive()`, iframe, dialoghelper, layout persist, AI-hide |
| **Slide code** | CRAFT **GPU** (`%gpu` kernel) | ▶ Run / Shift+Enter via `remote_kc` |

You do **not** run `await slive()` under `%gpu`. Connect GPU once with `%gpu`, then drive the deck under `%local`. In-slide Run still executes on the remote kernel.

```text
%local await slive()
  → presenter iframe (srcdoc)
  → edit / ▶ Run → postMessage → Python bridge
  → CRAFT GPU execute
  → in-place output + debounced dialog write-back
```

## What works

| Feature | Status |
|---------|--------|
| Edit code **in the slide** | ✅ |
| ▶ Run / Shift+Enter on GPU (CRAFT) | ✅ |
| Output updates under the cell | ✅ |
| Sync source → SolveIt dialog cell | ✅ |
| Fullscreen keeps current slide | ✅ |
| Preview keep-focus after Run | ✅ focus guard |
| Layout: move / size / font / reveal | ✅ `e` + floating toolbar |
| Note cells split (title, bullets, display math, images…) | ✅ |
| Markdown + LaTeX notes | ✅ mistletoe + latex2mathml (fallback basic) |
| Preview cell hidden from LLM | ✅ `skipped=1` (red eye) |

## Usage

```python
%local
%run path/to/sslive.py   # prefer path; do not paste the whole file into a cell

%gpu                     # once — bring CRAFT remote up

%local
await slive()
```

1. Click the **slide iframe**  
2. Edit a code box  
3. **▶ Run** or **Shift+Enter**  
4. Output updates; dialog source updates shortly after  

## LLM context (keep it small)

SolveIt only feeds **non-skipped** messages to the model. sslive uses that:

| Message | LLM? | Notes |
|---------|------|--------|
| Notes + code under `#\| s` | **Yes** | Your real deck content |
| `#\| sslive-layout` JSON | **No** | auto `skipped=1` |
| `await slive()` cell (huge iframe) | **No** | auto `skipped=1` after embed |
| Pasted `sslive.py` / CRAFT logs / git dumps | **If you leave them open** | Hide them |

```python
await hide_from_ai()              # current / last launcher cell
await hide_from_ai("_ea017cb0")   # explicit message id
```

If the eye on the preview cell is **not** red after `slive()`, call `await hide_from_ai()` so the srcdoc HTML does not burn tokens.

**Hygiene:** never paste the full library into the dialog; only `%run` it. Keep noise cells skipped.

## Commands

| Call | Role |
|------|------|
| `await slive()` | Start presentation (**%local**) |
| `await run_cell_index(i)` | Programmatic run (GPU) |
| `await pump_slide_runs()` | Drain stuck Run queue |
| `await sync_dialog()` | Batch write sources to dialog |
| `await hide_from_ai(id?)` | Mark message skipped (LLM hide) |
| `refocus_presenter()` | Focus slide iframe |
| `layout_ids()` | List layoutable element ids |
| `await set_layout(el_id, …)` | Position/style (live + persist) |
| `await clear_layout(…)` | Reset layout overrides |

Layout is design-space px on a 1920×1080 stage; overrides live in the skipped layout note.

### Edit mode

Press **`e`** (or ✎): select elements. Floating toolbar sits **next to** the selection (A−/A+, font, **reveal**, reset). Drag notes/outputs; code cells drag from the toolbar strip. Corner/edge handles resize. **← / →** advance reveal steps, then slides. Fragment step is not shown in the nav counter (slide `n / N` only).

Note ids look like `el-0-_abc123` (index + cell id). Structural edits to a note can shift indices — re-check layout after big content changes.

## Preview focus

After dialog write-back, SolveIt may try to focus the updated cell. sslive arms a short parent-page **focus guard** before `update_msg` so focus stays on the slide iframe when possible. Real dialog interaction always wins.
