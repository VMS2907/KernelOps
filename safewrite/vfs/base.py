"""The VFS interface: the only door between a storage engine and the disk.

Engines never call ``os.*`` directly.  Every byte they persist goes through one
of the methods below.  That single design rule is what lets the *same* engine
code run against

* a real file system with real system calls  (:class:`~safewrite.vfs.osfs.OsVFS`),
* an in-memory file system used to replay crash images quickly
  (:class:`~safewrite.vfs.memfs.MemVFS`), and
* a recorder that logs every operation so the crash simulator can compute which
  disk states a power failure could leave behind
  (:class:`~safewrite.vfs.recording.RecordingVFS`).

The interface deliberately mirrors POSIX: *data* operations act on an open
handle (an inode, like a file descriptor), while *namespace* operations act on
path names (directory entries).  That split matters for crash consistency:
``fsync(file)`` makes a file's *data* durable but not the directory entry that
names it, which needs ``fsync(directory)``.

Paths are relative to the store root and always use ``/``.  ``""`` is the root.
"""

from __future__ import annotations

import posixpath
from abc import ABC, abstractmethod
from typing import Dict, List, Optional


class CrashNow(BaseException):
    """Raised by an instrumented VFS to simulate the machine dying *right now*.

    It derives from ``BaseException`` so that ordinary ``except Exception``
    blocks inside an engine cannot accidentally swallow a simulated crash.
    """


def norm(path: str) -> str:
    """Normalise a store-relative path (``"./a//b"`` -> ``"a/b"``)."""
    path = path.replace("\\", "/")
    p = posixpath.normpath(path) if path else ""
    if p in (".", "/"):
        return ""
    if p.startswith("../") or p == ".." or p.startswith("/"):
        raise ValueError(f"path escapes the store root: {path!r}")
    return p


def parent(path: str) -> str:
    """Directory containing ``path`` (``""`` for the root)."""
    return posixpath.dirname(norm(path))


def join(*parts: str) -> str:
    return norm(posixpath.join(*[p for p in parts if p]))


class IOStats:
    """Counters used by the benchmarks (write amplification, fsyncs/commit...)."""

    __slots__ = ("writes", "bytes_written", "reads", "bytes_read", "fsyncs",
                 "dir_fsyncs", "renames", "unlinks", "creates", "truncates")

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        for name in self.__slots__:
            setattr(self, name, 0)

    def snapshot(self) -> Dict[str, int]:
        return {name: getattr(self, name) for name in self.__slots__}


class VFS(ABC):
    """Abstract file system used by every engine."""

    #: Granularity at which the simulated disk may tear a write.
    sector_size: int = 512

    def __init__(self) -> None:
        self.stats = IOStats()

    # ------------------------------------------------------------------ data
    @abstractmethod
    def open(self, path: str, create: bool = False, truncate: bool = False) -> int:
        """Open ``path`` read-write and return a handle.

        ``create`` behaves like ``O_CREAT``; ``truncate`` like ``O_TRUNC``.
        Raises ``FileNotFoundError`` if the file is missing and ``create`` is
        false.
        """

    @abstractmethod
    def close(self, h: int) -> None: ...

    @abstractmethod
    def pread(self, h: int, offset: int, n: int) -> bytes:
        """Read up to ``n`` bytes at ``offset`` (short only at end of file)."""

    @abstractmethod
    def pwrite(self, h: int, offset: int, data: bytes) -> None:
        """Write all of ``data`` at ``offset`` (extends the file if needed)."""

    @abstractmethod
    def size(self, h: int) -> int: ...

    @abstractmethod
    def ftruncate(self, h: int, size: int) -> None: ...

    @abstractmethod
    def fsync(self, h: int) -> None:
        """Make this file's data and size durable (a write barrier)."""

    # ------------------------------------------------------------- namespace
    @abstractmethod
    def exists(self, path: str) -> bool: ...

    @abstractmethod
    def isdir(self, path: str) -> bool: ...

    @abstractmethod
    def listdir(self, path: str) -> List[str]: ...

    @abstractmethod
    def mkdir(self, path: str) -> None: ...

    @abstractmethod
    def rename(self, src: str, dst: str) -> None:
        """Atomically make ``dst`` name the file ``src`` named (replacing it)."""

    @abstractmethod
    def unlink(self, path: str) -> None: ...

    @abstractmethod
    def fsync_dir(self, path: str) -> None:
        """Make directory entries (creates, renames, unlinks) in ``path`` durable."""

    # --------------------------------------------------------- instrumentation
    def note(self, label: str, **info) -> None:
        """Mark a protocol phase (e.g. ``"commit-point"``).  No-op on real disks.

        The recorder stores notes in the trace so the UI can show which line of
        the protocol each system call belongs to, and so a crash can be
        injected "at" a named phase.
        """

    # ----------------------------------------------------------- convenience
    def read_file(self, path: str) -> Optional[bytes]:
        """Whole-file read; ``None`` if the file does not exist."""
        if not self.exists(path) or self.isdir(path):
            return None
        h = self.open(path)
        try:
            return self.pread(h, 0, self.size(h))
        finally:
            self.close(h)

    def makedirs(self, path: str) -> None:
        path = norm(path)
        if not path or self.exists(path):
            return
        self.makedirs(parent(path))
        self.mkdir(path)

    def walk_files(self, path: str = "") -> List[str]:
        """Every regular file below ``path`` (sorted, store-relative)."""
        out: List[str] = []
        for name in self.listdir(path):
            child = join(path, name)
            if self.isdir(child):
                out.extend(self.walk_files(child))
            else:
                out.append(child)
        return sorted(out)

    def walk_dirs(self, path: str = "") -> List[str]:
        out: List[str] = []
        for name in self.listdir(path):
            child = join(path, name)
            if self.isdir(child):
                out.append(child)
                out.extend(self.walk_dirs(child))
        return sorted(out)

    def snapshot(self) -> Dict[str, bytes]:
        """Contents of every file, used to build crash-simulation base images."""
        return {p: self.read_file(p) or b"" for p in self.walk_files()}
