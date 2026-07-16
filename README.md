# sslive

Live GPU presentations for [SolveIt](https://solve.it.com), driven by [gpudev](https://github.com/rleyvasal/gpudev) / CRAFT.

Sibling of [sslides](https://github.com/rleyvasal/sslides) (static snapshot decks). **sslive** is the live run path.

## Model

| Role | Where |
|------|--------|
| **Editor** | SolveIt dialog code cells (native browser editor) |
| **Presentation** | Read-only srcdoc slide deck |
| **Execute** | CRAFT remote GPU kernel |

```text
Edit in SolveIt cell  →  await run_cell_index(i)  →  re-read dialog
                      →  CRAFT GPU  →  refresh slides
```

No second editor (no ipywidgets / no HTML textareas). The dialog is the source of truth.

## Usage

```python
%local
%run sslive/sslive.py

%gpu

%local
await slive()

# Edit the SolveIt code cell in the dialog (above / in the #| s section), then:
await run_cell_index(0)   # re-reads that cell → GPU → updates slide outputs
```

Requires a note cell with exactly `#| s`, then `#` / `##` slides below it.

### Commands

| Call | Effect |
|------|--------|
| `await slive()` | Load deck, show slides + run panel, skip launcher cell |
| `await run_cell_index(i)` | Re-read dialog cell `i` → GPU → refresh deck |
| `await run_cell(cell_id)` | Same by message id |
| `await reload_deck()` | Rebuild slides from dialog (structure changed) |
| `refresh_presenter()` | Re-draw deck without re-running |

Keep `slive` / `run_cell*` under **`%local`**. Only slide *source strings* run on the GPU.

Optional HTTP server (local notebooks only): `await slive(use_http=True)`.

## Status

- GPU execute + output capture: yes  
- Slide navigation (srcdoc): yes  
- Edit: **SolveIt cells only**  
- In-iframe ▶ Run over HTTP: not used on cloud SolveIt (browser cannot reach kernel port)  
