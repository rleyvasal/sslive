"""sslive — RISE-like live GPU slides for SolveIt.

**Working version 0.1.0** — in-slide edit + Run on GPU + dialog source sync.

Edit code **inside the slide**, hit ▶ Run / Shift+Enter:
  → executes on CRAFT GPU
  → updates slide output in place
  → syncs source into the SolveIt dialog cell

Usage::

    %local
    %run sslive/sslive.py
    %gpu
    %local
    await slive()
    # click the slide iframe, edit the code, press ▶ or Shift+Enter
"""

from __future__ import annotations

import asyncio
import html as html_module
import inspect
import json
import os
import re
import socket
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

__version__ = "0.1.0"

# ── optional SolveIt / FastHTML imports (available inside SolveIt) ───────────
try:
    from dialoghelper.core import (
        find_msgs,
        curr_dialog,
        update_msg,
        read_msg,
        js_eval,
        iife,
    )
except Exception:  # pragma: no cover - outside SolveIt
    find_msgs = curr_dialog = update_msg = read_msg = js_eval = iife = None

# Prefer async variant when present (dialoghelper also has sync js_eval)
try:
    from dialoghelper.core import js_eval_a
except Exception:  # pragma: no cover
    js_eval_a = None

try:
    from IPython.display import display, IFrame, clear_output, DisplayHandle, HTML as IPyHTML
    from IPython import get_ipython
except Exception:  # pragma: no cover
    display = IFrame = clear_output = DisplayHandle = IPyHTML = get_ipython = None

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
    """In-slide editable code (textarea) + Run — RISE-style."""
    cid = html_module.escape(cell.id)
    # raw id for JS (safe: dialog ids are alphanumeric + underscore)
    raw_id = cell.id
    src = html_module.escape(cell.source)
    n_lines = max(3, min(24, cell.source.count("\n") + 2))
    # ~1.45em line height in code-ta
    ta_h = max(72, int(n_lines * 22))
    return f"""
    <div id="el-code-{cid}" class="code-wrap" data-type="code" data-cell-id="{cid}" data-runnable="1"
         tabindex="0" onclick="selectCell('{cid}')">
      <div class="code-toolbar">
        <button type="button" class="run-btn" data-cell-id="{cid}"
          onclick="event.stopPropagation(); runCellFromSlide('{raw_id}')">▶ Run</button>
        <span class="cell-id">{cid}</span>
        <span class="hint">edit here · Shift+Enter run · GPU</span>
      </div>
      <textarea class="code-ta" id="ta-{cid}" data-cell-id="{cid}"
        spellcheck="false" rows="{n_lines}"
        style="height:{ta_h}px"
        onfocus="selectCell('{cid}')"
        onkeydown="onCodeKey(event,'{raw_id}')">{src}</textarea>
    </div>
    """


def _slide_html(deck: Deck, slide: Slide, *, active: bool = False) -> str:
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
    hidden = " active" if active else " hidden"
    return (
        f'<section class="{cls}{hidden}" data-slide="{slide.index}">'
        f'{"".join(parts)}</section>'
    )


def generate_presenter_html(
    deck: Deck,
    *,
    backend_label: str = "gpu",
    port: int | None = None,
    initial_slide: int = 0,
) -> str:
    """Full presenter page (custom JS). No Reveal.js.

    ``initial_slide`` restores position after a rare full rebuild.
    """
    theme = deck.theme or THEME_DARK
    n_slides = len(deck.slides)
    initial_slide = max(0, min(int(initial_slide), max(0, n_slides - 1)))
    slides_html = "\n".join(
        _slide_html(deck, s, active=(s.index == initial_slide)) for s in deck.slides
    )
    n = n_slides
    first_code = deck.ordered_code_ids[0] if deck.ordered_code_ids else ""
    port = int(port or _SESSION.get("port") or 8000)

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
    .code-toolbar {{ display:flex; align-items:center; gap:12px; margin-bottom:6px; flex-wrap:wrap; }}
    .run-btn {{ cursor:pointer; background:#2563eb; color:white; border:0; border-radius:6px;
      padding:6px 14px; font-size:14px; font-weight:600; }}
    .run-btn:hover {{ background:#1d4ed8; }}
    .run-btn:disabled {{ opacity:0.5; cursor:wait; }}
    .cell-id {{ font-size:11px; color:{theme.get("muted", "#9ca3af")}; font-family:ui-monospace,monospace; }}
    .hint {{ font-size:11px; color:#6b7280; }}
    .code-ta {{ width:100%; box-sizing:border-box; margin:0; resize:vertical;
      font:14px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace; white-space:pre;
      color:#e5e7eb; background:#111827; border:1px solid #4b5563; border-radius:6px;
      padding:10px; outline:none; }}
    .code-ta:focus {{ border-color:#60a5fa; }}
    #chrome {{ position:fixed; left:12px; top:12px; z-index:20; display:flex; gap:10px; align-items:center;
      background:rgba(0,0,0,0.55); color:#fff; padding:6px 12px; border-radius:8px; font-size:13px; }}
    #chrome .ok {{ color:#86efac; }} #chrome .bad {{ color:#fca5a5; }}
    #nav {{ position:fixed; right:16px; bottom:16px; z-index:20; display:flex; gap:12px; align-items:center;
      background:rgba(0,0,0,0.5); color:#fff; padding:8px 14px; border-radius:10px; opacity:0.35;
      transition:opacity 0.15s; }}
    #nav:hover {{ opacity:1; }}
    #nav button {{ background:transparent; border:0; color:#fff; font-size:20px; cursor:pointer; padding:0 6px; }}
    """

    js = f"""
    let currentSlide = {initial_slide};
    let selectedCellId = {json.dumps(first_code)};
    let lastResultT = 0;
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

    function showSlide(n, {{ selectFirst }} = {{ selectFirst: true }}) {{
      const ss = slides();
      if (!ss.length) return;
      ss.forEach((s, i) => {{
        s.classList.toggle('active', i === n);
        s.classList.toggle('hidden', i !== n);
      }});
      currentSlide = Math.max(0, Math.min(n, ss.length - 1));
      updateCounter();
      // tell parent our position (for rebuild recovery)
      try {{
        window.parent.__sslive_slide_index = currentSlide;
      }} catch (e) {{}}
      if (selectFirst) {{
        const first = ss[currentSlide].querySelector('[data-runnable]');
        if (first) selectCell(first.dataset.cellId);
      }}
    }}

    function codeSource(cellId) {{
      const ta = document.querySelector('textarea.code-ta[data-cell-id="' + cellId + '"]');
      return ta ? ta.value : '';
    }}

    function setRunning(cellId, msg) {{
      const out = document.getElementById('el-output-' + cellId);
      if (out) {{
        out.innerHTML = '<pre style="background:#1f2937;color:#fbbf24;padding:0.5rem;' +
          'font:13px/1.4 ui-monospace,monospace;border-radius:6px">' +
          (msg || 'Running…') + '</pre>';
      }}
      const btn = document.querySelector('.run-btn[data-cell-id="' + cellId + '"]');
      if (btn) btn.disabled = true;
    }}

    function applyRunResult(msg) {{
      // In-place only — never reload document (preserves slide + fullscreen)
      if (!msg || !msg.cell_id) return;
      if (msg.t && msg.t <= lastResultT) return;
      if (msg.t) lastResultT = msg.t;
      const cellId = msg.cell_id;
      const out = document.getElementById('el-output-' + cellId);
      if (out && msg.html) {{
        const tmp = document.createElement('div');
        tmp.innerHTML = msg.html;
        const neu = tmp.firstElementChild;
        if (neu) out.replaceWith(neu);
      }}
      const btn = document.querySelector('.run-btn[data-cell-id="' + cellId + '"]');
      if (btn) btn.disabled = false;
      selectCell(cellId);
      const ta = document.querySelector('textarea.code-ta[data-cell-id="' + cellId + '"]');
      // keep user's edited text if Python also sent source
      if (ta && msg.source != null && msg.source !== '') {{
        // only sync source from Python if textarea was not focused
        if (document.activeElement !== ta) ta.value = msg.source;
      }}
      if (ta && msg.keep_focus !== false) {{
        try {{ ta.focus({{ preventScroll: true }}); }} catch (e) {{ try {{ ta.focus(); }} catch (e2) {{}} }}
      }}
      const badge = document.getElementById('status-badge');
      if (badge) {{
        badge.textContent = msg.ok ? 'gpu · ok' : 'gpu · error';
        badge.className = msg.ok ? 'ok' : 'bad';
      }}
    }}

    window.addEventListener('message', function (e) {{
      if (!e.data || e.data.type !== 'sslive_result') return;
      applyRunResult(e.data);
    }});

    // More reliable than postMessage into srcdoc: poll parent for last result
    setInterval(function () {{
      try {{
        const r = window.parent.__sslive_last_result;
        if (r && r.type === 'sslive_result') applyRunResult(r);
      }} catch (e) {{}}
    }}, 150);

    function runCellFromSlide(cellId) {{
      selectCell(cellId);
      const source = codeSource(cellId);
      setRunning(cellId, 'Running on GPU…');
      try {{
        window.parent.__sslive_slide_index = currentSlide;
        window.parent.postMessage({{
          type: 'sslive_run',
          cell_id: cellId,
          source: source,
          slide_index: currentSlide
        }}, '*');
      }} catch (e) {{
        setRunning(cellId, 'postMessage failed: ' + e);
      }}
    }}

    function onCodeKey(e, cellId) {{
      if (e.key === 'Enter' && e.shiftKey) {{
        e.preventDefault();
        e.stopPropagation();
        runCellFromSlide(cellId);
      }}
    }}

    function runSelected() {{
      if (!selectedCellId) return;
      runCellFromSlide(selectedCellId);
    }}

    document.addEventListener('keydown', (e) => {{
      if (e.target && e.target.tagName === 'TEXTAREA') {{
        if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') return;
        return;
      }}
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

    // restore slide; if parent has a pending result, apply immediately
    showSlide(currentSlide, {{ selectFirst: false }});
    try {{
      var pending = window.parent.__sslive_last_result;
      if (pending && pending.type === 'sslive_result') applyRunResult(pending);
    }} catch (e) {{}}
    // re-select the code cell we care about
    if (selectedCellId) selectCell(selectedCellId);
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


def _in_solveit() -> bool:
    return bool(os.environ.get("IN_SOLVEIT"))


def _presenter_page():
    """HTML document for the live deck (served at / and /sslive on JupyUvi)."""
    deck = _SESSION.get("deck") or Deck()
    ex = _SESSION.get("executor") or LiveExecutor()
    ok, msg = ex.kernel_ok()
    label = f"gpu · {msg}"
    port = _SESSION.get("port")
    html = generate_presenter_html(deck, backend_label=label, port=port)
    if HTMLResponse is not None:
        return HTMLResponse(html)
    return html


def _ensure_live_server() -> int:
    """Start singleton FastHTML + JupyUvi server. Returns port."""
    if _SESSION.get("server") is not None and _SESSION.get("port"):
        return int(_SESSION["port"])

    if FastHTML is None:
        raise RuntimeError("fasthtml not available — install python-fasthtml in SolveIt")

    # Prefer 8000 band like pcviz (SolveIt convention); fall through if busy
    try:
        port = _pick_port(8000, 50)
    except RuntimeError:
        port = _pick_port(8100, 50)

    # Store port before routes render (presenter HTML embeds port for API probe)
    _SESSION["port"] = port

    # default_hdrs/htmx + notebook CORS when IN_NOTEBOOK (SolveIt sets this)
    app = FastHTML(hdrs=())

    sslive_rt = app.get("/sslive")(_presenter_page)
    app.get("/")(_presenter_page)

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
            "in_solveit": _in_solveit(),
        }
        if JSONResponse is not None:
            return JSONResponse(payload)
        return payload

    async def execute(req=None, cell_id: str = ""):
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

        import asyncio

        loop = asyncio.get_running_loop()
        html, headers, code = await loop.run_in_executor(None, lambda: _do_execute(str(cid)))
        if HTMLResponse is not None:
            return HTMLResponse(html, status_code=code, headers=headers)
        return html

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
        # host 0.0.0.0 (JupyUvi default) — required for notebook/SolveIt proxy
        srv = JupyUvi(app, port=port, host="0.0.0.0")
    else:
        import uvicorn

        config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="warning")
        srv = uvicorn.Server(config)
        t = threading.Thread(target=srv.run, daemon=True)
        t.start()
        for _ in range(50):
            if not _port_free(port):
                break
            time.sleep(0.05)

    _SESSION["app"] = app
    _SESSION["server"] = srv
    _SESSION["port"] = port
    _SESSION["sslive_rt"] = sslive_rt
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
    _SESSION["sslive_rt"] = None
    print("sslive: server stopped")


def _presenter_iframe_html(height: str = "720px", port: int | None = None) -> str:
    """Build srcdoc iframe HTML for the current deck (display only)."""
    if isinstance(height, int):
        height = f"{height}px"
    deck = _SESSION.get("deck") or Deck()
    ex = _SESSION.get("executor") or LiveExecutor()
    ok, msg = ex.kernel_ok()
    label = f"gpu · {msg}" if ok else f"gpu · {msg}"
    # On SolveIt, browser cannot reach JupyUvi — label says kernel-side run
    if _in_solveit() or not port:
        label = f"gpu · {msg} · edit in slide · ▶ Run"
    initial = int(_SESSION.get("slide_index") or 0)
    html = generate_presenter_html(
        deck, backend_label=label, port=port or 0, initial_slide=initial
    )
    escaped = html_module.escape(html, quote=True)
    return (
        f'<iframe id="sslive-frame" data-sslive="1" srcdoc="{escaped}" '
        f'style="width:100%;height:{height};border:none;background:#111;" '
        f'sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-modals" '
        f'allow="fullscreen"></iframe>'
    )


def refresh_presenter(height: str | None = None) -> None:
    """Re-draw the srcdoc deck (call after run_cell so outputs update)."""
    if display is None:
        return
    h = height or _SESSION.get("height") or "720px"
    port = _SESSION.get("port")
    try:
        from IPython.display import HTML as IPyHTML

        handle = _SESSION.get("presenter_handle")
        iframe = IPyHTML(_presenter_iframe_html(h, port=port))
        if handle is not None:
            handle.update(iframe)
        else:
            _SESSION["presenter_handle"] = display(iframe, display_id=True)
    except Exception as e:
        print(f"sslive: refresh_presenter failed: {e}")


def _id_candidates(cell_id: str) -> list[str]:
    """Dialog msg ids sometimes include a leading underscore."""
    bare = cell_id.lstrip("_")
    out = []
    for mid in (cell_id, bare, "_" + bare):
        if mid and mid not in out:
            out.append(mid)
    return out


def _msg_content(msg: Any) -> str | None:
    if msg is None:
        return None
    if isinstance(msg, dict):
        c = msg.get("content")
        return c if c is not None else None
    return getattr(msg, "content", None)


async def fetch_dialog_source(cell_id: str) -> str:
    """Re-read live source from the SolveIt dialog (source of truth).

    Falls back to the in-memory deck only if dialoghelper cannot load the msg.
    """
    deck = _SESSION.get("deck")
    last_err = None

    if find_msgs is not None:
        for mid in _id_candidates(cell_id):
            try:
                res = await find_msgs(ids=mid, include_output=False, include_meta=True)
                if res is not None and len(res) > 0:
                    content = _msg_content(res[0])
                    if content is not None:
                        return str(content)
            except Exception as e:
                last_err = e

    if read_msg is not None:
        for mid in _id_candidates(cell_id):
            try:
                msg = await read_msg(n=0, relative=True, id=mid)
                content = _msg_content(msg)
                if content is not None:
                    return str(content)
            except Exception as e:
                last_err = e
            try:
                msg = await read_msg(n=0, relative=False, id=mid)
                content = _msg_content(msg)
                if content is not None:
                    return str(content)
            except Exception as e:
                last_err = e

    if deck is not None and cell_id in deck.cells:
        if last_err:
            print(
                f"sslive: dialog re-read failed ({last_err}); "
                f"using deck snapshot for {cell_id}"
            )
        return deck.cells[cell_id].source

    raise KeyError(f"cell {cell_id!r} not found in dialog or deck")


def _apply_source_to_deck(cell_id: str, source: str) -> None:
    deck = _SESSION.get("deck")
    if deck is None or cell_id not in deck.cells:
        raise KeyError(cell_id)
    deck.cells[cell_id].source = source
    el_id = f"el-code-{cell_id}"
    if el_id in deck.elements:
        deck.elements[el_id].content = source


async def write_back_cell(cell_id: str, content: str) -> bool:
    """Write source into the SolveIt dialog message (unified source of truth)."""
    if update_msg is None:
        return False
    last_err = None
    for mid in _id_candidates(cell_id):
        try:
            await update_msg(id=mid, content=content)
            return True
        except Exception as e:
            last_err = e
    if last_err:
        print(f"sslive: write-back failed for {cell_id}: {last_err}")
    return False


def push_slide_result(cell_id: str, result: ExecResult, *, source: str | None = None) -> None:
    """Deliver GPU outputs to the live slide without rebuilding the iframe.

    Uses ``window.__sslive_last_result`` (iframe polls parent) + postMessage.
    Never calls refresh_presenter from here.
    """
    deck = _SESSION.get("deck")
    theme = (deck.theme if deck else None) or THEME_DARK
    if deck is not None and cell_id in deck.cells:
        deck.cells[cell_id].outputs = list(result.parts)
        if source is not None:
            _apply_source_to_deck(cell_id, source)

    html = render_output_html(result.parts or [], cell_id, theme)
    payload = {
        "type": "sslive_result",
        "cell_id": cell_id,
        "html": html,
        "ok": bool(result.ok),
        "keep_focus": True,
        "source": source,
        "t": int(time.time() * 1000),
        "slide_index": int(_SESSION.get("slide_index") or 0),
    }
    js = f"""
(function() {{
  var msg = {json.dumps(payload)};
  window.__sslive_last_result = msg;
  if (msg.slide_index != null) window.__sslive_slide_index = msg.slide_index;
  document.querySelectorAll('iframe').forEach(function(f) {{
    try {{ f.contentWindow.postMessage(msg, '*'); }} catch (e) {{}}
  }});
}})();
"""
    if iife is not None:
        try:
            iife(js)
            return
        except Exception as e:
            _SESSION["_last_push_err"] = str(e)


def _run_and_refresh(
    cell_id: str,
    *,
    source: str | None = None,
    full_refresh: bool = False,
    quiet: bool = False,
) -> ExecResult:
    """Execute on GPU; update slide outputs in place (default) or full rebuild."""
    deck = _SESSION.get("deck")
    executor = _SESSION.get("executor")
    if deck is None or executor is None:
        raise RuntimeError("Call await slive() first")
    if source is not None:
        _apply_source_to_deck(cell_id, source)

    echo = bool(_SESSION.get("echo_to_dialog", False))
    result = executor.execute_cell(deck, cell_id, echo_to_dialog=echo)
    if not quiet:
        preview = ""
        if result.parts:
            p0 = result.parts[0]
            preview = (p0.text or p0.kind)[:80].replace("\n", " ")
        print(
            f"run_cell({cell_id!r}): ok={result.ok} parts={len(result.parts)} "
            f"ms={result.duration_ms}"
            + (f" — {preview}" if preview else "")
            + (f" err={result.error}" if result.error else "")
        )
    if full_refresh:
        refresh_presenter()
    else:
        push_slide_result(cell_id, result, source=source)
    return result


def _refocus_presenter_js() -> str:
    """JS to steal focus back from the dialog cell to the slide iframe.

    SolveIt focuses the message updated by ``update_msg`` (same class of issue
    as HTMX live-preview focus jumps). We blur that and focus #sslive-frame.
    """
    return r"""
(function () {
  function focusFrame() {
    try {
      var ae = document.activeElement;
      if (ae && ae !== document.body && !(ae.id === 'sslive-frame' || ae.getAttribute('data-sslive') === '1')) {
        try { ae.blur(); } catch (e) {}
      }
    } catch (e) {}
    var ifr = document.getElementById('sslive-frame')
      || document.querySelector('iframe[data-sslive="1"]')
      || document.querySelector('iframe[srcdoc]');
    if (!ifr) return false;
    try { ifr.scrollIntoView({ block: 'nearest', inline: 'nearest', behavior: 'instant' }); } catch (e) {
      try { ifr.scrollIntoView(false); } catch (e2) {}
    }
    try { ifr.focus({ preventScroll: true }); } catch (e) {
      try { ifr.focus(); } catch (e2) {}
    }
    try {
      // return keyboard focus into the slide document when same-origin/srcdoc allows
      if (ifr.contentWindow) ifr.contentWindow.focus();
    } catch (e) {}
    return true;
  }
  focusFrame();
  requestAnimationFrame(function () {
    focusFrame();
    setTimeout(focusFrame, 0);
    setTimeout(focusFrame, 50);
    setTimeout(focusFrame, 150);
    setTimeout(focusFrame, 400);
  });
})();
"""


def refocus_presenter() -> None:
    """Best-effort: return focus to the slide iframe (preview mode)."""
    if iife is None:
        return
    try:
        iife(_refocus_presenter_js())
    except Exception:
        pass


async def _read_parent_slide_index() -> int | None:
    try:
        if js_eval is None and js_eval_a is None:
            return None
        idx = await _call_js_eval(
            "return (window.__sslive_slide_index != null) "
            "? window.__sslive_slide_index : 0;"
        )
        idx = _parse_js_eval_result(idx)
        return int(idx) if idx is not None else None
    except Exception:
        return None


async def _parent_in_fullscreen() -> bool:
    """True if the browser document (or iframe) is fullscreen."""
    try:
        if js_eval is None and js_eval_a is None:
            return False
        res = await _call_js_eval(
            "return !!(document.fullscreenElement "
            "|| document.webkitFullscreenElement "
            "|| document.mozFullScreenElement);"
        )
        res = _parse_js_eval_result(res)
        return bool(res)
    except Exception:
        return False


async def _sync_and_run(cell_id: str, source: str, *, slide_index: int | None = None) -> ExecResult:
    """Slide edit → GPU → in-place UI → dialog sync → keep current slide.

    Fullscreen: leave the iframe alone after in-place push (already stable).
    Preview: ``update_msg`` often recreates the output iframe from a *stale*
    snapshot (slide 0). After sync we refresh the display handle on the saved
    ``slide_index`` so the rebuilt preview opens on the correct slide.
    """
    if slide_index is not None:
        _SESSION["slide_index"] = int(slide_index)
    else:
        idx = await _read_parent_slide_index()
        if idx is not None:
            _SESSION["slide_index"] = idx

    _apply_source_to_deck(cell_id, source)

    result = _run_and_refresh(
        cell_id, source=source, full_refresh=False, quiet=True
    )

    # Re-read index after run (user may have been on slide N)
    idx = await _read_parent_slide_index()
    if idx is not None:
        _SESSION["slide_index"] = idx

    in_fs = await _parent_in_fullscreen()

    # Dialog source sync (SolveIt may re-render preview / steal focus)
    synced = await write_back_cell(cell_id, source)

    if in_fs:
        # Fullscreen path: do not rebuild iframe (would exit FS). In-place only.
        push_slide_result(cell_id, result, source=source)
        refocus_presenter()
    else:
        # Preview path: host often replaces iframe with stale slide-0 snapshot.
        # Rebuild display handle at the *current* slide with updated deck state.
        try:
            refresh_presenter()
        except Exception:
            pass
        push_slide_result(cell_id, result, source=source)
        refocus_presenter()

    preview = ""
    if result.parts:
        p0 = result.parts[0]
        preview = (p0.text or p0.kind)[:60].replace("\n", " ")
    print(
        f"sslive: ▶ {cell_id} ok={result.ok} {result.duration_ms}ms"
        + (f" — {preview}" if preview else "")
        + (f" · dialog={'synced' if synced else 'not synced'}")
        + f" · slide={_SESSION.get('slide_index', 0)}"
        + (f" · fullscreen" if in_fs else " · preview")
    )
    return result


async def sync_dialog() -> int:
    """Write current deck code sources into SolveIt dialog cells.

    Call **after** presenting if you need dialog cells to match slide edits.
    May move focus in the SolveIt UI (host behavior).
    """
    deck = _SESSION.get("deck")
    if deck is None:
        raise RuntimeError("Call await slive() first")
    n = 0
    for cid in deck.ordered_code_ids:
        src = deck.cells[cid].source
        if await write_back_cell(cid, src):
            n += 1
            print(f"sslive: dialog sync {cid} ({len(src)} chars)")
    print(f"sslive: synced {n}/{len(deck.ordered_code_ids)} code cells → dialog")
    return n


def _install_parent_bridge() -> None:
    """Listen on the SolveIt parent page for postMessage from the slide iframe.

    Slide Run cannot call the kernel HTTP port; it posts to parent, Python
    drains ``window.__sslive_q`` via js_eval.
    """
    bridge_js = r"""
if (!window.__sslive_bridge) {
  window.__sslive_bridge = true;
  window.__sslive_q = window.__sslive_q || [];
  window.__sslive_slide_index = window.__sslive_slide_index || 0;
  window.addEventListener('message', function (e) {
    var d = e.data;
    if (!d || d.type !== 'sslive_run') return;
    if (d.slide_index != null) window.__sslive_slide_index = d.slide_index;
    window.__sslive_q.push({
      cell_id: d.cell_id,
      source: d.source == null ? '' : String(d.source),
      slide_index: d.slide_index,
      t: Date.now()
    });
  });
}
"""
    # Prefer dialoghelper so the script lands on the real dialog DOM
    if iife is not None:
        try:
            iife(bridge_js)
            print("sslive: parent bridge installed (dialoghelper.iife)")
            return
        except Exception as e:
            print(f"sslive: iife bridge failed ({e}); trying HTML script")

    if display is not None and IPyHTML is not None:
        # Fallback: inject via notebook output (usually still parent page)
        display(
            IPyHTML(
                f"<script>{bridge_js}</script>"
                "<div style='font:12px system-ui;color:#9ca3af;margin:4px 0'>"
                "sslive: Run bridge active — edit code in the slide, press ▶"
                "</div>"
            )
        )
        print("sslive: parent bridge installed (HTML script tag)")
    else:
        print("sslive: WARNING — could not install parent bridge; in-slide Run will not work")


def _parse_js_eval_result(res: Any) -> Any:
    if res is None:
        return None
    # AttrDict / dict2obj from dialoghelper
    if hasattr(res, "result") and not isinstance(res, (str, bytes, list)):
        try:
            return res.result
        except Exception:
            pass
    if isinstance(res, dict):
        if "result" in res:
            return res["result"]
        if "data" in res:
            return res["data"]
        return res
    return res


async def _call_js_eval(expr: str) -> Any:
    """Call dialoghelper js_eval correctly (sync or async depending on version).

    In current dialoghelper, ``js_eval`` is **sync** and returns AttrDict —
    awaiting it raises: object AttrDict can't be used in 'await' expression.
    """
    if js_eval_a is not None:
        return await js_eval_a(expr)
    if js_eval is None:
        return None
    res = js_eval(expr)
    if inspect.isawaitable(res):
        return await res
    return res


async def _drain_slide_queue() -> list[dict]:
    """Pull pending {cell_id, source} runs from the parent page queue."""
    if js_eval is None and js_eval_a is None:
        return []
    try:
        res = await _call_js_eval(
            "const q = (window.__sslive_q || []).slice(); "
            "window.__sslive_q = []; "
            "return q;"
        )
        q = _parse_js_eval_result(res)
        if q is None:
            return []
        # list-like
        if hasattr(q, "__iter__") and not isinstance(q, (str, bytes, dict)):
            items = list(q)
            out = []
            for item in items:
                if isinstance(item, dict):
                    out.append(item)
                elif hasattr(item, "cell_id"):
                    out.append(
                        {
                            "cell_id": getattr(item, "cell_id", None),
                            "source": getattr(item, "source", ""),
                        }
                    )
            return out
        if isinstance(q, dict) and q.get("cell_id"):
            return [q]
        return []
    except Exception as e:
        if _SESSION.get("_bridge_err") != str(e):
            _SESSION["_bridge_err"] = str(e)
            print(f"sslive: bridge poll error: {e}")
        return []


async def _bridge_poll_loop() -> None:
    """Background: apply in-slide Run requests (edit → GPU; dialog when safe)."""
    was_fs = False
    while _SESSION.get("bridge_active"):
        try:
            pending = await _drain_slide_queue()
            for item in pending:
                if not isinstance(item, dict):
                    continue
                cid = item.get("cell_id")
                source = item.get("source", "")
                if not cid:
                    continue
                sidx = item.get("slide_index")
                try:
                    await _sync_and_run(
                        str(cid),
                        str(source if source is not None else ""),
                        slide_index=int(sidx) if sidx is not None else None,
                    )
                except Exception as e:
                    print(f"sslive: slide Run failed: {e}")

            # When leaving fullscreen, flush dialog sources queued during FS runs
            in_fs = await _parent_in_fullscreen()
            if was_fs and not in_fs:
                n = await _flush_pending_dialog_sync(refocus=True)
                if n:
                    print(f"sslive: flushed {n} dialog sync(s) after exiting fullscreen")
            was_fs = in_fs
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"sslive: bridge loop: {e}")
        await asyncio.sleep(0.2)


def _start_bridge() -> None:
    """Install parent listener + start async poll task."""
    _install_parent_bridge()
    _SESSION["bridge_active"] = True
    # cancel previous task if any
    old = _SESSION.get("bridge_task")
    if old is not None:
        try:
            old.cancel()
        except Exception:
            pass
    try:
        task = asyncio.get_running_loop().create_task(_bridge_poll_loop())
        _SESSION["bridge_task"] = task
        print("sslive: bridge poll started (in-slide ▶ → GPU + dialog sync)")
    except RuntimeError:
        # no running loop — try ensure future later
        print("sslive: no running event loop for bridge; use await pump_slide_runs()")


async def pump_slide_runs(max_items: int = 20) -> int:
    """Manually drain in-slide Run queue (if background poll is not running)."""
    n = 0
    for _ in range(max_items):
        pending = await _drain_slide_queue()
        if not pending:
            break
        for item in pending:
            if not isinstance(item, dict) or not item.get("cell_id"):
                continue
            await _sync_and_run(
                str(item["cell_id"]),
                str(item.get("source") or ""),
            )
            n += 1
    return n


def _show_run_panel(deck: Deck) -> None:
    """Short help under the deck (edit happens *inside* the slides)."""
    if display is None or IPyHTML is None:
        return
    html = """
<div style="font:14px system-ui;color:#e5e7eb;margin:10px 0;padding:12px 14px;
            background:#0b1220;border:1px solid #374151;border-radius:8px">
  <b>RISE-style:</b> edit in the slide → <b>▶ Run</b> / <b>Shift+Enter</b> → GPU.
  <div style="margin-top:8px;font-size:12px;color:#9ca3af">
    <b>Fullscreen:</b> stays in the slide; dialog sync is queued until you exit FS
    (focusing the dialog would exit fullscreen).<br/>
    <b>Preview:</b> runs + syncs dialog source, then returns focus to the slide
    (no full rebuild/flash).
  </div>
  <div style="margin-top:6px;font-size:12px;color:#9ca3af">
    <code>await sync_dialog()</code> · <code>await pump_slide_runs()</code>
  </div>
</div>
"""
    display(IPyHTML(html))


def _show_presenter(port: int | None, height: str = "720px"):
    """Embed deck (srcdoc) with in-slide editors + Run bridge."""
    if isinstance(height, int):
        height = f"{height}px"
    _SESSION["height"] = height

    _start_bridge()

    try:
        if display is not None and IPyHTML is not None:
            iframe = IPyHTML(_presenter_iframe_html(height, port=port))
            handle = display(iframe, display_id=True)
            _SESSION["presenter_handle"] = handle
            print("sslive: slides ready — edit code in the iframe, ▶ Run / Shift+Enter")
    except Exception as e:
        print(f"sslive: srcdoc embed failed: {e}")

    deck = _SESSION.get("deck")
    if deck is not None:
        _show_run_panel(deck)

    if port and HTMX is not None and not _in_solveit():
        try:
            preview = HTMX("/sslive", host="localhost", port=port, height=height)
            if display is not None:
                display(preview)
        except Exception:
            pass


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
        return f"http://127.0.0.1:{self.port}/sslive"

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
            "in_solveit": _in_solveit(),
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
    use_http: bool | None = None,
):
    """Load deck with **in-slide editable code** (RISE-style).

    ::

        %local
        await slive()
        # Click the slide iframe, edit the code box, press ▶ or Shift+Enter
        # → writes source to SolveIt cell + runs on GPU + refreshes outputs

    Fallback if bridge stalls: ``await pump_slide_runs()`` or ``await run_cell_index(0)``.
    """
    _ensure_local_magic()

    ok, msg = LiveExecutor().kernel_ok()
    if not ok:
        print(f"sslive: GPU not ready — {msg}")
        print("Load CRAFT and run %gpu, then call await slive() again under %local.")
        return None

    if use_http is None:
        use_http = not _in_solveit()

    theme_dict = theme if isinstance(theme, dict) else dict(THEME_DARK)
    deck = await build_deck(theme=theme_dict)
    executor = LiveExecutor()
    _SESSION["deck"] = deck
    _SESSION["executor"] = executor
    _SESSION["echo_to_dialog"] = echo_to_dialog
    _SESSION["theme"] = theme_dict

    port: int | None = None
    if use_http:
        if (
            _SESSION.get("server") is not None
            and _SESSION.get("port") is not None
            and _SESSION.get("sslive_rt") is not None
        ):
            port = int(_SESSION["port"])
        else:
            if _SESSION.get("server") is not None:
                try:
                    sstop()
                except Exception:
                    pass
            port = _ensure_live_server()
    else:
        _SESSION["port"] = None

    session = LiveSession(port=port or 0, backend="gpu", deck=deck)

    n_code = len(deck.ordered_code_ids)
    print(
        f"sslive: {len(deck.slides)} slides, {n_code} code cells, "
        f"backend=gpu ({msg}), IN_SOLVEIT={_in_solveit()}"
    )
    if n_code == 0 and len(deck.slides) == 0:
        print("No slides found — add a note with exactly `#| s`, then `#` / `##` content below it.")
    _SESSION["slide_index"] = 0
    _SESSION.setdefault("pending_dialog_sync", {})
    print("In-slide: edit · ▶ Run / Shift+Enter → GPU")
    print("Fullscreen: stays put; dialog sync when you exit FS (or await sync_dialog())")
    print("Preview: updates in place + dialog sync + refocus slide")

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


async def run_cell(
    cell_id: str,
    *,
    source: str | None = None,
    reload_from_dialog: bool = False,
    echo_to_dialog: bool | None = None,
    refresh: bool = True,
) -> ExecResult:
    """Run a code cell on GPU (in-place slide update; no dialog write-back).

    Prefer **in-slide edit + ▶**. For dialog sync after the talk: ``await sync_dialog()``.
    """
    deck: Deck | None = _SESSION.get("deck")
    executor: LiveExecutor | None = _SESSION.get("executor")
    if deck is None or executor is None:
        raise RuntimeError("Call await slive() first")
    if cell_id not in deck.cells or deck.cells[cell_id].kind != "code":
        raise KeyError(f"not a code cell: {cell_id!r}")

    if source is None and reload_from_dialog:
        source = await fetch_dialog_source(cell_id)
        print(f"sslive: loaded {cell_id} from dialog ({len(source)} chars)")

    if echo_to_dialog is None:
        echo_to_dialog = bool(_SESSION.get("echo_to_dialog", False))
    old = _SESSION.get("echo_to_dialog")
    _SESSION["echo_to_dialog"] = echo_to_dialog
    try:
        if source is not None:
            result = await _sync_and_run(cell_id, source)
        elif refresh:
            result = _run_and_refresh(cell_id, source=source)
        else:
            result = executor.execute_cell(deck, cell_id, echo_to_dialog=echo_to_dialog)
            print(
                f"run_cell({cell_id!r}): ok={result.ok} parts={len(result.parts)} "
                f"ms={result.duration_ms}" + (f" err={result.error}" if result.error else "")
            )
    finally:
        _SESSION["echo_to_dialog"] = old
    return result


async def run_cell_index(i: int = 0, **kw) -> ExecResult:
    """Run ordered code cell ``i`` (deck / optional dialog reload)."""
    deck: Deck | None = _SESSION.get("deck")
    if deck is None:
        raise RuntimeError("Call await slive() first")
    if not deck.ordered_code_ids:
        raise RuntimeError("No code cells in deck")
    if i < 0 or i >= len(deck.ordered_code_ids):
        raise IndexError(f"code cell index {i} out of range")
    return await run_cell(deck.ordered_code_ids[i], **kw)


async def reload_deck(theme: str | dict | None = None) -> Deck:
    """Rebuild deck from the dialog (after structural edits) and refresh slides."""
    theme_dict = (
        theme
        if isinstance(theme, dict)
        else (_SESSION.get("theme") or dict(THEME_DARK))
    )
    if isinstance(theme, str):
        theme_dict = dict(THEME_DARK)
    deck = await build_deck(theme=theme_dict)
    _SESSION["deck"] = deck
    _SESSION["theme"] = theme_dict
    if _SESSION.get("executor") is None:
        _SESSION["executor"] = LiveExecutor()
    refresh_presenter()
    _show_run_panel(deck)
    print(f"sslive: reloaded deck — {len(deck.slides)} slides, {len(deck.ordered_code_ids)} code cells")
    return deck


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
    "reload_deck",
    "fetch_dialog_source",
    "write_back_cell",
    "sync_dialog",
    "pump_slide_runs",
    "deck_summary",
    "refresh_presenter",
    "refocus_presenter",
    "render_output_html",
    "generate_presenter_html",
    "get_craft_exec_mgr",
]
