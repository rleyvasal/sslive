# Foundation map (S1-A)

## Reuse from sslides (`/Users/admin/sslides`)

| sslides | sslive piece | Action |
|---------|--------------|--------|
| `get_slides_cells_from_dialog` | loader | reuse logic |
| `get_slides_cells_from_notebook` | loader | reuse (seed outputs only) |
| `group_dialog_cells_by_heading` | loader → deck | reuse |
| `parse_markdown_to_elements` | deck elements | adapt: emit stable `el_id`s |
| `parse_code_to_elements` | deck elements | adapt: always emit output mount |
| themes (`THEME_*`) | presenter | reuse |
| `generate_slides_html` nav/scale JS | presenter | adapt (no pure srcdoc host) |
| `update_msg(..., skipped=1)` | entry | reuse for launcher hide |
| `sshow` / `srcdoc` | — | **drop** for live path |
| write-back / `update_msg` content | author pipe | **later (S1-B+)** |

## Data model (v0)

```
Deck
  slides: list[Slide]
  cells: dict[cell_id, Cell]
  elements: dict[el_id, Element]   # layout fields reserved, unused in v0
  theme: dict

Slide
  index: int
  cell_ids: list[str]
  is_title: bool

Cell
  id: str                 # dialog msg id
  kind: "note" | "code"
  source: str
  element_ids: list[str]
  outputs: list[OutputPart]   # last run; UI-only in v0

Element
  id: str
  cell_id: str
  kind: str               # heading|paragraph|list|code|output|image|...
  order: int
  # reserved for later author/layout:
  x: float | None
  y: float | None
  fragment_step: int | None

OutputPart
  kind: stream|error|image/png|text/html|text/plain
  text: str
  b64: str
```

## Execute path (v0)

```
POST /execute { cell_id }
  → source = deck.cells[cell_id].source
  → _exec_mgr.remote_kc.execute_interactive(code, output_hook=capture)
  → render #el-output-{cell_id}
```

Do **not** use `execute_remote()` alone (dialog-only hook). Same client, capture hook (pattern: CRAFT `remote_run_`).

## Build order

1. Deck model + loader  
2. Executor + capture (headless proof)  
3. Live host `/execute`  
4. Minimal presenter  
5. `slive()` + skip cell  
6. Interrupt / status polish  

## dialoghelper (SolveIt)

`find_msgs`, `curr_dialog`, and `update_msg` are **async**. Loader entry points
must use `await`:

- `await get_slides_cells_from_dialog()`
- `await build_deck()`
- `await slive()`

Call from a SolveIt cell as `await slive()` under `%local`.  


## Explicit non-goals (foundation)

- Write-back to SolveIt  
- Local kernel backend  
- Reveal.js  
- Drag/drop layout editor  
- Package publish  
