"""sslive — live GPU slides for SolveIt (foundation S1-A).

Run-only: load deck from dialog/notebook, execute on CRAFT remote kernel,
show outputs in a local presenter. No write-back of edits to SolveIt yet.

Usage (SolveIt — driver stays local; GPU used only for cell execute)::

    %local
    %run sslive/sslive.py   # or path to this file

    %gpu                    # connect CRAFT once

    %local
    await slive()
    print(deck_summary())
    run_cell_index(0)
"""

from __future__ import annotations

import html as html_module
import inspect
import io
import json
import re
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

# ── optional SolveIt / FastHTML imports (available inside SolveIt) ───────────
try:
    from dialoghelper.core import find_msgs, curr_dialog, update_msg
except Exception:  # pragma: no cover - outside SolveIt
    find_msgs = curr_dialog = update_msg = None

try:
    from IPython.display import display, IFrame
    from IPython import get_ipython
except Exception:  # pragma: no cover
    display = IFrame = get_ipython = None

try:
    from fasthtml.common import (
        FastHTML, Div, Pre, Img, Button, Span, Style, Script, NotStr, to_xml,
    )
    from fasthtml.jupyter import JupyUvi, HTMX
except Exception:  # pragma: no cover
    FastHTML = JupyUvi = HTMX = None


# ═══════════════════════════════════════════════════════════════════════════
# Piece 2 — Deck model (stable ids for future S1-B write-back)
# ═══════════════════════════════════════════════════════════════════════════

OutputKind = Literal["stream", "error", "image/png", "text/html", "text/plain"]


@dataclass
class OutputPart:
    kind: OutputKind
    text: str = ""
    b64: str = ""
    name: str = "stdout"  # stream name


@dataclass
class Element:
    """Visual chunk. Layout fields reserved for later author/UI tools."""
    id: str
    cell_id: str
    kind: str
    order: int
    content: str = ""  # source snippet or plain text as applicable
    x: float | None = None
    y: float | None = None
    fragment_step: int | None = None


@dataclass
class Cell:
    id: str
    kind: Literal["note", "code"]
    source: str
    element_ids: list[str] = field(default_factory=list)
    outputs: list[OutputPart] = field(default_factory=list)
    i_collapsed: bool = False
    o_collapsed: bool = False


@dataclass
class Slide:
    index: int
    cell_ids: list[str]
    is_title: bool = False


@dataclass
class Deck:
    slides: list[Slide] = field(default_factory=list)
    cells: dict[str, Cell] = field(default_factory=dict)
    elements: dict[str, Element] = field(default_factory=dict)
    theme: dict = field(default_factory=dict)
    ordered_code_ids: list[str] = field(default_factory=list)

    def code_source(self, cell_id: str) -> str:
        c = self.cells[cell_id]
        if c.kind != "code":
            raise KeyError(f"{cell_id} is not a code cell")
        return c.source


# ═══════════════════════════════════════════════════════════════════════════
# Piece 1 — Content loader (sslides logic; load only — no write-back)
# ═══════════════════════════════════════════════════════════════════════════

async def get_slides_cells_from_dialog(include_prompts: bool = False) -> list[dict]:
    """Cells after `#| s` marker. Requires dialoghelper (async API)."""
    if find_msgs is None:
        raise RuntimeError("dialoghelper not available — run inside SolveIt")
    all_msgs = await find_msgs()
    marker_idx = None
    for i, m in enumerate(all_msgs):
        if m.get("msg_type") == "note" and m.get("content", "").strip() == "#| s":
            marker_idx = i
            break
    if marker_idx is None:
        return []
    allowed = ["note", "code"]
    if include_prompts:
        allowed.append("prompt")
    return [
        m
        for m in all_msgs[marker_idx + 1 :]
        if m.get("msg_type") in allowed and not m.get("skipped", 0)
    ]


def load_notebook(filepath: str | Path) -> dict:
    with open(filepath, encoding="utf-8") as f:
        return json.load(f)


def get_slides_cells_from_notebook(filepath: str | Path, dialog_cells: list[dict]):
    slide_ids = {c["id"].lstrip("_") for c in dialog_cells}
    nb_cells, nb_attachments = {}, {}
    for c in load_notebook(filepath)["cells"]:
        if c.get("id") in slide_ids:
            nb_cells[c["id"]] = c
            for att_id, att_data in c.get("attachments", {}).items():
                nb_attachments[att_id] = att_data
    return nb_cells, nb_attachments


def group_dialog_cells_by_heading(dialog_slides_cells: list[dict]) -> list[list[dict]]:
    groups, current = [], []
    for cell in dialog_slides_cells:
        is_heading = False
        if cell.get("msg_type") == "note":
            content = cell.get("content", "").strip()
            if content.startswith("## ") or (
                content.startswith("# ") and not content.startswith("### ")
            ):
                is_heading = True
        if is_heading:
            if current:
                groups.append(current)
            current = [cell]
        else:
            current.append(cell)
    if current:
        groups.append(current)
    return groups


def _notebook_outputs_to_parts(nb_cell: dict) -> list[OutputPart]:
    """Seed UI from saved notebook outputs (static). Live runs replace these."""
    parts: list[OutputPart] = []
    for output in nb_cell.get("outputs", []) or []:
        otype = output.get("output_type")
        if otype == "stream":
            text = output.get("text", [])
            if isinstance(text, list):
                text = "".join(text)
            parts.append(OutputPart(kind="stream", text=text, name=output.get("name", "stdout")))
        elif otype in ("execute_result", "display_data"):
            data = output.get("data", {}) or {}
            if "image/png" in data:
                b64 = data["image/png"]
                if isinstance(b64, list):
                    b64 = "".join(b64)
                parts.append(OutputPart(kind="image/png", b64=b64))
            elif "text/html" in data:
                t = data["text/html"]
                if isinstance(t, list):
                    t = "".join(t)
                parts.append(OutputPart(kind="text/html", text=t))
            elif "text/plain" in data:
                t = data["text/plain"]
                if isinstance(t, list):
                    t = "".join(t)
                parts.append(OutputPart(kind="text/plain", text=t))
        elif otype == "error":
            tb = "\n".join(output.get("traceback", []) or [])
            parts.append(OutputPart(kind="error", text=tb))
    return parts


async def build_deck(
    dialog_cells: list[dict] | None = None,
    notebook_path: str | Path | None = None,
    theme: dict | None = None,
) -> Deck:
    """Load authoring source into Deck. Does not write back."""
    if dialog_cells is None:
        dialog_cells = await get_slides_cells_from_dialog()
    if notebook_path is None and curr_dialog is not None:
        dinfo = await curr_dialog()
        if isinstance(dinfo, dict) and dinfo.get("name"):
            notebook_path = Path(dinfo["name"]).name + ".ipynb"
    nb_cells, _nb_attachments = {}, {}
    if notebook_path and Path(notebook_path).exists():
        nb_cells, _nb_attachments = get_slides_cells_from_notebook(notebook_path, dialog_cells)

    groups = group_dialog_cells_by_heading(dialog_cells)
    deck = Deck(theme=theme or {})
    el_counter = 0

    for s_idx, group in enumerate(groups):
        if not group:
            continue
        first = group[0].get("content", "").strip()
        is_title = first.startswith("# ") and not first.startswith("## ")
        slide = Slide(index=s_idx, cell_ids=[], is_title=is_title)

        for cell in group:
            cid = cell["id"]
            kind = "code" if cell.get("msg_type") == "code" else "note"
            source = cell.get("content", "") or ""
            c = Cell(
                id=cid,
                kind=kind,
                source=source,
                i_collapsed=bool(cell.get("i_collapsed", False)),
                o_collapsed=bool(cell.get("o_collapsed", False)),
            )
            if kind == "code":
                bare = cid.lstrip("_")
                c.outputs = _notebook_outputs_to_parts(nb_cells.get(bare, {}))
                deck.ordered_code_ids.append(cid)
                # one code element + reserved output mount id
                el_id = f"el-code-{cid}"
                deck.elements[el_id] = Element(
                    id=el_id, cell_id=cid, kind="code", order=el_counter, content=source
                )
                c.element_ids.append(el_id)
                el_counter += 1
                out_id = f"el-output-{cid}"
                deck.elements[out_id] = Element(
                    id=out_id, cell_id=cid, kind="output", order=el_counter
                )
                c.element_ids.append(out_id)
                el_counter += 1
            else:
                # coarse note element — finer parse later (sslides mistletoe path)
                el_id = f"el-note-{cid}"
                deck.elements[el_id] = Element(
                    id=el_id, cell_id=cid, kind="note", order=el_counter, content=source
                )
                c.element_ids.append(el_id)
                el_counter += 1

            deck.cells[cid] = c
            slide.cell_ids.append(cid)

        deck.slides.append(slide)

    return deck


# ═══════════════════════════════════════════════════════════════════════════
# Piece 3 — GPU executor (CRAFT remote + capture hook)
# ═══════════════════════════════════════════════════════════════════════════

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\x1b\[[0-9;]*$|\x1b$")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text or "")


def _as_str(val: Any) -> str:
    if isinstance(val, list):
        return "".join(val)
    return str(val) if val is not None else ""


def get_craft_exec_mgr():
    """Discover CRAFT RemoteExecutionManager from the IPython user namespace."""
    if get_ipython is None:
        return None
    try:
        return get_ipython().user_ns.get("_exec_mgr")
    except Exception:
        return None


def make_capture_hook(
    parts: list[OutputPart],
    *,
    echo_to_dialog: bool = False,
) -> Callable:
    """IOPub hook: collect MIME parts for the slide UI.

    Does not mutate SolveIt cells. Optional echo uses CRAFT's dialog display path.
    """

    def hook(msg):
        msg_type = msg.get("msg_type")
        content = msg.get("content", {}) or {}

        if msg_type == "stream":
            parts.append(
                OutputPart(
                    kind="stream",
                    text=_strip_ansi(content.get("text", "")),
                    name=content.get("name", "stdout"),
                )
            )
        elif msg_type == "error":
            tb = "\n".join(content.get("traceback", []) or [])
            parts.append(OutputPart(kind="error", text=_strip_ansi(tb)))
        elif msg_type in ("display_data", "update_display_data", "execute_result"):
            data = content.get("data", {}) or {}
            if "image/png" in data:
                parts.append(OutputPart(kind="image/png", b64=_as_str(data["image/png"])))
            elif "text/html" in data:
                parts.append(OutputPart(kind="text/html", text=_as_str(data["text/html"])))
            elif "text/plain" in data:
                parts.append(OutputPart(kind="text/plain", text=_as_str(data["text/plain"])))
        elif msg_type == "clear_output":
            parts.clear()

        if echo_to_dialog:
            mgr = get_craft_exec_mgr()
            if mgr is not None and hasattr(mgr, "_output_hook"):
                try:
                    mgr._output_hook(msg)
                except Exception:
                    pass

    return hook


@dataclass
class ExecResult:
    ok: bool
    parts: list[OutputPart]
    duration_ms: int = 0
    error: str | None = None


class LiveExecutor:
    """GPU-only executor. Source comes from Deck; never from rewriting SolveIt cells."""

    def __init__(self):
        self._lock = threading.Lock()
        self.busy = False

    def kernel_ok(self) -> tuple[bool, str]:
        mgr = get_craft_exec_mgr()
        if mgr is None:
            return False, "CRAFT _exec_mgr not found — load CRAFT and run %gpu"
        if getattr(mgr, "remote_kc", None) is None:
            return False, "remote kernel not connected — run %gpu"
        if hasattr(mgr, "kernel_health"):
            return mgr.kernel_health()
        return True, "connected"

    def execute(self, code: str, *, echo_to_dialog: bool = False) -> ExecResult:
        with self._lock:
            if self.busy:
                return ExecResult(ok=False, parts=[], error="kernel busy")
            self.busy = True
            t0 = time.perf_counter()
            try:
                return self._execute_gpu(code, echo_to_dialog=echo_to_dialog, t0=t0)
            finally:
                self.busy = False

    def execute_cell(self, deck: Deck, cell_id: str, **kw) -> ExecResult:
        source = deck.code_source(cell_id)
        result = self.execute(source, **kw)
        if result.ok or result.parts:
            deck.cells[cell_id].outputs = list(result.parts)
        return result

    def _execute_gpu(self, code: str, *, echo_to_dialog: bool, t0: float) -> ExecResult:
        mgr = get_craft_exec_mgr()
        if mgr is None:
            return ExecResult(ok=False, parts=[], error="CRAFT _exec_mgr not found")

        # Prefer CRAFT reconnect path when present
        if hasattr(mgr, "_ensure_live") and not mgr._ensure_live():
            return ExecResult(
                ok=False,
                parts=[],
                error="remote kernel unreachable — check %kernel_status",
            )

        kc = getattr(mgr, "remote_kc", None)
        if kc is None:
            return ExecResult(ok=False, parts=[], error="no remote_kc")

        parts: list[OutputPart] = []
        hook = make_capture_hook(parts, echo_to_dialog=echo_to_dialog)
        try:
            # Same client as execute_remote / remote_run_, custom capture hook
            reply = kc.execute_interactive(code=code, output_hook=hook)
        except KeyboardInterrupt:
            self.interrupt()
            return ExecResult(
                ok=False,
                parts=parts,
                duration_ms=int((time.perf_counter() - t0) * 1000),
                error="interrupted",
            )
        except Exception as e:
            return ExecResult(
                ok=False,
                parts=parts,
                duration_ms=int((time.perf_counter() - t0) * 1000),
                error=str(e),
            )

        status = (reply or {}).get("content", {}).get("status")
        ok = status != "error" and not any(p.kind == "error" for p in parts)
        return ExecResult(
            ok=ok,
            parts=parts,
            duration_ms=int((time.perf_counter() - t0) * 1000),
        )

    def interrupt(self) -> bool:
        mgr = get_craft_exec_mgr()
        kc = getattr(mgr, "remote_kc", None) if mgr else None
        if kc is None:
            return False
        try:
            msg = kc.session.msg("interrupt_request")
            kc.control_channel.send(msg)
            return True
        except Exception:
            return False


# ═══════════════════════════════════════════════════════════════════════════
# Output → HTML fragment (presenter swap target)
# ═══════════════════════════════════════════════════════════════════════════

def render_output_html(parts: list[OutputPart], cell_id: str, theme: dict | None = None) -> str:
    """Return HTML for #el-output-{cell_id}. No FastHTML required."""
    theme = theme or THEME_DARK
    out_st = theme.get(
        "output",
        "background:#1f2937;color:#e5e7eb;padding:0.5rem;font:13px/1.4 ui-monospace,monospace;"
        "white-space:pre-wrap;border-radius:6px;margin:0.25rem 0;",
    )
    err_st = theme.get(
        "error",
        "background:#7f1d1d;color:#fecaca;padding:0.5rem;font:13px/1.4 ui-monospace,monospace;"
        "white-space:pre-wrap;border-radius:6px;margin:0.25rem 0;",
    )
    img_st = theme.get(
        "output-image",
        "max-width:100%;max-height:24rem;object-fit:contain;display:block;margin:0.5rem 0;",
    )

    chunks: list[str] = []
    for p in parts:
        if p.kind == "stream":
            chunks.append(f'<pre style="{out_st}">{html_module.escape(p.text)}</pre>')
        elif p.kind == "error":
            chunks.append(f'<pre style="{err_st}">{html_module.escape(p.text)}</pre>')
        elif p.kind == "image/png" and p.b64:
            chunks.append(
                f'<img src="data:image/png;base64,{p.b64}" style="{img_st}" alt="output"/>'
            )
        elif p.kind == "text/html":
            chunks.append(f'<div class="sslive-html">{p.text}</div>')
        elif p.kind == "text/plain":
            chunks.append(f'<pre style="{out_st}">{html_module.escape(p.text)}</pre>')

    if not chunks:
        chunks.append(f'<pre style="{out_st}opacity:0.5">(no output)</pre>')

    inner = "\n".join(chunks)
    return (
        f'<div id="el-output-{html_module.escape(cell_id)}" '
        f'data-type="output" data-cell-id="{html_module.escape(cell_id)}">'
        f"{inner}</div>"
    )


# ═══════════════════════════════════════════════════════════════════════════
# Piece 4 + 5 — Live host + presenter (FastHTML / HTMX)
# ═══════════════════════════════════════════════════════════════════════════

try:
    from starlette.responses import HTMLResponse, JSONResponse, Response
except Exception:  # pragma: no cover
    HTMLResponse = JSONResponse = Response = None  # type: ignore

THEME_DARK = {
    "bg": "#111827",
    "fg": "#f3f4f6",
    "muted": "#9ca3af",
    "code_bg": "#1f2937",
    "output": "background:#1f2937;color:#e5e7eb;padding:0.5rem;font:13px/1.4 ui-monospace,monospace;"
    "white-space:pre-wrap;word-break:break-word;border-radius:6px;margin:0.25rem 0;",
    "error": "background:#7f1d1d;color:#fecaca;padding:0.5rem;font:13px/1.4 ui-monospace,monospace;"
    "white-space:pre-wrap;border-radius:6px;margin:0.25rem 0;",
    "output-image": "max-width:100%;max-height:24rem;object-fit:contain;display:block;margin:0.5rem 0;",
}

_SESSION: dict[str, Any] = {
    "deck": None,
    "executor": None,
    "server": None,
    "port": None,
    "app": None,
    "echo_to_dialog": False,
    "theme": THEME_DARK,
}


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def _pick_port(start: int = 8100, span: int = 50) -> int:
    for p in range(start, start + span):
        if _port_free(p):
            return p
    raise RuntimeError(f"No free port in {start}..{start + span - 1}")


def _ensure_local_magic():
    """Keep slive local under %gpu when CRAFT is loaded."""
    if get_ipython is None:
        return
    try:
        reg = get_ipython().user_ns.get("register_local_magic")
        if callable(reg):
            reg("%slive")
            reg("slive")
    except Exception:
        pass


def _note_to_html(source: str) -> str:
    """Lightweight note render (headers + paragraphs). Full mistletoe later."""
    lines = (source or "").splitlines()
    if not lines:
        return ""
    first = lines[0].strip()
    body = "\n".join(lines[1:]).strip()
    if first.startswith("## "):
        h = f"<h2 class='slide-h2'>{html_module.escape(first[3:])}</h2>"
    elif first.startswith("# ") and not first.startswith("### "):
        h = f"<h1 class='slide-h1'>{html_module.escape(first[2:])}</h1>"
    else:
        h = ""
        body = source
    if body:
        # preserve paragraphs
        paras = []
        for block in re.split(r"\n\s*\n", body):
            block = block.strip()
            if not block:
                continue
            if block.startswith(("# ", "## ", "### ")):
                paras.append(f"<p class='slide-p'>{html_module.escape(block)}</p>")
            else:
                paras.append(
                    f"<p class='slide-p'>{html_module.escape(block).replace(chr(10), '<br/>')}</p>"
                )
        return h + "".join(paras)
    return h


def _code_block_html(cell: Cell) -> str:
    cid = html_module.escape(cell.id)
    src = html_module.escape(cell.source)
    collapsed = cell.i_collapsed
    code_inner = f"<pre class='code-pre'><code>{src}</code></pre>"
    if collapsed:
        code_inner = f"<details><summary>Code</summary>{code_inner}</details>"
    return f"""
    <div id="el-code-{cid}" class="code-wrap" data-type="code" data-cell-id="{cid}" data-runnable="1"
         tabindex="0" onclick="selectCell('{cid}')">
      <div class="code-toolbar">
        <button type="button" class="run-btn"
          hx-post="/execute"
          hx-vals='{{"cell_id": "{cid}"}}'
          hx-target="#el-output-{cid}"
          hx-swap="outerHTML"
          hx-disabled-elt="this"
          onclick="event.stopPropagation(); selectCell('{cid}')">▶ Run</button>
        <span class="cell-id">{cid}</span>
      </div>
      {code_inner}
    </div>
    """


def _slide_html(deck: Deck, slide: Slide) -> str:
    parts: list[str] = []
    for cid in slide.cell_ids:
        cell = deck.cells[cid]
        if cell.kind == "note":
            eid = html_module.escape(f"el-note-{cid}")
            parts.append(
                f'<div id="{eid}" class="note-block" data-el-id="{eid}" data-cell-id="{html_module.escape(cid)}">'
                f"{_note_to_html(cell.source)}</div>"
            )
        else:
            parts.append(_code_block_html(cell))
            # always emit output mount (seeded from last outputs / notebook)
            parts.append(render_output_html(cell.outputs, cell.id, deck.theme or THEME_DARK))
    cls = "slide title-slide" if slide.is_title else "slide"
    hidden = " active" if slide.index == 0 else " hidden"
    return (
        f'<section class="{cls}{hidden}" data-slide="{slide.index}">'
        f'{"".join(parts)}</section>'
    )


def generate_presenter_html(deck: Deck, *, backend_label: str = "gpu") -> str:
    """Full presenter page (custom JS + HTMX). No Reveal.js."""
    theme = deck.theme or THEME_DARK
    slides_html = "\n".join(_slide_html(deck, s) for s in deck.slides)
    n = len(deck.slides)
    first_code = deck.ordered_code_ids[0] if deck.ordered_code_ids else ""

    css = f"""
    * {{ box-sizing: border-box; }}
    html, body {{ margin:0; height:100%; background:{theme.get("bg", "#111")}; color:{theme.get("fg", "#eee")};
      font-family: system-ui, -apple-system, Segoe UI, sans-serif; overflow:hidden; }}
    #viewport {{ width:100vw; height:100vh; position:relative; overflow:hidden; }}
    #stage {{ position:absolute; left:0; top:0; transform-origin: top left; width:1920px; height:1080px; }}
    .slide {{ width:1920px; height:1080px; padding:48px 64px; display:none; flex-direction:column;
      justify-content:flex-start; align-items:stretch; overflow:auto; gap:12px; }}
    .slide.active {{ display:flex; }}
    .slide.hidden {{ display:none; }}
    .title-slide {{ justify-content:center; align-items:center; text-align:center; }}
    .slide-h1 {{ font-size:4.5rem; font-weight:700; margin:0 0 1rem; }}
    .slide-h2 {{ font-size:3rem; font-weight:700; margin:0 0 1rem; }}
    .slide-p {{ font-size:1.75rem; line-height:1.5; margin:0.5rem 0; color:{theme.get("fg", "#eee")}; }}
    .code-wrap {{ border:1px solid #374151; border-radius:8px; background:{theme.get("code_bg", "#1f2937")};
      padding:8px 12px; outline:none; }}
    .code-wrap.selected {{ border-color:#60a5fa; box-shadow:0 0 0 2px rgba(96,165,250,0.35); }}
    .code-toolbar {{ display:flex; align-items:center; gap:12px; margin-bottom:6px; }}
    .run-btn {{ cursor:pointer; background:#2563eb; color:white; border:0; border-radius:6px;
      padding:6px 14px; font-size:14px; font-weight:600; }}
    .run-btn:hover {{ background:#1d4ed8; }}
    .run-btn:disabled {{ opacity:0.5; cursor:wait; }}
    .cell-id {{ font-size:11px; color:{theme.get("muted", "#9ca3af")}; font-family:ui-monospace,monospace; }}
    .code-pre {{ margin:0; font:14px/1.45 ui-monospace,monospace; white-space:pre-wrap;
      color:#e5e7eb; overflow:auto; max-height:420px; }}
    #chrome {{ position:fixed; left:12px; top:12px; z-index:20; display:flex; gap:10px; align-items:center;
      background:rgba(0,0,0,0.55); color:#fff; padding:6px 12px; border-radius:8px; font-size:13px; }}
    #chrome .ok {{ color:#86efac; }} #chrome .bad {{ color:#fca5a5; }}
    #nav {{ position:fixed; right:16px; bottom:16px; z-index:20; display:flex; gap:12px; align-items:center;
      background:rgba(0,0,0,0.5); color:#fff; padding:8px 14px; border-radius:10px; opacity:0.35;
      transition:opacity 0.15s; }}
    #nav:hover {{ opacity:1; }}
    #nav button {{ background:transparent; border:0; color:#fff; font-size:20px; cursor:pointer; padding:0 6px; }}
    .htmx-request .run-btn, .run-btn.htmx-request {{ opacity:0.6; }}
    """

    js = f"""
    let currentSlide = 0;
    let selectedCellId = {json.dumps(first_code)};
    const slides = () => document.querySelectorAll('[data-slide]');

    function updateCounter() {{
      const el = document.getElementById('slide-counter');
      if (el) el.textContent = (currentSlide + 1) + ' / ' + slides().length;
    }}

    function selectCell(id) {{
      selectedCellId = id;
      document.querySelectorAll('[data-runnable]').forEach(el => {{
        el.classList.toggle('selected', el.dataset.cellId === id);
      }});
    }}

    function showSlide(n) {{
      const ss = slides();
      if (!ss.length) return;
      ss[currentSlide]?.classList.remove('active');
      ss[currentSlide]?.classList.add('hidden');
      currentSlide = Math.max(0, Math.min(n, ss.length - 1));
      ss[currentSlide].classList.remove('hidden');
      ss[currentSlide].classList.add('active');
      updateCounter();
      const first = ss[currentSlide].querySelector('[data-runnable]');
      if (first) selectCell(first.dataset.cellId);
    }}

    async function runSelected() {{
      if (!selectedCellId) return;
      const btn = document.querySelector(
        '[data-runnable][data-cell-id="' + selectedCellId + '"] .run-btn');
      if (btn) btn.click();
    }}

    document.addEventListener('keydown', (e) => {{
      if (e.key === 'ArrowRight') {{ e.preventDefault(); showSlide(currentSlide + 1); }}
      if (e.key === 'ArrowLeft')  {{ e.preventDefault(); showSlide(currentSlide - 1); }}
      if (e.key === 'Enter' && e.shiftKey) {{ e.preventDefault(); runSelected(); }}
      if (e.key === 'f' && !e.metaKey && !e.ctrlKey) {{
        document.documentElement.requestFullscreen?.();
      }}
      if (e.key === 'ArrowDown') {{
        document.querySelector('[data-slide].active')?.scrollBy({{ top: 100, behavior: 'smooth' }});
      }}
      if (e.key === 'ArrowUp') {{
        document.querySelector('[data-slide].active')?.scrollBy({{ top: -100, behavior: 'smooth' }});
      }}
    }});

    document.getElementById('prev-btn')?.addEventListener('click', () => showSlide(currentSlide - 1));
    document.getElementById('next-btn')?.addEventListener('click', () => showSlide(currentSlide + 1));

    // scale 1920x1080 stage to viewport
    (() => {{
      const DESIGN_W = 1920, DESIGN_H = 1080;
      const stage = document.getElementById('stage');
      const viewport = document.getElementById('viewport');
      function rescale() {{
        const vw = viewport.clientWidth, vh = viewport.clientHeight;
        const scale = Math.min(vw / DESIGN_W, vh / DESIGN_H);
        stage.style.transform = 'scale(' + scale + ')';
        stage.style.left = ((vw - DESIGN_W * scale) / 2) + 'px';
        stage.style.top = ((vh - DESIGN_H * scale) / 2) + 'px';
      }}
      new ResizeObserver(rescale).observe(viewport);
      rescale();
    }})();

    async function refreshStatus() {{
      try {{
        const r = await fetch('/status');
        const j = await r.json();
        const el = document.getElementById('status-badge');
        if (!el) return;
        el.textContent = (j.backend || '?') + (j.busy ? ' · busy' : ' · idle')
          + (j.kernel_ok ? '' : ' · kernel down');
        el.className = j.kernel_ok ? 'ok' : 'bad';
      }} catch (e) {{}}
    }}
    setInterval(refreshStatus, 3000);
    refreshStatus();
    if (selectedCellId) selectCell(selectedCellId);
    updateCounter();
    """

    empty = ""
    if n == 0:
        empty = (
            "<section class='slide active' data-slide='0'>"
            "<h2 class='slide-h2'>No slides</h2>"
            "<p class='slide-p'>Add a note with exactly <code>#| s</code>, "
            "then <code>#</code> / <code>##</code> content below it. "
            "Re-run <code>await slive()</code>.</p></section>"
        )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>sslive</title>
  <script src="https://unpkg.com/htmx.org@1.9.12"></script>
  <style>{css}</style>
</head>
<body>
  <div id="chrome">
    <strong>sslive</strong>
    <span id="status-badge" class="ok">{html_module.escape(backend_label)}</span>
    <span style="opacity:0.7">Shift+Enter run · ←/→ slides · f fullscreen</span>
  </div>
  <div id="viewport">
    <div id="stage">
      <div id="slides-container">
        {slides_html or empty}
      </div>
    </div>
  </div>
  <div id="nav">
    <button type="button" id="prev-btn" aria-label="Previous">‹</button>
    <span id="slide-counter">1 / {max(n, 1)}</span>
    <button type="button" id="next-btn" aria-label="Next">›</button>
  </div>
  <script>{js}</script>
</body>
</html>
"""


def _do_execute(cell_id: str) -> tuple[str, dict[str, str], int]:
    """Run cell; return (html, headers, status_code)."""
    deck: Deck | None = _SESSION.get("deck")
    executor: LiveExecutor | None = _SESSION.get("executor")
    theme = _SESSION.get("theme") or THEME_DARK
    headers = {
        "X-Slive-Backend": "gpu",
        "X-Slive-Cell": cell_id or "",
    }
    if not deck or not executor:
        html = render_output_html(
            [OutputPart(kind="error", text="sslive not initialized — await slive() first")],
            cell_id or "unknown",
            theme,
        )
        headers["X-Slive-Ok"] = "0"
        return html, headers, 503

    if not cell_id or cell_id not in deck.cells or deck.cells[cell_id].kind != "code":
        html = render_output_html(
            [OutputPart(kind="error", text=f"unknown code cell_id: {cell_id!r}")],
            cell_id or "unknown",
            theme,
        )
        headers["X-Slive-Ok"] = "0"
        return html, headers, 400

    echo = bool(_SESSION.get("echo_to_dialog", False))
    result = executor.execute_cell(deck, cell_id, echo_to_dialog=echo)
    if result.error and not result.parts:
        parts = [OutputPart(kind="error", text=result.error)]
    else:
        parts = list(result.parts)
        if result.error:
            parts.append(OutputPart(kind="error", text=result.error))
    html = render_output_html(parts, cell_id, theme)
    headers["X-Slive-Ok"] = "1" if result.ok else "0"
    headers["X-Slive-Ms"] = str(result.duration_ms)
    return html, headers, 200 if result.ok or result.parts else 500


def _ensure_live_server() -> int:
    """Start singleton FastHTML+JupyUvi (or uvicorn) server. Returns port."""
    if _SESSION.get("server") is not None and _SESSION.get("port"):
        return int(_SESSION["port"])

    if FastHTML is None:
        raise RuntimeError("fasthtml not available — install python-fasthtml in SolveIt")

    port = _pick_port(8100, 50)
    app = FastHTML(hdrs=())

    @app.get("/")
    def home():
        deck = _SESSION.get("deck") or Deck()
        ex = _SESSION.get("executor") or LiveExecutor()
        ok, msg = ex.kernel_ok()
        label = f"gpu · {msg}" if ok else f"gpu · {msg}"
        html = generate_presenter_html(deck, backend_label=label)
        if HTMLResponse is not None:
            return HTMLResponse(html)
        return html

    @app.get("/status")
    def status():
        deck = _SESSION.get("deck")
        ex = _SESSION.get("executor") or LiveExecutor()
        ok, msg = ex.kernel_ok()
        payload = {
            "backend": "gpu",
            "busy": bool(getattr(ex, "busy", False)),
            "kernel_ok": ok,
            "kernel_msg": msg,
            "slides": len(deck.slides) if deck else 0,
            "code_cells": len(deck.ordered_code_ids) if deck else 0,
            "port": _SESSION.get("port"),
        }
        if JSONResponse is not None:
            return JSONResponse(payload)
        return payload

    async def execute(req=None, cell_id: str = ""):
        # Accept form, query, or JSON body (HTMX hx-vals → form by default)
        cid = cell_id
        if req is not None and not cid:
            try:
                form = await req.form()
                cid = form.get("cell_id") or ""
            except Exception:
                pass
            if not cid:
                try:
                    body = await req.json()
                    cid = (body or {}).get("cell_id") or ""
                except Exception:
                    pass
            if not cid:
                cid = req.query_params.get("cell_id") or ""

        # GPU execute is blocking — keep event loop free
        import asyncio

        loop = asyncio.get_running_loop()
        html, headers, code = await loop.run_in_executor(None, lambda: _do_execute(str(cid)))
        if HTMLResponse is not None:
            return HTMLResponse(html, status_code=code, headers=headers)
        return html

    # Register POST with request object for form parsing
    try:
        from starlette.requests import Request

        @app.post("/execute")
        async def execute_route(request: Request):
            return await execute(req=request)

    except Exception:

        @app.post("/execute")
        async def execute_route(cell_id: str = ""):
            return await execute(cell_id=cell_id)

    @app.post("/interrupt")
    def interrupt():
        ex = _SESSION.get("executor")
        ok = bool(ex and ex.interrupt())
        payload = {"ok": ok, "backend": "gpu"}
        if JSONResponse is not None:
            return JSONResponse(payload)
        return payload

    if JupyUvi is not None:
        srv = JupyUvi(app, port=port)
    else:
        # Fallback: uvicorn in a daemon thread
        import uvicorn

        config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
        srv = uvicorn.Server(config)
        t = threading.Thread(target=srv.run, daemon=True)
        t.start()
        # wait briefly for bind
        for _ in range(50):
            if not _port_free(port):
                break
            time.sleep(0.05)

    _SESSION["app"] = app
    _SESSION["server"] = srv
    _SESSION["port"] = port
    return port


def sstop() -> None:
    """Stop the live presenter server (best-effort)."""
    srv = _SESSION.get("server")
    if srv is None:
        print("sslive: no server running")
        return
    try:
        if hasattr(srv, "stop"):
            srv.stop()
        elif hasattr(srv, "should_exit"):
            srv.should_exit = True
    except Exception as e:
        print(f"sslive: stop error: {e}")
    _SESSION["server"] = None
    _SESSION["app"] = None
    _SESSION["port"] = None
    print("sslive: server stopped")


def _show_presenter(port: int, height: str = "720px"):
    """Embed presenter in SolveIt dialog."""
    url = f"http://127.0.0.1:{port}/"
    if HTMX is not None and _SESSION.get("app") is not None:
        try:
            # HTMX helper from fasthtml.jupyter — may expect a route path
            display(IFrame(src=url, width="100%", height=height))  # type: ignore
            return
        except Exception:
            pass
    if display is not None and IFrame is not None:
        display(
            IFrame(
                src=url,
                width="100%",
                height=height,
            )
        )
    else:
        print(f"sslive presenter: open {url}")


# ═══════════════════════════════════════════════════════════════════════════
# Piece 6 — Entry
# ═══════════════════════════════════════════════════════════════════════════

@dataclass
class LiveSession:
    port: int
    backend: str = "gpu"
    deck: Deck | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/"

    def status(self) -> dict:
        ex = _SESSION.get("executor") or LiveExecutor()
        ok, msg = ex.kernel_ok()
        d = _SESSION.get("deck")
        return {
            "port": self.port,
            "url": self.url,
            "backend": self.backend,
            "kernel_ok": ok,
            "kernel_msg": msg,
            "slides": len(d.slides) if d else 0,
            "code_cells": len(d.ordered_code_ids) if d else 0,
            "busy": bool(getattr(ex, "busy", False)),
        }

    def stop(self) -> None:
        sstop()


async def slive(
    theme: str | dict = "dark",
    *,
    height: str = "720px",
    echo_to_dialog: bool = False,
    embed: bool = True,
):
    """Load deck, start live host, embed presenter.

    dialoghelper APIs are async — call from SolveIt as::

        %local
        await slive()

    Controls (click iframe first): ←/→ slides, Shift+Enter run, f fullscreen.
    """
    _ensure_local_magic()

    ok, msg = LiveExecutor().kernel_ok()
    if not ok:
        print(f"sslive: GPU not ready — {msg}")
        print("Load CRAFT and run %gpu, then call await slive() again under %local.")
        return None

    theme_dict = theme if isinstance(theme, dict) else dict(THEME_DARK)
    deck = await build_deck(theme=theme_dict)
    executor = LiveExecutor()
    _SESSION["deck"] = deck
    _SESSION["executor"] = executor
    _SESSION["echo_to_dialog"] = echo_to_dialog
    _SESSION["theme"] = theme_dict

    port = _ensure_live_server()
    session = LiveSession(port=port, backend="gpu", deck=deck)

    n_code = len(deck.ordered_code_ids)
    print(
        f"sslive: {len(deck.slides)} slides, {n_code} code cells, "
        f"backend=gpu ({msg}) → {session.url}"
    )
    if n_code == 0 and len(deck.slides) == 0:
        print("No slides found — add a note with exactly `#| s`, then `#` / `##` content below it.")
    print("Click the preview, then: ←/→ navigate · Shift+Enter run · f fullscreen")
    print("Headless still works: run_cell_index(0)")

    if embed:
        _show_presenter(port, height=height)

    # D6 — hide launcher from dialog context
    if update_msg is not None:
        try:
            caller_globals = inspect.currentframe().f_back.f_globals
            mid = caller_globals.get("__msg_id")
            if mid:
                await update_msg(id=mid, skipped=1)
        except Exception:
            pass

    return session


def run_cell(cell_id: str, *, echo_to_dialog: bool | None = None) -> ExecResult:
    """Execute one code cell on GPU by id; also updates deck outputs for presenter."""
    deck: Deck | None = _SESSION.get("deck")
    executor: LiveExecutor | None = _SESSION.get("executor")
    if deck is None or executor is None:
        raise RuntimeError("Call await slive() first")
    if echo_to_dialog is None:
        echo_to_dialog = bool(_SESSION.get("echo_to_dialog", False))
    result = executor.execute_cell(deck, cell_id, echo_to_dialog=echo_to_dialog)
    print(
        f"run_cell({cell_id!r}): ok={result.ok} parts={len(result.parts)} "
        f"ms={result.duration_ms}" + (f" err={result.error}" if result.error else "")
    )
    return result


def run_cell_index(i: int = 0, **kw) -> ExecResult:
    deck: Deck | None = _SESSION.get("deck")
    if deck is None:
        raise RuntimeError("Call await slive() first")
    if not deck.ordered_code_ids:
        raise RuntimeError("No code cells in deck")
    return run_cell(deck.ordered_code_ids[i], **kw)


def deck_summary(deck: Deck | None = None) -> str:
    deck = deck or _SESSION.get("deck")
    if deck is None:
        return "(no deck)"
    lines = [f"slides={len(deck.slides)} code_cells={len(deck.ordered_code_ids)}"]
    for cid in deck.ordered_code_ids:
        src = deck.cells[cid].source.strip().splitlines()
        preview = (src[0][:60] + "…") if src else "(empty)"
        lines.append(f"  {cid}: {preview}")
    return "\n".join(lines)


# Wire name for %run
__all__ = [
    "Deck",
    "Cell",
    "Element",
    "OutputPart",
    "LiveExecutor",
    "LiveSession",
    "build_deck",
    "slive",
    "sstop",
    "run_cell",
    "run_cell_index",
    "deck_summary",
    "render_output_html",
    "generate_presenter_html",
    "get_craft_exec_mgr",
]
