"""Where did each recovered byte come from?

Used to show a corrupted document as a coloured strip: bytes that match the
previous version, bytes that match the new version, zeros, and bytes that
match nothing.  This is what "a corrupted mixture" looks like.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple


def provenance(data: bytes, versions: Sequence[Tuple[str, Optional[bytes]]]) -> List[Dict[str, object]]:
    """Run-length segments ``{"src": label, "start": i, "end": j}``.

    ``src`` is the label of the first version whose byte at the same offset
    matches, ``"="`` if several versions agree, ``"0"`` for a zero byte that
    matches none, ``"?"`` for anything else.
    """
    segs: List[Dict[str, object]] = []
    vs = [(label, v) for label, v in versions if v is not None]
    cur: Optional[str] = None
    start = 0
    for i, byte in enumerate(data):
        hits = [label for label, v in vs if i < len(v) and v[i] == byte]
        if len(hits) > 1:
            src = "="
        elif hits:
            src = hits[0]
        elif byte == 0:
            src = "0"
        else:
            src = "?"
        if src != cur:
            if cur is not None:
                segs.append({"src": cur, "start": start, "end": i})
            cur, start = src, i
    if cur is not None:
        segs.append({"src": cur, "start": start, "end": len(data)})
    return segs


def summarize(segs: List[Dict[str, object]], total: int) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for s in segs:
        out[str(s["src"])] = out.get(str(s["src"]), 0) + int(s["end"]) - int(s["start"])  # type: ignore[arg-type]
    return {k: round(100.0 * v / max(1, total), 1) for k, v in out.items()}


def describe_mixture(data: bytes, versions: Sequence[Tuple[str, Optional[bytes]]]) -> str:
    if not data:
        return "empty file (0 bytes)"
    if not any(data):
        return f"{len(data)} bytes, all zeros"
    pct = summarize(provenance(data, versions), len(data))
    parts = [f"{v:.0f}% {k}" for k, v in sorted(pct.items(), key=lambda kv: -kv[1])
             if k not in ("=",) and v >= 1]
    return f"{len(data)} bytes: mixture of " + ", ".join(parts) if parts else f"{len(data)} bytes"
