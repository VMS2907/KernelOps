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
    for label, v in versions:
        if v and len(data) < len(v) and v.startswith(data):
            return f"truncated: only the first {len(data)} of {len(v)} bytes of the {label} version"
    for label, v in versions:
        for label2, w in versions:
            if v and w and label != label2:
                cut = _common_prefix(data, v)
                if cut and data[cut:] == w[cut:len(data)] and len(data) == len(w):
                    return (f"mixture: bytes 0-{cut} from the {label} version, "
                            f"bytes {cut}-{len(data)} from the {label2} version")
                if cut and len(data) > cut and data[cut:] == w[cut:len(data)]:
                    return (f"mixture: first {cut} bytes from the {label} version, "
                            f"the remaining {len(data) - cut} from the {label2} version")
    coarse = _coarse(provenance(data, versions))
    names = {"?": "unrecognised bytes", "0": "zeros"}
    if 2 <= len(coarse) <= 4:
        return "mixture: " + ", ".join(
            f"bytes {a}-{b} {'are ' + names[src] if src in names else 'from the ' + src + ' version'}"
            for src, a, b in coarse)
    pct = summarize(provenance(data, versions), len(data))
    names = {"?": "unrecognised bytes", "0": "zeros"}
    parts = [f"{v:.0f}% {names.get(k, k)}" for k, v in sorted(pct.items(), key=lambda kv: -kv[1])
             if k not in ("=",) and v >= 1]
    return f"{len(data)} bytes: mixture of " + ", ".join(parts) if parts else f"{len(data)} bytes"


def _common_prefix(a: bytes, b: bytes) -> int:
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    return i


def _coarse(segs: List[Dict[str, object]], slack: int = 24) -> List[Tuple[str, int, int]]:
    """Merge byte-level provenance into a few big runs ("=" bytes join their neighbours)."""
    out: List[List] = []
    for sg in segs:
        src, a, b = str(sg["src"]), int(sg["start"]), int(sg["end"])  # type: ignore[arg-type]
        if src == "=" or (out and out[-1][0] != src and b - a < slack and src != "0"):
            if out:
                out[-1][2] = b
            else:
                out.append(["=", a, b])
            continue
        if out and out[-1][0] in (src, "="):
            out[-1][0], out[-1][2] = src, b
        else:
            out.append([src, a, b])
    return [(str(s), int(a), int(b)) for s, a, b in out]
