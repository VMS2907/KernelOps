"""Copy-on-write shadow paging, in the style of LMDB, ZFS and btrfs.

Everything lives in one volume file divided into blocks.  **A block that is
part of a committed version is never overwritten.**  A commit writes the new
documents and a new *catalog* (key -> block run, length, CRC) into *free*
blocks, makes them durable, and then flips a single pointer::

    1  write new document bytes into free blocks       old version untouched
    2  write the new catalog into free blocks          (links to the previous catalog)
    3  fsync(volume)                                   BARRIER: tree durable before it is published
    4  write superblock (generation+1, root=catalog)   into the *inactive* of two slots
    5  fsync(volume)                                   COMMIT POINT -> ack

Recovery reads both superblock slots and picks the highest generation whose
superblock *and* catalog checksums are valid.  That is the whole procedure.
There is nothing to replay and nothing to undo, so recovery is O(1) in the
size of the store.  If the newest slot is torn, the other slot still names
the previous version, whose blocks were protected from reuse.

Integrity forms a Merkle chain: superblock --CRC--> catalog --CRC--> documents.
A torn block anywhere is detected on read.

Free extras that fall out of never overwriting:

* **Version history**: each catalog links to the previous one; the last
  ``keep_versions`` generations are retained and browsable.
* **Lock-free readers (MVCC)**: a reader pins the current root and reads
  a consistent snapshot while a writer prepares the next generation.  Pinned
  blocks are never recycled.
* **Garbage collection**: blocks unreachable from every retained or pinned
  generation go back to the free list.

Volume layout::

    block 0   superblock slot A   (one 512-byte sector used)
    block 1   superblock slot B
    block 2.. data and catalog blocks, allocated first-fit from the free list
"""

from __future__ import annotations

import struct
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Set, Tuple

from ..format import IntegrityError, blocks_for, crc32, pad, sha256
from .base import Engine, RecoveryReport, Version

VOLUME = "volume.img"
SECTOR = 512
SB_MAGIC = b"SWCOWSB1"
CAT_MAGIC = b"SWCAT001"
_SB = struct.Struct(">8sIIQQIIIQ")      # magic ver bs gen root_blk root_n root_len root_crc high
_CAT = struct.Struct(">8sQQIIIdI")      # magic gen prev_blk prev_n prev_len prev_crc time nent
_ENT = struct.Struct(">HQIQIQd")        # keylen length crc start nblocks version mtime


@dataclass
class Entry:
    start: int
    nblocks: int
    length: int
    crc: int
    version: int
    mtime: float


@dataclass
class RootPtr:
    block: int = 0
    nblocks: int = 0
    length: int = 0
    crc: int = 0


@dataclass
class Root:
    gen: int
    ptr: RootPtr
    prev: RootPtr
    timestamp: float
    entries: Dict[str, Entry] = field(default_factory=dict)

    def blocks(self) -> Set[int]:
        out = set(range(self.ptr.block, self.ptr.block + self.ptr.nblocks))
        for e in self.entries.values():
            out.update(range(e.start, e.start + e.nblocks))
        return out


def _encode_catalog(gen: int, prev: RootPtr, ts: float, entries: Dict[str, Entry]) -> bytes:
    parts = [_CAT.pack(CAT_MAGIC, gen, prev.block, prev.nblocks, prev.length, prev.crc, ts, len(entries))]
    for key in sorted(entries):
        e = entries[key]
        kb = key.encode("utf-8")
        parts.append(_ENT.pack(len(kb), e.length, e.crc, e.start, e.nblocks, e.version, e.mtime) + kb)
    body = b"".join(parts)
    return body + struct.pack(">I", crc32(body))


def _decode_catalog(raw: bytes, ptr: RootPtr) -> Root:
    if len(raw) != ptr.length or len(raw) < _CAT.size + 4:
        raise IntegrityError("catalog is truncated")
    body, (crc,) = raw[:-4], struct.unpack(">I", raw[-4:])
    if crc32(body) != crc or crc != ptr.crc:
        raise IntegrityError("catalog checksum mismatch")
    magic, gen, pb, pn, pl, pc, ts, n = _CAT.unpack_from(body)
    if magic != CAT_MAGIC:
        raise IntegrityError("bad catalog magic")
    off = _CAT.size
    entries: Dict[str, Entry] = {}
    for _ in range(n):
        klen, length, ecrc, start, nb, ver, mtime = _ENT.unpack_from(body, off)
        off += _ENT.size
        key = body[off:off + klen].decode("utf-8")
        off += klen
        entries[key] = Entry(start, nb, length, ecrc, ver, mtime)
    return Root(gen, ptr, RootPtr(pb, pn, pl, pc), ts, entries)


def _encode_super(gen: int, bs: int, ptr: RootPtr, high: int) -> bytes:
    body = _SB.pack(SB_MAGIC, 1, bs, gen, ptr.block, ptr.nblocks, ptr.length, ptr.crc, high)
    return pad(body + struct.pack(">I", crc32(body)), SECTOR)


def _decode_super(raw: bytes) -> Tuple[int, int, RootPtr, int]:
    if len(raw) < _SB.size + 4:
        raise IntegrityError("superblock is short")
    body = raw[:_SB.size]
    (crc,) = struct.unpack_from(">I", raw, _SB.size)
    if crc32(body) != crc:
        raise IntegrityError("superblock checksum mismatch (torn or never written)")
    magic, ver, bs, gen, rb, rn, rl, rc, high = _SB.unpack(body)
    if magic != SB_MAGIC:
        raise IntegrityError("bad superblock magic")
    return gen, bs, RootPtr(rb, rn, rl, rc), high


class Snapshot:
    """A consistent, read-only view of one generation (MVCC)."""

    def __init__(self, engine: "CowEngine", root: Root) -> None:
        self._engine = engine
        self.root = root
        self.generation = root.gen

    def get(self, key: str) -> Optional[bytes]:
        return self._engine._read_entry(self.root, key)

    def keys(self) -> List[str]:
        return sorted(self.root.entries)


class CowEngine(Engine):
    name = "cow"
    title = "Copy-on-write (shadow paging)"
    technique = "Never overwrite live blocks: write a new tree elsewhere, then flip one superblock pointer"
    commit_point = "new superblock durable (fsync)"
    recovers_to = "the newest generation whose superblock is durable: old or new, with nothing replayed"
    atomic_tx = True
    OPTIONS = {"block_size": 4096, "keep_versions": 16, "barrier": True, "two_slots": True}
    VARIANTS = {
        "no-barrier": {"options": {"barrier": False},
                       "breaks": "the superblock can reach the disk before the blocks it points to: committed tree is garbage"},
        "one-super": {"options": {"two_slots": False},
                      "breaks": "a single superblock overwritten in place can be torn: no valid root, store unreadable"},
    }

    def __init__(self, vfs, **options) -> None:
        super().__init__(vfs, **options)
        if self.opt["keep_versions"] < 2:
            raise ValueError("keep_versions must be >= 2 (the previous root must survive a torn flip)")
        self.bs = int(self.opt["block_size"])
        self._v = -1
        self._root: Optional[Root] = None
        self._slot = 0
        self._high = 2
        self._free: List[int] = []
        self._retained: List[Root] = []           # newest first
        self._pins: Dict[int, Tuple[Root, int]] = {}
        self._wlock = threading.Lock()            # one writer at a time
        self._mu = threading.Lock()               # protects root pointer, pins, free list

    # --------------------------------------------------------------- format
    def format(self) -> None:
        vfs = self.vfs
        h = vfs.open(VOLUME, create=True, truncate=True)
        try:
            cat = _encode_catalog(1, RootPtr(), time.time(), {})
            ptr = RootPtr(2, blocks_for(len(cat), self.bs), len(cat), struct.unpack(">I", cat[-4:])[0])
            vfs.pwrite(h, 2 * self.bs, cat)
            vfs.fsync(h)
            vfs.pwrite(h, 0, _encode_super(1, self.bs, ptr, 2 + ptr.nblocks))
            vfs.fsync(h)
        finally:
            vfs.close(h)
        vfs.fsync_dir("")

    # ------------------------------------------------------------- recovery
    def _load_root(self, ptr: RootPtr) -> Root:
        raw = self.vfs.pread(self._v, ptr.block * self.bs, ptr.length)
        return _decode_catalog(raw, ptr)

    def recover(self, report: RecoveryReport) -> None:
        vfs = self.vfs
        if not vfs.exists(VOLUME):
            raise IntegrityError("volume.img is missing: not a COW store (or creation never finished)")
        self._v = vfs.open(VOLUME)
        candidates = []
        for slot in (0, 1):
            raw = vfs.pread(self._v, slot * self.bs, SECTOR)
            name = "AB"[slot]
            try:
                gen, bs, ptr, high = _decode_super(raw)
            except IntegrityError as e:
                if any(raw):
                    self.log("discard", f"superblock slot {name}: {e}; ignored")
                    report.discarded += 1
                else:
                    self.log("info", f"superblock slot {name}: empty")
                continue
            if bs != self.bs:
                self.bs = bs
            self.log("info", f"superblock slot {name}: generation {gen}, checksum OK")
            candidates.append((gen, slot, ptr, high))
        for gen, slot, ptr, high in sorted(candidates, reverse=True):
            try:
                root = self._load_root(ptr)
            except IntegrityError as e:
                self.log("discard", f"generation {gen} (slot {'AB'[slot]}) points to a catalog that fails "
                                    f"verification ({e}): it was never fully committed; falling back")
                report.discarded += 1
                continue
            if root.gen != gen:
                self.log("discard", f"generation {gen}: catalog belongs to generation {root.gen}; falling back")
                continue
            self._root, self._slot, self._high = root, slot, high
            self.log("select", f"selected generation {gen} from slot {'AB'[slot]} with {len(root.entries)} "
                               f"document(s): nothing to replay, nothing to undo")
            break
        else:
            raise IntegrityError("no valid superblock: no committed version can be trusted")
        self._retained = self._walk_history(self._root)
        self._gc()
        self.log("ok", f"{len(self._retained)} generation(s) retained for history; "
                       f"{len(self._free)} free block(s) reclaimed by garbage collection")

    def _walk_history(self, root: Root) -> List[Root]:
        chain = [root]
        while len(chain) < self.opt["keep_versions"]:
            cur = chain[-1]
            if not cur.prev.length:
                break
            try:
                older = self._load_root(cur.prev)
            except IntegrityError:
                break
            if older.gen >= cur.gen:
                break
            chain.append(older)
        return chain

    def _gc(self) -> None:
        """Mark & sweep: every block not reachable from a retained or pinned root is free."""
        live: Set[int] = {0, 1}
        for r in self._retained:
            live |= r.blocks()
        for r, _ in self._pins.values():
            live |= r.blocks()
        self._free = [b for b in range(2, self._high) if b not in live]

    def close(self) -> None:
        if self._v != -1:
            self.vfs.close(self._v)
            self._v = -1
        super().close()

    # -------------------------------------------------------------- reading
    @contextmanager
    def snapshot(self) -> Iterator[Snapshot]:
        """Pin the current generation for a consistent multi-document read."""
        with self._mu:
            root = self._root
            assert root is not None
            r, n = self._pins.get(root.gen, (root, 0))
            self._pins[root.gen] = (r, n + 1)
        try:
            yield Snapshot(self, root)
        finally:
            with self._mu:
                r, n = self._pins[root.gen]
                if n <= 1:
                    del self._pins[root.gen]
                else:
                    self._pins[root.gen] = (r, n - 1)

    def _read_entry(self, root: Root, key: str) -> Optional[bytes]:
        e = root.entries.get(key)
        if e is None:
            return None
        data = self.vfs.pread(self._v, e.start * self.bs, e.length) if e.length else b""
        if len(data) != e.length or crc32(data) != e.crc:
            raise IntegrityError(f"document {key!r} (generation {root.gen}) fails its checksum")
        return data

    def get(self, key: str) -> Optional[bytes]:
        with self.snapshot() as snap:
            return snap.get(key)

    def keys(self) -> List[str]:
        assert self._root is not None
        return sorted(self._root.entries)

    @property
    def generation(self) -> int:
        return self._root.gen if self._root else 0

    def history(self, key: str) -> List[Version]:
        """Distinct versions of ``key`` across retained generations, newest first."""
        events: List[Version] = []
        last: Optional[int] = None                  # version seen in the previous (older) generation
        for root in reversed(self._retained):       # oldest -> newest
            e = root.entries.get(key)
            if e is None:
                if last is not None:
                    events.append(Version(key, root.gen, 0, "", root.timestamp, deleted=True))
                last = None
                continue
            if e.version == last:
                continue
            last = e.version
            try:
                digest = sha256(self._read_entry(root, key) or b"")
            except IntegrityError:
                digest = "damaged"
            events.append(Version(key, e.version, e.length, digest, e.mtime))
        return list(reversed(events))

    def get_version(self, key: str, version: int) -> Optional[bytes]:
        for root in self._retained:
            e = root.entries.get(key)
            if e is not None and e.version == version:
                return self._read_entry(root, key)
        raise KeyError(f"version {version} of {key!r} is not retained")

    def verify(self) -> List[str]:
        problems = []
        for root in self._retained:
            for key in root.entries:
                try:
                    self._read_entry(root, key)
                except IntegrityError as e:
                    problems.append(f"generation {root.gen}: {e}")
        return problems

    # -------------------------------------------------------------- writing
    def _alloc(self, n: int) -> int:
        """First-fit run of ``n`` contiguous free blocks, else grow the volume."""
        if n == 0:
            return 0
        free = self._free                          # sorted block numbers
        i = 0
        while i < len(free):
            j = i
            while j + 1 < len(free) and free[j + 1] == free[j] + 1 and j - i + 1 < n:
                j += 1
            if j - i + 1 >= n:
                start = free[i]
                del free[i:i + n]
                return start
            i = j + 1
        start = self._high
        self._high += n
        return start

    def commit(self, changes: Dict[str, Optional[bytes]]) -> None:
        changes = self._check(changes)
        vfs = self.vfs
        with self._wlock:
            cur = self._root
            assert cur is not None
            gen = cur.gen + 1
            now = time.time()
            entries = dict(cur.entries)
            with self._mu:
                saved_free, saved_high = list(self._free), self._high
            try:
                vfs.note("write-new-blocks", generation=gen)
                for key, value in changes.items():
                    if value is None:
                        entries.pop(key, None)
                        continue
                    n = blocks_for(len(value), self.bs) if value else 0
                    with self._mu:
                        start = self._alloc(n)
                    if value:
                        vfs.pwrite(self._v, start * self.bs, value)
                    entries[key] = Entry(start, n, len(value), crc32(value), gen, now)
                cat = _encode_catalog(gen, cur.ptr, now, entries)
                cn = blocks_for(len(cat), self.bs)
                with self._mu:
                    cstart = self._alloc(cn)
                ptr = RootPtr(cstart, cn, len(cat), struct.unpack(">I", cat[-4:])[0])
                vfs.note("write-catalog", generation=gen)
                vfs.pwrite(self._v, cstart * self.bs, cat)
                if self.opt["barrier"]:
                    vfs.note("fsync-barrier")
                    vfs.fsync(self._v)
                slot = (1 - self._slot) if self.opt["two_slots"] else 0
                vfs.note("flip-superblock", slot="AB"[slot], generation=gen)
                vfs.pwrite(self._v, slot * self.bs, _encode_super(gen, self.bs, ptr, self._high))
                vfs.fsync(self._v)
            except BaseException:
                with self._mu:  # nothing was published: give the blocks back
                    self._free, self._high = saved_free, saved_high
                raise
            vfs.note("commit", generation=gen, detail="superblock durable: generation published")
            new = Root(gen, ptr, cur.ptr, now, entries)
            with self._mu:
                self._root, self._slot = new, slot
                self._retained = [new] + self._retained[: self.opt["keep_versions"] - 1]
                self._gc()

    # ----------------------------------------------------------------- UI
    def layout(self) -> Dict[str, object]:
        """Block map for the Crash Lab: who owns each block."""
        owners: Dict[int, str] = {0: "superblock A", 1: "superblock B"}
        assert self._root is not None
        for root in reversed(self._retained):
            for b in range(root.ptr.block, root.ptr.block + root.ptr.nblocks):
                owners[b] = f"catalog g{root.gen}"
            for key, e in root.entries.items():
                for b in range(e.start, e.start + e.nblocks):
                    owners[b] = f"{key} v{e.version}"
        return {"block_size": self.bs, "high_water": self._high, "generation": self._root.gen,
                "active_slot": "AB"[self._slot], "owners": {str(k): v for k, v in sorted(owners.items())},
                "free": list(self._free)}

