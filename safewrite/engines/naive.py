"""The baseline: overwrite the file where it lives.  Unsafe on purpose.

This is what most programs do when they "save".  It exists so the crash
explorer and the torture test have something to catch, and so the demo can
show the corrupted mixture the problem statement warns about:

* ``inplace`` mode writes version B over version A, block by block.  A crash
  part-way leaves the first blocks of B followed by the tail of A: **a mixture**.
* ``truncate`` mode (``open(..., O_TRUNC)``, what ``open(path, "w")`` does)
  destroys A *before* B exists.  A crash leaves an empty or half-written file.

There is no header and no checksum, so the damage is also **silent**: ``get``
happily returns the garbage.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from ..concurrency import RWLock
from ..format import key_to_name, name_to_key
from ..vfs.base import join
from .base import Engine, RecoveryReport

DOCS = "docs"


class NaiveEngine(Engine):
    name = "naive"
    title = "Naive overwrite"
    technique = "Overwrite the document in place (what open(path, 'w') does)"
    commit_point = "none: the file is modified directly"
    recovers_to = "whatever bytes happened to reach the disk"
    atomic_tx = False
    safe = False
    OPTIONS = {"mode": "inplace", "chunk": 4096, "fsync": True}
    VARIANTS = {
        "trunc": {"options": {"mode": "truncate"},
                  "breaks": "O_TRUNC destroys version A before B is written: crash -> empty/partial file"},
    }

    def __init__(self, vfs, **options) -> None:
        super().__init__(vfs, **options)
        self._rw = RWLock()

    def format(self) -> None:
        self.vfs.makedirs(DOCS)

    def recover(self, report: RecoveryReport) -> None:
        self.log("info", "naive overwrite keeps no journal, temp file or checksum")
        self.log("warn", "nothing to check: damaged files cannot even be detected")

    def _path(self, key: str) -> str:
        return join(DOCS, key_to_name(key))

    def get(self, key: str) -> Optional[bytes]:
        with self._rw.read():
            return self.vfs.read_file(self._path(key))

    def keys(self) -> List[str]:
        return sorted(k for k in map(name_to_key, self.vfs.listdir(DOCS)) if k)

    def commit(self, changes: Dict[str, Optional[bytes]]) -> None:
        changes = self._check(changes)
        with self._rw.write():
            for key, value in changes.items():
                path = self._path(key)
                if value is None:
                    if self.vfs.exists(path):
                        self.vfs.unlink(path)
                    continue
                truncate = self.opt["mode"] == "truncate"
                self.vfs.note("overwrite", key=key, mode=self.opt["mode"])
                h = self.vfs.open(path, create=True, truncate=truncate)
                try:
                    chunk = self.opt["chunk"]
                    for off in range(0, len(value), chunk):
                        self.vfs.pwrite(h, off, value[off:off + chunk])
                    if not truncate and self.vfs.size(h) != len(value):
                        self.vfs.ftruncate(h, len(value))
                    if self.opt["fsync"]:
                        self.vfs.fsync(h)
                finally:
                    self.vfs.close(h)
            self.vfs.note("commit", detail="all chunks written")
