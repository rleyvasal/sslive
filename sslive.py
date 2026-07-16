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
    theme = theme or {}
    out_cls = theme.get("output", "bg-gray-800 p-2 text-sm font-mono text-gray-100")
    err_cls = theme.get("error", "bg-red-900 text-red-200 p-2 text-sm font-mono")
    img_cls = theme.get("output-image", "max-w-full max-h-96 object-contain")

    chunks: list[str] = []
    for p in parts:
        if p.kind == "stream":
            chunks.append(f'<pre class="{out_cls}">{html_module.escape(p.text)}</pre>')
        elif p.kind == "error":
            chunks.append(f'<pre class="{err_cls}">{html_module.escape(p.text)}</pre>')
        elif p.kind == "image/png" and p.b64:
            chunks.append(
                f'<img src="data:image/png;base64,{p.b64}" class="{img_cls}" alt="output"/>'
            )
        elif p.kind == "text/html":
            chunks.append(f'<div class="sslive-html">{p.text}</div>')
        elif p.kind == "text/plain":
            chunks.append(f'<pre class="{out_cls}">{html_module.escape(p.text)}</pre>')

    if not chunks:
        chunks.append(f'<pre class="{out_cls} opacity-50">(no output)</pre>')

    inner = "\n".join(chunks)
    return (
        f'<div id="el-output-{html_module.escape(cell_id)}" '
        f'data-type="output" data-cell-id="{html_module.escape(cell_id)}">'
        f"{inner}</div>"
    )


# ═══════════════════════════════════════════════════════════════════════════
# Piece 4 + 5 — Live host + presenter (stubs for next step)
# ═══════════════════════════════════════════════════════════════════════════

_SESSION: dict[str, Any] = {
    "deck": None,
    "executor": None,
    "server": None,
    "port": None,
    "app": None,
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


# ═══════════════════════════════════════════════════════════════════════════
# Piece 6 — Entry
# ═══════════════════════════════════════════════════════════════════════════

async def slive(
    theme: str | dict = "dark",
    *,
    height: str = "720px",
    echo_to_dialog: bool = False,
):
    """Start foundation session: load deck, verify GPU, print status.

    dialoghelper APIs are async — call from SolveIt as::

        %local
        await slive()

    Full presenter host is the next implementation step. For now this proves
    loader + executor wiring and skips the launcher cell (D6).
    """
    _ensure_local_magic()

    ok, msg = LiveExecutor().kernel_ok()
    if not ok:
        print(f"sslive: GPU not ready — {msg}")
        print("Load CRAFT and run %gpu, then call await slive() again under %local.")
        return None

    deck = await build_deck(theme=theme if isinstance(theme, dict) else {})
    executor = LiveExecutor()
    _SESSION["deck"] = deck
    _SESSION["executor"] = executor
    _SESSION["echo_to_dialog"] = echo_to_dialog

    n_code = len(deck.ordered_code_ids)
    print(
        f"sslive foundation: {len(deck.slides)} slides, {n_code} code cells, "
        f"backend=gpu ({msg})"
    )
    if n_code == 0 and len(deck.slides) == 0:
        print("No slides found — add a note with exactly `#| s`, then `#` / `##` content below it.")
    print("Headless run:  run_cell('<cell_id>')  or  run_cell_index(0)")
    print("Presenter host (GET / + POST /execute) — next step.")

    # D6 — hide launcher from dialog context
    if update_msg is not None:
        try:
            caller_globals = inspect.currentframe().f_back.f_globals
            mid = caller_globals.get("__msg_id")
            if mid:
                await update_msg(id=mid, skipped=1)
        except Exception:
            pass

    return deck


def run_cell(cell_id: str, *, echo_to_dialog: bool | None = None) -> ExecResult:
    """Foundation proof: execute one code cell on GPU by id, print summary."""
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
    "build_deck",
    "slive",
    "run_cell",
    "run_cell_index",
    "deck_summary",
    "render_output_html",
    "get_craft_exec_mgr",
]
