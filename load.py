"""Alias entrypoint — same as ``sslive.py``.

::

    %run /path/to/sslive/load.py
    %run /path/to/sslive/sslive.py
    %run /path/to/gpudev/addons/sslive.py   # thin wrapper → this clone
"""

from pathlib import Path

_loader = Path(__file__).resolve().parent / "sslive.py"
exec(compile(_loader.read_text(encoding="utf-8"), str(_loader), "exec"), globals())
