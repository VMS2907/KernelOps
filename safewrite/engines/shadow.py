"""Shadow file + atomic rename.  Our round-1 SafeWrite design, hardened.

Protocol for ``put(key, B)``::

    1  tmp = docs/<key>~<n>.tmp           unique name: concurrent writers can't collide
    2  write(tmp, header + B)             header = magic, length, CRC-32
    3  fsync(tmp)                         B's bytes are durable BEFORE anyone can see them
    4  read back + validate tmp           catches I/O errors before commit
    5  rename(tmp, docs/<key>)            COMMIT POINT: the name now means B, atomically
    6  fsync(docs/)                       the rename itself is durable -> ack

Recovery: delete every ``*~*.tmp`` (never committed, by definition) and
validate each document.

Why each step exists (the explorer has a counter-example for each):

* without (3) the rename can reach the disk *before* B's data does.  After a
  power cut the name points at an empty inode and A is gone too.  This is the
  2009 ext4 "zero-length file" bug.
* without (6) ``put`` returns, then a power cut silently brings back A.  That
  is a durability violation.

Limitation, shown on purpose: a multi-document commit is a *sequence* of
renames, so a crash between them leaves some documents new and some old.
Atomic multi-document commits need a journal (undo/wal) or a single root
pointer (cow).
"""

from __future__ import annotations

import itertools
import threading
from typing import Dict, List, Optional

from ..format import IntegrityError, decode_doc, encode_doc, key_to_name, name_to_key
from ..vfs.base import join
from .base import Engine, RecoveryReport

DOCS = "docs"


class ShadowEngine(Engine):
    name = "shadow"
    title = "Shadow file + rename"
    technique = "Write a complete temp copy, fsync it, then atomically rename it over the original"
    commit_point = "rename(tmp, doc)"
    recovers_to = "old or new version of each document (per-document atomicity)"
    atomic_tx = False
    OPTIONS = {"fsync_file": True, "fsync_dir": True, "verify": True}
    VARIANTS = {
        "no-fsync": {"options": {"fsync_file": False},
                     "breaks": "rename may persist before the data: the name points at an empty file, A and B both lost"},
        "no-dirsync": {"options": {"fsync_dir": False},
                       "breaks": "put() returns but the rename is not durable: an acknowledged write is lost"},
    }

    def __init__(self, vfs, **options) -> None:
        super().__init__(vfs, **options)
        self._seq = itertools.count(1)
        self._wlock = threading.Lock()

    def format(self) -> None:
        self.vfs.makedirs(DOCS)
        self.vfs.fsync_dir("")

    def _path(self, key: str) -> str:
        return join(DOCS, key_to_name(key))

    # ------------------------------------------------------------- recovery
    def recover(self, report: RecoveryReport) -> None:
        names = self.vfs.listdir(DOCS)
        temps = [n for n in names if n.endswith(".tmp") and "~" in n]
        for n in temps:
            self.vfs.unlink(join(DOCS, n))
            report.discarded += 1
            self.log("discard", f"deleted uncommitted temp file {n} (it was never renamed, so never committed)")
        if temps:
            self.vfs.fsync_dir(DOCS)
        else:
            self.log("info", "no leftover temp files")
        docs = [n for n in names if n not in temps]
        bad = 0
        for n in docs:
            try:
                decode_doc(self.vfs.read_file(join(DOCS, n)))
            except IntegrityError as e:
                bad += 1
                self.log("error", f"{n}: committed document failed validation ({e})")
        if docs and not bad:
            self.log("ok", f"{len(docs)} committed document(s) validated (length + CRC-32)")
        # Continue the temp-name sequence past anything we just saw.
        self._seq = itertools.count(len(names) + 1)

    # -------------------------------------------------------------- reading
    def get(self, key: str) -> Optional[bytes]:
        raw = self.vfs.read_file(self._path(key))
        if raw is None:
            return None
        return decode_doc(raw)

    def keys(self) -> List[str]:
        return sorted(k for k in map(name_to_key, self.vfs.listdir(DOCS)) if k)

    # -------------------------------------------------------------- writing
    def commit(self, changes: Dict[str, Optional[bytes]]) -> None:
        changes = self._check(changes)
        with self._wlock:
            for key, value in changes.items():
                if value is None:
                    self._delete(key)
                else:
                    self._put(key, value)

    def _put(self, key: str, value: bytes) -> None:
        vfs = self.vfs
        dest = self._path(key)
        tmp = join(DOCS, f"{key_to_name(key)}~{next(self._seq)}.tmp")
        record = encode_doc(value)
        vfs.note("write-temp", key=key, tmp=tmp)
        h = vfs.open(tmp, create=True, truncate=True)
        try:
            vfs.pwrite(h, 0, record)
            if self.opt["fsync_file"]:
                vfs.note("fsync-temp")
                vfs.fsync(h)
            intact = True
            if self.opt["verify"]:
                vfs.note("validate-temp")
                intact = vfs.pread(h, 0, len(record) + 1) == record
        finally:
            vfs.close(h)
        if not intact:
            vfs.unlink(tmp)
            raise IOError(f"read-back of {tmp} did not match; destination untouched")
        vfs.note("rename", key=key)
        vfs.rename(tmp, dest)
        vfs.note("commit", key=key, detail="rename done: the name now refers to the new version")
        if self.opt["fsync_dir"]:
            vfs.note("fsync-dir")
            vfs.fsync_dir(DOCS)

    def _delete(self, key: str) -> None:
        path = self._path(key)
        if not self.vfs.exists(path):
            return
        self.vfs.note("unlink", key=key)
        self.vfs.unlink(path)
        self.vfs.note("commit", key=key, detail="unlink done")
        if self.opt["fsync_dir"]:
            self.vfs.fsync_dir(DOCS)
