"""sslive — live GPU slides for SolveIt + CRAFT.

**Working version 0.1.0** — in-slide edit, layout, reveal; Run on GPU; dialog sync.

Architecture: **host on %local** (presenter, dialoghelper, bridge);
**slide code on the CRAFT GPU** (▶ Run / Shift+Enter).

Usage::

    %local
    %run path/to/sslive.py   # do not paste this file into the dialog
    %gpu                     # connect remote kernel once
    %local
    await slive()            # host must stay under %local
    # edit in the slide → ▶ Run → GPU execute → in-place output
"""

from __future__ import annotations

import asyncio
import base64
import html as html_module
import inspect
import json
import os
import re
import socket
import threading
import time
import warnings
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Literal

# IPython warns when embedding <iframe srcdoc=...> via HTML(); we need srcdoc
# for SolveIt and cannot use display.IFrame (URL-only). Silence that noise.
warnings.filterwarnings(
    "ignore",
    message=r".*IPython\.display\.IFrame.*",
    category=UserWarning,
)

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

# Optional: remove messages (name varies across dialoghelper builds)
delete_msg = None
for _del_name in ("delete_msg", "del_msg", "remove_msg", "delete_message"):
    try:
        from dialoghelper import core as _dh_core  # type: ignore

        delete_msg = getattr(_dh_core, _del_name, None)
        if delete_msg is not None:
            break
    except Exception:
        pass
LAYOUT_STUB_MARK = "(sslive: duplicate layout note — safe to delete)"

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
    html: str = ""  # pre-rendered fragment (S2-D note pieces)
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

_HOST_LOAD_HELP = """
sslive host must load on the SolveIt kernel (not the remote GPU).

  %local
  %run sslive/sslive.py   # auto-registers %slive
  %gpu                    # optional — stay here for torch / %pointcloud
  %slive                  # or: await slive()

If you %run under %gpu, this file executes on the remote kernel where
dialoghelper does not exist — that causes this error.
If %slive is missing after a bad order:  register_slive()
""".strip()

async def get_slides_cells_from_dialog(include_prompts: bool = False) -> list[dict]:
    """Cells after `#| s` marker. Requires dialoghelper (async API)."""
    if find_msgs is None:
        raise RuntimeError(
            "dialoghelper not available — host ran on remote GPU?\n" + _HOST_LOAD_HELP
        )
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
    nb_cells, nb_attachments = {}, {}
    if notebook_path and Path(notebook_path).exists():
        nb_cells, nb_attachments = get_slides_cells_from_notebook(notebook_path, dialog_cells)

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
                # S2-D: fine-grained note pieces (heading / list_item / math / image / …)
                pieces = parse_note_to_elements(source, cid, nb_attachments)
                if not pieces:
                    pieces = [
                        {
                            "id": f"el-0-{cid}",
                            "kind": "paragraph",
                            "html": "",
                            "content": source,
                        }
                    ]
                for p in pieces:
                    el_id = p["id"]
                    deck.elements[el_id] = Element(
                        id=el_id,
                        cell_id=cid,
                        kind=p.get("kind") or "paragraph",
                        order=el_counter,
                        content=p.get("content") or "",
                        html=p.get("html") or "",
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
# ``reveal`` = segmented reveal step (1,2,3…);  omit/0 = always visible.
# ``order``/``z`` remain for low-level CSS (API); toolbar uses ``reveal`` only.
_LAYOUT_KEYS = ("x", "y", "w", "h", "z", "order", "reveal", "fs", "ff", "align")
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
    """Human-visible note body (marker + label + JSON)."""
    n = len((layout or {}).get("elements") or {})
    return (
        f"{LAYOUT_MARKER}\n"
        f"sslive positions ({n} elements) — keep this note · red eye hides from AI\n"
        f"{json.dumps(layout, indent=1)}"
    )


def _parse_layout_msg(content: str) -> dict | None:
    """Overlay dict if ``content`` is a layout message, else None."""
    if not content:
        return None
    text = content.lstrip()
    if not text.startswith(LAYOUT_MARKER):
        return None
    body = text[len(LAYOUT_MARKER) :].lstrip()
    # Tolerate title line / junk after marker before JSON
    if body and body[0] not in "{[":
        brace = body.find("{")
        if brace >= 0:
            body = body[brace:]
    try:
        return _normalize_layout(json.loads(body))
    except Exception:
        return None


def _merge_layouts(*lays: dict) -> dict:
    """Merge overlay dicts; later entries win per-element key."""
    out = _empty_layout()
    for lay in lays:
        if not isinstance(lay, dict):
            continue
        for k, v in (lay.get("elements") or {}).items():
            if not isinstance(v, dict):
                continue
            prev = dict(out["elements"].get(k) or {})
            prev.update(v)
            out["elements"][str(k)] = prev
        dk = lay.get("deck")
        if isinstance(dk, dict):
            out["deck"].update(dk)
    return out


def _msg_type_of(m: Any) -> str:
    if isinstance(m, dict):
        return str(m.get("msg_type") or "")
    return str(getattr(m, "msg_type", "") or "")


def _msg_content_of(m: Any) -> str:
    if isinstance(m, dict):
        return m.get("content", "") or ""
    return getattr(m, "content", "") or ""


async def _list_layout_msgs() -> list[tuple[str, dict]]:
    """All ``#| sslive-layout`` notes (including skipped).

    Important: default ``find_msgs`` often **hides** skipped messages. After the
    first save we set ``skipped=1``, so a plain find misses the note and
    ``save_layout`` used to create *another* cell every time.
    """
    if find_msgs is None:
        return []
    attempts: list[dict] = [
        {"include_skipped": True, "include_meta": True},
        {"include_skipped": True},
        {
            "msg_type": "note",
            "include_skipped": True,
            "re_pattern": r"#\|\s*sslive-layout",
            "use_regex": True,
        },
        {"msg_type": "note", "include_skipped": True},
        {},
    ]
    msgs = None
    last_err = None
    for kw in attempts:
        try:
            msgs = await find_msgs(**kw)
            break
        except TypeError:
            continue
        except Exception as e:
            last_err = e
            continue
    if msgs is None and last_err is not None:
        print(f"sslive: layout lookup failed: {last_err}")
        return []

    out: list[tuple[str, dict]] = []
    for m in msgs or []:
        # When we didn't filter by type, only notes (or untyped) matter
        mt = _msg_type_of(m)
        if mt and mt not in ("note", "code"):
            # still allow if content matches (some APIs mis-tag)
            pass
        lay = _parse_layout_msg(_msg_content_of(m))
        if lay is None:
            continue
        mid = _msg_id_from_obj(m)
        if mid:
            out.append((str(mid), lay))
    return out


async def _find_layout_msg() -> tuple[str, dict] | None:
    """(msg_id, overlay) for the canonical layout note, if any."""
    found = await _list_layout_msgs()
    if not found:
        return None
    # Prefer session id, else the richest overlay (most element keys)
    prefer = _SESSION.get("layout_msg_id")
    if prefer:
        for mid, lay in found:
            if str(mid) == str(prefer) or str(mid).lstrip("_") == str(prefer).lstrip(
                "_"
            ):
                return mid, lay
    found_sorted = sorted(
        found, key=lambda t: len((t[1].get("elements") or {})), reverse=True
    )
    return found_sorted[0]


async def load_layout() -> dict:
    """Read the layout overlay from the dialog (empty overlay when absent).

    If multiple layout notes exist (legacy bug), merge them so nothing is lost.
    """
    found = await _list_layout_msgs()
    if not found:
        _SESSION.pop("layout_msg_id", None)
        return _empty_layout()
    # Merge all copies (later list order then richness for keeper id)
    merged = _merge_layouts(*(lay for _, lay in found))
    prefer = _SESSION.get("layout_msg_id")
    keeper = None
    if prefer:
        for mid, _ in found:
            if str(mid) == str(prefer) or str(mid).lstrip("_") == str(prefer).lstrip(
                "_"
            ):
                keeper = mid
                break
    if keeper is None:
        keeper = max(found, key=lambda t: len((t[1].get("elements") or {})))[0]
    _SESSION["layout_msg_id"] = keeper
    if len(found) > 1:
        _SESSION["_layout_dup_count"] = len(found)
    return merged


async def _ensure_layout_skipped(mid: str) -> None:
    """Best-effort: keep the layout note out of LLM + slides (red eye)."""
    if update_msg is None or not mid:
        return
    try:
        await update_msg(id=mid, skipped=1)
    except Exception:
        try:
            alt = mid[1:] if str(mid).startswith("_") else "_" + str(mid)
            await update_msg(id=alt, skipped=1)
        except Exception:
            pass


def _id_from_add_result(res: Any) -> str | None:
    """Normalize whatever ``add_msg`` returns into a message id string."""
    if res is None:
        return None
    if isinstance(res, str) and res.strip():
        return res.strip()
    if isinstance(res, dict):
        for k in ("id", "msg_id", "message_id"):
            if res.get(k):
                return str(res[k])
    mid = getattr(res, "id", None)
    if mid:
        return str(mid)
    if hasattr(res, "result"):
        try:
            return _id_from_add_result(res.result)
        except Exception:
            pass
    return None


def _layout_ids_match(a: str, b: str) -> bool:
    return str(a).lstrip("_") == str(b).lstrip("_")


async def _delete_dialog_msg(mid: str) -> bool:
    """Hard-delete a dialog message. Returns True if a delete API accepted it."""
    if not mid:
        return False
    if delete_msg is None:
        return False
    for call in (
        lambda: delete_msg(id=mid),
        lambda: delete_msg(mid),
        lambda: delete_msg(msg_id=mid),
        lambda: delete_msg(id=str(mid).lstrip("_")),
        lambda: delete_msg(id=f"_{str(mid).lstrip('_')}"),
    ):
        try:
            res = call()
            if inspect.isawaitable(res):
                await res
            return True
        except TypeError:
            continue
        except Exception:
            continue
    return False


async def _retire_layout_msg(mid: str) -> None:
    """Delete a duplicate layout/stub note (prefer hard delete, no stub spam)."""
    if not mid:
        return
    if await _delete_dialog_msg(mid):
        return
    # Fallback only if delete is unavailable — collapse, do not leave many stubs
    if update_msg is not None:
        try:
            await update_msg(id=mid, content=LAYOUT_STUB_MARK, skipped=1)
        except Exception:
            try:
                await update_msg(id=mid, content="", skipped=1)
            except Exception:
                pass


async def _list_layout_stub_ids() -> list[str]:
    """Ids of notes we previously blanked as duplicates (UI clutter)."""
    if find_msgs is None:
        return []
    msgs = None
    for kw in (
        {"include_skipped": True, "include_meta": True},
        {"include_skipped": True},
        {"msg_type": "note", "include_skipped": True},
        {},
    ):
        try:
            msgs = await find_msgs(**kw)
            break
        except TypeError:
            continue
        except Exception:
            continue
    if not msgs:
        return []
    out: list[str] = []
    for m in msgs:
        body = _msg_content_of(m) or ""
        if LAYOUT_STUB_MARK in body or body.strip() in (
            LAYOUT_STUB_MARK,
            "(sslive: duplicate layout note — safe to delete)",
        ):
            mid = _msg_id_from_obj(m)
            if mid:
                out.append(str(mid))
    return out


async def _cleanup_extra_layout_notes(keeper: str | None) -> int:
    """Delete every layout note except ``keeper``, plus old stub notes.

    Returns how many messages we attempted to remove.
    """
    n = 0
    for mid, _ in await _list_layout_msgs():
        if keeper and _layout_ids_match(mid, keeper):
            continue
        await _retire_layout_msg(str(mid))
        n += 1
    for mid in await _list_layout_stub_ids():
        if keeper and _layout_ids_match(mid, keeper):
            continue
        await _retire_layout_msg(str(mid))
        n += 1
    return n


async def _create_layout_msg(content: str) -> str | None:
    """Create the single layout note **below the %slive preview**, not at top."""
    if add_msg is None:
        _SESSION["_layout_add_err"] = "add_msg is None"
        return None
    after_id = _SESSION.get("launcher_msg_id") or _SESSION.get("skipped_launcher_id")
    trials: list[tuple[str, Any]] = []

    def _add(label: str, coro_factory):
        trials.append((label, coro_factory))

    if after_id:
        aid = str(after_id)
        _add("after/after_id", lambda: add_msg(content, placement="after", after_id=aid))
        _add("after/msg_id", lambda: add_msg(content, placement="after", msg_id=aid))
        _add("kw after=", lambda: add_msg(content, after=aid))
        _add("note+after", lambda: add_msg(content, "note", after=aid))
        _add("msg_type note after", lambda: add_msg(content, msg_type="note", after=aid))
    _add("msg_type note", lambda: add_msg(content, msg_type="note"))
    _add("pos note", lambda: add_msg(content, "note"))
    _add("plain", lambda: add_msg(content))
    _add("at_end note", lambda: add_msg(content, placement="at_end", msg_type="note"))
    _add("at_end", lambda: add_msg(content, placement="at_end"))

    errors: list[str] = []
    before_ids = set()
    try:
        before_ids = {str(m).lstrip("_") for m, _ in await _list_layout_msgs()}
    except Exception:
        pass

    for label, factory in trials:
        try:
            res = factory()
            if inspect.isawaitable(res):
                res = await res
            nid = _id_from_add_result(res)
            if nid:
                _SESSION["_layout_add_ok"] = label
                return nid
            found = await _list_layout_msgs()
            for mid, _ in found:
                if str(mid).lstrip("_") not in before_ids:
                    _SESSION["_layout_add_ok"] = f"{label} (refind)"
                    return str(mid)
            if found and not before_ids:
                _SESSION["_layout_add_ok"] = f"{label} (only)"
                return str(found[-1][0])
            errors.append(f"{label}: no id ({type(res).__name__})"[:80])
        except TypeError as e:
            errors.append(f"{label}: TypeError {e}")
            continue
        except Exception as e:
            errors.append(f"{label}: {e}")
            continue
    _SESSION["_layout_add_err"] = " | ".join(errors[-8:]) or "all trials failed"
    return None


async def _finalize_layout_note(mid: str, content: str) -> None:
    """Write content + red-eye (LLM hide). One note only — never spawn extras."""
    if not mid or update_msg is None:
        return
    for kw in (
        {"id": mid, "content": content, "skipped": 1},
        {"id": mid, "content": content},
    ):
        try:
            await update_msg(**kw)
            break
        except TypeError:
            continue
        except Exception:
            continue
    await _ensure_layout_skipped(mid)


async def save_layout(
    layout: dict | None = None,
    *,
    force_create: bool = False,
    quiet: bool = False,
) -> bool:
    """Persist overlay into **exactly one** ``#| sslive-layout`` dialog note.

    Rules:
      * If any layout note already exists → **update the richest one only**.
        Never create a second note (that was the multi-note bug).
      * Create only when zero layout notes exist (or force_create and zero).
      * Extra layout notes + old "safe to delete" stubs are deleted.

    ``quiet=True``: no slide-restore postMessage / no console chatter (used during
    ``%slive`` startup so the preview does not flash or steal focus).
    """
    if layout is None:
        deck = _SESSION.get("deck")
        layout = deck.layout if deck is not None else _empty_layout()
    if update_msg is None and add_msg is None:
        _SESSION["_layout_save_err"] = "update_msg/add_msg unavailable"
        if not quiet:
            print("sslive: layout save failed — dialoghelper add_msg/update_msg missing")
        return False
    content = _layout_msg_content(layout)
    if not quiet:
        try:
            await _sync_slide_index_from_parent()
        except Exception:
            pass
    n_els = len((layout or {}).get("elements") or {})

    def _mark_ok(mid: str, *, created: bool) -> None:
        _SESSION["layout_msg_id"] = mid
        _SESSION["_layout_save_ok_ts"] = time.time()
        _SESSION.pop("_layout_save_err", None)
        if quiet:
            return
        verb = "created" if created else "updated"
        # Quiet updates; announce creates and first update only
        if created or not _SESSION.get("_layout_save_announced"):
            _SESSION["_layout_save_announced"] = True
            print(
                f"sslive: layout {verb} ({n_els} elements) → "
                f"one Note `#| sslive-layout` id={mid}"
            )

    try:
        async with hold_dialog_focus(
            ms=5000, refocus=not quiet, soft=True, settle=0.08 if not quiet else 0.0
        ):
            existing = await _list_layout_msgs()

            # ── UPDATE PATH: never create when a real note is already present ──
            if existing and not force_create:
                prefer = _SESSION.get("layout_msg_id")
                keeper = None
                if prefer:
                    for mid, _ in existing:
                        if _layout_ids_match(mid, str(prefer)):
                            keeper = str(mid)
                            break
                if keeper is None:
                    keeper = str(
                        max(existing, key=lambda t: len((t[1].get("elements") or {})))[0]
                    )
                if update_msg is not None:
                    await _finalize_layout_note(keeper, content)
                    _mark_ok(keeper, created=False)
                    removed = await _cleanup_extra_layout_notes(keeper)
                    if removed and not quiet:
                        print(
                            f"sslive: removed {removed} extra layout/stub note(s) "
                            f"(keeping id={keeper})"
                        )
                    if not quiet:
                        _push_slide_index_restore(keep_edit=False)
                    return True

            # force_create with existing → still just update (ignore force)
            if existing and force_create:
                keeper = str(
                    max(existing, key=lambda t: len((t[1].get("elements") or {})))[0]
                )
                await _finalize_layout_note(keeper, content)
                _mark_ok(keeper, created=False)
                await _cleanup_extra_layout_notes(keeper)
                if not quiet:
                    _push_slide_index_restore(keep_edit=False)
                return True

            # ── CREATE PATH: only when zero layout notes exist ──
            if add_msg is None:
                _SESSION["_layout_save_err"] = "add_msg unavailable"
                if not quiet:
                    print("sslive: cannot create #| sslive-layout — add_msg unavailable")
                return False
            new_id = await _create_layout_msg(content)
            if not new_id:
                err = _SESSION.get("_layout_add_err") or "add_msg returned no id"
                _SESSION["_layout_save_err"] = err
                if not quiet:
                    print(f"sslive: failed to create #| sslive-layout — {err}")
                return False
            await _finalize_layout_note(new_id, content)
            _mark_ok(new_id, created=True)
            await _cleanup_extra_layout_notes(new_id)
            if not quiet:
                _push_slide_index_restore(keep_edit=False)
            return True
    except Exception as e:
        _SESSION["_layout_save_err"] = str(e)
        if not quiet:
            print(f"sslive: layout save failed: {e}")
        return False


async def ensure_layout_note(*, quiet: bool = True) -> str | None:
    """Ensure exactly one ``#| sslive-layout`` note; create only if none exist.

    On a warm re-``%slive`` when a single good note already exists, **do not**
    call ``update_msg`` (that was focusing the layout cell then the preview).
    Only write when creating, merging duplicates, or cleaning stubs.
    """
    try:
        existing = await _list_layout_msgs()
        deck = _SESSION.get("deck")
        lay = deck.layout if deck is not None else _empty_layout()
        stubs: list[str] = []
        try:
            stubs = await _list_layout_stub_ids()
        except Exception:
            stubs = []

        if existing:
            keeper = str(
                max(existing, key=lambda t: len((t[1].get("elements") or {})))[0]
            )
            _SESSION["layout_msg_id"] = keeper
            needs_write = len(existing) > 1 or bool(stubs)
            if needs_write:
                # Merge / dedupe only when something is actually wrong
                await save_layout(lay, quiet=quiet)
            # else: leave the note alone — deck.layout was already loaded from it
            return keeper

        # None — create once
        _SESSION.pop("layout_msg_id", None)
        ok = await save_layout(lay, quiet=quiet)
        mid = _SESSION.get("layout_msg_id")
        if not ok or not mid:
            if not quiet:
                print(
                    "sslive: could not create #| sslive-layout note — "
                    f"err={_SESSION.get('_layout_save_err') or _SESSION.get('_layout_add_err')}"
                )
            return None
        return str(mid)
    except Exception as e:
        _SESSION["_layout_ensure_err"] = str(e)
        if not quiet:
            print(f"sslive: ensure_layout_note failed: {e}")
        return None


async def cleanup_layout_notes() -> dict:
    """Public: collapse to one layout note and delete stubs. Returns a summary."""
    existing = await _list_layout_msgs()
    keeper = None
    if existing:
        keeper = str(max(existing, key=lambda t: len((t[1].get("elements") or {})))[0])
        deck = _SESSION.get("deck")
        lay = deck.layout if deck is not None else _merge_layouts(*(l for _, l in existing))
        await _finalize_layout_note(keeper, _layout_msg_content(lay))
        _SESSION["layout_msg_id"] = keeper
    removed = await _cleanup_extra_layout_notes(keeper)
    return {
        "keeper": keeper,
        "removed": removed,
        "n_layout_notes_before": len(existing),
    }


def _schedule_layout_save(delay: float = 0.5) -> None:
    """Debounced ``save_layout`` — one dialog write per editing burst."""
    prev = _SESSION.get("_layout_save_task")
    if prev is not None and not prev.done():
        prev.cancel()
    _SESSION["_layout_save_pending"] = True

    async def _later():
        try:
            await asyncio.sleep(delay)
            ok = await save_layout()
            if ok:
                _SESSION["_layout_save_pending"] = False
        except asyncio.CancelledError:
            pass
        except Exception as e:
            _SESSION["_layout_save_err"] = str(e)
            _SESSION["_layout_save_pending"] = False

    try:
        _SESSION["_layout_save_task"] = asyncio.get_running_loop().create_task(_later())
    except RuntimeError:  # no loop (headless tests) — persistence is moot there
        _SESSION["_layout_save_task"] = None
        _SESSION["_layout_save_pending"] = False


async def _drain_layout_queue_only() -> list[dict]:
    """Pull pending edit-mode layout patches without touching the Run queue."""
    if js_eval is None and js_eval_a is None:
        return []
    try:
        res = await _call_js_eval(
            "const l = (window.__sslive_layout_q || []).slice(); "
            "window.__sslive_layout_q = []; "
            "return l;"
        )
        q = _parse_js_eval_result(res)
        return _item_dicts(q, ("el_id", "patch", "t"))
    except Exception as e:
        _SESSION["_layout_drain_err"] = str(e)
        return []


async def flush_layout_save(*, quiet: bool = False, force: bool = False) -> bool:
    """Cancel debounce and write layout to the dialog now.

    Skips the dialog write when nothing is dirty (avoids focusing the layout
    note on a clean re-``%slive``). Use ``force=True`` to always write.

    ``quiet=True`` avoids slide-restore postMessage / console noise.
    """
    prev = _SESSION.get("_layout_save_task")
    had_pending_task = prev is not None and not prev.done()
    if had_pending_task:
        try:
            prev.cancel()
        except Exception:
            pass
        _SESSION["_layout_save_task"] = None

    layout_patches: list[dict] = []
    try:
        layout_patches = await _drain_layout_queue_only()
        if layout_patches:
            _apply_slide_layout_patches(layout_patches)
    except Exception as e:
        _SESSION["_layout_flush_err"] = str(e)

    dirty = (
        force
        or had_pending_task
        or bool(_SESSION.get("_layout_save_pending"))
        or bool(layout_patches)
    )
    if not dirty:
        _SESSION["_layout_save_pending"] = False
        return True

    ok = await save_layout(quiet=quiet)
    _SESSION["_layout_save_pending"] = False
    return ok


def layout_status(deck: "Deck | None" = None) -> dict:
    """Diagnostics for layout persistence (``#| sslive-layout`` dialog note)."""
    deck = deck or _SESSION.get("deck")
    lay = (deck.layout if deck is not None else None) or _empty_layout()
    els = lay.get("elements") or {}
    known = set((deck.elements or {}).keys()) if deck is not None else set()
    orphans = [k for k in els if k not in known]
    task = _SESSION.get("_layout_save_task")
    pending = bool(_SESSION.get("_layout_save_pending")) or (
        task is not None and not getattr(task, "done", lambda: True)()
    )
    return {
        "msg_id": _SESSION.get("layout_msg_id"),
        "n_elements": len(els),
        "n_orphans": len(orphans),
        "orphans": orphans[:12],
        "pending_debounce": pending,
        "last_err": _SESSION.get("_layout_save_err") or _SESSION.get("_layout_add_err"),
        "last_add_ok": _SESSION.get("_layout_add_ok"),
        "last_ok_ts": _SESSION.get("_layout_save_ok_ts"),
        "ids": list(els.keys())[:24],
        "hint": (
            "Look for a Note starting with #| sslive-layout under the %slive "
            "cell (red eye). If missing: await ensure_layout_note()"
        ),
    }


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
        elif k in ("z", "order", "reveal"):
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
    elif spec.get("z") is not None:
        # z-index only applies to positioned elements; keep in flow with relative.
        parts.append("position:relative")
    for key, prop in (("w", "width"), ("h", "height")):
        v = _css_len(spec.get(key))
        if v is not None:
            parts.append(f"{prop}:{v}px")
    # Explicit height pins a box like Google slides — scroll rather than spill.
    if spec.get("h") is not None:
        parts.append("overflow:auto")
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


def _strip_code_bar_height(style: str) -> str:
    """Remove layout height/overflow from a code-cell bar style.

    Edit-mode box height is for chrome placement; the bar itself is always
    one line. Expanded editing uses a floating panel (live + export).
    """
    if not style:
        return ""
    style_bar = re.sub(
        r"(?:^|;)\s*height\s*:\s*[^;]+;?",
        ";",
        style,
        flags=re.I,
    )
    style_bar = re.sub(r";{2,}", ";", style_bar).strip(";").strip()
    if style_bar and re.search(r"(?:^|;)\s*overflow\s*:", style_bar, flags=re.I):
        style_bar = re.sub(
            r"(?:^|;)\s*overflow\s*:\s*[^;]+;?",
            ";",
            style_bar,
            flags=re.I,
        )
        style_bar = re.sub(r";{2,}", ";", style_bar).strip(";").strip()
    return style_bar


def _reveal_attr(spec: dict) -> str:
    """`` data-reveal="N"`` when a positive reveal step is set."""
    r = spec.get("reveal")
    if r is None:
        return ""
    try:
        n = int(r)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    return f' data-reveal="{n}"'


def _push_layout(el_id: str) -> None:
    """Apply one element's overlay style in the live iframe (no rebuild)."""
    deck = _SESSION.get("deck")
    seq = int(_SESSION.get("_layout_push_seq") or int(time.time() * 1000)) + 1
    _SESSION["_layout_push_seq"] = seq
    spec = _layout_spec(deck, el_id)
    rev = spec.get("reveal")
    try:
        rev = int(rev) if rev is not None else None
    except (TypeError, ValueError):
        rev = None
    payload = {
        "type": "sslive_layout_apply",
        "el_id": el_id,
        "style": _el_style(spec),
        "reveal": rev if (rev is not None and rev > 0) else None,
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
    reveal: Any = _UNSET,
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
        await set_layout('el-note-_abc123', reveal=1)  # appear on first → press

    Omit a param to leave it unchanged; pass ``None`` to clear it.
    ``x``/``y`` position absolutely; without them the element stays in flow.
    ``reveal`` is the segmented-reveal step (1, 2, 3…); omit/0 = always shown.
    Right arrow advances reveal steps before changing slides. ``fs`` px,
    ``ff`` CSS family, ``z`` stacking, ``align`` left/center/right/justify.
    Applied live in the iframe; persisted (debounced) to the dialog.
    """
    deck: Deck | None = _SESSION.get("deck")
    if deck is None:
        raise RuntimeError("Call await slive() first")
    if el_id not in deck.elements:
        raise KeyError(f"unknown element {el_id!r} — see layout_ids() for options")
    updates = {
        "x": x, "y": y, "w": w, "h": h, "z": z,
        "order": order, "reveal": reveal, "fs": fs, "ff": ff, "align": align,
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
    """Discover CRAFT RemoteExecutionManager (local host client → remote GPU).

    Under ``%gpu``, code cells run on the remote kernel, but the CRAFT *client*
    (``_exec_mgr``) still lives in the SolveIt host namespace. Probe common
    names so ▶ Run works after a normal CRAFT + ``%gpu`` setup.
    """
    if get_ipython is None:
        return None
    try:
        ns = get_ipython().user_ns or {}
    except Exception:
        return None
    for key in (
        "_exec_mgr",
        "exec_mgr",
        "remote_exec_mgr",
        "craft_exec_mgr",
        "_craft_exec_mgr",
        "REM",
    ):
        mgr = ns.get(key)
        if mgr is not None and (
            hasattr(mgr, "remote_kc") or hasattr(mgr, "execute_interactive")
        ):
            return mgr
    # Nested under a craft/gpudev handle
    for key in ("craft", "gpudev", "_craft", "CRAFT"):
        obj = ns.get(key)
        if obj is None:
            continue
        for attr in ("_exec_mgr", "exec_mgr", "remote_mgr", "mgr"):
            mgr = getattr(obj, attr, None)
            if mgr is not None and hasattr(mgr, "remote_kc"):
                return mgr
    # Last resort: scan for object with remote_kc
    for val in list(ns.values()):
        try:
            if hasattr(val, "remote_kc") and getattr(val, "remote_kc", None) is not None:
                return val
        except Exception:
            continue
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
            parts.extend(_display_data_to_parts(data))
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


def _code_has_ipy_magic(code: str) -> bool:
    """True if source uses IPython line/cell magics or shell escapes."""
    for line in (code or "").splitlines():
        s = line.lstrip()
        if not s or s.startswith("#"):
            continue
        if s.startswith("%%") or s.startswith("%") or s.startswith("!"):
            return True
    return False


def _display_data_to_parts(data: dict) -> list[OutputPart]:
    """Convert a MIME bundle to OutputPart list (prefer rich types).

    Prefer ``application/vnd.plotly.v1+json`` over Plotly's full ``text/html``
    (which embeds CDN tags that SolveIt's parent HTMX mangles).
    """
    if not data:
        return []
    # Normalize keys that may be AttrDict / bytes
    data = {str(k): v for k, v in dict(data).items()}
    if "image/png" in data:
        return [OutputPart(kind="image/png", b64=_as_str(data["image/png"]))]
    if "image/jpeg" in data or "image/jpg" in data:
        b64 = _as_str(data.get("image/jpeg") or data.get("image/jpg"))
        # reuse png slot with data-url prefix in text/html for simplicity
        return [
            OutputPart(
                kind="text/html",
                text=(
                    f'<img src="data:image/jpeg;base64,{b64}" '
                    f'style="max-width:100%;height:auto;display:block" alt="output"/>'
                ),
            )
        ]
    if "image/svg+xml" in data:
        svg = _as_str(data["image/svg+xml"])
        return [OutputPart(kind="text/html", text=svg)]
    # Structured Plotly figure — compact + safe (no CDN HTML for parent HTMX)
    for key in (
        "application/vnd.plotly.v1+json",
        "application/vnd.plotly.v1+json; charset=utf-8",
    ):
        if key in data:
            return [
                OutputPart(
                    kind="text/html",
                    text=_plotly_spec_to_html(data[key]),
                )
            ]
    if "text/html" in data:
        return [OutputPart(kind="text/html", text=_as_str(data["text/html"]))]
    if "application/javascript" in data:
        js = _as_str(data["application/javascript"])
        return [OutputPart(kind="text/html", text=f"<script>{js}</script>")]
    if "text/plain" in data:
        return [OutputPart(kind="text/plain", text=_as_str(data["text/plain"]))]
    return []


def _object_to_parts(obj: Any) -> list[OutputPart]:
    """Turn a Python / IPython display object into OutputParts."""
    if obj is None:
        return []
    # Already a MIME bundle
    if isinstance(obj, dict) and any(
        str(k).startswith(("text/", "image/", "application/")) for k in obj
    ):
        return _display_data_to_parts(obj)
    # IPython.display.DisplayObject / HTML / IFrame / etc.
    data = getattr(obj, "data", None)
    if isinstance(data, dict) and data:
        parts = _display_data_to_parts(data)
        if parts:
            return parts
    for meth, kind in (
        ("_repr_mimebundle_", None),
        ("_repr_html_", "text/html"),
        ("_repr_png_", "image/png"),
        ("_repr_jpeg_", "image/jpeg"),
        ("_repr_svg_", "text/html"),
    ):
        fn = getattr(obj, meth, None)
        if not callable(fn):
            continue
        try:
            if meth == "_repr_mimebundle_":
                bundle = fn(include=None, exclude=None)
                if isinstance(bundle, tuple):
                    bundle = bundle[0]
                if isinstance(bundle, dict):
                    parts = _display_data_to_parts(bundle)
                    if parts:
                        return parts
            else:
                rep = fn()
                if not rep:
                    continue
                if kind == "image/png":
                    return [OutputPart(kind="image/png", b64=_as_str(rep))]
                return [OutputPart(kind="text/html" if kind != "text/plain" else "text/plain", text=_as_str(rep))]
        except Exception:
            continue
    # Plain string result
    if isinstance(obj, str) and obj.strip():
        if "<" in obj and ">" in obj:
            return [OutputPart(kind="text/html", text=obj)]
        return [OutputPart(kind="text/plain", text=obj)]
    return []

class LiveExecutor:
    """Execute slide code: pure Python → CRAFT remote GPU; magics → host IPython.

    ``%pointcloud`` and similar magics are often registered on the SolveIt host
    (or only available through IPython's shell), so sending them with
    ``remote_kc.execute_interactive`` yields "magic not found". Magics are
    therefore run via ``get_ipython().run_cell`` on the host — the same path
    a dialog cell uses for local magics under ``%gpu``.
    """

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
                if _code_has_ipy_magic(code):
                    return self._execute_host_ipython(code, t0=t0)
                return self._execute_gpu(code, echo_to_dialog=echo_to_dialog, t0=t0)
            finally:
                self.busy = False

    def execute_cell(self, deck: Deck, cell_id: str, **kw) -> ExecResult:
        source = deck.code_source(cell_id)
        result = self.execute(source, **kw)
        if result.ok or result.parts:
            deck.cells[cell_id].outputs = list(result.parts)
        return result

    def _execute_host_ipython(self, code: str, *, t0: float) -> ExecResult:
        """Run magics on host IPython; capture stdout + rich display (HTML/iframe).

        ``capture_output`` alone often misses viewers that call
        ``display_pub.publish`` or return HTML/IFrame objects — we hook both.
        """
        if get_ipython is None:
            return ExecResult(
                ok=False,
                parts=[],
                duration_ms=int((time.perf_counter() - t0) * 1000),
                error="IPython not available for magic execution",
            )
        ip = get_ipython()
        parts: list[OutputPart] = []
        published: list[dict] = []

        def _on_publish(data, metadata=None, source=None, **kwargs):
            try:
                if data:
                    published.append(dict(data) if not isinstance(data, dict) else data)
            except Exception:
                pass
            # Do NOT call the original display_pub. Forwarding Plotly/HTML into
            # SolveIt's dialog lets parent HTMX parse CDN tags (mangled
            # %22https://cdn.plot.ly … oobSwap errors) and freezes the page
            # while the slide spinner stays on "Running on GPU…".
            return None

        try:
            # --- hook DisplayPublisher (primary path for display()/IFrame/HTML)
            pub = getattr(ip, "display_pub", None)
            orig_publish = getattr(pub, "publish", None) if pub is not None else None
            if pub is not None and orig_publish is not None:
                _on_publish._orig = orig_publish  # type: ignore[attr-defined]
                pub.publish = _on_publish  # type: ignore[method-assign]

            try:
                from IPython.utils.capture import capture_output
            except Exception:
                capture_output = None  # type: ignore

            result = None
            if capture_output is not None:
                with capture_output(stdout=True, stderr=True, display=True) as cap:
                    result = ip.run_cell(code, store_history=False)
                if cap.stdout:
                    parts.append(
                        OutputPart(
                            kind="stream", text=_strip_ansi(cap.stdout), name="stdout"
                        )
                    )
                if cap.stderr:
                    parts.append(
                        OutputPart(
                            kind="stream", text=_strip_ansi(cap.stderr), name="stderr"
                        )
                    )
                for out in getattr(cap, "outputs", None) or []:
                    data = getattr(out, "data", None)
                    if data is None and isinstance(out, dict):
                        data = out.get("data")
                    if isinstance(data, dict):
                        parts.extend(_display_data_to_parts(data))
                    else:
                        parts.extend(_object_to_parts(out))
            else:
                result = ip.run_cell(code, store_history=False)

            # MIME bundles from display_pub.publish
            for data in published:
                parts.extend(_display_data_to_parts(data))

            # Return value of the cell / magic (HTML, IFrame, …)
            if result is not None:
                parts.extend(_object_to_parts(getattr(result, "result", None)))

            # Deduplicate consecutive identical html/text parts
            deduped: list[OutputPart] = []
            seen: set[str] = set()
            for p in parts:
                key = f"{p.kind}:{p.text[:200] if p.text else ''}:{p.b64[:40] if p.b64 else ''}"
                if key in seen:
                    continue
                seen.add(key)
                deduped.append(p)
            parts = deduped

            ok = True
            err = None
            if result is not None:
                if getattr(result, "error_in_exec", None) is not None:
                    ok = False
                    err = str(result.error_in_exec)
                elif getattr(result, "error_before_exec", None) is not None:
                    ok = False
                    err = str(result.error_before_exec)
                elif hasattr(result, "success") and not result.success:
                    ok = False
                    err = "execution failed"
            if err and "not found" in err and "%" in (code or ""):
                err = (
                    f"{err}\n\n"
                    "Magics like %pointcloud must be available on the SolveIt host. "
                    "Load the extension that provides them, then ▶ Run again."
                )
            if err and not any(p.kind == "error" for p in parts):
                parts.append(OutputPart(kind="error", text=err))

            if (
                not ok
                and err
                and "not found" in err
                and get_craft_exec_mgr() is not None
            ):
                remote = self._execute_gpu(code, echo_to_dialog=False, t0=t0)
                if remote.ok or remote.parts:
                    return remote

            # Successful magic with no captured display — still useful signal
            if ok and not parts:
                parts.append(
                    OutputPart(
                        kind="stream",
                        text=(
                            "(magic finished with no captured display — "
                            "viewer may have opened outside the slide)"
                        ),
                        name="stdout",
                    )
                )

            return ExecResult(
                ok=ok and not any(p.kind == "error" for p in parts),
                parts=parts,
                duration_ms=int((time.perf_counter() - t0) * 1000),
                error=err,
            )
        except Exception as e:
            return ExecResult(
                ok=False,
                parts=[OutputPart(kind="error", text=str(e))],
                duration_ms=int((time.perf_counter() - t0) * 1000),
                error=str(e),
            )
        finally:
            # restore display publisher
            try:
                pub = getattr(ip, "display_pub", None)
                orig = getattr(_on_publish, "_orig", None)
                if pub is not None and orig is not None:
                    pub.publish = orig  # type: ignore[method-assign]
            except Exception:
                pass
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
            # Pure Python on remote GPU (same client as execute_remote).
            # timeout prevents infinite "Running on GPU…" when the kernel wedges.
            try:
                reply = kc.execute_interactive(
                    code=code, output_hook=hook, timeout=90
                )
            except TypeError:
                # Older jupyter_client without timeout=
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

# Soft cap for in-slide rich HTML (base64 data-URL). Larger freezes dialoghelper
# iife / the parent page — pointcloud stays small (URL iframe); Plotly HTML does not.
_MAX_ISOLATED_HTML_BYTES = 1_800_000


def _plotly_spec_to_html(spec: Any) -> str:
    """Build a thin Plotly mount from structured figure JSON (no CDN tags)."""
    try:
        if isinstance(spec, (bytes, bytearray)):
            spec = spec.decode("utf-8", errors="replace")
        if isinstance(spec, str):
            # May already be JSON text
            try:
                obj = json.loads(spec)
            except Exception:
                obj = None
                payload = spec
            else:
                payload = json.dumps(obj, separators=(",", ":"))
        else:
            payload = json.dumps(spec, separators=(",", ":"))
    except Exception as e:
        return (
            f'<pre style="color:#fecaca;background:#7f1d1d;padding:0.5rem;'
            f'border-radius:6px">plotly spec encode failed: {html_module.escape(str(e))}</pre>'
        )
    if len(payload) > _MAX_ISOLATED_HTML_BYTES:
        return (
            '<pre style="color:#fecaca;background:#7f1d1d;padding:0.5rem;'
            f'border-radius:6px;white-space:pre-wrap">Plotly figure too large '
            f"({len(payload) // 1024} KB). Downsample the series or use a static image.</pre>"
        )
    uid = f"pl-{abs(hash(payload)) % (10**12):x}"
    b64 = base64.b64encode(payload.encode("utf-8")).decode("ascii")
    # Self-contained: works inside nested iframe OR presenter (has Plotly CDN).
    return (
        f'<div id="{uid}" class="plotly-graph-div js-plotly-plot" '
        f'style="width:100%;height:100%;min-height:480px;"></div>'
        f"<script>(function(){{"
        f"var id={json.dumps(uid)};"
        f"var b64={json.dumps(b64)};"
        f"function u8(s){{var a=new Uint8Array(s.length);for(var i=0;i<s.length;i++)a[i]=s.charCodeAt(i);return a;}}"
        f"var spec=JSON.parse(new TextDecoder('utf-8').decode(u8(atob(b64))));"
        f"var data=spec.data||[];var layout=Object.assign({{}},spec.layout||{{}});"
        # Fill the host iframe — fixed layout.height from fig.show makes tiny plots
        f"layout.autosize=true;delete layout.height;delete layout.width;"
        f"var cfg=Object.assign({{responsive:true,displayModeBar:true}},spec.config||{{}});"
        f"function draw(){{var gd=document.getElementById(id);if(!gd)return;"
        f"if(window.Plotly){{try{{window.Plotly.newPlot(gd,data,layout,cfg).then(function(){{"
        f"try{{window.Plotly.Plots.resize(gd);}}catch(e){{}};"
        f"}});}}catch(e){{gd.textContent=String(e);}}}}"
        f"else setTimeout(draw,40);}}"
        f"draw();"
        f"window.addEventListener('resize',function(){{var gd=document.getElementById(id);"
        f"if(gd&&window.Plotly)try{{window.Plotly.Plots.resize(gd);}}catch(e){{}}}});"
        f"}})();</script>"
    )


def _looks_like_plotly(html: str) -> bool:
    h = (html or "").lower()
    return any(
        s in h
        for s in (
            "plotly.newplot",
            "plotly.react",
            "plotly-graph-div",
            "js-plotly-plot",
            "cdn.plot.ly",
            "plotlycdn",
            "plotly-latest",
            "plotly-2.",
        )
    )


def _looks_like_rich_html(html: str) -> bool:
    """True when content should run in an isolated nested iframe (not parent DOM)."""
    h = html or ""
    if not h.strip():
        return False
    # Already a single top-level iframe (e.g. %pointcloud) — leave alone
    stripped = h.strip()
    if re.match(r"^<iframe\b", stripped, re.I):
        # only isolate further if it embeds plotly inline rather than a src URL
        if not _looks_like_plotly(h) and "srcdoc" not in h.lower():
            return False
    if _looks_like_plotly(h):
        return True
    if re.search(r"<\s*script\b", h, re.I):
        return True
    if re.search(r"<!DOCTYPE\s+html|<\s*html\b", h, re.I):
        return True
    if len(h) > 80_000:
        return True
    return False


def _scrub_output_html(html: str) -> str:
    """Neutralize bits that break the slide iframe (HTMX OOB, extra Plotly CDN)."""
    if not html:
        return ""
    # Undo prior HTMX URL mangling if content was re-captured
    html = html.replace("%22https://", "https://").replace("%22http://", "http://")
    html = re.sub(
        r"\s+hx-swap-oob=(['\"]).*?\1",
        "",
        html,
        flags=re.I | re.DOTALL,
    )
    html = re.sub(r"\s+data-hx-swap-oob=(['\"]).*?\1", "", html, flags=re.I | re.DOTALL)
    # Drop hx-* attrs entirely — never let parent HTMX process slide output
    html = re.sub(r"\s+hx-([a-zA-Z0-9_-]+)=(['\"]).*?\2", "", html, flags=re.I | re.DOTALL)
    html = re.sub(r"\s+data-hx-([a-zA-Z0-9_-]+)=(['\"]).*?\2", "", html, flags=re.I | re.DOTALL)
    # Presenter / nested iframe load Plotly — drop duplicate CDN tags
    html = re.sub(
        r"<script[^>]+src=[\"'][^\"']*plotly[^\"']*[\"'][^>]*>\s*</script>",
        "",
        html,
        flags=re.I,
    )
    # require.js plotly loaders occasionally appear in notebook HTML
    html = re.sub(
        r"<script[^>]*>\s*require\.config\(\s*\{[^}]*plotly[^}]*\}[\s\S]*?</script>",
        "",
        html,
        flags=re.I,
    )

    # Drop *inline* full plotly.js bundles (fig.show(include_plotlyjs=True) embeds
    # ~3MB). Nested iframe already loads the CDN; keeping the bundle freezes push.
    def _drop_fat_plotly_script(m: re.Match) -> str:
        body = m.group(0)
        # Keep small Plotly.newPlot bootstrap scripts
        if len(body) < 80_000:
            return body
        if re.search(r"Plotly\.newPlot|plotly-graph-div", body, re.I):
            # Large script that still *calls* newPlot — only strip if it looks
            # like the library itself (has module preamble / many internals)
            if re.search(
                r"plotly\.js|Plotly\.version|function createPlotlyComponent|"
                r"exports\.Plotly|__webpack_require__",
                body,
                re.I,
            ):
                return "<!-- sslive: stripped embedded plotly.js -->"
        elif re.search(
            r"plotly\.js|Plotly\.version|__webpack_require__|define\(function",
            body,
            re.I,
        ):
            return "<!-- sslive: stripped embedded plotly.js -->"
        return body

    html = re.sub(
        r"<script\b[^>]*>[\s\S]*?</script>",
        _drop_fat_plotly_script,
        html,
        flags=re.I,
    )
    return html


# Default viz height in design-space px (stage is 1920×1080). Nested data-URL
# iframes were collapsing to Plotly's ~700px default width — plotly now mounts
# directly in the slide (already a srcdoc sandbox) so width:100% is real.
_DEFAULT_VIZ_H = 680


def _height_px_from_style(style: str) -> int | None:
    """Parse ``height: Npx`` from an inline style string, if present."""
    if not style:
        return None
    m = re.search(r"(?:^|;)\s*height\s*:\s*([\d.]+)\s*px", style, flags=re.I)
    if not m:
        return None
    try:
        return max(80, int(float(m.group(1))))
    except (TypeError, ValueError):
        return None


def _width_px_from_style(style: str) -> int | None:
    """Parse ``width: Npx`` from an inline style string, if present."""
    if not style:
        return None
    m = re.search(r"(?:^|;)\s*width\s*:\s*([\d.]+)\s*px", style, flags=re.I)
    if not m:
        return None
    try:
        return max(80, int(float(m.group(1))))
    except (TypeError, ValueError):
        return None


def _plotly_fill_script(host_id: str) -> str:
    """JS: force every Plotly graph inside host to the host's pixel box."""
    return (
        f"<script>(function(){{"
        f"var host=document.getElementById({json.dumps(host_id)});"
        f"if(!host)return;"
        f"function fill(){{"
        f"if(!window.Plotly)return;"
        f"var w=host.clientWidth|0,h=host.clientHeight|0;"
        f"if(w<40||h<40)return;"
        f"host.querySelectorAll('.js-plotly-plot,.plotly-graph-div').forEach(function(gd){{"
        f"gd.style.width='100%';gd.style.height='100%';gd.style.minHeight=h+'px';"
        f"try{{window.Plotly.relayout(gd,{{autosize:false,width:w,height:h}});"
        f"}}catch(e){{try{{window.Plotly.Plots.resize(gd);}}catch(e2){{}}}}"
        f"}});"
        f"}}"
        f"function boot(){{fill();setTimeout(fill,80);setTimeout(fill,300);setTimeout(fill,800);}}"
        f"if(window.Plotly)boot();else{{var n=0,t=setInterval(function(){{"
        f"if(window.Plotly||++n>80){{clearInterval(t);if(window.Plotly)boot();}}"
        f"}},50);}}"
        f"if(window.ResizeObserver)try{{new ResizeObserver(function(){{fill();}}).observe(host);}}catch(e){{}}"
        f"window.addEventListener('resize',fill);"
        f"}})();</script>"
    )


def _plotly_host_html(inner: str, *, height: int) -> str:
    """Full-width Plotly mount in the slide (no nested iframe)."""
    h = max(240, min(int(height), 1000))
    host_id = f"ph-{abs(hash(inner)) % (10**12):x}"
    # Neutralize fixed px sizes Plotly/fig.show bake into wrappers
    cleaned = re.sub(
        r"""(?i)(style\s*=\s*["'][^"']*?)(?:max-)?width\s*:\s*[\d.]+px\s*;?""",
        r"\1",
        inner,
    )
    cleaned = re.sub(
        r"""(?i)(style\s*=\s*["'][^"']*?)(?:max-)?height\s*:\s*[\d.]+px\s*;?""",
        r"\1",
        cleaned,
    )
    return (
        f'<div id="{host_id}" class="sslive-plotly-host" '
        f'style="width:100%;height:{h}px;min-height:{h}px;max-width:100%;'
        f'display:block;position:relative;overflow:hidden;'
        f'border-radius:8px;background:#0b1220;box-sizing:border-box">'
        f"{cleaned}"
        f"{_plotly_fill_script(host_id)}"
        f"</div>"
    )


def _isolate_rich_html(html: str, *, min_height: int | None = None) -> str:
    """Prepare rich HTML for the slide.

    * **Plotly** — mount directly in the slide (srcdoc is already sandboxed from
      parent HTMX). Nested ``data:`` iframes were collapsing to Plotly's default
      ~700×450 box, which looks tiny on the 1920×1080 stage.
    * **Other script HTML** — still sandboxed in a nested iframe so parent HTMX
      never parses tags (push path also base64-encodes the fragment).
    """
    html = _scrub_output_html(html or "")
    if not html.strip():
        return ""
    if not _looks_like_rich_html(html):
        return html

    h = int(min_height or _DEFAULT_VIZ_H)
    h = max(240, min(h, 1000))

    # Plotly: full-width host in the slide document (presenter has Plotly CDN)
    if _looks_like_plotly(html):
        return _plotly_host_html(html, height=h)

    # Non-plotly rich HTML (generic scripts) — nested iframe isolation
    doc = (
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\">\n"
        "<style>html,body{margin:0;padding:0;width:100%;height:100%;"
        "background:#0b1220;color:#e5e7eb;overflow:auto}</style>\n"
        f"</head><body>\n{html}\n</body></html>"
    )
    raw = doc.encode("utf-8")
    if len(raw) > _MAX_ISOLATED_HTML_BYTES:
        return (
            '<pre style="color:#fecaca;background:#7f1d1d;padding:0.5rem;'
            'border-radius:6px;white-space:pre-wrap">'
            f"HTML output too large for in-slide display "
            f"({len(raw) // 1024} KB).</pre>"
        )
    b64 = base64.b64encode(raw).decode("ascii")
    return (
        f'<iframe class="sslive-viz-frame" '
        f'sandbox="allow-scripts allow-same-origin allow-popups" '
        f'style="width:100%;height:{h}px;min-height:{h}px;'
        f'border:0;border-radius:8px;background:#0b1220;display:block" '
        f'src="data:text/html;base64,{b64}" '
        f'title="cell output"></iframe>'
    )


def _iframe_src_from_html(html: str) -> str | None:
    m = re.search(
        r"""<iframe\b[^>]*\bsrc\s*=\s*["']([^"']+)["']""",
        html or "",
        flags=re.I,
    )
    return m.group(1).strip() if m else None


def _is_nonportable_viewer_url(url: str) -> bool:
    """True for localhost / relative / blob URLs that break outside SolveIt."""
    u = (url or "").strip()
    if not u:
        return True
    low = u.lower()
    if low.startswith("data:"):
        return False
    if low.startswith(("blob:", "about:", "javascript:")):
        return True
    if "localhost" in low or "127.0.0.1" in low or "0.0.0.0" in low:
        return True
    # Host-relative path (not //cdn...)
    if u.startswith("/") and not u.startswith("//"):
        return True
    if not re.match(r"^https?://", u, flags=re.I) and not u.startswith("//"):
        return True
    return False


def _export_viz_placeholder(reason: str, *, min_height: int = 360) -> str:
    msg = html_module.escape(reason)
    return (
        f'<div class="sslive-viz-missing" style="width:100%;min-height:{min_height}px;'
        f'display:flex;align-items:center;justify-content:center;text-align:center;'
        f'padding:1.5rem;border-radius:8px;background:#1f2937;border:1px dashed #4b5563;'
        f'color:#9ca3af;font:14px/1.5 system-ui,sans-serif">'
        f"<div><div style='font-size:2rem;margin-bottom:0.5rem'>◇</div>"
        f"<strong style='color:#e5e7eb'>Interactive viewer not embedded</strong><br/>"
        f"<span style='font-size:13px'>{msg}</span></div></div>"
    )


def _try_embed_local_viewer(src: str, *, min_height: int) -> str | None:
    """If viewer is on localhost, fetch HTML and put in data: URL with <base>.

    Works when the export is viewed while that server is still up (same machine).
    Fully offline-safe only if the page is self-contained.
    """
    try:
        from urllib.parse import urlparse
        from urllib.request import Request, urlopen
    except Exception:
        return None
    try:
        req = Request(src, headers={"User-Agent": "sslive-export/0.1"})
        with urlopen(req, timeout=4.0) as resp:  # nosec B310 — local export helper
            raw = resp.read()
            ctype = (resp.headers.get("Content-Type") or "").lower()
    except Exception:
        return None
    if len(raw) > 2_500_000:
        return None
    if "html" not in ctype and not raw.lstrip()[:50].lower().startswith(
        (b"<!doctype", b"<html", b"<")
    ):
        return None
    try:
        text = raw.decode("utf-8", errors="replace")
    except Exception:
        return None
    parsed = urlparse(src)
    base = f"{parsed.scheme}://{parsed.netloc}/"
    if re.search(r"<base\b", text, flags=re.I) is None:
        if re.search(r"<head\b", text, flags=re.I):
            text = re.sub(
                r"(<head\b[^>]*>)",
                rf'\1<base href="{html_module.escape(base, quote=True)}">',
                text,
                count=1,
                flags=re.I,
            )
        else:
            text = f'<head><base href="{html_module.escape(base, quote=True)}"></head>\n' + text
    b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
    return (
        f'<iframe class="sslive-viz-frame" '
        f'sandbox="allow-scripts allow-same-origin allow-popups allow-forms" '
        f'style="width:100%;height:{min_height}px;min-height:{min_height}px;'
        f'border:0;border-radius:8px;background:#0b1220;display:block" '
        f'src="data:text/html;base64,{b64}" title="embedded viewer"></iframe>'
    )


def _html_for_export(html: str, *, min_height: int = 420) -> str:
    """Make captured HTML safer for a portable file (pointcloud / Three.js / etc.)."""
    html = _scrub_output_html(html or "")
    if not html.strip():
        return _export_viz_placeholder("Empty HTML output.", min_height=min_height)

    src = _iframe_src_from_html(html)
    if src:
        if not _is_nonportable_viewer_url(src):
            # Public absolute URL — keep as-is (needs network)
            return html
        # Localhost / relative: try to snapshot the viewer document
        embedded = _try_embed_local_viewer(src, min_height=min_height)
        if embedded:
            return embedded
        return _export_viz_placeholder(
            "This was a SolveIt/host viewer (e.g. %pointcloud / Three.js) served from "
            f"<code style='color:#93c5fd'>{html_module.escape(src[:120])}</code>. "
            "That URL is not available outside the live environment. "
            "Re-open in SolveIt, or use a static plot (matplotlib/Plotly) for portable export.",
            min_height=min_height,
        )

    # Inline Three.js / full HTML apps — sandbox in nested data-URL iframe
    if re.search(
        r"three\.js|three\.min\.js|\bTHREE\b|webgl|pointcloud|babylon",
        html,
        flags=re.I,
    ):
        return _isolate_rich_html(html, min_height=min_height)

    if _looks_like_rich_html(html):
        return _isolate_rich_html(html, min_height=min_height)
    return html


def render_output_html(
    parts: list[OutputPart],
    cell_id: str,
    theme: dict | None = None,
    *,
    style: str = "",
    extra_attrs: str = "",
    portable: bool = False,
) -> str:
    """Return HTML for #el-output-{cell_id}. No FastHTML required.

    ``portable=True`` rewrites host-only viewers (localhost iframes) for export.
    """
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

    # When the output box has an explicit layout height, fill it; else a roomy default.
    layout_h = _height_px_from_style(style)
    layout_w = _width_px_from_style(style)
    viz_h = max(240, layout_h - 8) if layout_h else _DEFAULT_VIZ_H
    has_html = any(p.kind == "text/html" for p in parts)

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
            if portable:
                body = _html_for_export(p.text or "", min_height=viz_h)
            else:
                body = _isolate_rich_html(p.text or "", min_height=viz_h)
            fill = "height:100%;" if layout_h else f"min-height:{viz_h}px;"
            chunks.append(
                f'<div class="sslive-html sslive-html-viz" style="width:100%;{fill}'
                f'overflow:hidden">{body}</div>'
            )
        elif p.kind == "text/plain":
            chunks.append(f'<pre style="{out_st}">{html_module.escape(p.text)}</pre>')

    if not chunks:
        chunks.append(f'<pre style="{out_st}opacity:0.5">(no output)</pre>')
    inner = "\n".join(chunks)
    eid = html_module.escape(f"el-output-{cell_id}")
    # Plotly/HTML: force full slide width + roomy height unless user set layout w/h.
    # Tiny saved overlays (or shrink-to-fit absolute boxes) made the plot card tiny.
    out_style = style or ""
    compact = out_style.replace(" ", "").lower()
    if has_html:
        if not layout_w or (layout_w is not None and layout_w < 500):
            # Drop a too-small explicit width so the plot can span the column
            if layout_w is not None and layout_w < 500:
                out_style = re.sub(
                    r"(?:^|;)\s*width\s*:\s*[\d.]+px\s*;?",
                    ";",
                    out_style,
                    flags=re.I,
                )
                compact = out_style.replace(" ", "").lower()
            if "width:" not in compact:
                out_style = (out_style.rstrip(";") + "; " if out_style else "") + (
                    "width:100%;max-width:100%;box-sizing:border-box;"
                )
                compact = out_style.replace(" ", "").lower()
        if not layout_h:
            if "height:" not in compact:
                out_style = (
                    out_style.rstrip(";") + "; " if out_style else ""
                ) + f"height:{viz_h}px;min-height:{viz_h}px;"
    return (
        f'<div id="{eid}" data-el-id="{eid}" '
        f'data-type="output" data-cell-id="{html_module.escape(cell_id)}"'
        f'{extra_attrs}{_style_attr(out_style)}>'
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


def _mark_slive_local_magic() -> None:
    """Tell CRAFT/SolveIt that %slive / %slive_export run on the *host* under %gpu."""
    if get_ipython is None:
        return
    ip = get_ipython()
    names = (
        "%slive",
        "slive",
        "%sslive",
        "sslive",
        "%slive_export",
        "slive_export",
    )
    # CRAFT helper
    try:
        reg = (ip.user_ns or {}).get("register_local_magic")
        if callable(reg):
            for n in names:
                try:
                    reg(n)
                except Exception:
                    pass
    except Exception:
        pass
    # Some CRAFT builds keep a set of local magic names
    try:
        ns = ip.user_ns or {}
        for key in ("_local_magics", "local_magics", "_craft_local_magics"):
            bag = ns.get(key)
            if isinstance(bag, set):
                bag.update(names)
            elif isinstance(bag, list):
                for n in names:
                    if n not in bag:
                        bag.append(n)
    except Exception:
        pass


def _ensure_local_magic():
    """Register ``%slive`` as a *local* magic so it works under ``%gpu`` mode."""
    _mark_slive_local_magic()

def _host_ok() -> tuple[bool, str]:
    """Whether the sslive *host* can run (SolveIt host + dialoghelper).

    Under ``%gpu``, *code cells* run remotely — including ``%run sslive`` and
    bare ``await slive()`` unless they are local magics. dialoghelper only
    exists on the SolveIt host, so the module must be loaded with ``%local``.
    """
    if find_msgs is None or update_msg is None:
        return False, "dialoghelper missing (loaded on remote GPU kernel?)"
    if not _in_solveit():
        return True, "dev host"
    return True, "host"


def _math_is_display(raw: str) -> bool:
    r = (raw or "").strip()
    return (r.startswith("$$") and r.endswith("$$")) or (
        r.startswith("\\[") and r.endswith("\\]")
    )


def _math_to_mathml(latex_str: str) -> str:
    """`$...$` / `$$...$$` / ``\\(...\\)`` / ``\\[...\\]`` → MathML."""
    raw = (latex_str or "").strip()
    if not raw:
        return ""
    display = _math_is_display(raw)
    body = raw
    if raw.startswith("$$") and raw.endswith("$$") and len(raw) >= 4:
        body = raw[2:-2].strip()
    elif raw.startswith("\\[") and raw.endswith("\\]"):
        body = raw[2:-2].strip()
    elif raw.startswith("\\(") and raw.endswith("\\)"):
        body = raw[2:-2].strip()
    elif raw.startswith("$") and raw.endswith("$") and not raw.startswith("$$"):
        body = raw[1:-1].strip()
    if _l2m is None:
        esc = html_module.escape(raw if not body else body)
        if display:
            return f'<div class="math-block">{esc}</div>'
        return f'<span class="math-inline">{esc}</span>'
    try:
        mathml = _l2m.convert(body)
    except Exception:
        esc = html_module.escape(body or raw)
        if display:
            return f'<div class="math-block">{esc}</div>'
        return f'<span class="math-inline">{esc}</span>'
    if display:
        mathml = mathml.replace('display="inline"', 'display="block"')
        return f'<div class="math-block">{mathml}</div>'
    return f'<span class="math-inline">{mathml}</span>'


def _extract_math_placeholders(content: str) -> tuple[str, list[str]]:
    """Pull math out before markdown so `_`/`^`/`\\` survive.

    Placeholders distinguish display vs inline so S2-D can split elements only
    on **display** math (``$$…$$`` / ``\\[…\\]``). Inline ``$…$`` / ``\\(…\\)``
    stay inside the same bullet/paragraph.
    """
    math_blocks: list[str] = []

    def save_display(m: "re.Match[str]") -> str:
        math_blocks.append(m.group(0))
        return f"SSLIVEDISP{len(math_blocks) - 1}X"

    def save_inline(m: "re.Match[str]") -> str:
        math_blocks.append(m.group(0))
        return f"SSLIVEINL{len(math_blocks) - 1}X"

    # Display first so nested/adjacent cases stay correct
    content = re.sub(r"\$\$(.*?)\$\$", save_display, content, flags=re.DOTALL)
    content = re.sub(r"\\\[(.*?)\\\]", save_display, content, flags=re.DOTALL)
    content = re.sub(r"\$([^\$\n]+)\$", save_inline, content)
    content = re.sub(r"\\\((.*?)\\\)", save_inline, content, flags=re.DOTALL)
    return content, math_blocks


def _restore_math_in_html(html: str, math_blocks: list[str]) -> str:
    for i, block in enumerate(math_blocks):
        try:
            rep = _math_to_mathml(block)
        except Exception:
            rep = html_module.escape(block)
        # display + inline (+ legacy SSLIVEMATH from older paths)
        for prefix in ("SSLIVEDISP", "SSLIVEINL", "SSLIVEMATH"):
            html = html.replace(f"{prefix}{i}X", rep)
    return html


# Only **display** math + images create new slide elements; inline math does not.
_DISP_PH_RE = re.compile(r"SSLIVEDISP(\d+)X")
_ANY_MATH_PH_RE = re.compile(r"SSLIVE(?:DISP|INL|MATH)(\d+)X")
_MD_IMG_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
_HTML_IMG_RE = re.compile(r"<img\b[^>]*>", re.I)
_ATTACH_RE = re.compile(r"!\[([^\]]*)\]\(attachment:([^)]+)\)")
_MEDIA_SPLIT_RE = re.compile(
    r"(SSLIVEDISP\d+X)|(<img\b[^>]*>)|(!\[[^\]]*\]\([^)]+\))",
    re.I,
)


def _resolve_note_attachments(content: str, nb_attachments: dict | None) -> str:
    """Rewrite ``attachment:`` markdown images to data-URLs when available."""
    if not nb_attachments:
        return content

    def repl(m: "re.Match[str]") -> str:
        alt, att_id = m.group(1), m.group(2)
        att = nb_attachments.get(att_id) or {}
        for mime in ("image/png", "image/jpeg", "image/gif", "image/webp"):
            b64 = att.get(mime)
            if b64:
                if isinstance(b64, list):
                    b64 = "".join(b64)
                return f"![{alt}](data:{mime};base64,{b64})"
        return m.group(0)

    return _ATTACH_RE.sub(repl, content)


def _note_to_html_md(content: str) -> str:
    """Full note render: mistletoe markdown + latex2mathml (sslides pipeline)."""
    content, math_blocks = _extract_math_placeholders(content or "")
    with _MdHTMLRenderer() as renderer:
        html = renderer.render(_MdDocument(content))
    return _restore_math_in_html(html, math_blocks)


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


def _image_payload_to_html(payload: str) -> str:
    payload = (payload or "").strip()
    if payload.lower().startswith("<img"):
        return f'<figure class="note-image">{payload}</figure>'
    m = _MD_IMG_RE.match(payload)
    if m:
        alt, src = m.group(1), m.group(2)
        return (
            f'<figure class="note-image">'
            f'<img src="{html_module.escape(src, quote=True)}" '
            f'alt="{html_module.escape(alt, quote=True)}" draggable="false"/>'
            f"</figure>"
        )
    return f'<figure class="note-image"><img alt="image" draggable="false"/></figure>'


def _wrap_list_item_html(inner_html: str) -> str:
    h = (inner_html or "").strip()
    if h.startswith("<ul") or h.startswith("<ol"):
        return h
    if h.startswith("<li"):
        return f'<ul class="note-list">{h}</ul>'
    return f'<ul class="note-list"><li class="note-li">{h}</li></ul>'


def _html_is_effectively_empty(html: str) -> bool:
    text = re.sub(r"<[^>]+>", "", html or "")
    text = html_module.unescape(text).strip()
    return not text


def _split_media_runs(
    fragment: str,
    math_blocks: list[str],
    *,
    text_kind: str,
) -> list[tuple[str, str, str]]:
    """Split HTML/text on **display math** + images only (not inline math).

    Inline ``$…$`` placeholders stay in the text run and are restored as MathML
    inside the same list_item / paragraph element.
    """
    frag = fragment or ""
    if not frag.strip():
        return []
    if not _MEDIA_SPLIT_RE.search(frag):
        return []

    runs: list[tuple[str, str, str]] = []
    pos = 0
    for m in _MEDIA_SPLIT_RE.finditer(frag):
        if m.start() > pos:
            chunk = frag[pos : m.start()]
            if not _html_is_effectively_empty(chunk):
                html = _restore_math_in_html(chunk.strip(), math_blocks)
                if text_kind == "list_item":
                    html = _wrap_list_item_html(html)
                plain = re.sub(r"<[^>]+>", "", chunk).strip()
                plain = _ANY_MATH_PH_RE.sub("", plain).strip()
                if plain or not _html_is_effectively_empty(html):
                    runs.append((text_kind, plain, html))
        token = m.group(0)
        if token.startswith("SSLIVEDISP"):
            mm = _DISP_PH_RE.fullmatch(token)
            idx = int(mm.group(1)) if mm else -1
            raw = math_blocks[idx] if 0 <= idx < len(math_blocks) else token
            runs.append(("math", raw, _math_to_mathml(raw)))
        else:
            runs.append(("image", token, _image_payload_to_html(token)))
        pos = m.end()
    if pos < len(frag):
        chunk = frag[pos:]
        if not _html_is_effectively_empty(chunk):
            html = _restore_math_in_html(chunk.strip(), math_blocks)
            if text_kind == "list_item":
                html = _wrap_list_item_html(html)
            plain = re.sub(r"<[^>]+>", "", chunk).strip()
            plain = _ANY_MATH_PH_RE.sub("", plain).strip()
            if plain or not _html_is_effectively_empty(html):
                runs.append((text_kind, plain, html))
    # If the only non-empty runs are the same kind we started with and no
    # actual media, treat as "no split" (caller emits whole block).
    if runs and all(k == text_kind for k, _, _ in runs) and len(runs) == 1:
        return runs
    if not any(k in ("math", "image") for k, _, _ in runs):
        return []
    return runs


def _token_plain(token: Any) -> str:
    parts: list[str] = []

    def walk(t: Any) -> None:
        c = getattr(t, "content", None)
        if isinstance(c, str) and c:
            parts.append(c)
        for ch in getattr(t, "children", None) or []:
            walk(ch)

    walk(token)
    return "".join(parts)


def parse_note_to_elements(
    source: str,
    cell_id: str,
    nb_attachments: dict | None = None,
) -> list[dict]:
    """Split a note cell into fine-grained elements (S2-D).

    Returns ``[{id, kind, html, content}, ...]`` with ids ``el-{idx}-{cell_id}``.
    """
    source = _resolve_note_attachments(source or "", nb_attachments)
    out: list[dict] = []

    def emit(kind: str, html: str, content: str = "") -> None:
        html = (html or "").strip()
        if not html:
            return
        idx = len(out)
        out.append(
            {
                "id": f"el-{idx}-{cell_id}",
                "kind": kind,
                "html": html,
                "content": content or "",
            }
        )

    def emit_runs(runs: list[tuple[str, str, str]]) -> None:
        for kind, content, html in runs:
            emit(kind, html, content)

    if _MdDocument is not None and _MdHTMLRenderer is not None:
        try:
            content, math_blocks = _extract_math_placeholders(source)
            doc = _MdDocument(content)
            with _MdHTMLRenderer() as renderer:
                for child in doc.children or []:
                    tname = type(child).__name__
                    if tname == "ThematicBreak":
                        continue
                    if tname == "List":
                        for item in child.children or []:
                            item_html = renderer.render(item)
                            runs = _split_media_runs(
                                item_html, math_blocks, text_kind="list_item"
                            )
                            if runs:
                                emit_runs(runs)
                                continue
                            html = _restore_math_in_html(item_html, math_blocks)
                            if not html.strip().startswith("<ul") and not html.strip().startswith("<ol"):
                                html = _wrap_list_item_html(html)
                            emit("list_item", html, _token_plain(item))
                        continue

                    kind_map = {
                        "Heading": "heading",
                        "Paragraph": "paragraph",
                        "CodeFence": "code",
                        "BlockCode": "code",
                        "Quote": "quote",
                        "Table": "table",
                        "HTMLBlock": "html",
                    }
                    kind = kind_map.get(tname, (tname or "block").lower())
                    html = renderer.render(child)
                    plain = _token_plain(child)

                    if kind in ("paragraph", "heading", "quote", "html"):
                        runs = _split_media_runs(html, math_blocks, text_kind=kind)
                        if runs:
                            # image-only paragraph → just image(s)
                            emit_runs(runs)
                            continue
                        # paragraph that is only image(s) after render
                        imgs = _HTML_IMG_RE.findall(html)
                        without = _HTML_IMG_RE.sub("", html)
                        if imgs and _html_is_effectively_empty(without):
                            for im in imgs:
                                emit("image", _image_payload_to_html(im), im)
                            continue

                    emit(kind, _restore_math_in_html(html, math_blocks), plain)
            if out:
                return out
        except Exception as e:
            try:
                _SESSION["_note_parse_err"] = str(e)
            except Exception:
                pass

    return _parse_note_to_elements_basic(source, cell_id)


def _parse_note_to_elements_basic(source: str, cell_id: str) -> list[dict]:
    """Line-heuristic fallback when mistletoe is unavailable."""
    out: list[dict] = []

    def emit(kind: str, html: str, content: str = "") -> None:
        if not (html or "").strip():
            return
        out.append(
            {
                "id": f"el-{len(out)}-{cell_id}",
                "kind": kind,
                "html": html,
                "content": content,
            }
        )

    text = source or ""
    # Split out display-math blocks first
    segments: list[tuple[str, str]] = []
    pos = 0
    for m in re.finditer(r"\$\$(.*?)\$\$", text, flags=re.DOTALL):
        if m.start() > pos:
            segments.append(("body", text[pos : m.start()]))
        segments.append(("math", m.group(0)))
        pos = m.end()
    if pos < len(text):
        segments.append(("body", text[pos:]))
    if not segments:
        segments = [("body", text)]

    for seg_kind, chunk in segments:
        if seg_kind == "math":
            emit("math", _math_to_mathml(chunk), chunk)
            continue
        for block in re.split(r"\n\s*\n", chunk):
            block = block.strip()
            if not block:
                continue
            if _MD_IMG_RE.fullmatch(block):
                emit("image", _image_payload_to_html(block), block)
                continue
            lines = [ln for ln in block.splitlines() if ln.strip()]
            if lines and all(re.match(r"^\s*([-*+]|\d+\.)\s+", ln) for ln in lines):
                for ln in lines:
                    item = re.sub(r"^\s*([-*+]|\d+\.)\s+", "", ln.strip())
                    ph, maths = _extract_math_placeholders(item)
                    runs = _split_media_runs(ph, maths, text_kind="list_item")
                    if runs:
                        for k, c, h in runs:
                            emit(k, h, c)
                    else:
                        # Inline math stays in the bullet (placeholders are alnum-safe)
                        inner = _restore_math_in_html(
                            f"<p>{html_module.escape(ph)}</p>", maths
                        )
                        emit("list_item", _wrap_list_item_html(inner), item)
                continue
            first = lines[0].strip() if lines else block
            if first.startswith("#"):
                level = len(first) - len(first.lstrip("#"))
                level = min(max(level, 1), 3)
                title = first[level:].strip()
                tag = f"h{level}"
                cls = " slide-h1" if level == 1 else (" slide-h2" if level == 2 else "")
                emit(
                    "heading",
                    f'<{tag} class="{cls.strip()}">{html_module.escape(title)}</{tag}>',
                    title,
                )
                rest = "\n".join(lines[1:]).strip()
                if rest:
                    emit(
                        "paragraph",
                        f"<p class='slide-p'>{html_module.escape(rest)}</p>",
                        rest,
                    )
                continue
            ph, maths = _extract_math_placeholders(block)
            runs = _split_media_runs(ph, maths, text_kind="paragraph")
            if runs:
                for k, c, h in runs:
                    emit(k, h, c)
            else:
                inner = _restore_math_in_html(
                    f"<p class='slide-p'>{html_module.escape(ph).replace(chr(10), '<br/>')}</p>",
                    maths,
                )
                emit("paragraph", inner, block)
    if not out and (source or "").strip():
        emit("paragraph", _note_to_html_basic(source), source)
    return out


def _code_block_html(cell: Cell, *, style: str = "", extra_attrs: str = "") -> str:
    """In-slide editable code (one-line bar) + Run; edit opens floating panel."""
    cid = html_module.escape(cell.id)
    # raw id for JS (safe: dialog ids are alphanumeric + underscore)
    raw_id = cell.id
    src = html_module.escape(cell.source)
    # Default to a single visible line so long viz scripts don't dominate the
    # slide; focus/click opens a floating ~6-line editor (not in-place grow).
    n_lines = 1
    ta_h = 34  # ~one line at 14px / 1.45 line-height
    style_bar = _strip_code_bar_height(style)
    return f"""
    <div id="el-code-{cid}" class="code-wrap" data-el-id="el-code-{cid}" data-type="code"
         data-cell-id="{cid}" data-runnable="1"{extra_attrs}{_style_attr(style_bar)}
         tabindex="0" onclick="selectCell('{cid}')">
      <div class="code-toolbar">
        <span class="drag-grip" data-drag-for="el-code-{cid}" title="drag to move">⠿</span>
        <button type="button" class="run-btn" data-cell-id="{cid}"
          onclick="event.stopPropagation(); runCellFromSlide('{raw_id}')">▶ Run</button>
        <span class="cell-id">{cid}</span>
        <span class="hint">click code · floating edit · Shift+Enter run</span>
      </div>
      <textarea class="code-ta" id="ta-{cid}" data-cell-id="{cid}"
        spellcheck="false" rows="{n_lines}"
        style="height:{ta_h}px"
        data-collapsed-h="{ta_h}"
        onfocus="selectCell('{cid}'); codeTaFocus(this)"
        onblur="codeTaBlur(this)"
        onkeydown="onCodeKey(event,'{raw_id}')">{src}</textarea>
    </div>
    """


def _slide_html(deck: Deck, slide: Slide, *, active: bool = False) -> str:
    parts: list[str] = []
    for cid in slide.cell_ids:
        cell = deck.cells[cid]
        if cell.kind == "note":
            # S2-D: one DOM node per parsed piece (title, bullet, math, image, …)
            note_ids = cell.element_ids or [f"el-0-{cid}"]
            for el_id in note_ids:
                el = deck.elements.get(el_id)
                if el is None:
                    continue
                eid = html_module.escape(el_id)
                spec = _layout_spec(deck, el_id)
                style = _el_style(spec)
                kind = html_module.escape(el.kind or "paragraph")
                body = el.html if el.html else _note_to_html(el.content or cell.source)
                parts.append(
                    f'<div id="{eid}" class="note-block" data-el-id="{eid}" '
                    f'data-type="{kind}" data-cell-id="{html_module.escape(cid)}"'
                    f'{_reveal_attr(spec)}{_style_attr(style)}>{body}</div>'
                )
        else:
            cspec = _layout_spec(deck, f"el-code-{cid}")
            ospec = _layout_spec(deck, f"el-output-{cid}")
            parts.append(
                _code_block_html(
                    cell,
                    style=_el_style(cspec),
                    extra_attrs=_reveal_attr(cspec),
                )
            )
            # always emit output mount (seeded from last outputs / notebook)
            parts.append(
                render_output_html(
                    cell.outputs, cell.id, deck.theme or THEME_DARK,
                    style=_el_style(ospec),
                    extra_attrs=_reveal_attr(ospec),
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
    /* S2-D fine pieces: tighter consecutive list items, math / image / table boxes */
    .note-block[data-type="list_item"] {{ margin:0.15rem 0; }}
    .note-block[data-type="list_item"] .note-list {{ margin:0; padding-left:1.4em; }}
    .note-block[data-type="math"] {{ margin:0.6rem 0; }}
    .note-block .math-block {{ text-align:center; margin:0.4em 0; overflow-x:auto; }}
    .note-block .math-inline {{ display:inline; }}
    .note-block[data-type="image"], .note-block .note-image {{ margin:0.5rem 0; }}
    .note-block .note-image {{ margin:0; }}
    .note-block .note-image img, .note-block[data-type="image"] img {{
      max-width:100%; height:auto; display:block; border-radius:6px; }}
    .note-block[data-type="table"] {{ overflow-x:auto; margin:0.5rem 0; }}
    .note-block table {{ border-collapse:collapse; width:100%; font-size:0.9em; }}
    .note-block th, .note-block td {{ border:1px solid #374151; padding:0.35em 0.6em; text-align:left; }}
    .note-block th {{ background:#1f2937; }}
    .note-block code {{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:0.8em;
      background:{theme.get("code_bg", "#1f2937")}; border:1px solid #374151; border-radius:4px;
      padding:0.08em 0.35em; }}
    .note-block pre {{ background:#111827; border:1px solid #374151; border-radius:6px;
      padding:0.6em 0.8em; overflow-x:auto; }}
    .note-block pre code {{ background:none; border:0; padding:0; }}
    .note-block img {{ max-width:100%; }}
    .sslive-html {{ width:100%; max-width:100%; }}
    .sslive-html-viz {{ display:block; width:100%; max-width:100%; }}
    /* Plotly host: full content-column width, large default height */
    .sslive-plotly-host {{ width:100% !important; max-width:100%; box-sizing:border-box;
      position:relative; overflow:hidden; border-radius:8px; background:#0b1220; }}
    .sslive-plotly-host .js-plotly-plot,
    .sslive-plotly-host .plotly-graph-div,
    .sslive-plotly-host .plot-container,
    .sslive-plotly-host .svg-container {{ width:100% !important; height:100% !important;
      min-height:100% !important; }}
    .sslive-html iframe, iframe.sslive-viz-frame {{ width:100%; min-height:560px; border:0;
      border-radius:8px; background:#0b1220; display:block; }}
    [data-type="output"] {{ display:block; width:100%; max-width:100%; box-sizing:border-box;
      min-height:0; }}
    [data-type="output"] .sslive-html-viz {{ width:100%; }}
    [data-type="output"] .sslive-html-viz iframe.sslive-viz-frame {{ height:100%; min-height:240px; }}
    .sslive-html canvas, .sslive-html video {{ max-width:100%; height:auto; }}
    .note-block a {{ color:#60a5fa; }}
    .note-block blockquote {{ border-left:3px solid #4b5563; margin:0.5rem 0;
      padding:0 0 0 0.8em; color:{theme.get("muted", "#9ca3af")}; }}
    .note-block .math-block {{ text-align:center; margin:0.8em 0; }}
    .note-block math {{ font-size:1.1em; }}
    /* Code box: toolbar + one-line bar; editing uses #live-code-pop (floating) */
    .code-wrap {{ border:1px solid #374151; border-radius:8px; background:{theme.get("code_bg", "#1f2937")};
      padding:8px 12px; outline:none; flex:0 0 auto; align-self:stretch; max-width:100%;
      box-sizing:border-box; }}
    .code-wrap.selected {{ border-color:#60a5fa; box-shadow:0 0 0 2px rgba(96,165,250,0.35); }}
    .code-wrap.code-open {{ border-color:#60a5fa; }}
    .code-toolbar {{ display:flex; align-items:center; gap:12px; margin-bottom:6px; flex-wrap:wrap; }}
    .run-btn {{ cursor:pointer; background:#2563eb; color:white; border:0; border-radius:6px;
      padding:6px 14px; font-size:14px; font-weight:600; }}
    .run-btn:hover {{ background:#1d4ed8; }}
    .run-btn:disabled {{ opacity:0.5; cursor:wait; }}
    .cell-id {{ font-size:11px; color:{theme.get("muted", "#9ca3af")}; font-family:ui-monospace,monospace; }}
    .hint {{ font-size:11px; color:#6b7280; }}
    .code-ta {{ width:100%; box-sizing:border-box; margin:0; resize:none;
      font-family:ui-monospace,SFMono-Regular,Menlo,monospace; line-height:1.45;
      font-size:var(--code-fs, 14px); white-space:pre;
      color:#e5e7eb; background:#111827; border:1px solid #4b5563; border-radius:6px;
      padding:6px 10px; outline:none; height:34px; min-height:34px; max-height:34px;
      overflow:hidden; cursor:pointer; }}
    .code-ta:focus {{ border-color:#60a5fa; }}
    /* Floating live editor (above plots; body-portaled so stage scale is fine) */
    #live-code-pop {{
      position:fixed; z-index:45; display:flex; flex-direction:column;
      width:min(920px, 90vw); height:200px; min-width:280px; min-height:148px;
      max-width:95vw; max-height:50vh;
      background:#0f172a; border:1px solid #60a5fa; border-radius:10px;
      box-shadow:0 12px 40px rgba(0,0,0,0.55), 0 0 0 1px rgba(96,165,250,0.25);
      overflow:hidden; box-sizing:border-box;
    }}
    #live-code-pop[hidden] {{ display:none !important; }}
    #live-code-pop .live-code-pop-head {{
      display:flex; align-items:center; gap:10px; flex:0 0 auto;
      padding:8px 12px; border-bottom:1px solid #1f2937; background:#111827;
    }}
    #live-code-pop .live-code-pop-head .hint {{ flex:1; }}
    #live-code-pop .live-code-pop-close {{
      background:#1f2937; border:1px solid #4b5563; color:#e5e7eb; border-radius:6px;
      font-size:12px; padding:4px 10px; cursor:pointer;
    }}
    #live-code-pop .live-code-pop-close:hover {{ border-color:#60a5fa; color:#93c5fd; }}
    #live-code-pop-ta {{
      flex:1 1 auto; width:100%; margin:0; padding:10px 14px; border:0; resize:none;
      font-family:ui-monospace,SFMono-Regular,Menlo,monospace; line-height:1.45;
      font-size:14px; white-space:pre; tab-size:4;
      color:#e5e7eb; background:#0b1220; outline:none; box-sizing:border-box;
      overflow:auto; min-height:0;
    }}
    #live-code-pop .live-code-pop-rs {{
      position:absolute; right:2px; bottom:2px; width:14px; height:14px; cursor:se-resize;
      background:linear-gradient(135deg, transparent 50%, #60a5fa 50%);
      border-radius:0 0 8px 0; opacity:0.85;
    }}
    #live-code-pop .live-code-pop-rs:hover {{ opacity:1; }}
    #chrome {{ position:fixed; left:12px; top:12px; z-index:50; display:flex; gap:10px; align-items:center;
      background:rgba(0,0,0,0.55); color:#fff; padding:6px 12px; border-radius:8px; font-size:13px; }}
    #chrome .ok {{ color:#86efac; }} #chrome .bad {{ color:#fca5a5; }}
    #nav {{ position:fixed; right:16px; bottom:16px; z-index:50; display:flex; gap:12px; align-items:center;
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
    /* Viz/output embeds (iframe/canvas) steal clicks — disable hit-testing in
       edit mode so the output *box* can be selected, dragged, and resized. */
    body.editing [data-type="output"] {{
      position:relative; min-height:64px; touch-action:none; }}
    body.editing [data-type="output"] iframe,
    body.editing [data-type="output"] canvas,
    body.editing [data-type="output"] video,
    body.editing [data-type="output"] .sslive-html,
    body.editing [data-type="output"] .sslive-html * {{
      pointer-events:none !important; }}
    body.editing [data-type="output"]::after {{
      content:'⠿ drag / resize'; position:absolute; top:6px; right:8px; z-index:3;
      font:11px/1.2 system-ui,sans-serif; color:#fbbf24; background:rgba(0,0,0,0.55);
      padding:3px 8px; border-radius:6px; pointer-events:none; letter-spacing:0.02em; }}
    .drag-grip {{ display:none; cursor:move; user-select:none; color:#9ca3af;
      font-size:18px; padding:2px 6px; touch-action:none; }}
    body.editing .drag-grip {{ display:inline-block; }}
    /* ── edit toolbar (S2-C): floats next to selection (viewport-fixed, unscaled) ── */
    #edit-toolbar {{ position:fixed; top:0; left:0; z-index:50;
      display:none; align-items:center; gap:6px; flex-wrap:wrap;
      max-width:min(96vw, 520px);
      background:rgba(3,7,18,0.94); border:1px solid #4b5563; color:#e5e7eb;
      padding:5px 8px; border-radius:8px; font-size:12px;
      box-shadow:0 6px 20px rgba(0,0,0,0.5); pointer-events:auto; }}
    body.editing #edit-toolbar.show {{ display:flex; }}
    #edit-toolbar button {{ background:#1f2937; border:1px solid #4b5563; color:#e5e7eb;
      border-radius:6px; padding:3px 8px; font-size:12px; cursor:pointer; }}
    #edit-toolbar button:hover:not(:disabled) {{ background:#374151; }}
    #edit-toolbar button:disabled {{ opacity:0.35; cursor:default; }}
    #edit-toolbar select {{ background:#1f2937; border:1px solid #4b5563; color:#e5e7eb;
      border-radius:6px; padding:3px 5px; font-size:12px; }}
    #edit-toolbar input[type=number] {{ background:#1f2937; border:1px solid #4b5563; color:#e5e7eb;
      border-radius:6px; padding:3px 4px; font-size:12px; width:48px; text-align:center;
      -moz-appearance:textfield; }}
    #edit-toolbar input[type=number]::-webkit-outer-spin-button,
    #edit-toolbar input[type=number]::-webkit-inner-spin-button {{ -webkit-appearance:none; margin:0; }}
    #edit-toolbar .tb-label {{ color:#9ca3af; font-family:ui-monospace,monospace; font-size:10px;
      max-width:120px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
    #edit-toolbar .tb-field {{ color:#9ca3af; font-size:11px; display:inline-flex; align-items:center; gap:4px; }}
    #edit-toolbar .tb-sep {{ width:1px; height:16px; background:#374151; }}
    #edit-toolbar #tb-fs-val {{ min-width:28px; text-align:center; }}
    /* Segmented reveal: hidden until → advances past data-reveal step */
    .frag-hidden {{ opacity:0 !important; visibility:hidden !important; pointer-events:none !important; }}
    body.editing .frag-hidden {{ opacity:0.35 !important; visibility:visible !important; pointer-events:auto !important; }}
    body.editing [data-reveal]:not([data-reveal=""]):not([data-reveal="0"])::before {{
      content: attr(data-reveal); position:absolute; top:-10px; left:-10px; z-index:8;
      min-width:18px; height:18px; padding:0 4px; border-radius:9px; font:700 11px/18px system-ui,sans-serif;
      background:#f59e0b; color:#111; text-align:center; pointer-events:none; }}
    body.editing .note-block[data-reveal], body.editing .code-wrap[data-reveal],
    body.editing [data-type="output"][data-reveal] {{ position:relative; }}
    /* External resize frame — lives on the slide, OUTSIDE the element (not clipped) */
    #rs-box {{ position:absolute; pointer-events:none; z-index:40;
      border:2px solid #60a5fa; box-sizing:border-box; margin:0; padding:0; }}
    #rs-box .rs-handle {{ position:absolute; width:14px; height:14px; background:#60a5fa;
      border:2px solid #fff; border-radius:2px; box-sizing:border-box;
      touch-action:none; z-index:41; pointer-events:auto;
      box-shadow:0 0 0 1px rgba(0,0,0,0.35); }}
    /* Handles sit fully outside the frame */
    #rs-box .rs-nw {{ left:-9px; top:-9px; cursor:nwse-resize; }}
    #rs-box .rs-n  {{ left:50%; top:-9px; transform:translateX(-50%); cursor:ns-resize; }}
    #rs-box .rs-ne {{ right:-9px; top:-9px; cursor:nesw-resize; }}
    #rs-box .rs-e  {{ right:-9px; top:50%; transform:translateY(-50%); cursor:ew-resize; }}
    #rs-box .rs-se {{ right:-9px; bottom:-9px; cursor:nwse-resize; }}
    #rs-box .rs-s  {{ left:50%; bottom:-9px; transform:translateX(-50%); cursor:ns-resize; }}
    #rs-box .rs-sw {{ left:-9px; bottom:-9px; cursor:nesw-resize; }}
    #rs-box .rs-w  {{ left:-9px; top:50%; transform:translateY(-50%); cursor:ew-resize; }}
    body.editing .el-editsel {{ outline:2px solid #f59e0b; outline-offset:2px; }}
    """

    js = f"""
    // Start at Python initial_slide (0 on fresh %slive). Only honor a sticky
    // parent index when force-restore is set (layout-save iframe rebuild).
    let currentSlide = {initial_slide};
    try {{
      if (window.parent && window.parent.__sslive_force_slide_restore) {{
        const p = window.parent.__sslive_slide_index;
        if (p != null && Number.isFinite(+p)) currentSlide = Math.max(0, (+p) | 0);
        window.parent.__sslive_force_slide_restore = false;
      }}
    }} catch (e) {{}}
    let selectedCellId = {json.dumps(first_code)};
    let lastResultT = 0;
    let fragStep = 0;  // how far into this slide's reveal sequence we are
    const slides = () => document.querySelectorAll('[data-slide]');

    function slideEls(slideEl) {{
      if (!slideEl) return [];
      return slideEl.querySelectorAll('.note-block, .code-wrap, [data-type="output"]');
    }}
    function revealOf(el) {{
      const r = parseInt(el.getAttribute('data-reveal'), 10);
      return (Number.isFinite(r) && r > 0) ? r : 0;
    }}
    function maxReveal(slideEl) {{
      let m = 0;
      slideEls(slideEl).forEach(el => {{ m = Math.max(m, revealOf(el)); }});
      return m;
    }}
    function applyFragments() {{
      const ss = slides()[currentSlide];
      if (!ss) return;
      // Present: hide until step reaches data-reveal.
      // Edit: show all, but dim elements that have a reveal step assigned.
      slideEls(ss).forEach(el => {{
        const r = revealOf(el);
        const hide = editing ? (r > 0) : (r > 0 && r > fragStep);
        el.classList.toggle('frag-hidden', hide);
      }});
      updateCounter();
    }}

    function updateCounter() {{
      const el = document.getElementById('slide-counter');
      if (!el) return;
      const n = slides().length;
      el.textContent = (currentSlide + 1) + ' / ' + Math.max(n, 1);
    }}

    function selectCell(id) {{
      selectedCellId = id;
      document.querySelectorAll('[data-runnable]').forEach(el => {{
        el.classList.toggle('selected', el.dataset.cellId === id);
      }});
    }}

    function showSlide(n, {{ selectFirst, frag }} = {{ selectFirst: true }}) {{
      closeLiveCodePop({{ sync: true }});
      const ss = slides();
      if (!ss.length) return;
      ss.forEach((s, i) => {{
        s.classList.toggle('active', i === n);
        s.classList.toggle('hidden', i !== n);
      }});
      currentSlide = Math.max(0, Math.min(n, ss.length - 1));
      fragStep = (frag == null) ? 0 : frag;
      applyFragments();
      // tell parent our position (for rebuild recovery)
      try {{
        window.parent.__sslive_slide_index = currentSlide;
      }} catch (e) {{}}
      if (selectFirst) {{
        const first = ss[currentSlide].querySelector('[data-runnable]');
        if (first) selectCell(first.dataset.cellId);
      }}
      placeRsBox();
    }}

    // → advances reveal steps first, then next slide. ← reverses.
    // Navigation must never toggle edit mode (only ✎ / e / Esc do that).
    function goNext() {{
      consumeGotoKeepEdit();
      if (editing) {{ showSlide(currentSlide + 1); return; }}
      const maxR = maxReveal(slides()[currentSlide]);
      if (fragStep < maxR) {{ fragStep++; applyFragments(); return; }}
      if (currentSlide < slides().length - 1) showSlide(currentSlide + 1);
    }}
    function goPrev() {{
      consumeGotoKeepEdit();
      if (editing) {{ showSlide(currentSlide - 1); return; }}
      if (fragStep > 0) {{ fragStep--; applyFragments(); return; }}
      if (currentSlide > 0) {{
        const prev = currentSlide - 1;
        showSlide(prev, {{ selectFirst: false, frag: maxReveal(slides()[prev]) }});
      }}
    }}

    // Floating live code editor (~6 lines default; SE-resize; above plots)
    const CODE_LINE = 14 * 1.45;
    const CODE_HEAD = 44;
    const CODE_DEF_LINES = 6;
    const CODE_MIN_LINES = 5;
    let liveCodeOpen = null;  // {{ cellId, wrap, ta }}

    function codeSource(cellId) {{
      if (liveCodeOpen && liveCodeOpen.cellId === cellId) {{
        const popTa = document.getElementById('live-code-pop-ta');
        if (popTa) return popTa.value;
      }}
      const ta = document.querySelector('textarea.code-ta[data-cell-id="' + cellId + '"]');
      return ta ? ta.value : '';
    }}

    function forceCodeTaCollapsed(ta) {{
      if (!ta) return;
      delete ta.dataset.userSized;
      const h = parseInt(ta.dataset.collapsedH || '34', 10) || 34;
      ta.style.height = h + 'px';
      ta.style.maxHeight = h + 'px';
      ta.style.minHeight = h + 'px';
      ta.scrollTop = 0;
    }}

    function closeLiveCodePop(opts) {{
      const sync = !opts || opts.sync !== false;
      const pop = document.getElementById('live-code-pop');
      const popTa = document.getElementById('live-code-pop-ta');
      if (liveCodeOpen) {{
        if (sync && liveCodeOpen.ta && popTa) liveCodeOpen.ta.value = popTa.value;
        if (liveCodeOpen.wrap) liveCodeOpen.wrap.classList.remove('code-open');
        forceCodeTaCollapsed(liveCodeOpen.ta);
      }}
      liveCodeOpen = null;
      if (pop) pop.hidden = true;
    }}

    function positionLiveCodePop(pop, wrap) {{
      const bar = wrap.getBoundingClientRect();
      const w = Math.min(920, Math.floor(window.innerWidth * 0.9));
      const h = Math.round(CODE_LINE * CODE_DEF_LINES + CODE_HEAD + 16);
      let left = Math.round(bar.left);
      let top = Math.round(bar.bottom + 8);
      if (left + w > window.innerWidth - 12) left = Math.max(12, window.innerWidth - w - 12);
      if (left < 12) left = 12;
      if (top + h > window.innerHeight - 12) {{
        top = Math.max(12, Math.round(bar.top - h - 8));
      }}
      pop.style.width = w + 'px';
      pop.style.height = h + 'px';
      pop.style.left = left + 'px';
      pop.style.top = top + 'px';
    }}

    function openLiveCodePop(fromTa) {{
      if (!fromTa) return;
      const cellId = fromTa.dataset.cellId;
      const wrap = fromTa.closest('.code-wrap');
      if (!cellId || !wrap) return;
      const pop = document.getElementById('live-code-pop');
      const popTa = document.getElementById('live-code-pop-ta');
      const idEl = document.getElementById('live-code-pop-id');
      if (!pop || !popTa) return;
      // Already open for this cell — keep focus in the floating editor
      if (liveCodeOpen && liveCodeOpen.cellId === cellId) {{
        try {{ popTa.focus({{ preventScroll: true }}); }} catch (e) {{ try {{ popTa.focus(); }} catch (e2) {{}} }}
        return;
      }}
      // Sync previous open cell before switching
      closeLiveCodePop({{ sync: true }});
      liveCodeOpen = {{ cellId: cellId, wrap: wrap, ta: fromTa }};
      wrap.classList.add('code-open');
      forceCodeTaCollapsed(fromTa);
      popTa.value = fromTa.value;
      if (idEl) idEl.textContent = cellId;
      pop.dataset.cellId = cellId;
      pop.hidden = false;
      positionLiveCodePop(pop, wrap);
      selectCell(cellId);
      // Defer focus so the in-slide ta blur settles first
      requestAnimationFrame(() => {{
        try {{ popTa.focus({{ preventScroll: true }}); }} catch (e) {{ try {{ popTa.focus(); }} catch (e2) {{}} }}
      }});
    }}

    // Focus on the one-line bar → open floating editor (not in-place grow)
    function codeTaFocus(ta) {{
      openLiveCodePop(ta);
    }}
    function codeTaBlur(ta) {{
      // In-slide bar always stays one line; floating panel owns editing
      forceCodeTaCollapsed(ta);
    }}

    // Outside click closes floating editor (sync source back)
    document.addEventListener('mousedown', (e) => {{
      if (!liveCodeOpen) return;
      const pop = document.getElementById('live-code-pop');
      if (pop && pop.contains(e.target)) return;
      if (liveCodeOpen.wrap && liveCodeOpen.wrap.contains(e.target)) {{
        // Clicking Run / toolbar on same cell keeps panel; re-click bar refocuses
        if (e.target.closest && e.target.closest('button.run-btn')) return;
        return;
      }}
      closeLiveCodePop({{ sync: true }});
    }});

    // SE-corner resize for live floating editor
    (function liveCodePopResize() {{
      let drag = null;
      document.addEventListener('mousedown', (e) => {{
        const h = e.target.closest && e.target.closest('.live-code-pop-rs');
        if (!h) return;
        e.preventDefault();
        e.stopPropagation();
        const pop = document.getElementById('live-code-pop');
        if (!pop || pop.hidden) return;
        const r = pop.getBoundingClientRect();
        drag = {{ pop: pop, x: e.clientX, y: e.clientY, w: r.width, h: r.height }};
      }});
      document.addEventListener('mousemove', (e) => {{
        if (!drag) return;
        const minH = Math.round(CODE_LINE * CODE_MIN_LINES + CODE_HEAD + 16);
        const maxH = Math.floor(window.innerHeight * 0.5);
        const minW = 280;
        const maxW = Math.floor(window.innerWidth * 0.95);
        const nw = Math.max(minW, Math.min(maxW, drag.w + (e.clientX - drag.x)));
        const nh = Math.max(minH, Math.min(maxH, drag.h + (e.clientY - drag.y)));
        drag.pop.style.width = nw + 'px';
        drag.pop.style.height = nh + 'px';
      }});
      document.addEventListener('mouseup', () => {{ drag = null; }});
    }})();

    const runTimers = {{}};  // cellId → timeout id (clear stuck spinners)

    function setRunBtn(cellId, disabled) {{
      document.querySelectorAll('.run-btn[data-cell-id="' + cellId + '"]').forEach((btn) => {{
        btn.disabled = !!disabled;
      }});
      const popRun = document.getElementById('live-code-pop-run');
      if (popRun && liveCodeOpen && liveCodeOpen.cellId === cellId) {{
        popRun.disabled = !!disabled;
      }}
    }}

    function collapseCodeTa(cellId) {{
      // After Run / collapse: close floating editor and keep bar one-line
      if (liveCodeOpen && (!cellId || liveCodeOpen.cellId === cellId)) {{
        closeLiveCodePop({{ sync: true }});
      }}
      const ta = document.querySelector('textarea.code-ta[data-cell-id="' + cellId + '"]');
      forceCodeTaCollapsed(ta);
    }}

    function setRunning(cellId, msg) {{
      // Collapse code while running so ▶ Run never leaves a tall editor open
      collapseCodeTa(cellId);
      const out = document.getElementById('el-output-' + cellId);
      if (out) {{
        out.innerHTML = '<pre style="background:#1f2937;color:#fbbf24;padding:0.5rem;' +
          'font:13px/1.4 ui-monospace,monospace;border-radius:6px">' +
          (msg || 'Running…') + '</pre>';
      }}
      setRunBtn(cellId, true);
      if (runTimers[cellId]) clearTimeout(runTimers[cellId]);
      // Never leave the spinner forever if the host drops the result
      runTimers[cellId] = setTimeout(function () {{
        setRunBtn(cellId, false);
        const o = document.getElementById('el-output-' + cellId);
        if (o && /Running/i.test(o.textContent || '')) {{
          o.innerHTML = '<pre style="background:#7f1d1d;color:#fecaca;padding:0.5rem;' +
            'font:13px/1.4 ui-monospace,monospace;border-radius:6px">' +
            'Timed out waiting for result. Re-run %slive or check the GPU kernel.</pre>';
        }}
        const badge = document.getElementById('status-badge');
        if (badge) {{ badge.textContent = 'gpu · timeout'; badge.className = 'bad'; }}
      }}, 95000);
    }}

    // Decode base64 payload from parent (avoids HTMX parsing raw Plotly HTML).
    function decodeHtmlB64(b64) {{
      if (!b64) return '';
      try {{
        const bin = atob(b64);
        const bytes = new Uint8Array(bin.length);
        for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
        return new TextDecoder('utf-8').decode(bytes);
      }} catch (e) {{
        try {{ return atob(b64); }} catch (e2) {{ return ''; }}
      }}
    }}

    // Re-run <script> tags after HTML inject (innerHTML does not execute them).
    // Nested data-URL iframes self-run; Plotly hosts need script activation + fill.
    function sanitizeAndActivateOutput(root) {{
      if (!root) return;
      root.querySelectorAll('[hx-swap-oob],[data-hx-swap-oob]').forEach(function (n) {{
        n.removeAttribute('hx-swap-oob');
        n.removeAttribute('data-hx-swap-oob');
      }});
      root.querySelectorAll('script').forEach(function (old) {{
        if (old.closest && old.closest('iframe.sslive-viz-frame')) return;
        const s = document.createElement('script');
        for (let i = 0; i < old.attributes.length; i++) {{
          const a = old.attributes[i];
          try {{ s.setAttribute(a.name, a.value); }} catch (e) {{}}
        }}
        s.text = old.textContent || '';
        old.parentNode.replaceChild(s, old);
      }});
      // Force Plotly graphs to fill their host box (fig.show defaults ~700px wide)
      function fillPlotly() {{
        if (!window.Plotly || !root.querySelector) return;
        root.querySelectorAll('.sslive-plotly-host').forEach(function (host) {{
          const w = host.clientWidth | 0, h = host.clientHeight | 0;
          if (w < 40 || h < 40) return;
          host.querySelectorAll('.js-plotly-plot, .plotly-graph-div').forEach(function (gd) {{
            gd.style.width = '100%';
            gd.style.height = '100%';
            try {{
              window.Plotly.relayout(gd, {{ autosize: false, width: w, height: h }});
            }} catch (e) {{
              try {{ window.Plotly.Plots.resize(gd); }} catch (e2) {{}}
            }}
          }});
        }});
        root.querySelectorAll('.js-plotly-plot, .plotly-graph-div').forEach(function (gd) {{
          if (gd.closest && gd.closest('.sslive-plotly-host')) return;
          try {{ window.Plotly.Plots.resize(gd); }} catch (e) {{}}
        }});
      }}
      setTimeout(fillPlotly, 0);
      setTimeout(fillPlotly, 120);
      setTimeout(fillPlotly, 400);
    }}

    function applyRunResult(msg) {{
      // In-place only — never reload document (preserves slide + fullscreen)
      if (!msg || !msg.cell_id) return;
      const cellId = msg.cell_id;
      // Always clear stuck spinner / disabled button first
      if (runTimers[cellId]) {{ clearTimeout(runTimers[cellId]); delete runTimers[cellId]; }}
      setRunBtn(cellId, false);
      if (msg.t && msg.t <= lastResultT) return;
      if (msg.t) lastResultT = msg.t;
      const out = document.getElementById('el-output-' + cellId);
      let html = msg.html || '';
      if (msg.html_b64) {{
        const decoded = decodeHtmlB64(msg.html_b64);
        if (decoded) html = decoded;
      }}
      if (out && html) {{
        const tmp = document.createElement('div');
        tmp.innerHTML = html;
        const neu = tmp.firstElementChild;
        if (neu) {{
          const wasSel = (typeof editSel !== 'undefined' && editSel === out);
          // Keep live layout (drag/resize) — but never keep a tiny width for viz
          const liveStyle = out.getAttribute('style') || '';
          const neuStyle = neu.getAttribute('style') || '';
          // Prefer Python-baked style (includes width:100% for plotly); only copy
          // live left/top/height when the user has positioned the box.
          if (liveStyle && /position\\s*:\\s*absolute/i.test(liveStyle)) {{
            const merged = neuStyle || liveStyle;
            neu.setAttribute('style', merged);
            // If live box was dragged narrow (< 500px), expand to full column
            try {{
              const mw = parseFloat((liveStyle.match(/width\\s*:\\s*([\\d.]+)px/i) || [])[1] || '0');
              if (mw && mw < 500) {{
                neu.style.width = '100%';
                neu.style.maxWidth = '100%';
              }}
            }} catch (e) {{}}
          }}
          out.replaceWith(neu);
          // Ensure plotly host is large even if a prior overlay shrank the box
          try {{
            const host = neu.querySelector('.sslive-plotly-host');
            const slide = neu.closest('[data-slide]');
            if (host && slide) {{
              const colW = Math.max(400, (slide.clientWidth || 1920) - 128);
              if (!neu.style.width || neu.clientWidth < 500) {{
                neu.style.width = '100%';
                neu.style.maxWidth = '100%';
              }}
              const targetH = Math.max(
                640,
                parseInt(host.style.height, 10) || 0,
                neu.clientHeight || 0
              );
              host.style.width = '100%';
              host.style.height = targetH + 'px';
              host.style.minHeight = targetH + 'px';
              // if absolute and no left set, keep flow-like full width
              void colW;
            }}
            const ifr = neu.querySelector('iframe.sslive-viz-frame');
            if (ifr) {{
              const boxH = neu.clientHeight || 0;
              const h = boxH > 120 ? Math.max(240, boxH - 4) : (parseInt(ifr.style.height, 10) || 680);
              ifr.style.width = '100%';
              ifr.style.height = h + 'px';
              ifr.style.minHeight = h + 'px';
            }}
          }} catch (e) {{}}
          sanitizeAndActivateOutput(neu);
          if (wasSel) selectEl(neu);  // keep selection/handle on the fresh node
        }}
      }}
      selectCell(cellId);
      const ta = document.querySelector('textarea.code-ta[data-cell-id="' + cellId + '"]');
      // keep user's edited text if Python also sent source
      if (ta && msg.source != null && msg.source !== '') {{
        // only sync source from Python if textarea was not focused
        if (document.activeElement !== ta) ta.value = msg.source;
      }}
      // After Run: stay collapsed (one-line bar). keep_focus re-opens floating editor.
      if (ta && msg.keep_focus === true) {{
        openLiveCodePop(ta);
      }} else {{
        collapseCodeTa(cellId);
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
      // Code bars ignore layout height (floating editor owns expand size)
      if (el.classList.contains('code-wrap') || el.dataset.type === 'code') {{
        el.style.height = '';
        el.style.overflow = '';
        const ta = el.querySelector('textarea.code-ta');
        forceCodeTaCollapsed(ta);
      }}
      if ('reveal' in msg) {{
        if (msg.reveal == null || msg.reveal <= 0) el.removeAttribute('data-reveal');
        else el.setAttribute('data-reveal', String(msg.reveal));
      }}
      applyFragments();
      placeRsBox();
    }}

    // ── S2-B edit mode: select / drag / nudge → layout patches to parent ──
    let editing = false;
    let editSel = null;
    let drag = null;
    let nudgeTimer = null;

    function setEditing(on) {{
      const was = editing;
      editing = !!on;
      document.body.classList.toggle('editing', editing);
      document.getElementById('edit-btn')?.classList.toggle('on', editing);
      // Keep parent index fresh so a layout-save rebuild can restore this slide
      try {{ window.parent.__sslive_slide_index = currentSlide; }} catch (e) {{}}
      if (!editing) selectEl(null);
      applyFragments();  // show all while editing; restore hide when done
      // Leaving edit mode: ask host to flush debounced layout to the dialog now
      if (was && !editing) {{
        try {{
          window.parent.postMessage({{ type: 'sslive_layout_flush', t: Date.now() }}, '*');
        }} catch (e) {{}}
      }}
    }}

    function selectEl(el) {{
      if (editSel) editSel.classList.remove('el-editsel');
      editSel = el || null;
      if (editSel) editSel.classList.add('el-editsel');
      updateToolbar();
    }}

    // ── S2-C toolbar: font / reveal step / external resize frame ──
    const TB_FONTS = [
      ['', 'Default'],
      ["Georgia, 'Times New Roman', serif", 'Serif'],
      ["'Helvetica Neue', Arial, sans-serif", 'Sans'],
      ['ui-monospace, Menlo, monospace', 'Mono'],
    ];
    const RS_DIRS = ['nw','n','ne','e','se','s','sw','w'];
    const RS_MIN = 40;

    function selElId() {{ return editSel ? (editSel.dataset.elId || editSel.id) : null; }}
    function curFs() {{
      if (!editSel) return 28;
      return Math.round(parseFloat(editSel.style.fontSize)
        || parseFloat(getComputedStyle(editSel).fontSize) || 28);
    }}
    function curReveal() {{
      if (!editSel) return 0;
      return revealOf(editSel);
    }}

    // Resize frame lives on the slide (sibling overlay), so handles are never
    // clipped by overflow:auto / borders inside the element itself.
    function removeHandle() {{
      document.getElementById('rs-box')?.remove();
    }}
    function placeRsBox() {{
      removeHandle();
      if (!editing || !editSel) return;
      const slide = editSel.closest('[data-slide]');
      if (!slide) return;
      // Pin a min box size so tiny/collapsed outputs still get grab targets
      const sr = slide.getBoundingClientRect();
      const er = editSel.getBoundingClientRect();
      const sc = sr.width / 1920 || 1;
      const left = (er.left - sr.left) / sc;
      const top = (er.top - sr.top) / sc;
      const w = Math.max(80, er.width / sc);
      const h = Math.max(48, er.height / sc);
      const box = document.createElement('div');
      box.id = 'rs-box';
      box.style.left = left + 'px';
      box.style.top = top + 'px';
      box.style.width = w + 'px';
      box.style.height = h + 'px';
      RS_DIRS.forEach((dir) => {{
        const hEl = document.createElement('div');
        hEl.className = 'rs-handle rs-' + dir;
        hEl.dataset.dir = dir;
        hEl.title = 'drag to resize';
        box.appendChild(hEl);
      }});
      // Extra center drag strip for large viz outputs (easier than edge-only)
      if (editSel.getAttribute('data-type') === 'output' || editSel.dataset.type === 'output') {{
        const bar = document.createElement('div');
        bar.className = 'rs-move';
        bar.title = 'drag to move';
        bar.style.cssText = 'position:absolute;left:50%;top:8px;transform:translateX(-50%);'
          + 'padding:4px 12px;border-radius:6px;background:#60a5fa;color:#0b1220;'
          + 'font:700 11px/1 system-ui,sans-serif;pointer-events:auto;cursor:move;'
          + 'z-index:42;user-select:none;touch-action:none;';
        bar.textContent = 'move';
        bar.addEventListener('pointerdown', (ev) => {{
          ev.preventDefault();
          ev.stopPropagation();
          beginDrag(editSel, ev);
        }});
        box.appendChild(bar);
      }}
      slide.appendChild(box);
    }}
    function ensureHandle() {{ placeRsBox(); }}

    // Float toolbar next to the selected element (Google Slides–style), not
    // stuck at the top of the viewport — easier when editing many pieces.
    function placeToolbar() {{
      const tb = document.getElementById('edit-toolbar');
      if (!tb || !editing || !editSel) return;
      if (!tb.classList.contains('show')) return;
      const er = editSel.getBoundingClientRect();
      const gap = 8;
      const pad = 8;
      // measure after show so width/height are real
      const tw = tb.offsetWidth || 280;
      const th = tb.offsetHeight || 40;
      const vw = window.innerWidth;
      const vh = window.innerHeight;
      // Prefer just above the selection; fall below if no room.
      let top = er.top - th - gap;
      if (top < pad) top = er.bottom + gap;
      if (top + th > vh - pad) top = Math.max(pad, vh - th - pad);
      // Align left edges; clamp into the viewport.
      let left = er.left;
      if (left + tw > vw - pad) left = vw - tw - pad;
      if (left < pad) left = pad;
      tb.style.top = Math.round(top) + 'px';
      tb.style.left = Math.round(left) + 'px';
    }}

    function updateToolbar() {{
      const tb = document.getElementById('edit-toolbar');
      if (!tb) return;
      if (!editing || !editSel) {{ tb.classList.remove('show'); removeHandle(); return; }}
      tb.classList.add('show');
      document.getElementById('tb-el').textContent = selElId();
      document.getElementById('tb-fs-val').textContent = curFs();
      const sel = document.getElementById('tb-font');
      if (sel) sel.value = editSel.style.fontFamily || '';
      const rIn = document.getElementById('tb-reveal');
      if (rIn && document.activeElement !== rIn) {{
        const r = curReveal();
        rIn.value = r > 0 ? String(r) : '';
      }}
      ensureHandle();
      // Position after layout so size is known (double-rAF for flex wrap)
      requestAnimationFrame(() => {{
        placeToolbar();
        requestAnimationFrame(placeToolbar);
      }});
    }}

    function tbPatch(patch) {{
      // apply locally (live, no rebuild) + persist via bridge
      if (!editSel) return;
      const isCode = editSel.classList.contains('code-wrap')
        || editSel.dataset.type === 'code';
      if ('fs' in patch) {{
        if (patch.fs == null) {{
          editSel.style.fontSize = '';
          editSel.style.removeProperty('--code-fs');
        }} else {{
          editSel.style.fontSize = patch.fs + 'px';
          editSel.style.setProperty('--code-fs', patch.fs + 'px');
        }}
      }}
      if ('ff' in patch) editSel.style.fontFamily = patch.ff || '';
      if ('reveal' in patch) {{
        if (patch.reveal == null || patch.reveal <= 0) editSel.removeAttribute('data-reveal');
        else editSel.setAttribute('data-reveal', String(patch.reveal));
      }}
      if ('w' in patch) editSel.style.width = patch.w == null ? '' : patch.w + 'px';
      // Code bars stay one-line; height is owned by the floating editor
      if ('h' in patch && !isCode) {{
        editSel.style.height = patch.h == null ? '' : patch.h + 'px';
      }}
      if ('x' in patch) editSel.style.left = patch.x == null ? '' : patch.x + 'px';
      if ('y' in patch) editSel.style.top = patch.y == null ? '' : patch.y + 'px';
      sendLayoutPatch(selElId(), patch);
      updateToolbar();
    }}

    function sendLayoutPatch(elId, patch) {{
      // Stamp slide index so host can restore after dialog write rebuilds iframe
      try {{ window.parent.__sslive_slide_index = currentSlide; }} catch (e) {{}}
      try {{
        window.parent.postMessage(
          {{ type: 'sslive_layout', el_id: elId, patch: patch,
             slide_index: currentSlide, t: Date.now() }}, '*');
      }} catch (e) {{}}
    }}

    function slideLayoutEls(slide) {{
      if (!slide) return [];
      return Array.from(slide.querySelectorAll(
        '.note-block, .code-wrap, [data-type="output"]'
      ));
    }}

    function pinFlowElements(slide) {{
      // Freeze every still-in-flow element at its current visual box BEFORE any
      // one of them leaves the flex stack. Otherwise dragging the plot takes it
      // out of flow and the code bar reflows into that hole (looks like it
      // "moved" or slipped behind the viz).
      if (!slide) return;
      const sr = slide.getBoundingClientRect();
      const sc = sr.width / 1920 || 1;
      const snaps = slideLayoutEls(slide).map((el) => {{
        const er = el.getBoundingClientRect();
        const cs = window.getComputedStyle(el);
        return {{
          el: el,
          elId: el.dataset.elId || el.id,
          x: Math.round((er.left - sr.left) / sc),
          y: Math.round((er.top - sr.top) / sc),
          w: Math.max(1, Math.round(er.width / sc)),
          h: Math.max(1, Math.round(er.height / sc)),
          needsPin: cs.position !== 'absolute',
        }};
      }});
      snaps.forEach((s) => {{
        if (!s.needsPin || !s.elId) return;
        const el = s.el;
        const isCode = el.classList.contains('code-wrap')
          || el.dataset.type === 'code';
        el.style.position = 'absolute';
        el.style.margin = '0';
        el.style.left = s.x + 'px';
        el.style.top = s.y + 'px';
        el.style.width = s.w + 'px';
        const patch = {{ x: s.x, y: s.y, w: s.w }};
        // Code bars stay content-tall (one-line); pin h for notes/outputs only
        if (!isCode) {{
          el.style.height = s.h + 'px';
          el.style.overflow = 'auto';
          patch.h = s.h;
        }}
        sendLayoutPatch(s.elId, patch);
      }});
    }}

    function bringToFront(el) {{
      // Dragged/resized element stacks above siblings (avoids "behind the plot")
      if (!el) return;
      const slide = el.closest('[data-slide]');
      if (!slide) return;
      let maxZ = 0;
      slideLayoutEls(slide).forEach((e) => {{
        const z = parseInt(e.style.zIndex || '0', 10);
        if (Number.isFinite(z) && z > maxZ) maxZ = z;
      }});
      const nz = maxZ + 1;
      el.style.zIndex = String(nz);
      sendLayoutPatch(el.dataset.elId || el.id, {{ z: nz }});
    }}

    function ensureAbs(el) {{
      // Pin all in-flow siblings first (same visual places), then ensure this
      // element is absolute. Never leave mixed flow+absolute on a drag.
      const slide = el.closest('[data-slide]');
      if (slide) pinFlowElements(slide);
      const sr = slide.getBoundingClientRect();
      const er = el.getBoundingClientRect();
      const sc = sr.width / 1920 || 1;
      const converted = el.style.position !== 'absolute';
      const x = Math.round((er.left - sr.left) / sc);
      const y = Math.round((er.top - sr.top) / sc);
      const w = Math.round(er.width / sc);
      const h = Math.round(er.height / sc);
      if (converted) {{
        el.style.width = w + 'px';
        el.style.position = 'absolute';
        el.style.margin = '0';
        el.style.left = x + 'px';
        el.style.top = y + 'px';
      }} else {{
        // Already absolute (possibly just pinned); keep numeric left/top
        if (!el.style.left) el.style.left = x + 'px';
        if (!el.style.top) el.style.top = y + 'px';
      }}
      const fx = Math.round(parseFloat(el.style.left));
      const fy = Math.round(parseFloat(el.style.top));
      return {{
        x: Number.isFinite(fx) ? fx : x,
        y: Number.isFinite(fy) ? fy : y,
        w: w, h: h, converted: converted
      }};
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
      placeRsBox();
      const elId = editSel.dataset.elId || editSel.id;
      const patch = {{ x: nx, y: ny }};
      if (cur.converted) {{ patch.w = cur.w; updateToolbar(); }}
      else placeToolbar();
      clearTimeout(nudgeTimer);
      nudgeTimer = setTimeout(() => sendLayoutPatch(elId, patch), 350);
    }}

    let rsDrag = null;

    function applyResizeFrame(state, clientX, clientY) {{
      // Google Slides–style: drag a corner/edge → box grows/shrinks from that side.
      const dx = (clientX - state.sx) / state.sc;
      const dy = (clientY - state.sy) / state.sc;
      let x = state.ox, y = state.oy, w = state.ow, h = state.oh;
      const dir = state.dir || '';
      const touchW = dir.indexOf('e') >= 0 || dir.indexOf('w') >= 0;
      const touchH = dir.indexOf('n') >= 0 || dir.indexOf('s') >= 0;
      if (dir.indexOf('e') >= 0) w = Math.max(RS_MIN, state.ow + dx);
      if (dir.indexOf('s') >= 0) h = Math.max(RS_MIN, state.oh + dy);
      if (dir.indexOf('w') >= 0) {{
        w = Math.max(RS_MIN, state.ow - dx);
        x = state.ox + (state.ow - w);
      }}
      if (dir.indexOf('n') >= 0) {{
        h = Math.max(RS_MIN, state.oh - dy);
        y = state.oy + (state.oh - h);
      }}
      // Corner drag on notes (or Alt+corner anywhere): scale font with the box
      if (state.scaleFs && state.ofs && dir.length === 2) {{
        const sx = w / Math.max(1, state.ow), sy = h / Math.max(1, state.oh);
        const scale = Math.max(0.25, Math.min(sx, sy));
        const fs = Math.max(8, Math.round(state.ofs * scale));
        state.el.style.fontSize = fs + 'px';
        state.el.style.setProperty('--code-fs', fs + 'px');
        state.fs = fs;
      }}
      x = Math.round(x); y = Math.round(y);
      w = Math.round(w); h = Math.round(h);
      state.el.style.left = x + 'px';
      state.el.style.top = y + 'px';
      if (touchW) state.el.style.width = w + 'px';
      // Code bars stay one-line tall — height is owned by the floating editor
      const isCode = state.el.classList.contains('code-wrap')
        || state.el.dataset.type === 'code';
      if (touchH && !isCode) {{
        state.el.style.height = h + 'px';
        state.el.style.overflow = 'auto';
      }}
      state.cx = x; state.cy = y; state.cw = w; state.ch = h;
      state.touchW = touchW; state.touchH = touchH && !isCode;
      // Keep external frame + floating toolbar in sync while resizing
      const box = document.getElementById('rs-box');
      if (box) {{
        box.style.left = x + 'px';
        box.style.top = y + 'px';
        if (touchW) box.style.width = w + 'px';
        if (touchH) box.style.height = h + 'px';
      }}
      placeToolbar();
    }}

    document.addEventListener('pointerdown', (e) => {{
      if (!editing || e.button !== 0) return;
      if (e.target.closest('#edit-toolbar')) return;  // toolbar clicks never drag/deselect
      const rh = e.target.closest('#rs-box .rs-handle, .rs-handle');
      if (rh) {{
        const el = editSel;  // handles live on #rs-box, not inside the element
        if (!el) return;
        const start = ensureAbs(el);
        bringToFront(el);
        const slide = el.closest('[data-slide]');
        const sc = slide ? (slide.getBoundingClientRect().width / 1920 || 1) : 1;
        // Prefer explicit layout size; fall back to measured box.
        const explicitH = parseFloat(el.style.height);
        const oh = Number.isFinite(explicitH) && explicitH > 0 ? explicitH : start.h;
        const explicitW = parseFloat(el.style.width);
        const ow = Number.isFinite(explicitW) && explicitW > 0 ? explicitW : start.w;
        const isNote = el.classList.contains('note-block');
        rsDrag = {{
          el: el, elId: el.dataset.elId || el.id, dir: rh.dataset.dir || 'se',
          sx: e.clientX, sy: e.clientY, sc: sc,
          ox: start.x, oy: start.y, ow: ow, oh: oh,
          converted: start.converted,
          // Corner-drag notes scale font with the box (Google-ish text grow);
          // hold Alt on any element to force font-scale on corner drag.
          scaleFs: isNote || e.altKey,
          ofs: curFs(), fs: null,
          cx: start.x, cy: start.y, cw: ow, ch: oh
        }};
        e.preventDefault();
        e.stopPropagation();
        try {{ rh.setPointerCapture(e.pointerId); }} catch (err) {{}}
        return;
      }}
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
      // Outputs / viz (pointcloud iframe, images) — whole mount is one layout el
      const out = e.target.closest('[data-type="output"]');
      if (out) {{ selectEl(out); beginDrag(out, e); return; }}
      const el = e.target.closest('.note-block');
      if (el) {{ selectEl(el); beginDrag(el, e); return; }}
      const cw = e.target.closest('.code-wrap');
      selectEl(cw || null);  // code body: select only; move via toolbar/arrows
    }}, true);

    document.addEventListener('pointermove', (e) => {{
      if (rsDrag) {{
        applyResizeFrame(rsDrag, e.clientX, e.clientY);
        return;
      }}
      if (!drag) return;
      if (!drag.started) {{
        if (Math.abs(e.clientX - drag.sx) + Math.abs(e.clientY - drag.sy) <= 2) return;
        // Pin siblings at current spots, then lift this element above them
        const start = ensureAbs(drag.el);
        bringToFront(drag.el);
        drag.ox = start.x; drag.oy = start.y;
        drag.w = start.w; drag.includeW = start.converted;
        drag.started = true;
      }}
      drag.el.style.left = (drag.ox + (e.clientX - drag.sx) / drag.sc) + 'px';
      drag.el.style.top = (drag.oy + (e.clientY - drag.sy) / drag.sc) + 'px';
      placeRsBox();
      placeToolbar();
    }});

    document.addEventListener('pointerup', (e) => {{
      if (rsDrag) {{
        applyResizeFrame(rsDrag, e.clientX, e.clientY);
        const patch = {{ x: rsDrag.cx, y: rsDrag.cy }};
        if (rsDrag.touchW) patch.w = rsDrag.cw;
        if (rsDrag.touchH) patch.h = rsDrag.ch;
        if (rsDrag.fs != null) patch.fs = rsDrag.fs;
        sendLayoutPatch(rsDrag.elId, patch);
        rsDrag = null;
        updateToolbar();
        return;
      }}
      if (!drag) return;
      if (drag.started) {{
        const patch = {{
          x: Math.round(parseFloat(drag.el.style.left) || 0),
          y: Math.round(parseFloat(drag.el.style.top) || 0)
        }};
        if (drag.includeW) patch.w = drag.w;
        sendLayoutPatch(drag.elId, patch);
        updateToolbar();
      }}
      drag = null;
    }});

    document.addEventListener('dragstart', (e) => {{ if (editing) e.preventDefault(); }});

    let lastGotoT = 0;
    function consumeGotoKeepEdit() {{
      // One-shot: never leave keep_edit sticky on the parent (it re-armed
      // edit mode every time the iframe reloaded or polled an old goto).
      try {{
        const g = window.parent && window.parent.__sslive_goto;
        if (g && g.type === 'sslive_goto') {{
          window.parent.__sslive_goto = Object.assign({{}}, g, {{ keep_edit: false }});
        }}
      }} catch (e) {{}}
    }}
    function gotoSlideFromHost(msg) {{
      if (!msg || msg.slide_index == null) return;
      const n = Math.max(0, (+msg.slide_index) | 0);
      showSlide(n, {{ selectFirst: false, frag: fragStep }});
      // Do NOT auto-enter edit mode. keep_edit was re-enabling ✎ whenever
      // arrows rebuilt/restored the slide after a layout save.
      consumeGotoKeepEdit();
    }}

    window.addEventListener('message', function (e) {{
      if (!e.data) return;
      if (e.data.type === 'sslive_result') applyRunResult(e.data);
      else if (e.data.type === 'sslive_layout_apply') applyLayoutMsg(e.data);
      else if (e.data.type === 'sslive_goto') {{
        if (e.data.t) lastGotoT = e.data.t;
        gotoSlideFromHost(e.data);
      }}
    }});

    // More reliable than postMessage into srcdoc: poll parent for last result / goto
    setInterval(function () {{
      try {{
        const r = window.parent.__sslive_last_result;
        if (r && r.type === 'sslive_result') applyRunResult(r);
        const l = window.parent.__sslive_last_layout;
        if (l && l.type === 'sslive_layout_apply') applyLayoutMsg(l);
        const g = window.parent.__sslive_goto;
        // One-shot restore after layout-save rebuild only (g.force)
        if (g && g.type === 'sslive_goto' && g.force && g.t && g.t !== lastGotoT) {{
          lastGotoT = g.t;
          gotoSlideFromHost(g);
          try {{ window.parent.__sslive_goto = null; }} catch (e2) {{}}
        }}
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
      const tag = e.target && e.target.tagName;
      if (tag === 'TEXTAREA' || tag === 'INPUT' || tag === 'SELECT') {{
        if (tag === 'TEXTAREA' && (e.key === 'ArrowRight' || e.key === 'ArrowLeft')) return;
        return;
      }}
      // Exact letter e only (not arrows / not key repeat)
      if (e.key === 'e' && !e.repeat && !e.metaKey && !e.ctrlKey && !e.altKey) {{
        e.preventDefault();
        setEditing(!editing);
        return;
      }}
      if (e.key === 'Escape' && editing) {{
        setEditing(false);  // exits directly (and deselects); ✎/e toggle back
        return;
      }}
      // While editing + selection: arrows nudge. Otherwise arrows navigate slides.
      if (editing && editSel && e.key.startsWith('Arrow')) {{
        e.preventDefault();
        const step = e.shiftKey ? 10 : 1;
        if (e.key === 'ArrowLeft')  nudgeSel(-step, 0);
        if (e.key === 'ArrowRight') nudgeSel(step, 0);
        if (e.key === 'ArrowUp')    nudgeSel(0, -step);
        if (e.key === 'ArrowDown')  nudgeSel(0, step);
        placeRsBox();
        return;
      }}
      if (e.key === 'ArrowRight') {{ e.preventDefault(); goNext(); return; }}
      if (e.key === 'ArrowLeft')  {{ e.preventDefault(); goPrev(); return; }}
      if (e.key === 'Enter' && e.shiftKey) {{ e.preventDefault(); runSelected(); }}
      if (e.key === 'f' && !e.metaKey && !e.ctrlKey && !e.altKey) {{
        document.documentElement.requestFullscreen?.();
      }}
      if (e.key === 'ArrowDown') {{
        document.querySelector('[data-slide].active')?.scrollBy({{ top: 100, behavior: 'smooth' }});
      }}
      if (e.key === 'ArrowUp') {{
        document.querySelector('[data-slide].active')?.scrollBy({{ top: -100, behavior: 'smooth' }});
      }}
    }});

    document.getElementById('prev-btn')?.addEventListener('click', (e) => {{
      e.preventDefault(); e.stopPropagation(); goPrev();
    }});
    document.getElementById('next-btn')?.addEventListener('click', (e) => {{
      e.preventDefault(); e.stopPropagation(); goNext();
    }});
    document.getElementById('edit-btn')?.addEventListener('click', (e) => {{
      e.preventDefault(); e.stopPropagation(); setEditing(!editing);
    }});
    // Keep floating toolbar glued to the selection on viewport changes
    window.addEventListener('resize', () => {{ placeRsBox(); placeToolbar(); }});
    document.addEventListener('scroll', () => {{ placeRsBox(); placeToolbar(); }}, true);

    (function initToolbar() {{
      const sel = document.getElementById('tb-font');
      if (!sel) return;
      TB_FONTS.forEach(([v, label]) => {{
        const o = document.createElement('option');
        o.value = v; o.textContent = label;
        sel.appendChild(o);
      }});
      sel.addEventListener('change', () => tbPatch({{ ff: sel.value || null }}));
      document.getElementById('tb-fs-minus').addEventListener('click',
        () => tbPatch({{ fs: Math.max(8, curFs() - 2) }}));
      document.getElementById('tb-fs-plus').addEventListener('click',
        () => tbPatch({{ fs: curFs() + 2 }}));
      // Reveal step: empty/0 = always visible; 1,2,3… appear on successive →
      const rIn = document.getElementById('tb-reveal');
      if (rIn) {{
        const applyReveal = () => {{
          if (!editSel) return;
          const raw = (rIn.value || '').trim();
          if (raw === '' || raw === '0') {{
            tbPatch({{ reveal: null }});
            return;
          }}
          const n = parseInt(raw, 10);
          if (!Number.isFinite(n) || n < 0) return;
          tbPatch({{ reveal: n > 0 ? n : null }});
        }};
        rIn.addEventListener('change', applyReveal);
        rIn.addEventListener('keydown', (ev) => {{
          if (ev.key === 'Enter') {{ ev.preventDefault(); applyReveal(); rIn.blur(); }}
          ev.stopPropagation();
        }});
        rIn.addEventListener('pointerdown', (ev) => ev.stopPropagation());
      }}
      // Full clear → original document-flow size + position (no absolute box).
      const CLEAR_LAYOUT = {{
        x: null, y: null, w: null, h: null, z: null,
        order: null, reveal: null, fs: null, ff: null, align: null
      }};
      function restoreFlowLayout(el) {{
        if (!el) return;
        el.style.cssText = '';
        el.removeAttribute('data-reveal');
        // Explicitly drop absolute geometry so the element re-enters the slide
        // flex stack in DOM order (title → code → output).
        ['position','left','top','width','height','margin','overflow',
         'zIndex','order','fontSize','fontFamily','textAlign'].forEach((p) => {{
          try {{ el.style[p] = ''; }} catch (e) {{}}
        }});
        try {{ el.style.removeProperty('--code-fs'); }} catch (e) {{}}
        if (el.classList.contains('code-wrap') || el.dataset.type === 'code') {{
          closeLiveCodePop({{ sync: true }});
          forceCodeTaCollapsed(el.querySelector('textarea.code-ta'));
        }}
      }}
      function layoutPairOf(el) {{
        // Code + its output share a cell id; reset them together so one does
        // not jump into the middle while the other is still absolutely placed.
        const id = (el && (el.dataset.elId || el.id)) || '';
        if (id.indexOf('el-code-') === 0)
          return document.getElementById('el-output-' + id.slice(8));
        if (id.indexOf('el-output-') === 0)
          return document.getElementById('el-code-' + id.slice(10));
        return null;
      }}
      document.getElementById('tb-reset').addEventListener('click', () => {{
        if (!editSel) return;
        const el = editSel;
        const elId = el.dataset.elId || el.id;
        const pair = layoutPairOf(el);
        restoreFlowLayout(el);
        sendLayoutPatch(elId, Object.assign({{}}, CLEAR_LAYOUT));
        if (pair) {{
          restoreFlowLayout(pair);
          sendLayoutPatch(pair.dataset.elId || pair.id, Object.assign({{}}, CLEAR_LAYOUT));
        }}
        // Keep selection on the element the user reset; refresh handles
        selectEl(el);
        updateToolbar();
      }});
    }})();

    // Floating editor chrome (Run / collapse / Shift+Enter)
    (function liveCodePopChrome() {{
      const popRun = document.getElementById('live-code-pop-run');
      const popClose = document.getElementById('live-code-pop-close');
      const popTa = document.getElementById('live-code-pop-ta');
      if (popRun) popRun.addEventListener('click', (e) => {{
        e.preventDefault();
        e.stopPropagation();
        if (!liveCodeOpen) return;
        // sync before run
        if (liveCodeOpen.ta && popTa) liveCodeOpen.ta.value = popTa.value;
        runCellFromSlide(liveCodeOpen.cellId);
      }});
      if (popClose) popClose.addEventListener('click', (e) => {{
        e.preventDefault();
        e.stopPropagation();
        closeLiveCodePop({{ sync: true }});
      }});
      if (popTa) {{
        popTa.addEventListener('input', () => {{
          if (liveCodeOpen && liveCodeOpen.ta) liveCodeOpen.ta.value = popTa.value;
        }});
        popTa.addEventListener('keydown', (e) => {{
          if (e.key === 'Escape') {{
            e.preventDefault();
            e.stopPropagation();
            closeLiveCodePop({{ sync: true }});
            return;
          }}
          if (e.key === 'Enter' && e.shiftKey && liveCodeOpen) {{
            e.preventDefault();
            e.stopPropagation();
            if (liveCodeOpen.ta) liveCodeOpen.ta.value = popTa.value;
            runCellFromSlide(liveCodeOpen.cellId);
          }}
        }});
      }}
    }})();

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
        if (liveCodeOpen) {{
          const pop = document.getElementById('live-code-pop');
          if (pop && !pop.hidden) positionLiveCodePop(pop, liveCodeOpen.wrap);
        }}
      }}
      new ResizeObserver(rescale).observe(viewport);
      rescale();
    }})();

    // Fresh open: stay on initial_slide. Mid-edit rebuild: apply one-shot goto.
    showSlide(currentSlide, {{ selectFirst: false }});
    try {{
      var pending = window.parent.__sslive_last_result;
      if (pending && pending.type === 'sslive_result') applyRunResult(pending);
      var g0 = window.parent && window.parent.__sslive_goto;
      // Only consume goto when force-restore was requested (layout save rebuild)
      if (g0 && g0.type === 'sslive_goto' && g0.force) {{
        lastGotoT = g0.t || 0;
        gotoSlideFromHost(g0);
        try {{ window.parent.__sslive_goto = null; }} catch (e2) {{}}
      }}
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
  <!-- No HTMX in the slide iframe — SolveIt parent already uses it; loading it
       here caused oobSwap/insertBefore errors on Plotly HTML injects. -->
  <script src="https://cdn.plot.ly/plotly-2.35.2.min.js" charset="utf-8"></script>
  <style>{css}</style>
</head>
<body>
  <div id="chrome">
    <strong>sslive</strong>
    <span id="status-badge" class="ok">{html_module.escape(backend_label)}</span>
    <span id="edit-badge">✎ edit</span>
    <span style="opacity:0.7">Shift+Enter run · ←/→ reveal then slides · f fullscreen · e edit</span>
  </div>
  <div id="viewport">
    <div id="stage">
      <div id="slides-container">
        {slides_html or empty}
      </div>
    </div>
  </div>
  <div id="edit-toolbar">
    <span class="tb-label" id="tb-el">—</span>
    <span class="tb-sep"></span>
    <button type="button" id="tb-fs-minus" title="smaller text">A−</button>
    <span id="tb-fs-val">–</span>
    <button type="button" id="tb-fs-plus" title="bigger text">A+</button>
    <select id="tb-font" title="font family"></select>
    <span class="tb-sep"></span>
    <span class="tb-field" title="Reveal step: blank = always visible. 1 appears on first →, 2 on second →, …">reveal
      <input type="number" id="tb-reveal" min="0" step="1" placeholder="—" />
    </span>
    <span class="tb-sep"></span>
    <button type="button" id="tb-reset" title="Restore original size &amp; position (code+output together)">reset</button>
  </div>
  <div id="live-code-pop" hidden>
    <div class="live-code-pop-head">
      <button type="button" class="run-btn" id="live-code-pop-run">▶ Run</button>
      <span class="cell-id" id="live-code-pop-id">—</span>
      <span class="hint">Shift+Enter run · Esc collapse · ↘ resize</span>
      <button type="button" class="live-code-pop-close" id="live-code-pop-close">collapse</button>
    </div>
    <textarea id="live-code-pop-ta" spellcheck="false"></textarea>
    <div class="live-code-pop-rs" title="Drag to resize"></div>
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


# ═══════════════════════════════════════════════════════════════════════════
# Portable HTML export (static player — no SolveIt / CRAFT / live Run)
# ═══════════════════════════════════════════════════════════════════════════


def _code_block_html_export(cell: Cell, *, style: str = "", extra_attrs: str = "") -> str:
    """Read-only code block for portable export — collapsed one-line like live UI.

    Click opens a standard floating panel (~6 lines, syntax-highlighted, SE-resize).
    Layout height is ignored for the bar (always one line); layout x/y/w place the bar only.
    """
    cid = html_module.escape(cell.id)
    src = cell.source or ""
    full_esc = html_module.escape(src)
    n_lines = max(1, src.count("\n") + (1 if src else 0))
    more = f" · {n_lines} lines" if n_lines > 1 else ""
    style_bar = _strip_code_bar_height(style)
    first_line = (src.split("\n", 1)[0] if src else "") or " "
    first_esc = html_module.escape(first_line)
    return (
        f'<div id="el-code-{cid}" class="code-wrap code-frozen" '
        f'data-el-id="el-code-{cid}" data-type="code" data-cell-id="{cid}"'
        f"{extra_attrs}{_style_attr(style_bar)}>"
        f'<div class="code-toolbar" onclick="ssToggleCode(this.closest(\'.code-wrap\'))">'
        f'<span class="cell-id">{cid}</span>'
        f'<span class="hint">exported · click to expand{more}</span>'
        f"</div>"
        f'<pre class="code-pre code-pre-collapsed" title="Click to expand" '
        f'onclick="ssToggleCode(this.closest(\'.code-wrap\'))">{first_esc}</pre>'
        f'<div class="code-pop" id="code-pop-{cid}" hidden data-cell-id="{cid}">'
        f'<div class="code-pop-head">'
        f'<span class="cell-id">{cid}</span>'
        f'<span class="hint">Esc · outside click · ↘ resize</span>'
        f'<button type="button" class="code-pop-close" '
        f'onclick="event.stopPropagation();ssCollapseCode()">collapse</button>'
        f"</div>"
        f'<pre class="code-pop-body"><code class="language-python">{full_esc}</code></pre>'
        f'<div class="code-pop-rs" title="Drag to resize"></div>'
        f"</div>"
        f"</div>"
    )


def _slide_html_export(deck: Deck, slide: Slide, *, active: bool = False) -> str:
    """Slide HTML for static export (layout + frozen code/outputs)."""
    theme = deck.theme or THEME_DARK
    parts: list[str] = []
    for cid in slide.cell_ids:
        cell = deck.cells[cid]
        if cell.kind == "note":
            note_ids = cell.element_ids or [f"el-0-{cid}"]
            for el_id in note_ids:
                el = deck.elements.get(el_id)
                if el is None:
                    continue
                eid = html_module.escape(el_id)
                spec = _layout_spec(deck, el_id)
                style = _el_style(spec)
                kind = html_module.escape(el.kind or "paragraph")
                body = el.html if el.html else _note_to_html(el.content or cell.source)
                parts.append(
                    f'<div id="{eid}" class="note-block" data-el-id="{eid}" '
                    f'data-type="{kind}" data-cell-id="{html_module.escape(cid)}"'
                    f"{_reveal_attr(spec)}{_style_attr(style)}>{body}</div>"
                )
        else:
            cspec = _layout_spec(deck, f"el-code-{cid}")
            ospec = _layout_spec(deck, f"el-output-{cid}")
            # Code box: do not force a large height from missing layout —
            # strip only overflow:auto if height missing so bar stays thin
            code_style = _el_style(cspec)
            parts.append(
                _code_block_html_export(
                    cell,
                    style=code_style,
                    extra_attrs=_reveal_attr(cspec),
                )
            )
            parts.append(
                render_output_html(
                    cell.outputs,
                    cell.id,
                    theme,
                    style=_el_style(ospec),
                    extra_attrs=_reveal_attr(ospec),
                    portable=True,
                )
            )
    cls = "slide title-slide" if slide.is_title else "slide"
    hidden = " active" if active else " hidden"
    return (
        f'<section class="{cls}{hidden}" data-slide="{slide.index}">'
        f'{"".join(parts)}</section>'
    )


def _export_title_from_deck(deck: Deck) -> str:
    for slide in deck.slides:
        for cid in slide.cell_ids:
            cell = deck.cells.get(cid)
            if not cell or cell.kind != "note":
                continue
            for el_id in cell.element_ids or []:
                el = deck.elements.get(el_id)
                if el and el.kind == "heading" and (el.content or "").strip():
                    return (el.content or "").strip()[:80]
            src = (cell.source or "").strip()
            if src.startswith("# "):
                return src[2:].splitlines()[0].strip()[:80]
    return "sslive deck"


def generate_export_html(
    deck: Deck,
    *,
    title: str | None = None,
    offline: bool = False,
    initial_slide: int = 0,
) -> str:
    """Self-contained static HTML player (no SolveIt, no GPU Run).

    Includes navigation, reveal steps, layout, frozen code, and last-run outputs.
    ``offline=True`` currently still uses the Plotly CDN (full offline bundle is
    a follow-up); reserved for future inlining.
    """
    del offline  # reserved
    theme = deck.theme or THEME_DARK
    n_slides = len(deck.slides)
    initial_slide = max(0, min(int(initial_slide), max(0, n_slides - 1)))
    slides_html = "\n".join(
        _slide_html_export(deck, s, active=(s.index == initial_slide))
        for s in deck.slides
    )
    n = n_slides
    doc_title = html_module.escape(title or _export_title_from_deck(deck))

    empty = ""
    if n == 0:
        empty = (
            "<section class='slide active' data-slide='0'>"
            "<h2 class='slide-h2'>No slides</h2>"
            "<p class='slide-p'>Export ran with an empty deck.</p></section>"
        )

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
    .note-block {{ font-size:1.75rem; }}
    .slide-h1, .note-block h1 {{ font-size:2.5714em; font-weight:700; margin:0 0 1rem; }}
    .slide-h2, .note-block h2 {{ font-size:1.7143em; font-weight:700; margin:0 0 1rem; }}
    .note-block h3 {{ font-size:1.3em; font-weight:700; margin:0 0 0.75rem; }}
    .slide-p, .note-block p {{ font-size:1em; line-height:1.5; margin:0.5rem 0; color:{theme.get("fg", "#eee")}; }}
    .note-block ul, .note-block ol {{ font-size:1em; line-height:1.5; margin:0.5rem 0; padding-left:1.4em; }}
    .note-block li {{ margin:0.2em 0; }}
    .note-block[data-type="list_item"] {{ margin:0.15rem 0; }}
    .note-block[data-type="math"] {{ margin:0.6rem 0; }}
    .note-block .math-block {{ text-align:center; margin:0.4em 0; overflow-x:auto; }}
    .note-block .math-inline {{ display:inline; }}
    .note-block img, .note-block[data-type="image"] img {{
      max-width:100%; height:auto; display:block; border-radius:6px; }}
    .note-block table {{ border-collapse:collapse; width:100%; font-size:0.9em; }}
    .note-block th, .note-block td {{ border:1px solid #374151; padding:0.35em 0.6em; text-align:left; }}
    .sslive-html {{ width:100%; max-width:100%; }}
    .sslive-plotly-host {{ width:100% !important; max-width:100%; box-sizing:border-box;
      position:relative; overflow:hidden; border-radius:8px; background:#0b1220; }}
    .code-wrap {{ border:1px solid #374151; border-radius:8px; background:{theme.get("code_bg", "#1f2937")};
      padding:8px 12px; outline:none; flex:0 0 auto; max-width:100%; align-self:stretch;
      cursor:pointer; box-sizing:border-box; }}
    .code-wrap.code-open {{ border-color:#60a5fa; box-shadow:0 0 0 1px rgba(96,165,250,0.35); }}
    .code-toolbar {{ display:flex; align-items:center; gap:12px; margin-bottom:6px; flex-wrap:wrap; }}
    .cell-id {{ font-size:11px; color:{theme.get("muted", "#9ca3af")}; font-family:ui-monospace,monospace; }}
    .hint {{ font-size:11px; color:#6b7280; }}
    .code-pre {{ width:100%; box-sizing:border-box; margin:0; padding:6px 10px;
      font-family:ui-monospace,SFMono-Regular,Menlo,monospace; line-height:1.45; font-size:14px;
      white-space:pre; color:#e5e7eb; background:#111827;
      border:1px solid #4b5563; border-radius:6px; }}
    .code-pre-collapsed {{ height:34px; min-height:34px; max-height:34px;
      overflow:hidden; white-space:nowrap; text-overflow:ellipsis; cursor:pointer; }}
    /* Floating standard expand panel (viewport-fixed; above plots) */
    .code-pop {{
      position:fixed; z-index:40; display:flex; flex-direction:column;
      width:min(920px, 90vw); height:200px; min-width:280px; min-height:148px;
      max-width:95vw; max-height:50vh;
      background:#0f172a; border:1px solid #60a5fa; border-radius:10px;
      box-shadow:0 12px 40px rgba(0,0,0,0.55), 0 0 0 1px rgba(96,165,250,0.25);
      overflow:hidden; box-sizing:border-box; cursor:default;
    }}
    .code-pop[hidden] {{ display:none !important; }}
    .code-pop-head {{
      display:flex; align-items:center; gap:10px; flex:0 0 auto;
      padding:8px 12px; border-bottom:1px solid #1f2937; background:#111827;
    }}
    .code-pop-head .hint {{ flex:1; }}
    .code-pop-close {{
      background:#1f2937; border:1px solid #4b5563; color:#e5e7eb; border-radius:6px;
      font-size:12px; padding:4px 10px; cursor:pointer;
    }}
    .code-pop-close:hover {{ border-color:#60a5fa; color:#93c5fd; }}
    .code-pop-body {{
      flex:1 1 auto; margin:0; padding:10px 14px; overflow:auto;
      font-family:ui-monospace,SFMono-Regular,Menlo,monospace; line-height:1.45; font-size:14px;
      white-space:pre; color:#e5e7eb; background:#0b1220; tab-size:4;
    }}
    .code-pop-body code {{ font-family:inherit; font-size:inherit; background:transparent;
      padding:0; white-space:pre; }}
    .code-pop-rs {{
      position:absolute; right:2px; bottom:2px; width:14px; height:14px; cursor:se-resize;
      background:linear-gradient(135deg, transparent 50%, #60a5fa 50%);
      border-radius:0 0 8px 0; opacity:0.85;
    }}
    .code-pop-rs:hover {{ opacity:1; }}
    .sslive-viz-frame, iframe.sslive-viz-frame {{ width:100%; border:0; border-radius:8px;
      background:#0b1220; display:block; }}
    [data-type="output"] {{ display:block; width:100%; max-width:100%; box-sizing:border-box; }}
    .frag-hidden {{ opacity:0 !important; visibility:hidden !important; pointer-events:none !important; }}
    #chrome {{ position:fixed; left:12px; top:12px; z-index:50; display:flex; gap:10px; align-items:center;
      background:rgba(0,0,0,0.55); color:#fff; padding:6px 12px; border-radius:8px; font-size:13px; }}
    #chrome .ok {{ color:#86efac; }}
    #nav {{ position:fixed; right:16px; bottom:16px; z-index:50; display:flex; gap:12px; align-items:center;
      background:rgba(0,0,0,0.5); color:#fff; padding:8px 14px; border-radius:10px; opacity:0.85; }}
    #nav button {{ background:transparent; border:0; color:#fff; font-size:20px; cursor:pointer; padding:0 6px; }}
    #nav button:hover {{ color:#93c5fd; }}
    @media print {{
      html, body {{ overflow:visible; height:auto; background:#fff; color:#000; }}
      #chrome, #nav {{ display:none !important; }}
      .code-pop {{ position:static !important; width:auto !important; height:auto !important;
        max-height:none !important; box-shadow:none; border-color:#ccc; display:flex !important; }}
      .code-pop[hidden] {{ display:flex !important; }}
      .code-pop-rs, .code-pop-close {{ display:none !important; }}
      .code-pre-collapsed {{ display:none !important; }}
      #viewport {{ width:auto; height:auto; overflow:visible; }}
      #stage {{ position:static; transform:none !important; left:0 !important; top:0 !important;
        width:100%; height:auto; }}
      .slide {{ display:flex !important; page-break-after:always; height:auto; min-height:100vh;
        overflow:visible; color:#111; }}
      .slide.hidden {{ display:flex !important; }}
      .frag-hidden {{ opacity:1 !important; visibility:visible !important; }}
    }}
    """

    js = f"""
    let currentSlide = {initial_slide};
    let fragStep = 0;
    let openCodeWrap = null;
    let openCodePop = null;
    const slides = () => document.querySelectorAll('[data-slide]');
    // Standard expanded panel: ~6 lines of mono 14px / 1.45 + header
    const CODE_LINE = 14 * 1.45;
    const CODE_HEAD = 40;
    const CODE_DEF_LINES = 6;
    const CODE_MIN_LINES = 5;

    function slideEls(slideEl) {{
      if (!slideEl) return [];
      return slideEl.querySelectorAll('.note-block, .code-wrap, [data-type="output"]');
    }}
    function revealOf(el) {{
      const r = parseInt(el.getAttribute('data-reveal'), 10);
      return (Number.isFinite(r) && r > 0) ? r : 0;
    }}
    function maxReveal(slideEl) {{
      let m = 0;
      slideEls(slideEl).forEach(el => {{ m = Math.max(m, revealOf(el)); }});
      return m;
    }}
    function applyFragments() {{
      const ss = slides()[currentSlide];
      if (!ss) return;
      slideEls(ss).forEach(el => {{
        const r = revealOf(el);
        el.classList.toggle('frag-hidden', r > 0 && r > fragStep);
      }});
      updateCounter();
    }}
    function updateCounter() {{
      const el = document.getElementById('slide-counter');
      if (!el) return;
      const n = slides().length;
      el.textContent = (currentSlide + 1) + ' / ' + Math.max(n, 1);
    }}
    function showSlide(n, frag) {{
      ssCollapseCode();
      const ss = slides();
      if (!ss.length) return;
      n = Math.max(0, Math.min(n, ss.length - 1));
      ss.forEach((s, i) => {{
        s.classList.toggle('active', i === n);
        s.classList.toggle('hidden', i !== n);
      }});
      currentSlide = n;
      fragStep = (frag == null) ? 0 : frag;
      applyFragments();
      try {{
        const u = new URL(window.location.href);
        u.searchParams.set('slide', String(currentSlide + 1));
        history.replaceState(null, '', u);
      }} catch (e) {{}}
    }}
    function goNext() {{
      const maxR = maxReveal(slides()[currentSlide]);
      if (fragStep < maxR) {{ fragStep++; applyFragments(); return; }}
      if (currentSlide < slides().length - 1) showSlide(currentSlide + 1, 0);
    }}
    function goPrev() {{
      if (fragStep > 0) {{ fragStep--; applyFragments(); return; }}
      if (currentSlide > 0) {{
        const prev = currentSlide - 1;
        showSlide(prev, maxReveal(slides()[prev]));
      }}
    }}

    function ssCollapseCode() {{
      if (openCodeWrap) openCodeWrap.classList.remove('code-open');
      if (openCodePop) {{
        openCodePop.hidden = true;
        // return panel to its code-wrap so DOM stays tidy
        const ownerId = openCodePop.dataset.owner;
        const owner = ownerId ? document.getElementById(ownerId) : null;
        if (owner && openCodePop.parentElement !== owner) owner.appendChild(openCodePop);
      }}
      openCodeWrap = null;
      openCodePop = null;
    }}
    function ssPositionPop(pop, wrap) {{
      const bar = wrap.getBoundingClientRect();
      const w = Math.min(920, Math.floor(window.innerWidth * 0.9));
      const h = Math.round(CODE_LINE * CODE_DEF_LINES + CODE_HEAD + 16);
      let left = Math.round(bar.left);
      let top = Math.round(bar.bottom + 8);
      if (left + w > window.innerWidth - 12) left = Math.max(12, window.innerWidth - w - 12);
      if (left < 12) left = 12;
      if (top + h > window.innerHeight - 12) {{
        top = Math.max(12, Math.round(bar.top - h - 8));
      }}
      pop.style.width = w + 'px';
      pop.style.height = h + 'px';
      pop.style.left = left + 'px';
      pop.style.top = top + 'px';
    }}
    function ssHighlightPop(pop) {{
      const code = pop.querySelector('code.language-python');
      if (!code || code.dataset.hl === '1') return;
      if (window.hljs && typeof hljs.highlightElement === 'function') {{
        try {{ hljs.highlightElement(code); code.dataset.hl = '1'; }} catch (e) {{}}
      }}
    }}
    function ssToggleCode(wrap) {{
      if (!wrap) return;
      if (openCodeWrap === wrap) {{ ssCollapseCode(); return; }}
      ssCollapseCode();
      const pop = wrap.querySelector('.code-pop');
      if (!pop) return;
      openCodeWrap = wrap;
      openCodePop = pop;
      wrap.classList.add('code-open');
      pop.dataset.owner = wrap.id || '';
      // Portal to body so #stage transform does not trap position:fixed
      if (pop.parentElement !== document.body) document.body.appendChild(pop);
      pop.hidden = false;
      ssPositionPop(pop, wrap);
      ssHighlightPop(pop);
    }}
    window.ssToggleCode = ssToggleCode;
    window.ssCollapseCode = ssCollapseCode;

    document.addEventListener('mousedown', (e) => {{
      if (!openCodePop) return;
      if (openCodePop.contains(e.target)) return;
      if (openCodeWrap && openCodeWrap.contains(e.target)) return;
      ssCollapseCode();
    }});
    document.addEventListener('keydown', (e) => {{
      if (e.key === 'Escape' && openCodePop) {{
        e.preventDefault();
        ssCollapseCode();
        return;
      }}
      if (e.key === 'ArrowRight' || e.key === ' ') {{ e.preventDefault(); goNext(); }}
      if (e.key === 'ArrowLeft') {{ e.preventDefault(); goPrev(); }}
      if (e.key === 'f' && !e.metaKey && !e.ctrlKey) {{
        document.documentElement.requestFullscreen?.();
      }}
      if (e.key === 'Home') {{ e.preventDefault(); showSlide(0, 0); }}
      if (e.key === 'End') {{ e.preventDefault(); showSlide(slides().length - 1, 0); }}
    }});
    // SE-corner resize for expanded code panel (session-only, not persisted)
    (function codePopResize() {{
      let drag = null;
      document.addEventListener('mousedown', (e) => {{
        const h = e.target.closest && e.target.closest('.code-pop-rs');
        if (!h) return;
        e.preventDefault();
        e.stopPropagation();
        const pop = h.closest('.code-pop');
        if (!pop) return;
        const r = pop.getBoundingClientRect();
        drag = {{ pop, x: e.clientX, y: e.clientY, w: r.width, h: r.height }};
      }});
      document.addEventListener('mousemove', (e) => {{
        if (!drag) return;
        const minH = Math.round(CODE_LINE * CODE_MIN_LINES + CODE_HEAD + 16);
        const maxH = Math.floor(window.innerHeight * 0.5);
        const minW = 280;
        const maxW = Math.floor(window.innerWidth * 0.95);
        const nw = Math.max(minW, Math.min(maxW, drag.w + (e.clientX - drag.x)));
        const nh = Math.max(minH, Math.min(maxH, drag.h + (e.clientY - drag.y)));
        drag.pop.style.width = nw + 'px';
        drag.pop.style.height = nh + 'px';
      }});
      document.addEventListener('mouseup', () => {{ drag = null; }});
    }})();
    document.getElementById('prev-btn')?.addEventListener('click', (e) => {{
      e.preventDefault(); goPrev();
    }});
    document.getElementById('next-btn')?.addEventListener('click', (e) => {{
      e.preventDefault(); goNext();
    }});
    (function scale() {{
      const DESIGN_W = 1920, DESIGN_H = 1080;
      const stage = document.getElementById('stage');
      const viewport = document.getElementById('viewport');
      function rescale() {{
        const vw = viewport.clientWidth, vh = viewport.clientHeight;
        const sc = Math.min(vw / DESIGN_W, vh / DESIGN_H);
        stage.style.transform = 'scale(' + sc + ')';
        stage.style.left = ((vw - DESIGN_W * sc) / 2) + 'px';
        stage.style.top = ((vh - DESIGN_H * sc) / 2) + 'px';
        if (openCodePop && openCodeWrap) ssPositionPop(openCodePop, openCodeWrap);
      }}
      new ResizeObserver(rescale).observe(viewport);
      rescale();
    }})();
    (function boot() {{
      let start = {initial_slide};
      try {{
        const q = new URLSearchParams(window.location.search).get('slide');
        if (q) {{
          const n = parseInt(q, 10);
          if (Number.isFinite(n) && n >= 1) start = n - 1;
        }}
      }} catch (e) {{}}
      showSlide(start, 0);
    }})();
    """

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <meta name="generator" content="sslive {__version__} export"/>
  <title>{doc_title}</title>
  <script src="https://cdn.plot.ly/plotly-2.35.2.min.js" charset="utf-8"></script>
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/styles/github-dark.min.css"/>
  <script src="https://cdnjs.cloudflare.com/ajax/libs/highlight.js/11.9.0/highlight.min.js"></script>
  <style>{css}</style>
</head>
<body>
  <div id="chrome">
    <strong>sslive</strong>
    <span id="status-badge" class="ok">exported · static</span>
    <span style="opacity:0.7">←/→ reveal then slides · space next · f fullscreen · ?slide=N</span>
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


def export_html_str(
    deck: Deck | None = None,
    *,
    title: str | None = None,
    offline: bool = False,
    initial_slide: int = 0,
) -> str:
    """Return portable HTML for ``deck`` (default: current session deck)."""
    deck = deck or _SESSION.get("deck")
    if deck is None:
        raise RuntimeError("No deck — call await slive() first, or pass deck=")
    return generate_export_html(
        deck, title=title, offline=offline, initial_slide=initial_slide
    )


def export_html(
    path: str | Path,
    deck: Deck | None = None,
    *,
    title: str | None = None,
    offline: bool = False,
    initial_slide: int = 0,
) -> Path:
    """Write a portable HTML player to ``path`` and return the resolved Path.

    Snapshot of the current deck (layout + last-run outputs). Open the file in
    any browser — no SolveIt or GPU required.

    ::

        await slive()
        # ▶ Run cells you want frozen, then:
        export_html("talk.html")
        export_html("talk.html", title="Demo")
    """
    deck = deck or _SESSION.get("deck")
    if deck is None:
        raise RuntimeError("No deck — call await slive() first, or pass deck=")
    html = generate_export_html(
        deck, title=title, offline=offline, initial_slide=initial_slide
    )
    out = Path(path).expanduser().resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    # Summary for the author
    n_code = len(deck.ordered_code_ids)
    n_empty = sum(
        1
        for cid in deck.ordered_code_ids
        if not (deck.cells[cid].outputs or [])
    )
    print(
        f"sslive: exported {len(deck.slides)} slide(s), {n_code} code cell(s) "
        f"→ {out}"
        + (f" ({n_empty} code cell(s) had no outputs)" if n_empty else "")
    )
    return out


async def export_html_a(
    path: str | Path,
    deck: Deck | None = None,
    **kw: Any,
) -> Path:
    """Async alias of ``export_html`` (for await-style SolveIt cells)."""
    return export_html(path, deck=deck, **kw)


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
    # Soft-start: show deck even if GPU offline; badge reflects attach state
    if ok:
        label = f"gpu · ready · ▶ Run"
    else:
        short = (msg or "offline")[:40]
        label = f"gpu · offline · {short}"
    # Fresh open always starts at 0 unless a force-restore is in flight
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


def _display_presenter_html(html_str: str, *, update: bool = False):
    """Display srcdoc HTML without IPython's IFrame UserWarning clutter."""
    if display is None or IPyHTML is None:
        return None
    obj = IPyHTML(html_str)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        handle = _SESSION.get("presenter_handle")
        if update and handle is not None:
            handle.update(obj)
            return handle
        return display(obj, display_id=True)


def refresh_presenter(height: str | None = None) -> None:
    """Re-draw the srcdoc deck (call after run_cell so outputs update)."""
    if display is None:
        return
    h = height or _SESSION.get("height") or "720px"
    port = _SESSION.get("port")
    try:
        html_str = _presenter_iframe_html(h, port=port)
        handle = _display_presenter_html(html_str, update=True)
        if handle is not None:
            _SESSION["presenter_handle"] = handle
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
    """Write source into the SolveIt dialog message (unified source of truth).

    Callers that already wrap a batch in ``hold_dialog_focus`` can pass
    through this helper as-is (nested holds only extend the guard).
    """
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
        deck.cells[cell_id].outputs = list(result.parts or [])
        if source is not None:
            _apply_source_to_deck(cell_id, source)

    # Keep overlay position/size/reveal when the output block is replaced in-place
    out_spec = _layout_spec(deck, f"el-output-{cell_id}")
    try:
        html = render_output_html(
            result.parts or [],
            cell_id,
            theme,
            style=_el_style(out_spec),
            extra_attrs=_reveal_attr(out_spec),
        )
    except Exception as e:
        html = render_output_html(
            [OutputPart(kind="error", text=f"render failed: {e}")],
            cell_id,
            theme,
            style=_el_style(out_spec),
            extra_attrs=_reveal_attr(out_spec),
        )
    # Deliver HTML as base64 so dialoghelper iife / parent HTMX never parse
    # raw tags (Plotly CDN URLs became %22https… and oobSwap crashed).
    try:
        html_b64 = base64.b64encode(html.encode("utf-8")).decode("ascii")
    except Exception as e:
        html = render_output_html(
            [OutputPart(kind="error", text=f"output encode failed: {e}")],
            cell_id,
            theme,
        )
        html_b64 = base64.b64encode(html.encode("utf-8")).decode("ascii")

    # Soft cap: multi-MB iife freezes the SolveIt parent (spinner never clears)
    if len(html_b64) > 2_400_000:
        html = render_output_html(
            [
                OutputPart(
                    kind="error",
                    text=(
                        f"Output too large to push into the slide "
                        f"({len(html_b64) // 1024} KB base64). "
                        "Downsample the Plotly series or use a static image."
                    ),
                )
            ],
            cell_id,
            theme,
        )
        html_b64 = base64.b64encode(html.encode("utf-8")).decode("ascii")

    payload = {
        "type": "sslive_result",
        "cell_id": cell_id,
        "html_b64": html_b64,
        # Empty string keeps older slide JS from treating missing html as null
        "html": "",
        "ok": bool(result.ok),
        # False: keep code box one-line after Run (expand only on user focus/resize)
        "keep_focus": False,
        "source": source,
        "t": int(time.time() * 1000),
        "slide_index": int(_SESSION.get("slide_index") or 0),
    }
    try:
        raw = json.dumps(payload, ensure_ascii=True)
    except Exception as e:
        tiny_html = render_output_html(
            [OutputPart(kind="error", text=f"output too large or invalid: {e}")],
            cell_id,
            theme,
        )
        payload = {
            "type": "sslive_result",
            "cell_id": cell_id,
            "html_b64": base64.b64encode(tiny_html.encode("utf-8")).decode("ascii"),
            "html": "",
            "ok": False,
            "t": int(time.time() * 1000),
        }
        raw = json.dumps(payload, ensure_ascii=True)
    js = f"""
(function() {{
  var msg = {raw};
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
            # Last-ditch: tiny payload so the spinner always clears
            try:
                tiny_html = render_output_html(
                    [OutputPart(kind="error", text=f"push failed: {e}")],
                    cell_id,
                    theme,
                )
                tiny = {
                    "type": "sslive_result",
                    "cell_id": cell_id,
                    "html_b64": base64.b64encode(tiny_html.encode("utf-8")).decode(
                        "ascii"
                    ),
                    "html": "",
                    "ok": False,
                    "t": int(time.time() * 1000),
                }
                iife(
                    f"window.__sslive_last_result={json.dumps(tiny)};"
                    "document.querySelectorAll('iframe').forEach(function(f){"
                    "try{f.contentWindow.postMessage(window.__sslive_last_result,'*');}catch(e){}});"
                )
            except Exception as e2:
                _SESSION["_last_push_err"] = f"{e} / {e2}"


def _run_and_refresh(
    cell_id: str,
    *,
    source: str | None = None,
    full_refresh: bool = False,
    quiet: bool = False,
) -> ExecResult:
    """Execute; always push a result so the slide spinner cannot stick."""
    deck = _SESSION.get("deck")
    executor = _SESSION.get("executor")
    if deck is None or executor is None:
        raise RuntimeError("Call await slive() first")
    if source is not None:
        _apply_source_to_deck(cell_id, source)

    echo = bool(_SESSION.get("echo_to_dialog", False))
    try:
        result = executor.execute_cell(deck, cell_id, echo_to_dialog=echo)
    except Exception as e:
        result = ExecResult(
            ok=False,
            parts=[OutputPart(kind="error", text=str(e))],
            error=str(e),
        )
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
        try:
            refresh_presenter()
        except Exception as e:
            _SESSION["_refresh_err"] = str(e)
            push_slide_result(cell_id, result, source=source)
    else:
        try:
            push_slide_result(cell_id, result, source=source)
        except Exception as e:
            _SESSION["_last_push_err"] = str(e)
            # Minimal payload so the iframe always clears "Running…"
            try:
                push_slide_result(
                    cell_id,
                    ExecResult(
                        ok=False,
                        parts=[OutputPart(kind="error", text=f"push failed: {e}")],
                        error=str(e),
                    ),
                    source=source,
                )
            except Exception as e2:
                _SESSION["_last_push_err"] = f"{e} / {e2}"
    return result


def _refocus_presenter_js(*, soft: bool = True) -> str:
    """JS to return focus to the slide iframe after dialog ``update_msg`` churn.

    ``soft``: only focus if needed; avoid scrollIntoView when the frame is
    already on-screen (repeated scroll was flashing the preview on %slive).
    """
    soft_js = "true" if soft else "false"
    return f"""
(function () {{
  var soft = {soft_js};
  function findFrame() {{
    return document.getElementById('sslive-frame')
      || document.querySelector('iframe[data-sslive="1"]')
      || document.querySelector('iframe[srcdoc]');
  }}
  function inView(el) {{
    try {{
      var r = el.getBoundingClientRect();
      var vh = window.innerHeight || document.documentElement.clientHeight;
      return r.top < vh * 0.85 && r.bottom > vh * 0.15;
    }} catch (e) {{ return false; }}
  }}
  function focusFrame() {{
    var fs = document.fullscreenElement || document.webkitFullscreenElement;
    if (fs) return true;
    var ifr = findFrame();
    if (!ifr) return false;

    // Already focused on the slides — do nothing (no flash)
    if (document.activeElement === ifr) return true;

    try {{
      var ae = document.activeElement;
      if (ae && ae !== document.body && ae !== ifr
          && ae.id !== 'sslive-frame' && ae.getAttribute('data-sslive') !== '1') {{
        try {{ ae.blur(); }} catch (e) {{}}
      }}
    }} catch (e) {{}}

    // Only scroll if the preview is mostly off-screen (soft mode)
    if (!soft || !inView(ifr)) {{
      try {{
        ifr.scrollIntoView({{ block: 'nearest', inline: 'nearest', behavior: 'instant' }});
      }} catch (e) {{
        try {{ ifr.scrollIntoView(false); }} catch (e2) {{}}
      }}
    }}
    try {{ ifr.focus({{ preventScroll: true }}); }} catch (e) {{
      try {{ ifr.focus(); }} catch (e2) {{}}
    }}
    try {{
      if (ifr.contentWindow) ifr.contentWindow.focus();
    }} catch (e) {{}}
    return true;
  }}
  focusFrame();
}})();
"""


def refocus_presenter(*, soft: bool = True) -> None:
    """Best-effort: return focus to the slide iframe (preview mode)."""
    if iife is None:
        return
    try:
        iife(_refocus_presenter_js(soft=soft))
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
        # Extend (don't shorten) an already-armed guard
        iife(
            f"(function(){{"
            f"var until=Date.now()+{int(ms)};"
            f"if((window.__sslive_guard_until||0)<until)"
            f"  window.__sslive_guard_until=until;"
            f"}})();"
        )
    except Exception:
        pass


@asynccontextmanager
async def hold_dialog_focus(
    *,
    ms: int = 4000,
    refocus: bool = True,
    soft: bool = True,
    settle: float = 0.05,
) -> AsyncIterator[None]:
    """Keep focus on the slide preview while dialoghelper mutates messages.

    Same pattern used when writing code-cell sources back to the dialog after
    ▶ Run: arm the parent focus/scroll guard so ``update_msg`` / ``add_msg``
    cannot jump to the changed cell, then soft-refocus ``#sslive-frame``.

    Usage::

        async with hold_dialog_focus():
            await update_msg(id=mid, content=...)
            await update_msg(id=layout_id, content=...)

    Nested holds only extend the guard window. Fullscreen skips refocus
    (would thrash the FS UI). User pointer/key in the dialog still wins
    (see ``__sslive_user_ts`` on the parent bridge).
    """
    _arm_focus_guard(ms)
    in_fs = False
    try:
        in_fs = await _parent_in_fullscreen()
    except Exception:
        in_fs = False
    try:
        yield
    finally:
        # Cover late SolveIt focus jobs that fire after update_msg returns
        _arm_focus_guard(ms)
        if refocus and not in_fs:
            if settle and settle > 0:
                try:
                    await asyncio.sleep(settle)
                except Exception:
                    pass
            try:
                refocus_presenter(soft=soft)
            except Exception:
                pass


async def dialog_call(awaitable, *, ms: int = 4000, refocus: bool = True):
    """Run one async dialoghelper call under ``hold_dialog_focus``."""
    async with hold_dialog_focus(ms=ms, refocus=refocus):
        result = awaitable
        if inspect.isawaitable(result):
            return await result
        return result


async def _read_parent_slide_index() -> int | None:
    try:
        if js_eval is None and js_eval_a is None:
            return None
        idx = await _call_js_eval(
            "return (window.__sslive_slide_index != null "
            "&& window.__sslive_slide_index !== '') "
            "? window.__sslive_slide_index : null;"
        )
        idx = _parse_js_eval_result(idx)
        if idx is None or idx is False or idx == "":
            return None
        return int(idx)
    except Exception:
        return None


async def _sync_slide_index_from_parent() -> int | None:
    """Copy parent ``__sslive_slide_index`` into the session (for rebuilds)."""
    idx = await _read_parent_slide_index()
    if idx is not None:
        _SESSION["slide_index"] = max(0, int(idx))
        return int(idx)
    return None


def _reset_slide_index_for_open() -> None:
    """Fresh ``%slive``: always start at slide 1 (index 0).

    Without this, a sticky ``parent.__sslive_slide_index`` from the previous
    session (e.g. 6 → UI 7/7) wins over ``initial_slide=0`` and re-opens the
    deck on the last slide every time.
    """
    _SESSION["slide_index"] = 0
    if iife is None:
        return
    try:
        iife(
            "(function(){"
            "window.__sslive_slide_index=0;"
            "window.__sslive_force_slide_restore=false;"
            "window.__sslive_goto=null;"
            "})();"
        )
    except Exception as e:
        _SESSION["_slide_reset_err"] = str(e)


def _push_slide_index_restore(*, keep_edit: bool = False) -> None:
    """One-shot: restore slide after a layout-save rebuild (not on fresh %slive).

    Sets ``__sslive_force_slide_restore`` so the new iframe may honor the index;
    normal ``%slive`` clears that flag and always opens at slide 0.
    """
    idx = int(_SESSION.get("slide_index") or 0)
    if iife is None:
        return
    js = f"""
(function() {{
  var idx = {idx};
  var msg = {{
    type: 'sslive_goto',
    slide_index: idx,
    keep_edit: false,
    force: true,
    t: Date.now()
  }};
  window.__sslive_slide_index = idx;
  window.__sslive_force_slide_restore = true;
  window.__sslive_goto = msg;
  function push() {{
    document.querySelectorAll('iframe').forEach(function(f) {{
      try {{ f.contentWindow.postMessage(msg, '*'); }} catch (e) {{}}
    }});
  }}
  push();
  setTimeout(push, 50);
  setTimeout(push, 200);
}})();
"""
    try:
        iife(js)
    except Exception as e:
        _SESSION["_slide_restore_err"] = str(e)


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
    n = 0
    async with hold_dialog_focus(
        ms=2000 + 500 * len(pending),
        refocus=refocus,
        soft=True,
        settle=0.1 if refocus else 0.0,
    ):
        for cid, src in pending.items():
            if await write_back_cell(cid, src):
                n += 1
    return n


async def _sync_and_run(cell_id: str, source: str, *, slide_index: int | None = None) -> ExecResult:
    """Update deck → execute (thread) → push result → deferred dialog write.

    Execution runs in a worker thread so ``execute_interactive`` / ``run_cell``
    cannot freeze the asyncio bridge (which would leave the slide spinner stuck).
    """
    if slide_index is not None:
        _SESSION["slide_index"] = int(slide_index)

    _apply_source_to_deck(cell_id, source)
    _queue_dialog_sync(cell_id, source)

    loop = asyncio.get_running_loop()
    try:
        # Hard ceiling so a wedged kernel cannot spin forever
        result = await asyncio.wait_for(
            loop.run_in_executor(
                None,
                lambda: _run_and_refresh(
                    cell_id, source=source, full_refresh=False, quiet=True
                ),
            ),
            timeout=90.0,
        )
    except asyncio.TimeoutError:
        result = ExecResult(
            ok=False,
            parts=[
                OutputPart(
                    kind="error",
                    text=(
                        "Timed out after 90s waiting for the kernel. "
                        "Interrupt the kernel or simplify the cell, then ▶ Run again."
                    ),
                )
            ],
            error="timeout",
        )
        try:
            push_slide_result(cell_id, result, source=source)
        except Exception as e:
            _SESSION["_last_push_err"] = str(e)
    except Exception as e:
        result = ExecResult(
            ok=False,
            parts=[OutputPart(kind="error", text=str(e))],
            error=str(e),
        )
        try:
            push_slide_result(cell_id, result, source=source)
        except Exception as e2:
            _SESSION["_last_push_err"] = f"{e} / {e2}"

    # Deferred dialog write-back (never block the result path on this)
    if _SESSION.get("auto_sync_dialog", True):

        async def _deferred_dialog_write(cid=cell_id, src=source):
            try:
                await asyncio.sleep(0.2)
                pending = dict(_SESSION.get("pending_dialog_sync") or {})
                pending[cid] = src
                _SESSION["pending_dialog_sync"] = {}
                # One hold for the whole batch — same as layout/skip writes
                async with hold_dialog_focus(
                    ms=2000 + 500 * max(1, len(pending)),
                    refocus=True,
                    soft=True,
                    settle=0.12,
                ):
                    for pcid, psrc in pending.items():
                        await write_back_cell(pcid, psrc)
            except Exception as e:
                _SESSION["_dialog_sync_err"] = str(e)

        try:
            loop.create_task(_deferred_dialog_write())
        except RuntimeError:
            try:
                async with hold_dialog_focus(ms=2500, refocus=True, soft=True):
                    await write_back_cell(cell_id, source)
            except Exception:
                pass

    return result


async def sync_dialog() -> int:
    """Write current deck code sources into SolveIt dialog cells.

    Also flushes sources queued during fullscreen Runs.
    Uses ``hold_dialog_focus`` so SolveIt does not jump to each updated cell.
    """
    deck = _SESSION.get("deck")
    if deck is None:
        raise RuntimeError("Call await slive() first")
    pending = _SESSION.get("pending_dialog_sync") or {}
    for cid, src in pending.items():
        if cid in deck.cells:
            _apply_source_to_deck(cid, src)
    _SESSION["pending_dialog_sync"] = {}

    n = 0
    async with hold_dialog_focus(
        ms=2000 + 500 * max(1, len(deck.ordered_code_ids)),
        refocus=True,
        soft=True,
        settle=0.12,
    ):
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
// Layout patch queue (S2-B): edit-mode drag/nudge patches from the slide.
// Own flag so it installs on pages that already have an older bridge.
if (!window.__sslive_layout_bridge_v1) {
  window.__sslive_layout_bridge_v1 = true;
  window.__sslive_layout_q = window.__sslive_layout_q || [];
  window.__sslive_layout_flush = false;
  window.addEventListener('message', function (e) {
    var d = e.data;
    if (!d) return;
    if (d.type === 'sslive_layout_flush') {
      window.__sslive_layout_flush = true;
      return;
    }
    if (d.type !== 'sslive_layout' || !d.el_id) return;
    // Remember slide so a dialog write that rebuilds the iframe can restore it
    if (d.slide_index != null) window.__sslive_slide_index = d.slide_index;
    window.__sslive_layout_q.push({
      el_id: String(d.el_id),
      patch: d.patch || {},
      slide_index: d.slide_index,
      t: d.t || Date.now()
    });
  });
}
// Upgrade older bridges that lack the flush flag
if (window.__sslive_layout_flush === undefined) {
  window.__sslive_layout_flush = false;
  window.addEventListener('message', function (e) {
    var d = e.data;
    if (d && d.type === 'sslive_layout_flush') window.__sslive_layout_flush = true;
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
    if (!ifr) return true;
    if (el === ifr) return true;
    try {
      if (el && ifr.contains && ifr.contains(el)) return true;
    } catch (e) {}
    // Also allow focus inside the frame's contentDocument when reachable
    try {
      var doc = ifr.contentDocument;
      if (doc && el && doc.documentElement && doc.documentElement.contains(el)) return true;
    } catch (e) {}
    return false;
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
            return
        except Exception:
            pass  # fall through to HTML script

    if display is not None and IPyHTML is not None:
        # Fallback: inject via notebook output (usually still parent page)
        display(IPyHTML(f"<script>{bridge_js}</script>"))
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


async def _drain_slide_queue() -> tuple[list[dict], list[dict], bool]:
    """Pull pending (runs, layout patches, flush flag) from the parent page.

    One js_eval round-trip drains ``__sslive_q``, ``__sslive_layout_q``, and
    ``__sslive_layout_flush`` (set when leaving edit mode).
    """
    if js_eval is None and js_eval_a is None:
        return [], [], False
    try:
        res = await _call_js_eval(
            "const r = (window.__sslive_q || []).slice(); "
            "window.__sslive_q = []; "
            "const l = (window.__sslive_layout_q || []).slice(); "
            "window.__sslive_layout_q = []; "
            "const f = !!window.__sslive_layout_flush; "
            "window.__sslive_layout_flush = false; "
            "return {runs: r, layouts: l, flush: f};"
        )
        q = _parse_js_eval_result(res)
        if q is None:
            return [], [], False
        flush = False
        if isinstance(q, dict) and ("runs" in q or "layouts" in q or "flush" in q):
            runs_raw, layouts_raw = q.get("runs"), q.get("layouts")
            flush = bool(q.get("flush"))
        elif hasattr(q, "runs") or hasattr(q, "layouts"):
            runs_raw, layouts_raw = getattr(q, "runs", None), getattr(q, "layouts", None)
            flush = bool(getattr(q, "flush", False))
        else:  # old bridge on the page: bare run list
            runs_raw, layouts_raw = q, None
        return (
            _item_dicts(runs_raw, ("cell_id", "source", "slide_index")),
            _item_dicts(layouts_raw, ("el_id", "patch", "t", "slide_index")),
            flush,
        )
    except Exception as e:
        if _SESSION.get("_bridge_err") != str(e):
            _SESSION["_bridge_err"] = str(e)
            print(f"sslive: bridge poll error: {e}")
        return [], [], False


def _apply_slide_layout_patches(items: list[dict]) -> int:
    """Apply edit-mode patches from the slide to the overlay + persist.

    No ``_push_layout`` echo — the iframe DOM already shows the dragged
    position; pushing back could fight a drag still in progress.

    Patches are stored even when ``el_id`` is not in ``deck.elements`` (orphan
    keys survive content edits; load still applies when the id returns).
    """
    deck: Deck | None = _SESSION.get("deck")
    if deck is None or not items:
        return 0
    n = 0
    orphans = 0
    for it in items:
        el_id = str(it.get("el_id") or "")
        if not el_id:
            continue
        # Capture slide index from the patch batch (first drag after edit)
        sidx = it.get("slide_index")
        if sidx is not None:
            try:
                _SESSION["slide_index"] = max(0, int(sidx))
            except (TypeError, ValueError):
                pass
        if el_id not in deck.elements:
            orphans += 1
        patch = it.get("patch") or {}
        try:
            _apply_layout_patch(deck, el_id, dict(patch))
            n += 1
        except Exception as e:
            _SESSION["_layout_patch_err"] = f"{el_id}: {e}"
    if orphans:
        _SESSION["_layout_patch_orphans"] = int(
            _SESSION.get("_layout_patch_orphans") or 0
        ) + orphans
    if n:
        _schedule_layout_save()
    return n


async def _bridge_poll_loop() -> None:
    """Background: apply in-slide Run requests + edit-mode layout patches."""
    while _SESSION.get("bridge_active"):
        try:
            pending, layout_patches, want_flush = await _drain_slide_queue()
            # layout first: a Run in the same batch re-renders the output
            # block and must see the just-dragged position
            _apply_slide_layout_patches(layout_patches)
            # Edit-mode exit asks for an immediate dialog write
            if want_flush:
                try:
                    await flush_layout_save()
                except Exception as e:
                    _SESSION["_layout_flush_err"] = str(e)
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
    except RuntimeError:
        # no running loop — user can await pump_slide_runs() if needed
        pass


async def pump_slide_runs(max_items: int = 20) -> int:
    """Manually drain in-slide Run queue (if background poll is not running)."""
    n = 0
    for _ in range(max_items):
        pending, layout_patches, want_flush = await _drain_slide_queue()
        _apply_slide_layout_patches(layout_patches)
        if want_flush:
            try:
                await flush_layout_save()
            except Exception as e:
                _SESSION["_layout_flush_err"] = str(e)
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
    """Deprecated: help used to render under the deck; kept as no-op."""
    return


def _show_presenter(port: int | None, height: str = "720px"):
    """Embed deck (srcdoc) — **one** iframe paint, no dialog ``update_msg``.

    Callers should red-eye the launcher **before** this (or not at all after),
    because ``update_msg(skipped=1)`` after display reloads the srcdoc (2nd flash).

    Reuses the existing display handle when present so a re-``%slive`` updates
    in place instead of stacking outputs.
    """
    if isinstance(height, int):
        height = f"{height}px"
    _SESSION["height"] = height

    _start_bridge()

    try:
        reuse = _SESSION.get("presenter_handle") is not None
        html_str = _presenter_iframe_html(height, port=port)
        # First show in this kernel: clear_output(wait=True) so the next display
        # is a single swap (avoids stacking old previews).
        if not reuse and clear_output is not None:
            try:
                clear_output(wait=True)
            except Exception:
                pass
        handle = _display_presenter_html(html_str, update=reuse)
        if handle is not None:
            _SESSION["presenter_handle"] = handle
    except Exception as e:
        print(f"sslive: srcdoc embed failed: {e}")

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

def _find_caller_msg_id() -> str | None:
    """Sync probes for SolveIt's current message id (stack / ns / find_var)."""
    frame = inspect.currentframe()
    try:
        f = frame.f_back if frame is not None else None
        while f is not None:
            for ns in (f.f_locals, f.f_globals):
                mid = ns.get("__msg_id") if isinstance(ns, dict) else None
                if mid:
                    return str(mid)
            f = f.f_back
    finally:
        del frame
    if get_ipython is not None:
        try:
            ip = get_ipython()
            if ip is not None:
                for ns_name in ("user_ns", "user_global_ns"):
                    ns = getattr(ip, ns_name, None) or {}
                    mid = ns.get("__msg_id") if isinstance(ns, dict) else None
                    if mid:
                        return str(mid)
        except Exception:
            pass
    # dialoghelper uses safepyrun.find_var for __dialog_name — same for __msg_id
    try:
        from safepyrun import find_var  # type: ignore

        mid = find_var("__msg_id")
        if mid:
            return str(mid)
    except Exception:
        pass
    return None


def _msg_id_from_obj(msg: Any) -> str | None:
    if msg is None:
        return None
    if isinstance(msg, dict):
        mid = msg.get("id")
    else:
        mid = getattr(msg, "id", None)
        if mid is None and hasattr(msg, "get"):
            try:
                mid = msg.get("id")
            except Exception:
                mid = None
    return str(mid) if mid else None


async def _resolve_launcher_msg_id(hint: str | None = None) -> str | None:
    """Resolve the cell that is running ``slive()`` — no reliance on ``__msg_id`` alone.

    Order (dialoghelper-native first, matching current-message semantics)::

      1. explicit hint / early capture
      2. stack / user_ns / safepyrun.find_var
      3. read_msg(n=0, relative=True)  — defaults to *current* message
      4. msg_idx() + find_msgs
      5. browser selectedMsgId (js_eval)
      6. find_msgs content: code cells calling slive(
    """
    if hint:
        return str(hint)

    mid = _find_caller_msg_id()
    if mid:
        return mid

    # 3) current message via dialoghelper (id defaults to current)
    if read_msg is not None:
        try:
            msg = await read_msg(n=0, relative=True)
            mid = _msg_id_from_obj(msg)
            if mid:
                return mid
        except Exception as e:
            _SESSION["_skip_read_msg_err"] = str(e)

    # 4) msg_idx() → absolute index of *current* message, then read that slot
    try:
        from dialoghelper.core import msg_idx as _dh_msg_idx

        idx = await _dh_msg_idx()
        if idx is not None and read_msg is not None:
            msg = await read_msg(n=int(idx), relative=False)
            mid = _msg_id_from_obj(msg)
            if mid:
                return mid
    except Exception as e:
        _SESSION["_skip_msg_idx_err"] = str(e)

    # 5) browser-side selection / running marker
    if js_eval is not None or js_eval_a is not None:
        try:
            res = await _call_js_eval(
                """
try {
  if (typeof selectedMsgId === 'function') {
    const v = selectedMsgId();
    if (v) return String(v);
  } else if (typeof selectedMsgId !== 'undefined' && selectedMsgId) {
    return String(selectedMsgId);
  }
  const run = document.querySelector(
    '[data-running="1"], .msg-running, .is-running[id], [id^="_"].running');
  if (run && (run.dataset.id || run.id))
    return String(run.dataset.id || run.id).replace(/^_/,'');
  const sel = document.querySelector(
    '.msg.selected, [data-selected="1"], [aria-selected="true"][id^="_"]');
  if (sel && (sel.dataset.id || sel.id))
    return String(sel.dataset.id || sel.id).replace(/^_/,'');
  return null;
} catch (e) { return null; }
"""
            )
            mid = _parse_js_eval_result(res)
            if mid:
                s = str(mid).strip()
                if s and s not in ("null", "None", "undefined"):
                    # DOM ids are often `_abc`; update_msg accepts with or without
                    return s if s.startswith("_") else s
        except Exception as e:
            _SESSION["_skip_js_err"] = str(e)

    # 6) content heuristic: last code cell that calls slive(
    if find_msgs is not None:
        try:
            msgs = await find_msgs(
                msg_type="code",
                re_pattern=r"slive\s*\(",
                include_output=False,
                include_meta=True,
                include_skipped=True,
                use_regex=True,
            )
            best = None
            for m in msgs or []:
                c = ""
                if isinstance(m, dict):
                    c = m.get("content") or ""
                    mid_m = m.get("id")
                else:
                    c = getattr(m, "content", "") or ""
                    mid_m = getattr(m, "id", None)
                lines = [
                    ln
                    for ln in str(c).splitlines()
                    if ln.strip() and not ln.strip().startswith("%")
                ]
                body = "\n".join(lines)
                if re.search(r"(?:await\s+)?slive\s*\(", body):
                    best = mid_m
            if best:
                return str(best)
        except Exception as e:
            _SESSION["_skip_find_err"] = str(e)

    return None


def _resolve_update_msg():
    """Fresh ``update_msg`` from dialoghelper (module import can be stale/None)."""
    global update_msg
    um = update_msg
    if um is not None:
        return um
    try:
        from dialoghelper.core import update_msg as um2

        update_msg = um2
        return um2
    except Exception:
        return None


async def _call_update_msg(**kwargs) -> Any:
    """Call dialoghelper ``update_msg`` whether it is sync (sslides) or async."""
    um = _resolve_update_msg()
    if um is None:
        raise RuntimeError("update_msg unavailable")
    result = um(**kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


async def _skip_msg(mid: str | None, *, quiet: bool = False, settle: float = 1.0) -> str | None:
    """Mark a dialog cell ``skipped=1`` (red eye — hidden from AI).

    Mirrors sslides ``sshow``::

        show(...); time.sleep(1); update_msg(id=..., skipped=1)

    Arms the focus guard so SolveIt does not yank focus to the first dialog cell.
    """
    if not mid:
        mid = await _resolve_launcher_msg_id()
    if not mid:
        if not quiet:
            errs = [
                f"{k}={_SESSION[k]}"
                for k in (
                    "_skip_read_msg_err",
                    "_skip_msg_idx_err",
                    "_skip_js_err",
                    "_skip_find_err",
                )
                if _SESSION.get(k)
            ]
            extra = (" (" + "; ".join(errs) + ")") if errs else ""
            print(
                "sslive: could not resolve launcher msg id — eye stays open"
                + extra
            )
        return None
    if _resolve_update_msg() is None:
        if not quiet:
            print("sslive: update_msg unavailable — cannot AI-hide preview cell")
        return None
    try:
        # Already red-eyed this session — skip a second update_msg (focus thrash)
        prev = _SESSION.get("skipped_launcher_id")
        if prev and str(prev).lstrip("_") == str(mid).lstrip("_"):
            _SESSION["skipped_launcher_id"] = str(prev)
            return str(prev)

        # Brief settle so the output iframe is attached (keep short to avoid flash)
        if settle and settle > 0:
            await asyncio.sleep(settle)
        # Same hold as code-cell write-back: guard + soft refocus to #sslive-frame
        async with hold_dialog_focus(
            ms=int(max(3500, settle * 1000 + 2500)),
            refocus=not quiet,
            soft=True,
            settle=0.05 if not quiet else 0.0,
        ):
            mid_try = str(mid)
            try:
                await _call_update_msg(id=mid_try, skipped=1)
            except Exception:
                alt = mid_try[1:] if mid_try.startswith("_") else "_" + mid_try
                await _call_update_msg(id=alt, skipped=1)
                mid_try = alt
            _SESSION["skipped_launcher_id"] = mid_try
            return mid_try
    except Exception as e:
        if not quiet:
            print(f"sslive: skip failed for {mid}: {e}")
        return None


async def _skip_caller_msg(*, quiet: bool = False, mid: str | None = None) -> str | None:
    """Skip the calling cell (full async resolve if mid missing)."""
    return await _skip_msg(mid, quiet=quiet)


async def hide_from_ai(msg_id: str | None = None, *, settle: float = 0.0) -> str | None:
    """Mark a dialog message ``skipped=1`` (red eye — excluded from LLM context).

    ::

        await hide_from_ai()                 # current / launcher cell
        await hide_from_ai('_ea017cb0')      # explicit id
        await hide_from_ai()  # after %run, if that cell is still current

    Use this for giant outputs (preview, CRAFT bootstrap logs, git dumps).
    Layout already lives in a skipped ``#| sslive-layout`` note.
    """
    mid = msg_id or await _resolve_launcher_msg_id(_SESSION.get("launcher_msg_id"))
    return await _skip_msg(mid, quiet=False, settle=settle)


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

    def __repr__(self) -> str:
        # Avoid dumping the full Deck (cells, sources, outputs) into cell output.
        d = self.deck
        n_s = len(d.slides) if d else 0
        n_c = len(d.ordered_code_ids) if d else 0
        return f"LiveSession(backend={self.backend!r}, slides={n_s}, code_cells={n_c})"


async def slive(
    theme: str | dict = "dark",
    *,
    height: str = "720px",
    echo_to_dialog: bool = False,
    embed: bool = True,
    use_http: bool | None = None,
    return_session: bool = False,
    require_gpu: bool = False,
):
    """Start the live deck (host magic; slide ▶ Run uses GPU).

    Load the module on the **host** first, then use ``%slive`` under ``%gpu``::

        %local
        %run path/to/sslive.py   # MUST be %local — auto-registers %slive
        %gpu                     # stay here for torch / %pointcloud
        %slive                   # local magic → host deck, Run → GPU

    Soft-start: if CRAFT is offline, the deck still opens; ▶ Run waits until ready.
    Returns ``None`` by default (clean output). Session: ``session()``.
    """
    # Sync probe first (may be empty under await — full resolve after embed).
    launcher_msg_id = _find_caller_msg_id()
    _SESSION["launcher_msg_id"] = launcher_msg_id

    _register_slive_magic(quiet=True)

    host_ok, host_msg = _host_ok()
    if not host_ok:
        print(f"sslive: host not ready — {host_msg}")
        print(_HOST_LOAD_HELP)
        return None

    ok, msg = LiveExecutor().kernel_ok()
    _SESSION["gpu_ok"] = ok
    _SESSION["gpu_msg"] = msg
    if not ok and require_gpu:
        print(f"sslive: GPU not ready — {msg}")
        print(
            "Load CRAFT on the SolveIt host so `_exec_mgr` exists, run %gpu, "
            "then %slive again."
        )
        return None

    if use_http is None:
        use_http = not _in_solveit()

    theme_dict = theme if isinstance(theme, dict) else dict(THEME_DARK)
    # Only flush layout if a debounced save is actually pending (dirty).
    # Always flushing on re-%slive rewrote the layout note → focus layout → preview.
    if _SESSION.get("deck") is not None:
        try:
            await flush_layout_save(quiet=True, force=False)
        except Exception as e:
            _SESSION["_layout_flush_err"] = str(e)
    try:
        deck = await build_deck(theme=theme_dict)
    except RuntimeError as e:
        if "dialoghelper" in str(e).lower():
            print(f"sslive: {e}")
            return None
        raise
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

    session = LiveSession(port=port or 0, backend="gpu" if ok else "offline", deck=deck)

    n_code = len(deck.ordered_code_ids)
    if n_code == 0 and len(deck.slides) == 0:
        print(
            "sslive: no slides found — add a note with exactly `#| s`, "
            "then `#` / `##` content below it."
        )
    _SESSION["slide_index"] = 0
    _SESSION.setdefault("pending_dialog_sync", {})
    _SESSION["auto_sync_dialog"] = True  # deferred update_msg after Run

    if embed:
        # ── Aim for a single iframe paint ─────────────────────────────────
        # Flash #2 was always ``update_msg(skipped=1)`` *after* display (SolveIt
        # re-mounts the cell output). So: red-eye *before* show when needed,
        # show once, never skip after the iframe is on screen.
        _start_bridge()
        # Clear sticky last-slide (e.g. 7/7) before building the srcdoc
        _reset_slide_index_for_open()
        mid = await _resolve_launcher_msg_id(launcher_msg_id)
        _SESSION["launcher_msg_id"] = mid

        already_skipped = False
        if mid:
            prev = _SESSION.get("skipped_launcher_id")
            already_skipped = bool(
                prev and str(prev).lstrip("_") == str(mid).lstrip("_")
            )
        if mid and not already_skipped:
            # Metadata only — no iframe yet, so this is not a second slide load
            try:
                async with hold_dialog_focus(
                    ms=4000, refocus=False, soft=True, settle=0.0
                ):
                    await _call_update_msg(id=str(mid), skipped=1)
                _SESSION["skipped_launcher_id"] = str(mid)
            except Exception:
                try:
                    alt = (
                        str(mid)[1:]
                        if str(mid).startswith("_")
                        else "_" + str(mid)
                    )
                    async with hold_dialog_focus(
                        ms=4000, refocus=False, soft=True, settle=0.0
                    ):
                        await _call_update_msg(id=alt, skipped=1)
                    _SESSION["skipped_launcher_id"] = alt
                except Exception as e:
                    _SESSION["_skip_before_show_err"] = str(e)

        # The only full preview mount/update in this path
        _show_presenter(port, height=height)

        # Layout note is a *different* dialog message — create if missing only.
        # Never touch the launcher cell again (would re-flash the iframe).
        async def _deferred_layout_only():
            try:
                await asyncio.sleep(0.5)
                async with hold_dialog_focus(
                    ms=6000, refocus=True, soft=True, settle=0.08
                ):
                    await ensure_layout_note(quiet=True)
            except Exception as e:
                _SESSION["_slive_housekeeping_err"] = str(e)

        try:
            asyncio.get_running_loop().create_task(_deferred_layout_only())
        except RuntimeError:
            try:
                await _deferred_layout_only()
            except Exception as e:
                _SESSION["_slive_housekeeping_err"] = str(e)
    else:
        mid = await _resolve_launcher_msg_id(launcher_msg_id)
        _SESSION["launcher_msg_id"] = mid
        try:
            async with hold_dialog_focus(ms=5000, refocus=True, soft=True):
                if mid:
                    await _skip_msg(mid, settle=0.0, quiet=True)
                await ensure_layout_note(quiet=True)
        except Exception as e:
            _SESSION["_layout_ensure_err"] = str(e)

    _SESSION["session"] = session
    return session if return_session else None


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
    # Don't lose a drag that hasn't hit the debounced dialog write yet
    if _SESSION.get("deck") is not None:
        try:
            await flush_layout_save()
        except Exception as e:
            _SESSION["_layout_flush_err"] = str(e)
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


def session() -> LiveSession | None:
    """Active ``LiveSession`` from the last ``await slive()`` (if any)."""
    return _SESSION.get("session")


def _run_slive_from_magic(line: str = "") -> Any:
    """Shared body for %slive / %sslive line magics."""
    height = "720px"
    line = (line or "").strip()
    if line:
        m = re.search(r"(?:height\s*=\s*)?(\d+)\s*(px)?", line, re.I)
        if m:
            height = f"{m.group(1)}px"

    async def _run():
        return await slive(height=height, return_session=False)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_run())

    try:
        import nest_asyncio  # type: ignore

        nest_asyncio.apply()
        return loop.run_until_complete(_run())
    except Exception:
        # Last resort: schedule on the running loop (display still happens)
        return loop.create_task(_run())


def _run_slive_export_magic(line: str = "") -> Any:
    """``%slive_export path.html`` — host-local; works under ``%gpu``.

    Under ``%gpu``, bare ``export_html(...)`` runs on the *remote* kernel and
    fails with NameError. This magic always runs on the SolveIt host where the
    deck session lives.
    """
    line = (line or "").strip()
    # first token = path; optional title=... / offline
    parts = line.split() if line else []
    path = parts[0] if parts else "sslive-export.html"
    title = None
    offline = False
    for p in parts[1:]:
        if p.startswith("title="):
            title = p.split("=", 1)[1].strip("\"'")
        elif p in ("--offline", "offline=1", "offline"):
            offline = True
    if _SESSION.get("deck") is None:
        print(
            "sslive: no deck in host session — open slides first:\n"
            "  %slive\n"
            "then export with:\n"
            "  %slive_export talk.html"
        )
        return None
    try:
        return export_html(path, title=title, offline=offline)
    except Exception as e:
        print(f"sslive: export failed: {e}")
        return None


def _inject_public_api_into_user_ns() -> None:
    """Expose export helpers on the *host* user_ns (for %local cells).

    Under ``%gpu``, prefer ``%slive_export`` (local magic) — bare names still
    execute on the remote kernel.
    """
    if get_ipython is None:
        return
    ip = get_ipython()
    if ip is None or not isinstance(getattr(ip, "user_ns", None), dict):
        return
    ns = ip.user_ns
    for name, obj in (
        ("export_html", export_html),
        ("export_html_str", export_html_str),
        ("export_html_a", export_html_a),
        ("generate_export_html", generate_export_html),
        ("slive", slive),
        ("hold_dialog_focus", hold_dialog_focus),
        ("layout_status", layout_status),
        ("cleanup_layout_notes", cleanup_layout_notes),
    ):
        ns[name] = obj


def _register_slive_magic(*, quiet: bool = True) -> bool:
    """Install ``%slive`` / ``%slive_export`` and mark them local for ``%gpu``.

    Always re-registers (safe) so ``%run`` then ``%gpu`` still finds the magics.
    Returns True on success.
    """
    if get_ipython is None:
        return False
    ip = get_ipython()
    if ip is None:
        return False

    ok = False
    # 1) Preferred: magics_manager.register_function (reliable under %run)
    try:
        mm = ip.magics_manager
        mm.register_function(_run_slive_from_magic, magic_kind="line", magic_name="slive")
        mm.register_function(_run_slive_from_magic, magic_kind="line", magic_name="sslive")
        mm.register_function(
            _run_slive_export_magic, magic_kind="line", magic_name="slive_export"
        )
        ok = True
    except Exception as e:
        _SESSION["_magic_reg_err"] = f"register_function: {e}"

    # 2) Fallback: Magics class
    if not ok:
        try:
            from IPython.core.magic import Magics, magics_class, line_magic

            @magics_class
            class SSliveMagics(Magics):
                @line_magic("slive")
                def slive_magic(self, line: str = ""):
                    return _run_slive_from_magic(line)

                @line_magic("sslive")
                def sslive_magic(self, line: str = ""):
                    return _run_slive_from_magic(line)

                @line_magic("slive_export")
                def slive_export_magic(self, line: str = ""):
                    return _run_slive_export_magic(line)

            ip.register_magics(SSliveMagics(ip))
            ok = True
        except Exception as e:
            _SESSION["_magic_reg_err"] = f"Magics class: {e}"

    # Critical under %gpu: route magics to host, not remote kernel
    _mark_slive_local_magic()
    _ensure_local_magic()
    try:
        _inject_public_api_into_user_ns()
    except Exception:
        pass

    # Verify
    try:
        lm = ip.magics_manager.magics.get("line", {})
        if "slive" not in lm and "sslive" not in lm:
            ok = False
            if not quiet:
                print(
                    "sslive: %slive failed to register — use: await slive()"
                )
        elif not quiet:
            print(
                "sslive: %slive / %slive_export ready "
                "(local magics — work under %gpu)"
            )
    except Exception:
        pass

    ip._sslive_magics_loaded = ok  # type: ignore[attr-defined]
    return ok


def register_slive() -> bool:
    """Public: re-register ``%slive`` (call after ``%gpu`` if magic is missing)."""
    return _register_slive_magic(quiet=False)


def load_ipython_extension(ip=None) -> None:
    """``%load_ext sslive`` / auto on ``%run`` when possible."""
    _register_slive_magic(quiet=True)


# Auto-register when the file is %run (must run at end of module)
try:
    if get_ipython is not None and get_ipython() is not None:
        _ok = _register_slive_magic(quiet=True)
        if not _ok and _SESSION.get("_magic_reg_err"):
            print(f"sslive: magic registration issue: {_SESSION['_magic_reg_err']}")
            print("sslive: use  await slive()  or  register_slive()")
except Exception as _e:
    try:
        print(f"sslive: could not auto-register %slive ({_e}); use await slive()")
    except Exception:
        pass


# Wire name for %run
__all__ = [
    "Deck",
    "Cell",
    "Element",
    "OutputPart",
    "LiveExecutor",
    "LiveSession",
    "build_deck",
    "parse_note_to_elements",
    "slive",
    "session",
    "register_slive",
    "hide_from_ai",
    "load_ipython_extension",
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
    "layout_status",
    "save_layout",
    "load_layout",
    "flush_layout_save",
    "ensure_layout_note",
    "cleanup_layout_notes",
    "pump_slide_runs",
    "deck_summary",
    "refresh_presenter",
    "refocus_presenter",
    "hold_dialog_focus",
    "render_output_html",
    "generate_presenter_html",
    "generate_export_html",
    "export_html",
    "export_html_str",
    "export_html_a",
    "get_craft_exec_mgr",
]
