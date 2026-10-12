"""Real file system access with honest durability.

Every method maps to one (or a short loop of) real system calls, which you can
watch with::

    strace -f -e trace=openat,pwrite64,write,fsync,fdatasync,rename,unlink,ftruncate \\
        python -m safewrite put mystore inventory.csv --file b.csv

Platform notes (the "synchronization with storage" details that matter):

* **Linux/BSD:** ``fsync(fd)`` flushes the file's data *and* asks the drive to
  empty its volatile write cache.  A directory is fsynced by opening it with
  ``O_DIRECTORY`` and fsyncing that descriptor.  This makes a ``rename`` or a
  newly created name durable.
* **macOS:** plain ``fsync`` does *not* flush the drive cache.
  ``fcntl(F_FULLFSYNC)`` does, so we use it.
* **Windows:** ``os.fsync`` calls ``FlushFileBuffers``.  Directories cannot be
  opened for fsync.  NTFS journals its metadata, so ``fsync_dir`` is a no-op
  there.  Files must be opened ``O_BINARY`` or the C runtime would translate
  newlines.
"""

from __future__ import annotations

import os
import sys
import threading
from typing import Dict, List

from .base import VFS, norm

_O_BINARY = getattr(os, "O_BINARY", 0)
_O_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_HAS_PREAD = hasattr(os, "pread") and hasattr(os, "pwrite")

try:  # pragma: no cover - platform specific
    import fcntl
    _F_FULLFSYNC = getattr(fcntl, "F_FULLFSYNC", None) if sys.platform == "darwin" else None
except ImportError:  # Windows
    fcntl = None  # type: ignore[assignment]
    _F_FULLFSYNC = None


class OsVFS(VFS):
    """VFS backed by a real directory on a real file system."""

    def __init__(self, root: str, durable: bool = True) -> None:
        super().__init__()
        self.root = os.path.abspath(root)
        #: ``durable=False`` turns fsync into a no-op.  Never use this for real
        #: data.  It exists only to measure what fsync costs in the benchmarks.
        self.durable = durable
        self._fds: Dict[int, int] = {}
        self._locks: Dict[int, threading.Lock] = {}
        self._next = 1
        self._mu = threading.Lock()

    # ----------------------------------------------------------------- helpers
    def real(self, path: str) -> str:
        path = norm(path)
        return os.path.join(self.root, *path.split("/")) if path else self.root

    def _fd(self, h: int) -> int:
        try:
            return self._fds[h]
        except KeyError:
            raise ValueError(f"bad handle {h}") from None

    # -------------------------------------------------------------------- data
    def open(self, path: str, create: bool = False, truncate: bool = False) -> int:
        flags = os.O_RDWR | _O_BINARY | _O_CLOEXEC
        if create:
            flags |= os.O_CREAT
        if truncate:
            flags |= os.O_TRUNC
        existed = create and os.path.exists(self.real(path))
        fd = os.open(self.real(path), flags, 0o644)
        if create and not existed:
            self.stats.creates += 1
        if truncate:
            self.stats.truncates += 1
        with self._mu:
            h = self._next
            self._next += 1
            self._fds[h] = fd
            self._locks[h] = threading.Lock()
        return h

    def close(self, h: int) -> None:
        with self._mu:
            fd = self._fds.pop(h)
            self._locks.pop(h, None)
        os.close(fd)

    def pread(self, h: int, offset: int, n: int) -> bytes:
        fd = self._fd(h)
        chunks: List[bytes] = []
        remaining = n
        while remaining > 0:
            if _HAS_PREAD:
                chunk = os.pread(fd, remaining, offset)
            else:  # pragma: no cover - Windows
                with self._locks[h]:
                    os.lseek(fd, offset, os.SEEK_SET)
                    chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            offset += len(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        self.stats.reads += 1
        self.stats.bytes_read += len(data)
        return data

    def pwrite(self, h: int, offset: int, data: bytes) -> None:
        fd = self._fd(h)
        view = memoryview(data)
        while view:
            if _HAS_PREAD:
                written = os.pwrite(fd, view, offset)
            else:  # pragma: no cover - Windows
                with self._locks[h]:
                    os.lseek(fd, offset, os.SEEK_SET)
                    written = os.write(fd, view)
            view = view[written:]
            offset += written
        self.stats.writes += 1
        self.stats.bytes_written += len(data)

    def size(self, h: int) -> int:
        return os.fstat(self._fd(h)).st_size

    def ftruncate(self, h: int, size: int) -> None:
        os.ftruncate(self._fd(h), size)
        self.stats.truncates += 1

    def fsync(self, h: int) -> None:
        self.stats.fsyncs += 1
        if not self.durable:
            return
        fd = self._fd(h)
        if _F_FULLFSYNC is not None:  # pragma: no cover - macOS
            try:
                fcntl.fcntl(fd, _F_FULLFSYNC)
                return
            except OSError:
                pass
        os.fsync(fd)

    # --------------------------------------------------------------- namespace
    def exists(self, path: str) -> bool:
        return os.path.exists(self.real(path))

    def isdir(self, path: str) -> bool:
        return os.path.isdir(self.real(path))

    def listdir(self, path: str) -> List[str]:
        return sorted(os.listdir(self.real(path)))

    def mkdir(self, path: str) -> None:
        os.mkdir(self.real(path))

    def rename(self, src: str, dst: str) -> None:
        # os.replace = rename(2) on POSIX, MoveFileEx(REPLACE_EXISTING) on Windows.
        os.replace(self.real(src), self.real(dst))
        self.stats.renames += 1

    def unlink(self, path: str) -> None:
        os.unlink(self.real(path))
        self.stats.unlinks += 1

    def fsync_dir(self, path: str) -> None:
        self.stats.dir_fsyncs += 1
        if not self.durable or os.name == "nt":
            return
        fd = os.open(self.real(path), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
