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
        add_msg,
        read_msg,
        js_eval,
        iife,
    )
except Exception:  # pragma: no cover - outside SolveIt
    find_msgs = curr_dialog = update_msg = add_msg = read_msg = js_eval = iife = None

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

# Markdown + LaTeX note rendering (sslides pipeline); falls back to a
# lightweight renderer when unavailable
try:
    from mistletoe import Document as _MdDocument
    from mistletoe.html_renderer import HTMLRenderer as _MdHTMLRenderer
except Exception:  # pragma: no cover
    _MdDocument = _MdHTMLRenderer = None

try:
    from latex2mathml import converter as _l2m
except Exception:  # pragma: no cover
    _l2m = None


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
    # S2 layout overlay: {"version":1, "elements":{el_id: spec}, "deck":{}}.
    # Kept JSON-shaped (not on Element) so unknown el_ids survive cell deletion
    # and the whole thing round-trips to the hidden dialog message untouched.
    layout: dict = field(default_factory=lambda: _empty_layout())

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

    try:
        deck.layout = await load_layout()
    except Exception as e:
        print(f"sslive: layout load failed ({e}) — starting with empty overlay")

    return deck


# ═══════════════════════════════════════════════════════════════════════════
# Piece 2b — Layout overlay (S2-A): position/size/font/order per element
# ═══════════════════════════════════════════════════════════════════════════
#
# Coordinates are design-space px (1920×1080 stage); the presenter scale
# transform makes them viewport-independent. Persisted as JSON in a hidden
# dialog note starting with `#| sslive-layout` (skipped=1 keeps it out of
# both the slides loader and the LLM context).

LAYOUT_MARKER = "#| sslive-layout"
_UNSET = object()
_ALIGN_VALUES = {"left", "center", "right", "justify"}
_LAYOUT_KEYS = ("x", "y", "w", "h", "z", "order", "fs", "ff", "align")
_FF_SAFE_RE = re.compile(r"[^\w\s,'\"-]")


def _empty_layout() -> dict:
    return {"version": 1, "elements": {}, "deck": {}}


def _normalize_layout(data: Any) -> dict:
    lay = _empty_layout()
    if isinstance(data, dict):
        els = data.get("elements")
        if isinstance(els, dict):
            lay["elements"] = {
                str(k): dict(v) for k, v in els.items() if isinstance(v, dict)
            }
        dk = data.get("deck")
        if isinstance(dk, dict):
            lay["deck"] = dict(dk)
    return lay


def _layout_msg_content(layout: dict) -> str:
    return LAYOUT_MARKER + "\n" + json.dumps(layout, indent=1)


def _parse_layout_msg(content: str) -> dict | None:
    """Overlay dict if ``content`` is a layout message, else None."""
    if not content or not content.lstrip().startswith(LAYOUT_MARKER):
        return None
    body = content.lstrip()[len(LAYOUT_MARKER):]
    try:
        return _normalize_layout(json.loads(body))
    except Exception:
        return None


async def _find_layout_msg() -> tuple[str, dict] | None:
    """(msg_id, overlay) for the hidden layout note, if the dialog has one."""
    if find_msgs is None:
        return None
    try:
        for m in await find_msgs():
            if m.get("msg_type") != "note":
                continue
            lay = _parse_layout_msg(m.get("content", "") or "")
            if lay is not None:
                return m["id"], lay
    except Exception as e:
        print(f"sslive: layout lookup failed: {e}")
    return None


async def load_layout() -> dict:
    """Read the layout overlay from the dialog (empty overlay when absent)."""
    found = await _find_layout_msg()
    if found is None:
        _SESSION.pop("layout_msg_id", None)
        return _empty_layout()
    mid, lay = found
    _SESSION["layout_msg_id"] = mid
    return lay


async def save_layout(layout: dict | None = None) -> bool:
    """Persist the overlay into the hidden dialog note (created if missing)."""
    if layout is None:
        deck = _SESSION.get("deck")
        layout = deck.layout if deck is not None else _empty_layout()
    if update_msg is None:
        return False
    content = _layout_msg_content(layout)
    _arm_focus_guard(2000)  # update_msg would steal focus in preview otherwise
    mid = _SESSION.get("layout_msg_id")
    if mid:
        try:
            await update_msg(id=mid, content=content)
            return True
        except Exception as e:
            print(f"sslive: layout save to {mid} failed ({e}); re-finding message")
            _SESSION.pop("layout_msg_id", None)
    found = await _find_layout_msg()
    if found is not None:
        _SESSION["layout_msg_id"] = found[0]
        await update_msg(id=found[0], content=content)
        return True
    if add_msg is None:
        return False
    new_id = await add_msg(content, placement="at_start")
    _SESSION["layout_msg_id"] = new_id
    try:
        await update_msg(id=new_id, skipped=1)
    except Exception:
        pass
    return True


def _schedule_layout_save(delay: float = 0.5) -> None:
    """Debounced ``save_layout`` — one dialog write per editing burst."""
    prev = _SESSION.get("_layout_save_task")
    if prev is not None and not prev.done():
        prev.cancel()

    async def _later():
        try:
            await asyncio.sleep(delay)
            await save_layout()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            _SESSION["_layout_save_err"] = str(e)

    try:
        _SESSION["_layout_save_task"] = asyncio.get_running_loop().create_task(_later())
    except RuntimeError:  # no loop (headless tests) — persistence is moot there
        _SESSION["_layout_save_task"] = None


def _layout_spec(deck: "Deck | None", el_id: str) -> dict:
    if deck is None:
        return {}
    els = (deck.layout or {}).get("elements") or {}
    spec = els.get(el_id)
    return spec if isinstance(spec, dict) else {}


def _apply_layout_patch(deck: "Deck", el_id: str, patch: dict) -> dict:
    """Merge a validated patch into the overlay; returns the new spec.

    ``None`` values clear keys; unknown keys are dropped. Shared by
    ``set_layout`` (Python API) and the slide edit-mode bridge (S2-B),
    so both paths sanitize identically.
    """
    els = deck.layout.setdefault("elements", {})
    spec = dict(els.get(el_id) or {})
    for k, v in (patch or {}).items():
        if k not in _LAYOUT_KEYS:
            continue
        if v is None:
            spec.pop(k, None)
        elif k in ("z", "order"):
            spec[k] = int(v)
        elif k == "ff":
            spec[k] = str(v)
        elif k == "align":
            if v not in _ALIGN_VALUES:
                raise ValueError(f"align must be one of {sorted(_ALIGN_VALUES)}")
            spec[k] = v
        else:
            spec[k] = float(v)
    if spec:
        els[el_id] = spec
    else:
        els.pop(el_id, None)
    return spec


def _css_len(v: Any) -> str | None:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f"{int(f)}" if f.is_integer() else f"{f:g}"


def _el_style(spec: dict) -> str:
    """Inline CSS for one element's layout spec (values sanitized).

    ``--code-fs`` mirrors ``fs`` so the code textarea follows too (its own
    font-size reads that var; ``ff`` is never applied to code — stays mono).
    """
    if not spec:
        return ""
    parts: list[str] = []
    x, y = spec.get("x"), spec.get("y")
    if x is not None or y is not None:
        parts.append("position:absolute")
        parts.append(f"left:{_css_len(x) or 0}px")
        parts.append(f"top:{_css_len(y) or 0}px")
        parts.append("margin:0")
    for key, prop in (("w", "width"), ("h", "height")):
        v = _css_len(spec.get(key))
        if v is not None:
            parts.append(f"{prop}:{v}px")
    for key, prop in (("z", "z-index"), ("order", "order")):
        try:
            if spec.get(key) is not None:
                parts.append(f"{prop}:{int(spec[key])}")
        except (TypeError, ValueError):
            pass
    fs = _css_len(spec.get("fs"))
    if fs is not None:
        parts.append(f"font-size:{fs}px")
        parts.append(f"--code-fs:{fs}px")
    ff = spec.get("ff")
    if ff:
        parts.append(f"font-family:{_FF_SAFE_RE.sub('', str(ff))}")
    align = spec.get("align")
    if align in _ALIGN_VALUES:
        parts.append(f"text-align:{align}")
    return ";".join(parts) + (";" if parts else "")


def _style_attr(style: str) -> str:
    """`` style="…"`` fragment (or empty) — escaped for the HTML attribute."""
    return f' style="{html_module.escape(style)}"' if style else ""


def _push_layout(el_id: str) -> None:
    """Apply one element's overlay style in the live iframe (no rebuild)."""
    deck = _SESSION.get("deck")
    seq = int(_SESSION.get("_layout_push_seq") or int(time.time() * 1000)) + 1
    _SESSION["_layout_push_seq"] = seq
    payload = {
        "type": "sslive_layout_apply",
        "el_id": el_id,
        "style": _el_style(_layout_spec(deck, el_id)),
        "t": seq,
    }
    if iife is None:
        return
    js = f"""
(function() {{
  var msg = {json.dumps(payload)};
  window.__sslive_last_layout = msg;
  document.querySelectorAll('iframe').forEach(function(f) {{
    try {{ f.contentWindow.postMessage(msg, '*'); }} catch (e) {{}}
  }});
}})();
"""
    try:
        iife(js)
    except Exception as e:
        _SESSION["_last_push_err"] = str(e)


async def set_layout(
    el_id: str,
    *,
    x: Any = _UNSET,
    y: Any = _UNSET,
    w: Any = _UNSET,
    h: Any = _UNSET,
    z: Any = _UNSET,
    order: Any = _UNSET,
    fs: Any = _UNSET,
    ff: Any = _UNSET,
    align: Any = _UNSET,
    save: bool = True,
) -> dict:
    """Position/style one slide element. Design space is 1920×1080 px.

    ::

        await set_layout('el-note-_abc123', x=120, y=80, w=800, fs=36)
        await set_layout('el-output-_def456', x=1100, y=200, w=700)
        await set_layout('el-note-_abc123', x=None, y=None)  # back to flow

    Omit a param to leave it unchanged; pass ``None`` to clear it.
    ``x``/``y`` position absolutely; without them the element stays in flow
    and ``order`` controls its flex position. ``fs`` px, ``ff`` CSS family,
    ``z`` stacking, ``align`` left/center/right/justify.
    Applied live in the iframe; persisted (debounced) to the dialog.
    """
    deck: Deck | None = _SESSION.get("deck")
    if deck is None:
        raise RuntimeError("Call await slive() first")
    if el_id not in deck.elements:
        raise KeyError(f"unknown element {el_id!r} — see layout_ids() for options")
    updates = {
        "x": x, "y": y, "w": w, "h": h, "z": z,
        "order": order, "fs": fs, "ff": ff, "align": align,
    }
    spec = _apply_layout_patch(
        deck, el_id, {k: v for k, v in updates.items() if v is not _UNSET}
    )
    _push_layout(el_id)
    if save:
        _schedule_layout_save()
    return spec


async def clear_layout(el_id: str | None = None, *, save: bool = True) -> int:
    """Remove layout overrides for one element (or all when ``el_id`` is None)."""
    deck: Deck | None = _SESSION.get("deck")
    if deck is None:
        raise RuntimeError("Call await slive() first")
    els = deck.layout.setdefault("elements", {})
    targets = [el_id] if el_id else list(els)
    n = 0
    for eid in targets:
        if els.pop(eid, None) is not None:
            n += 1
        _push_layout(eid)
    if save and n:
        _schedule_layout_save()
    return n


def layout_ids(deck: "Deck | None" = None) -> list[str]:
    """Element ids for ``set_layout``, annotated with kind/slide (* = has overrides)."""
    deck = deck or _SESSION.get("deck")
    if deck is None:
        return []
    overrides = (deck.layout or {}).get("elements") or {}
    out: list[str] = []
    for slide in deck.slides:
        for cid in slide.cell_ids:
            for eid in deck.cells[cid].element_ids:
                el = deck.elements[eid]
                mark = " *" if overrides.get(eid) else ""
                out.append(f"{eid}  [{el.kind}, slide {slide.index}]{mark}")
    return out


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

def render_output_html(
    parts: list[OutputPart], cell_id: str, theme: dict | None = None, *, style: str = ""
) -> str:
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
    eid = html_module.escape(f"el-output-{cell_id}")
    return (
        f'<div id="{eid}" data-el-id="{eid}" '
        f'data-type="output" data-cell-id="{html_module.escape(cell_id)}"{_style_attr(style)}>'
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


def _math_to_mathml(latex_str: str) -> str:
    """`$...$` / `$$...$$` → MathML (display math centered as a block)."""
    if _l2m is None:
        return html_module.escape(latex_str)
    if latex_str.startswith("$$"):
        mathml = _l2m.convert(latex_str[2:-2].strip())
        mathml = mathml.replace('display="inline"', 'display="block"')
        return f'<div class="math-block">{mathml}</div>'
    return _l2m.convert(latex_str[1:-1])


def _note_to_html_md(content: str) -> str:
    """Full note render: mistletoe markdown + latex2mathml (sslides pipeline).

    Math is pulled out before markdown so `_`/`^`/`\\` survive, using plain
    alphanumeric placeholders (markdown-inert). Unlike sslides' global HTML
    unescape, this leaves entities inside code spans intact (`` `<div>` ``
    stays escaped). Raw HTML in notes passes through — mistletoe default,
    and the content is the author's own dialog.
    """
    math_blocks: list[str] = []

    def save_math(m: "re.Match[str]") -> str:
        math_blocks.append(m.group(0))
        return f"SSLIVEMATH{len(math_blocks) - 1}X"

    content = re.sub(r"\$\$(.*?)\$\$", save_math, content, flags=re.DOTALL)
    content = re.sub(r"\$([^\$\n]+)\$", save_math, content)

    with _MdHTMLRenderer() as renderer:
        html = renderer.render(_MdDocument(content))

    for i, block in enumerate(math_blocks):
        try:
            rep = _math_to_mathml(block)
        except Exception:
            rep = html_module.escape(block)
        html = html.replace(f"SSLIVEMATH{i}X", rep)
    return html


def _note_to_html(source: str) -> str:
    """Note → HTML. Markdown + LaTeX when mistletoe is available."""
    if _MdDocument is not None and _MdHTMLRenderer is not None:
        try:
            return _note_to_html_md(source or "")
        except Exception as e:
            _SESSION["_md_render_err"] = str(e)
    return _note_to_html_basic(source)


def _note_to_html_basic(source: str) -> str:
    """Lightweight fallback render (headers + paragraphs, no markdown/math)."""
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


def _code_block_html(cell: Cell, *, style: str = "") -> str:
    """In-slide editable code (textarea) + Run — RISE-style."""
    cid = html_module.escape(cell.id)
    # raw id for JS (safe: dialog ids are alphanumeric + underscore)
    raw_id = cell.id
    src = html_module.escape(cell.source)
    n_lines = max(3, min(24, cell.source.count("\n") + 2))
    # ~1.45em line height in code-ta
    ta_h = max(72, int(n_lines * 22))
    return f"""
    <div id="el-code-{cid}" class="code-wrap" data-el-id="el-code-{cid}" data-type="code"
         data-cell-id="{cid}" data-runnable="1"{_style_attr(style)}
         tabindex="0" onclick="selectCell('{cid}')">
      <div class="code-toolbar">
        <span class="drag-grip" data-drag-for="el-code-{cid}" title="drag to move">⠿</span>
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
            style = _el_style(_layout_spec(deck, f"el-note-{cid}"))
            parts.append(
                f'<div id="{eid}" class="note-block" data-el-id="{eid}" '
                f'data-cell-id="{html_module.escape(cid)}"{_style_attr(style)}>'
                f"{_note_to_html(cell.source)}</div>"
            )
        else:
            parts.append(
                _code_block_html(cell, style=_el_style(_layout_spec(deck, f"el-code-{cid}")))
            )
            # always emit output mount (seeded from last outputs / notebook)
            parts.append(
                render_output_html(
                    cell.outputs, cell.id, deck.theme or THEME_DARK,
                    style=_el_style(_layout_spec(deck, f"el-output-{cid}")),
                )
            )
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
      justify-content:flex-start; align-items:stretch; overflow:auto; gap:12px; position:relative; }}
    .slide.active {{ display:flex; }}
    .slide.hidden {{ display:none; }}
    .title-slide {{ justify-content:center; align-items:center; text-align:center; }}
    /* note typography in em off .note-block so one font-size override (layout
       overlay `fs`) scales headings + body proportionally; defaults unchanged
       (28px base: 2.5714em≈72px h1, 1.7143em≈48px h2 — the old rem values) */
    .note-block {{ font-size:1.75rem; }}
    .slide-h1, .note-block h1 {{ font-size:2.5714em; font-weight:700; margin:0 0 1rem; }}
    .slide-h2, .note-block h2 {{ font-size:1.7143em; font-weight:700; margin:0 0 1rem; }}
    .note-block h3 {{ font-size:1.3em; font-weight:700; margin:0 0 0.75rem; }}
    .slide-p, .note-block p {{ font-size:1em; line-height:1.5; margin:0.5rem 0; color:{theme.get("fg", "#eee")}; }}
    .note-block ul, .note-block ol {{ font-size:1em; line-height:1.5; margin:0.5rem 0; padding-left:1.4em; }}
    .note-block li {{ margin:0.2em 0; }}
    .note-block code {{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:0.8em;
      background:{theme.get("code_bg", "#1f2937")}; border:1px solid #374151; border-radius:4px;
      padding:0.08em 0.35em; }}
    .note-block pre {{ background:#111827; border:1px solid #374151; border-radius:6px;
      padding:0.6em 0.8em; overflow-x:auto; }}
    .note-block pre code {{ background:none; border:0; padding:0; }}
    .note-block img {{ max-width:100%; }}
    .note-block a {{ color:#60a5fa; }}
    .note-block blockquote {{ border-left:3px solid #4b5563; margin:0.5rem 0;
      padding:0 0 0 0.8em; color:{theme.get("muted", "#9ca3af")}; }}
    .note-block .math-block {{ text-align:center; margin:0.8em 0; }}
    .note-block math {{ font-size:1.1em; }}
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
      font-family:ui-monospace,SFMono-Regular,Menlo,monospace; line-height:1.45;
      font-size:var(--code-fs, 14px); white-space:pre;
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
    /* ── edit mode (S2-B): outlines + drag affordances ── */
    #edit-btn.on {{ color:#f59e0b; }}
    #edit-badge {{ display:none; color:#f59e0b; font-weight:600; }}
    body.editing #edit-badge {{ display:inline; }}
    body.editing .note-block, body.editing [data-type="output"] {{
      outline:1px dashed rgba(96,165,250,0.5); outline-offset:2px; cursor:move; }}
    body.editing .code-wrap {{ outline:1px dashed rgba(96,165,250,0.5); outline-offset:2px; }}
    body.editing .el-editsel {{ outline:2px solid #f59e0b; outline-offset:2px; }}
    body.editing img {{ -webkit-user-drag:none; user-drag:none; }}
    body.editing .code-toolbar {{ cursor:move; }}
    .drag-grip {{ display:none; cursor:move; user-select:none; color:#9ca3af;
      font-size:18px; padding:2px 6px; touch-action:none; }}
    body.editing .drag-grip {{ display:inline-block; }}
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

    let lastLayoutT = 0;
    function applyLayoutMsg(msg) {{
      // Overwrite the element's inline style with the overlay style (both are
      // generated by the same Python _el_style — wholesale replace is correct)
      if (!msg || !msg.el_id) return;
      if (msg.t && msg.t <= lastLayoutT) return;
      if (msg.t) lastLayoutT = msg.t;
      const el = document.getElementById(msg.el_id);
      if (!el) return;
      el.style.cssText = msg.style || '';
    }}

    // ── S2-B edit mode: select / drag / nudge → layout patches to parent ──
    let editing = false;
    let editSel = null;
    let drag = null;
    let nudgeTimer = null;

    function setEditing(on) {{
      editing = !!on;
      document.body.classList.toggle('editing', editing);
      document.getElementById('edit-btn')?.classList.toggle('on', editing);
      if (!editing) selectEl(null);
    }}

    function selectEl(el) {{
      if (editSel) editSel.classList.remove('el-editsel');
      editSel = el || null;
      if (editSel) editSel.classList.add('el-editsel');
    }}

    function sendLayoutPatch(elId, patch) {{
      try {{
        window.parent.postMessage(
          {{ type: 'sslive_layout', el_id: elId, patch: patch, t: Date.now() }}, '*');
      }} catch (e) {{}}
    }}

    function ensureAbs(el) {{
      // Pin a flow element at its current visual spot (no jump); freeze the
      // rendered width so text keeps its wrap after leaving the flex flow.
      const slide = el.closest('[data-slide]');
      const sr = slide.getBoundingClientRect();
      const er = el.getBoundingClientRect();
      const sc = sr.width / 1920 || 1;
      const converted = el.style.position !== 'absolute';
      const x = Math.round((er.left - sr.left) / sc);
      const y = Math.round((er.top - sr.top) / sc);
      const w = Math.round(er.width / sc);
      if (converted) {{
        el.style.width = w + 'px';
        el.style.position = 'absolute';
        el.style.margin = '0';
      }}
      el.style.left = x + 'px';
      el.style.top = y + 'px';
      return {{ x: x, y: y, w: w, converted: converted }};
    }}

    function beginDrag(el, ev) {{
      // Conversion to absolute is deferred to the first real movement —
      // a plain click must not change the element's layout mode.
      const slide = el.closest('[data-slide]');
      const sc = slide ? (slide.getBoundingClientRect().width / 1920 || 1) : 1;
      drag = {{ el: el, elId: el.dataset.elId || el.id, sx: ev.clientX, sy: ev.clientY,
               ox: 0, oy: 0, w: 0, includeW: false, sc: sc, started: false }};
      ev.preventDefault();
      try {{ el.setPointerCapture(ev.pointerId); }} catch (e) {{}}
    }}

    function nudgeSel(dx, dy) {{
      if (!editSel) return;
      const cur = ensureAbs(editSel);
      const nx = cur.x + dx, ny = cur.y + dy;
      editSel.style.left = nx + 'px';
      editSel.style.top = ny + 'px';
      const elId = editSel.dataset.elId || editSel.id;
      const patch = {{ x: nx, y: ny }};
      if (cur.converted) patch.w = cur.w;
      clearTimeout(nudgeTimer);
      nudgeTimer = setTimeout(() => sendLayoutPatch(elId, patch), 350);
    }}

    document.addEventListener('pointerdown', (e) => {{
      if (!editing || e.button !== 0) return;
      const grip = e.target.closest('.drag-grip');
      if (grip) {{
        const el = document.getElementById(grip.dataset.dragFor);
        if (el) {{ selectEl(el); beginDrag(el, e); }}
        return;
      }}
      // interactive bits keep working while editing
      if (e.target.closest('textarea, button, a, input, select')) return;
      // code cells drag by their toolbar strip (big target; textarea untouched)
      const tb = e.target.closest('.code-toolbar');
      if (tb) {{
        const cw = tb.closest('.code-wrap');
        if (cw) {{ selectEl(cw); beginDrag(cw, e); return; }}
      }}
      const el = e.target.closest('.note-block, [data-type="output"]');
      if (el) {{ selectEl(el); beginDrag(el, e); return; }}
      const cw = e.target.closest('.code-wrap');
      selectEl(cw || null);  // code body: select only; move via toolbar/arrows
    }}, true);

    document.addEventListener('pointermove', (e) => {{
      if (!drag) return;
      if (!drag.started) {{
        if (Math.abs(e.clientX - drag.sx) + Math.abs(e.clientY - drag.sy) <= 2) return;
        const start = ensureAbs(drag.el);
        drag.ox = start.x; drag.oy = start.y;
        drag.w = start.w; drag.includeW = start.converted;
        drag.started = true;
      }}
      drag.el.style.left = (drag.ox + (e.clientX - drag.sx) / drag.sc) + 'px';
      drag.el.style.top = (drag.oy + (e.clientY - drag.sy) / drag.sc) + 'px';
    }});

    document.addEventListener('pointerup', (e) => {{
      if (!drag) return;
      if (drag.started) {{
        const patch = {{
          x: Math.round(parseFloat(drag.el.style.left) || 0),
          y: Math.round(parseFloat(drag.el.style.top) || 0)
        }};
        if (drag.includeW) patch.w = drag.w;
        sendLayoutPatch(drag.elId, patch);
      }}
      drag = null;
    }});

    document.addEventListener('dragstart', (e) => {{ if (editing) e.preventDefault(); }});

    window.addEventListener('message', function (e) {{
      if (!e.data) return;
      if (e.data.type === 'sslive_result') applyRunResult(e.data);
      else if (e.data.type === 'sslive_layout_apply') applyLayoutMsg(e.data);
    }});

    // More reliable than postMessage into srcdoc: poll parent for last result
    setInterval(function () {{
      try {{
        const r = window.parent.__sslive_last_result;
        if (r && r.type === 'sslive_result') applyRunResult(r);
        const l = window.parent.__sslive_last_layout;
        if (l && l.type === 'sslive_layout_apply') applyLayoutMsg(l);
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
      if (e.key === 'e' && !e.metaKey && !e.ctrlKey && !e.altKey) {{
        setEditing(!editing);
        return;
      }}
      if (e.key === 'Escape' && editing) {{
        setEditing(false);  // exits directly (and deselects); ✎/e toggle back
        return;
      }}
      if (editing && editSel && e.key.startsWith('Arrow')) {{
        e.preventDefault();
        const step = e.shiftKey ? 10 : 1;
        if (e.key === 'ArrowLeft')  nudgeSel(-step, 0);
        if (e.key === 'ArrowRight') nudgeSel(step, 0);
        if (e.key === 'ArrowUp')    nudgeSel(0, -step);
        if (e.key === 'ArrowDown')  nudgeSel(0, step);
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
    document.getElementById('edit-btn')?.addEventListener('click', () => setEditing(!editing));

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
    <span id="edit-badge">✎ edit</span>
    <span style="opacity:0.7">Shift+Enter run · ←/→ slides · f fullscreen · e edit</span>
  </div>
  <div id="viewport">
    <div id="stage">
      <div id="slides-container">
        {slides_html or empty}
      </div>
    </div>
  </div>
  <div id="nav">
    <button type="button" id="edit-btn" title="edit layout (e)" aria-label="Edit layout">✎</button>
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

    # Keep overlay position/size when the output block is replaced in-place
    out_style = _el_style(_layout_spec(deck, f"el-output-{cell_id}"))
    html = render_output_html(result.parts or [], cell_id, theme, style=out_style)
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
  function findFrame() {
    return document.getElementById('sslive-frame')
      || document.querySelector('iframe[data-sslive="1"]')
      || document.querySelector('iframe[srcdoc]');
  }
  function focusFrame() {
    // Never run this thrash while document is fullscreen (would exit FS)
    var fs = document.fullscreenElement || document.webkitFullscreenElement;
    if (fs) return true;

    // Focus never left the slides (guard worked) — no thrash needed
    var cur = findFrame();
    if (cur && document.activeElement === cur) return true;

    try {
      var ae = document.activeElement;
      if (ae && ae !== document.body && ae.tagName !== 'IFRAME'
          && ae.id !== 'sslive-frame' && ae.getAttribute('data-sslive') !== '1') {
        try { ae.blur(); } catch (e) {}
      }
    } catch (e) {}

    var ifr = findFrame();
    if (!ifr) return false;

    // Keep the presentation visible in the dialog scroller
    try {
      ifr.scrollIntoView({ block: 'center', inline: 'nearest', behavior: 'instant' });
    } catch (e) {
      try { ifr.scrollIntoView(true); } catch (e2) {}
    }
    // Prefer focusing the iframe without scrolling the page again
    try { ifr.focus({ preventScroll: true }); } catch (e) {
      try { ifr.focus(); } catch (e2) {}
    }
    try {
      if (ifr.contentWindow) {
        ifr.contentWindow.focus();
        // Prefer the code textarea the user was editing
        var doc = ifr.contentDocument || ifr.contentWindow.document;
        if (doc) {
          var ta = doc.querySelector('textarea.code-ta.selected, .code-wrap.selected textarea, textarea.code-ta');
          if (ta) {
            try { ta.focus({ preventScroll: true }); } catch (e3) { try { ta.focus(); } catch (e4) {} }
          }
        }
      }
    } catch (e) {}
    return true;
  }
  focusFrame();
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


def _arm_focus_guard(ms: int = 2000) -> None:
    """Pre-empt SolveIt's focus/scroll steal for the next ``ms`` (parent page).

    The parent bridge patches ``focus``/``scrollIntoView`` while armed, so the
    dialog write-back never visibly yanks focus away from the slide iframe.
    Call *before* ``update_msg``; user gestures in the dialog override it.
    """
    if iife is None:
        return
    try:
        iife(f"window.__sslive_guard_until = Date.now() + {int(ms)};")
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
    """True if document or the sslive iframe is fullscreen."""
    try:
        if js_eval is None and js_eval_a is None:
            return False
        res = await _call_js_eval(
            "return !!(document.fullscreenElement "
            "|| document.webkitFullscreenElement "
            "|| document.mozFullScreenElement);"
        )
        return bool(_parse_js_eval_result(res))
    except Exception:
        return False


def _queue_dialog_sync(cell_id: str, source: str) -> None:
    _SESSION.setdefault("pending_dialog_sync", {})[cell_id] = source


async def _flush_pending_dialog_sync(*, refocus: bool = True) -> int:
    """Write queued slide sources into dialog cells (after leaving fullscreen)."""
    pending = dict(_SESSION.get("pending_dialog_sync") or {})
    if not pending:
        return 0
    _SESSION["pending_dialog_sync"] = {}
    _arm_focus_guard(2000 + 500 * len(pending))
    n = 0
    for cid, src in pending.items():
        if await write_back_cell(cid, src):
            n += 1
    if n and refocus:
        refocus_presenter()
    return n


async def _sync_and_run(cell_id: str, source: str, *, slide_index: int | None = None) -> ExecResult:
    """Update deck → GPU → in-place slide output → quiet deferred dialog write.

    Hot path never calls ``refresh_presenter`` / ``refocus_presenter`` (those
    reset slides / exit fullscreen). Dialog ``update_msg`` runs in a deferred
    task after the slide UI has updated, with no follow-up UI thrash.
    """
    if slide_index is not None:
        _SESSION["slide_index"] = int(slide_index)

    _apply_source_to_deck(cell_id, source)
    _queue_dialog_sync(cell_id, source)

    # Quiet GPU execute + push result into existing iframe (no rebuild)
    result = _run_and_refresh(
        cell_id, source=source, full_refresh=False, quiet=True
    )

    # Deferred dialog write-back (unified source). Never refresh_presenter.
    # After update_msg, SolveIt focuses the dialog cell in *preview* mode —
    # steal focus back to #sslive-frame (fullscreen already keeps focus).
    if _SESSION.get("auto_sync_dialog", True):

        async def _deferred_dialog_write(cid=cell_id, src=source):
            try:
                await asyncio.sleep(0.2)
                # Preview: arm the parent-page guard *before* update_msg so the
                # host's focus/scroll-on-update is swallowed at the source
                # instead of corrected after the fact (no visible jump).
                in_fs = await _parent_in_fullscreen()
                if not in_fs:
                    _arm_focus_guard(2000)
                pending = dict(_SESSION.get("pending_dialog_sync") or {})
                pending[cid] = src
                _SESSION["pending_dialog_sync"] = {}
                for pcid, psrc in pending.items():
                    await write_back_cell(pcid, psrc)
                push_slide_result(cid, result, source=src)
                if not in_fs:
                    # Backstop for host paths the guard can't intercept —
                    # no-op when focus never left the slides.
                    for delay in (0.1, 0.4):
                        await asyncio.sleep(delay)
                        refocus_presenter()
            except Exception as e:
                _SESSION["_dialog_sync_err"] = str(e)

        try:
            asyncio.get_running_loop().create_task(_deferred_dialog_write())
        except RuntimeError:
            try:
                _arm_focus_guard(2000)
                await write_back_cell(cell_id, source)
                refocus_presenter()
            except Exception:
                pass

    return result


async def sync_dialog() -> int:
    """Write current deck code sources into SolveIt dialog cells.

    Also flushes sources queued during fullscreen Runs.
    May move focus in the SolveIt UI (host behavior).
    """
    deck = _SESSION.get("deck")
    if deck is None:
        raise RuntimeError("Call await slive() first")
    pending = _SESSION.get("pending_dialog_sync") or {}
    for cid, src in pending.items():
        if cid in deck.cells:
            _apply_source_to_deck(cid, src)
    _SESSION["pending_dialog_sync"] = {}

    _arm_focus_guard(2000 + 500 * len(deck.ordered_code_ids))
    n = 0
    for cid in deck.ordered_code_ids:
        src = deck.cells[cid].source
        if await write_back_cell(cid, src):
            n += 1
            print(f"sslive: dialog sync {cid} ({len(src)} chars)")
    print(f"sslive: synced {n}/{len(deck.ordered_code_ids)} code cells → dialog")
    refocus_presenter()
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
// Layout patch queue (S2-B): edit-mode drag/nudge patches from the slide.
// Own flag so it installs on pages that already have an older bridge.
if (!window.__sslive_layout_bridge_v1) {
  window.__sslive_layout_bridge_v1 = true;
  window.__sslive_layout_q = window.__sslive_layout_q || [];
  window.addEventListener('message', function (e) {
    var d = e.data;
    if (!d || d.type !== 'sslive_layout' || !d.el_id) return;
    window.__sslive_layout_q.push({
      el_id: String(d.el_id),
      patch: d.patch || {},
      t: d.t || Date.now()
    });
  });
}
// Focus guard: pre-empt SolveIt's focus/scroll-on-update after update_msg.
// Armed from Python (window.__sslive_guard_until) just before dialog
// write-back. Real user gestures in the dialog always win (see __sslive_user_ts).
// Separate flag from __sslive_bridge so it installs on pages with an old bridge.
if (!window.__sslive_guard_v1) {
  window.__sslive_guard_v1 = true;
  window.__sslive_guard_until = 0;
  window.__sslive_user_ts = 0;
  var slFrame = function () {
    return document.getElementById('sslive-frame')
      || document.querySelector('iframe[data-sslive="1"]');
  };
  var slArmed = function () {
    return Date.now() < (window.__sslive_guard_until || 0)
      && Date.now() - (window.__sslive_user_ts || 0) > 400;
  };
  var slAllowed = function (el) {
    var ifr = slFrame();
    return !ifr || el === ifr || (el && ifr.contains && ifr.contains(el));
  };
  window.addEventListener('pointerdown', function () { window.__sslive_user_ts = Date.now(); }, true);
  window.addEventListener('keydown', function () { window.__sslive_user_ts = Date.now(); }, true);
  var slFocus = HTMLElement.prototype.focus;
  HTMLElement.prototype.focus = function () {
    if (slArmed() && !slAllowed(this)) return;
    return slFocus.apply(this, arguments);
  };
  var slScroll = Element.prototype.scrollIntoView;
  Element.prototype.scrollIntoView = function () {
    if (slArmed() && !slAllowed(this)) return;
    return slScroll.apply(this, arguments);
  };
  // Backstop for focus paths that bypass .focus() (e.g. autofocus in swapped
  // HTMX content): bounce focus back to the iframe within the same tick.
  document.addEventListener('focusin', function (e) {
    if (!slArmed() || slAllowed(e.target)) return;
    var ifr = slFrame();
    if (!ifr) return;
    try { ifr.focus({ preventScroll: true }); } catch (err) { try { ifr.focus(); } catch (e2) {} }
    try { if (ifr.contentWindow) ifr.contentWindow.focus(); } catch (err) {}
  }, true);
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


def _item_dicts(seq: Any, fields: tuple[str, ...]) -> list[dict]:
    """Normalize js_eval list items (dict or AttrDict-ish) to plain dicts."""
    if seq is None:
        return []
    if isinstance(seq, dict):
        seq = [seq]
    if not (hasattr(seq, "__iter__") and not isinstance(seq, (str, bytes))):
        return []
    out: list[dict] = []
    for item in seq:
        if isinstance(item, dict):
            out.append(item)
        elif any(hasattr(item, f) for f in fields):
            out.append({f: getattr(item, f, None) for f in fields})
    return out


async def _drain_slide_queue() -> tuple[list[dict], list[dict]]:
    """Pull pending (runs, layout patches) from the parent page queues.

    One js_eval round-trip drains both ``__sslive_q`` (Run requests) and
    ``__sslive_layout_q`` (edit-mode drag/nudge patches).
    """
    if js_eval is None and js_eval_a is None:
        return [], []
    try:
        res = await _call_js_eval(
            "const r = (window.__sslive_q || []).slice(); "
            "window.__sslive_q = []; "
            "const l = (window.__sslive_layout_q || []).slice(); "
            "window.__sslive_layout_q = []; "
            "return {runs: r, layouts: l};"
        )
        q = _parse_js_eval_result(res)
        if q is None:
            return [], []
        if isinstance(q, dict) and ("runs" in q or "layouts" in q):
            runs_raw, layouts_raw = q.get("runs"), q.get("layouts")
        elif hasattr(q, "runs") or hasattr(q, "layouts"):
            runs_raw, layouts_raw = getattr(q, "runs", None), getattr(q, "layouts", None)
        else:  # old bridge on the page: bare run list
            runs_raw, layouts_raw = q, None
        return (
            _item_dicts(runs_raw, ("cell_id", "source", "slide_index")),
            _item_dicts(layouts_raw, ("el_id", "patch", "t")),
        )
    except Exception as e:
        if _SESSION.get("_bridge_err") != str(e):
            _SESSION["_bridge_err"] = str(e)
            print(f"sslive: bridge poll error: {e}")
        return [], []


def _apply_slide_layout_patches(items: list[dict]) -> int:
    """Apply edit-mode patches from the slide to the overlay + persist.

    No ``_push_layout`` echo — the iframe DOM already shows the dragged
    position; pushing back could fight a drag still in progress.
    """
    deck: Deck | None = _SESSION.get("deck")
    if deck is None or not items:
        return 0
    n = 0
    for it in items:
        el_id = str(it.get("el_id") or "")
        if not el_id or el_id not in deck.elements:
            continue
        patch = it.get("patch") or {}
        try:
            _apply_layout_patch(deck, el_id, dict(patch))
            n += 1
        except Exception as e:
            _SESSION["_layout_patch_err"] = f"{el_id}: {e}"
    if n:
        _schedule_layout_save()
    return n


async def _bridge_poll_loop() -> None:
    """Background: apply in-slide Run requests + edit-mode layout patches."""
    while _SESSION.get("bridge_active"):
        try:
            pending, layout_patches = await _drain_slide_queue()
            # layout first: a Run in the same batch re-renders the output
            # block and must see the just-dragged position
            _apply_slide_layout_patches(layout_patches)
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
                    # Avoid noisy prints (they re-render SolveIt output / reset slides)
                    _SESSION["_last_run_err"] = str(e)
        except asyncio.CancelledError:
            break
        except Exception as e:
            _SESSION["_bridge_loop_err"] = str(e)
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
        pending, layout_patches = await _drain_slide_queue()
        _apply_slide_layout_patches(layout_patches)
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
  <b>RISE-style:</b> edit in the slide → <b>▶ Run</b> / <b>Shift+Enter</b>
  → GPU (in place) → dialog source updates shortly after.
  <div style="margin-top:8px;font-size:12px;color:#9ca3af">
    No full preview rebuild on Run. Manual: <code>await sync_dialog()</code>
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
    _SESSION["auto_sync_dialog"] = True  # deferred update_msg after Run
    if _MdDocument is None or _l2m is None:
        print(
            "sslive: basic note render — `pip install mistletoe latex2mathml` "
            "for markdown + LaTeX"
        )
    print("In-slide: edit · ▶ Run / Shift+Enter → GPU (in-place)")
    print("Dialog source: auto-sync shortly after Run (no rebuild/refocus)")
    print("Manual: await sync_dialog()  if needed")

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
    "set_layout",
    "clear_layout",
    "layout_ids",
    "save_layout",
    "load_layout",
    "pump_slide_runs",
    "deck_summary",
    "refresh_presenter",
    "refocus_presenter",
    "render_output_html",
    "generate_presenter_html",
    "get_craft_exec_mgr",
]
