# Working baseline (0.1.0)

**Do not paste `sslive.py` into a SolveIt dialog cell.**

```text
%local
%run /path/to/sslive/sslive.py   # host only — registers %slive
%gpu
%slive
```

CRAFT keeps GPU connection/execution. This repo is a **host addon**, not part of the CRAFT dialog bootstrap blob.

See `CHANGELOG.md` and the modularity plan (CRAFT core thin; sslive / pcviz / mojo on disk).
