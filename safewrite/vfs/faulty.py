"""A controlled fault adapter: a small or failing disk in front of any VFS.

Wraps a real (:class:`~safewrite.vfs.osfs.OsVFS`) or in-memory VFS and adds the
two kinds of trouble the live challenges need, without ever touching the
host's real free space or hardware:

* a **free-space budget**.  ``free_bytes`` is how much room the simulated
  device has left.  A write that would grow files past it writes what still
  fits (a short write, as the kernel does on a filling disk) and then fails with
  ``ENOSPC``.  Truncating, unlinking and renaming over a file give space back.
* **injected errors**.  ``fail("fsync_dir", errno.EIO)`` makes the next
  matching call fail once, e.g. a directory fsync reporting EIO right after a
  rename succeeded.

Every allocation, release and fault is appended to :attr:`events`, so a demo
can print the resource trace next to the engine's protocol.
"""

from __future__ import annotations

import errno as _errno
import os
from typing import Any, Dict, List, Optional

from .base import VFS, norm


class FaultyVFS(VFS):
    def __init__(self, inner: VFS, free_bytes: Optional[int] = None) -> None:
        super().__init__()
        self.inner = inner
        self.sector_size = inner.sector_size
        #: Bytes the simulated device can still allocate (``None`` = unlimited).
        self.free_bytes = free_bytes
        #: Resource and fault trace: dicts with kind, op, path, detail, free.
        self.events: List[Dict[str, Any]] = []
        self._faults: List[Dict[str, Any]] = []
        self._paths: Dict[int, str] = {}

    # ------------------------------------------------------------- controls
    def fail(self, op: str, err: int = _errno.EIO, skip: int = 0, path: Optional[str] = None) -> None:
        """Make the next call of ``op`` (after skipping ``skip`` matching calls)
        fail once with ``err``.  ``op`` is a VFS method name, e.g. ``"fsync_dir"``."""
        self._faults.append({"op": op, "errno": err, "skip": skip,
                             "path": None if path is None else norm(path)})

    def _event(self, kind: str, op: str, path: str, detail: str) -> None:
        self.events.append({"kind": kind, "op": op, "path": path, "detail": detail,
                            "free": self.free_bytes})

    def _maybe_fail(self, op: str, path: str) -> None:
        for f in self._faults:
            if f["op"] == op and (f["path"] is None or f["path"] == path):
                if f["skip"]:
                    f["skip"] -= 1
                    return
                self._faults.remove(f)
                name = _errno.errorcode.get(f["errno"], str(f["errno"]))
                self._event("fault", op, path, f"injected {name}")
                raise OSError(f["errno"], os.strerror(f["errno"]), path)

    def _size_of(self, path: str) -> int:
        if not self.inner.exists(path) or self.inner.isdir(path):
            return 0
        h = self.inner.open(path)
        try:
            return self.inner.size(h)
        finally:
            self.inner.close(h)

    def _release(self, op: str, path: str, n: int) -> None:
        if n and self.free_bytes is not None:
            self.free_bytes += n
            self._event("free", op, path, f"released {n:,} bytes")

    # -------------------------------------------------------------------- data
    def open(self, path: str, create: bool = False, truncate: bool = False) -> int:
        path = norm(path)
        self._maybe_fail("open", path)
        freed = self._size_of(path) if truncate else 0
        h = self.inner.open(path, create=create, truncate=truncate)
        self._paths[h] = path
        self._release("open(O_TRUNC)", path, freed)
        return h

    def close(self, h: int) -> None:
        self._paths.pop(h, None)
        self.inner.close(h)

    def pread(self, h: int, offset: int, n: int) -> bytes:
        return self.inner.pread(h, offset, n)

    def pwrite(self, h: int, offset: int, data: bytes) -> None:
        path = self._paths.get(h, "?")
        self._maybe_fail("pwrite", path)
        cur = self.inner.size(h)
        growth = max(0, offset + len(data) - cur)
        if self.free_bytes is not None and growth > self.free_bytes:
            fit = max(0, min(len(data), cur + self.free_bytes - offset))
            if fit:
                self.inner.pwrite(h, offset, data[:fit])
                self.free_bytes -= max(0, offset + fit - cur)
            self._event("enospc", "pwrite", path,
                        f"wanted {len(data):,} bytes at offset {offset:,}; {fit:,} fit, then the device was full")
            raise OSError(_errno.ENOSPC, os.strerror(_errno.ENOSPC), path)
        self.inner.pwrite(h, offset, data)
        if growth and self.free_bytes is not None:
            self.free_bytes -= growth
            self._event("alloc", "pwrite", path, f"allocated {growth:,} bytes")

    def size(self, h: int) -> int:
        return self.inner.size(h)

    def ftruncate(self, h: int, size: int) -> None:
        path = self._paths.get(h, "?")
        self._maybe_fail("ftruncate", path)
        cur = self.inner.size(h)
        self.inner.ftruncate(h, size)
        self._release("ftruncate", path, max(0, cur - size))

    def fsync(self, h: int) -> None:
        self._maybe_fail("fsync", self._paths.get(h, "?"))
        self.inner.fsync(h)

    # --------------------------------------------------------------- namespace
    def exists(self, path: str) -> bool:
        return self.inner.exists(path)

    def isdir(self, path: str) -> bool:
        return self.inner.isdir(path)

    def listdir(self, path: str) -> List[str]:
        return self.inner.listdir(path)

    def mkdir(self, path: str) -> None:
        self._maybe_fail("mkdir", norm(path))
        self.inner.mkdir(path)

    def rename(self, src: str, dst: str) -> None:
        src, dst = norm(src), norm(dst)
        self._maybe_fail("rename", src)
        freed = self._size_of(dst) if dst != src else 0
        self.inner.rename(src, dst)
        for h, p in self._paths.items():
            if p == src:
                self._paths[h] = dst
        self._release("rename-over", dst, freed)

    def unlink(self, path: str) -> None:
        path = norm(path)
        self._maybe_fail("unlink", path)
        freed = self._size_of(path)
        self.inner.unlink(path)
        self._release("unlink", path, freed)

    def fsync_dir(self, path: str) -> None:
        self._maybe_fail("fsync_dir", norm(path))
        self.inner.fsync_dir(path)

    def note(self, label: str, **info: Any) -> None:
        self.inner.note(label, **info)
