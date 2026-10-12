"""Which disk states can a crash leave behind?

Given the recorded trace of an engine's system calls and a crash *point* (the
number of trace entries executed before the power went out), this module
enumerates every disk image the persistence model allows and builds each one.

The rules (``power`` model), the guarantees POSIX actually gives you:

1. ``fsync(f)`` is a barrier.  Every earlier write/truncate *of that inode* is
   durable.
2. ``fsync(dir)`` is a barrier.  Every earlier create/rename/unlink in that
   directory is durable.
3. Namespace operations reach the disk **in order** (journaled metadata, as
   in ext4, XFS, NTFS, APFS), so the durable namespace is a *prefix* of the
   namespace operations issued.
4. Data writes that no barrier covers may each be **lost or kept,
   independently and in any order**.
5. One unforced write may additionally be **torn**: only a prefix or suffix of
   its sectors persists, or the file size grows but the data is still zeros.
   Sectors are atomic (512 B) in ``power``.  ``paranoid`` drops even that
   assumption (16-byte tearing, as on drives without power-loss protection).

The ``process`` model is ``kill -9``: the kernel's page cache survives, so every
issued call persists.  Only the call in flight may be cut short (Linux can
return a partial ``write`` at page granularity on a fatal signal).

This is the methodology of ALICE (OSDI'14) and CrashMonkey (OSDI'18), applied to
our own engines.
"""

from __future__ import annotations

import hashlib
import itertools
import random
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

from ..vfs.base import parent
from ..vfs.recording import DATA_OPS, NAMESPACE_OPS, BaseImage, Op


@dataclass(frozen=True)
class Model:
    name: str
    title: str
    reorder: bool              # unforced data ops may persist independently
    tear_unit: int             # tear granularity in bytes (0 = writes are atomic)
    zeros: bool                # an extending write may persist as size-only (zeros)
    process: bool = False      # kill -9: everything issued persists
    max_states_per_point: int = 600

    def to_json(self) -> Dict[str, object]:
        return {"name": self.name, "title": self.title, "tear_unit": self.tear_unit,
                "reorder": self.reorder, "process": self.process}


MODELS: Dict[str, Model] = {
    "process": Model("process", "Process crash (kill -9): page cache survives",
                     reorder=False, tear_unit=4096, zeros=False, process=True),
    "power": Model("power", "Power loss: unflushed writes lost, reordered or torn (512 B sectors)",
                   reorder=True, tear_unit=512, zeros=True),
    "paranoid": Model("paranoid", "Power loss without sector atomicity (16 B tearing)",
                      reorder=True, tear_unit=16, zeros=True),
}


@dataclass(frozen=True)
class Tear:
    op: int                    # trace index of the torn write
    mode: str                  # "prefix" | "suffix" | "hole" | "zeros"
    cut: int = 0               # byte offset within the write
    end: int = 0               # hole: end of the missing range

    def describe(self, trace: Sequence[Op]) -> str:
        op = trace[self.op]
        if self.mode == "zeros":
            return f"#{self.op} {op.describe()} persisted only as a size change (zeros)"
        if self.mode == "hole":
            return (f"#{self.op} {op.describe()} torn: bytes {self.cut}-{self.end} never reached the disk, "
                    "both ends did (sectors written out of order)")
        part = f"first {self.cut}" if self.mode == "prefix" else f"last {len(op.data) - self.cut}"
        return f"#{self.op} {op.describe()} torn: only the {part} of {len(op.data)} bytes persisted"


@dataclass
class CrashState:
    """One possible post-crash disk."""
    point: int                       # trace entries executed before the crash
    ns_len: int                      # how many namespace ops persisted
    persisted: frozenset             # trace indices of data ops that persisted
    tear: Optional[Tear] = None
    lost: Tuple[int, ...] = ()       # unforced ops that did NOT persist
    lucky: Tuple[int, ...] = ()      # unforced ops that happened to persist
    forced: Tuple[int, ...] = ()     # ops a barrier guaranteed
    ns_lost: Tuple[int, ...] = ()    # namespace ops that did not persist

    def describe(self, trace: Sequence[Op]) -> str:
        bits = []
        if self.lost:
            bits.append("lost " + ", ".join(f"#{i} {trace[i].describe()}" for i in self.lost))
        if self.ns_lost:
            bits.append("namespace ops not persisted: " + ", ".join(f"#{i} {trace[i].describe()}" for i in self.ns_lost))
        if self.tear:
            bits.append(self.tear.describe(trace))
        return "; ".join(bits) or "every issued operation reached the disk"

    def to_json(self, trace: Sequence[Op]) -> Dict[str, object]:
        return {"point": self.point, "lost": list(self.lost), "lucky": list(self.lucky),
                "forced": list(self.forced), "ns_lost": list(self.ns_lost),
                "tear": None if not self.tear else {"op": self.tear.op, "mode": self.tear.mode,
                                                     "cut": self.tear.cut},
                "text": self.describe(trace)}


# ----------------------------------------------------------------------------
# labels
# ----------------------------------------------------------------------------
def phase_at(trace: Sequence[Op], point: int) -> str:
    """The most recent protocol note before ``point`` (for labelling)."""
    for op in reversed(trace[:point]):
        if op.kind == "note":
            return op.label
    return "start"


def _forced(trace: Sequence[Op], point: int) -> Tuple[Set[int], int]:
    """(data ops forced by fsync, length of namespace prefix forced by fsync_dir)."""
    prefix = trace[:point]
    forced_data: Set[int] = set()
    ns_indices = [op.idx for op in prefix if op.kind in NAMESPACE_OPS]
    forced_ns_upto = -1
    for j, op in enumerate(prefix):
        if op.kind == "fsync":
            for earlier in prefix[:j]:
                if earlier.kind in DATA_OPS and earlier.ino == op.ino:
                    forced_data.add(earlier.idx)
        elif op.kind == "fsync_dir":
            for earlier in prefix[:j]:
                if earlier.kind in NAMESPACE_OPS:
                    dirs = {parent(earlier.path)}
                    if earlier.kind == "rename":
                        dirs.add(parent(earlier.dst))
                    if op.path in dirs:
                        forced_ns_upto = max(forced_ns_upto, earlier.idx)
    forced_ns = sum(1 for i in ns_indices if i <= forced_ns_upto)
    return forced_data, forced_ns


def _content_before(trace: Sequence[Op], base: BaseImage, op: Op) -> bytes:
    """The inode's contents just before ``op``, assuming everything issued persisted."""
    buf = bytearray(base.contents.get(op.ino, b""))
    for o in trace[:op.idx]:
        if o.ino != op.ino:
            continue
        if o.kind == "write":
            if o.offset > len(buf):
                buf.extend(b"\0" * (o.offset - len(buf)))
            buf[o.offset:o.offset + len(o.data)] = o.data
        elif o.kind == "trunc":
            del buf[o.size:]
            buf.extend(b"\0" * (o.size - len(buf)))
    return bytes(buf)


def _spread(items: List[int], limit: int) -> List[int]:
    if len(items) <= limit:
        return items
    return sorted({items[(len(items) - 1) * i // (limit - 1)] for i in range(limit)})


def tear_variants(op: Op, model: Model, old: bytes) -> List[Tear]:
    """Representative ways ``op`` can be torn.

    Cut points are chosen *inside the byte range the write actually changes*:
    tearing a sector whose new bytes equal the old ones changes nothing, and a
    512-byte superblock may only have 68 meaningful bytes.
    """
    out: List[Tear] = []
    if op.kind != "write" or not op.data:
        return out
    data, off = op.data, op.offset
    changed = [i for i in range(len(data)) if off + i >= len(old) or old[off + i] != data[i]]
    if model.tear_unit and changed:
        u = model.tear_unit
        lo, hi = changed[0], changed[-1]
        # cut c: bytes [0, c) and [c, len) are written separately; both halves must change something
        cuts = [c - off for c in range((off + lo) // u * u + u, off + hi + 1, u)]
        for c in _spread(cuts, 4):
            out.append(Tear(op.idx, "prefix", c))
            if not model.process:
                out.append(Tear(op.idx, "suffix", c))
        if not model.process and len(cuts) >= 2:
            # one interior unit missing while both ends landed (out-of-order sectors)
            holes = [(cuts[i], cuts[i + 1]) for i in range(len(cuts) - 1)]
            for h0, h1 in [holes[j] for j in _spread(list(range(len(holes))), 3)]:
                out.append(Tear(op.idx, "hole", h0, h1))
    if model.zeros and off + len(data) > len(old):
        out.append(Tear(op.idx, "zeros"))
    return out


# ----------------------------------------------------------------------------
# enumeration
# ----------------------------------------------------------------------------
def _subsets(items: List[int], budget: int, rng: random.Random) -> Tuple[List[Tuple[int, ...]], bool]:
    """All subsets if they fit in ``budget``; else a structured sample.

    The sample always contains every *prefix* (writes reaching the disk in
    issue order), every "all but one" and every "only one" subset, plus random
    subsets.  Returns (subsets, exhaustive?)."""
    n = len(items)
    if 2 ** n <= budget:
        return [c for r in range(n + 1) for c in itertools.combinations(items, r)], True
    picks: Set[Tuple[int, ...]] = set()
    for i in range(n + 1):
        picks.add(tuple(items[:i]))
    for i in range(n):
        picks.add(tuple(x for x in items if x != items[i]))
        picks.add((items[i],))
    while len(picks) < budget:
        picks.add(tuple(x for x in items if rng.random() < 0.5))
    return sorted(picks, key=lambda t: (len(t), t)), False


def enumerate_states(trace: Sequence[Op], base: BaseImage, point: int,
                     model: Model, seed: int = 0) -> Tuple[List[CrashState], bool]:
    """Every crash state the model allows at ``point``.  Returns (states, exhaustive?)."""
    prefix = [op for op in trace[:point] if op.mutating]
    data_ops = [op.idx for op in prefix if op.kind in DATA_OPS]
    ns_ops = [op.idx for op in prefix if op.kind in NAMESPACE_OPS]

    if model.process:
        full = CrashState(point, len(ns_ops), frozenset(data_ops))
        states = [full]
        nxt = next((op for op in trace[point:] if op.mutating), None)
        if nxt is not None and nxt.kind == "write":
            # killed inside write(2): a prefix of the bytes landed in the page cache
            for t in tear_variants(nxt, model, _content_before(trace, base, nxt)):
                states.append(CrashState(point, len(ns_ops), frozenset(data_ops + [nxt.idx]), t))
        return states, True

    forced_data, forced_ns = _forced(trace, point)
    unforced = [i for i in data_ops if i not in forced_data]
    rng = random.Random(point * 1_000_003 + seed)
    budget = max(1, model.max_states_per_point // max(1, len(ns_ops) - forced_ns + 1))
    subsets, exhaustive = _subsets(unforced, budget, rng)
    olds = {i: _content_before(trace, base, trace[i]) for i in unforced if trace[i].kind == "write"}
    states: List[CrashState] = []
    for ns_len in range(forced_ns, len(ns_ops) + 1):
        ns_lost = tuple(ns_ops[ns_len:])
        for sub in subsets:
            kept = set(sub)
            persisted = frozenset(forced_data | kept)
            lost = tuple(i for i in unforced if i not in kept)
            st = CrashState(point, ns_len, persisted, None, lost, tuple(sub),
                            tuple(sorted(forced_data)), ns_lost)
            states.append(st)
            for i in sub:  # one torn write per state
                for t in tear_variants(trace[i], model, olds.get(i, b"")):
                    states.append(CrashState(point, ns_len, persisted, t, lost, tuple(sub),
                                             tuple(sorted(forced_data)), ns_lost))
    if len(states) > model.max_states_per_point * 4:
        keep = rng.sample(range(len(states)), model.max_states_per_point * 4)
        states = [states[i] for i in sorted(keep)]
        exhaustive = False
    return states, exhaustive


# ----------------------------------------------------------------------------
# materialising an image
# ----------------------------------------------------------------------------
@dataclass
class Image:
    files: Dict[str, bytes]
    dirs: List[str]
    #: path -> list of (sector index, trace idx of the last write to land there, or -1 for base)
    owners: Dict[str, List[int]] = field(default_factory=dict)

    def fingerprint(self) -> str:
        h = hashlib.sha1()
        for p in sorted(self.files):
            h.update(p.encode() + b"\0" + hashlib.sha1(self.files[p]).digest())
        for d in self.dirs:
            h.update(b"D" + d.encode())
        return h.hexdigest()


def build_image(trace: Sequence[Op], base: BaseImage, state: CrashState,
                track: bool = False, sector: int = 512) -> Image:
    names = dict(base.names)
    contents: Dict[int, bytearray] = {ino: bytearray(b) for ino, b in base.contents.items()}
    owners: Dict[int, List[int]] = {}
    if track:
        for ino, b in base.contents.items():
            owners[ino] = [-1] * (-(-len(b) // sector))
    dirs = set(base.dirs)
    ns_seen = 0
    tear = state.tear
    ops = list(trace[:state.point])
    if tear is not None and tear.op >= state.point:   # process model: the in-flight write
        ops.append(trace[tear.op])

    def mark(ino: int, start: int, end: int, idx: int) -> None:
        if not track or end <= start:
            return
        own = owners.setdefault(ino, [])
        need = -(-len(contents[ino]) // sector)
        if len(own) < need:
            own.extend([-1] * (need - len(own)))
        for s in range(start // sector, -(-end // sector)):
            own[s] = idx

    for op in ops:
        k = op.kind
        if k in NAMESPACE_OPS:
            if ns_seen >= state.ns_len:
                ns_seen += 1
                continue
            ns_seen += 1
            if k == "create":
                names[op.path] = op.ino
                contents.setdefault(op.ino, bytearray())
            elif k == "rename":
                ino = names.pop(op.path, op.ino)
                names[op.dst] = ino
            elif k == "unlink":
                names.pop(op.path, None)
            elif k == "mkdir":
                dirs.add(op.path)
            continue
        if k not in DATA_OPS or op.idx not in state.persisted:
            continue
        buf = contents.setdefault(op.ino, bytearray())
        if k == "trunc":
            if op.size < len(buf):
                del buf[op.size:]
            else:
                buf.extend(b"\0" * (op.size - len(buf)))
            if track:
                own = owners.setdefault(op.ino, [])
                del own[-(-op.size // sector):]
            continue
        data, off = op.data, op.offset
        if tear is not None and tear.op == op.idx:
            if tear.mode == "zeros":
                end = off + len(data)
                if end > len(buf):
                    buf.extend(b"\0" * (end - len(buf)))
                continue
            if tear.mode == "hole":
                end = off + len(data)
                if end > len(buf):           # the file grew; the hole reads back as zeros
                    buf.extend(b"\0" * (end - len(buf)))
                buf[off:off + tear.cut] = data[:tear.cut]
                buf[off + tear.end:end] = data[tear.end:]
                mark(op.ino, off, off + tear.cut, op.idx)
                mark(op.ino, off + tear.end, end, op.idx)
                continue
            if tear.mode == "prefix":
                data = data[:tear.cut]
            else:
                off, data = off + tear.cut, data[tear.cut:]
                if off > len(buf):          # the size update comes with the persisted tail
                    buf.extend(b"\0" * (off - len(buf)))
        if off > len(buf):
            buf.extend(b"\0" * (off - len(buf)))
        buf[off:off + len(data)] = data
        mark(op.ino, off, off + len(data), op.idx)

    files = {p: bytes(contents.get(ino, b"")) for p, ino in names.items()}
    img = Image(files, sorted(d for d in dirs if d))
    if track:
        img.owners = {p: owners.get(ino, [])[: -(-len(files[p]) // sector)] for p, ino in names.items()}
    return img

