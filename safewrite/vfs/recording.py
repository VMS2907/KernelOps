"""A VFS wrapper that records every mutating operation and can inject crashes.

The trace it produces is the input of the crash simulator.  Each entry stores
the *inode* it touched, not just the path.  Without that you could not express
the most famous crash bug of all: "the ``rename`` reached the disk but the data
written to the renamed file did not".

Crash injection is used in two ways:

* ``on_crash`` raises :class:`CrashNow` (simulation inside one process), or
* ``on_crash`` calls ``os._exit`` (a *real* process death, used by the CLI's
  ``crash`` command and the torture tests).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set

from .base import VFS, CrashNow, join, norm, parent

DATA_OPS = ("write", "trunc")
NAMESPACE_OPS = ("create", "rename", "unlink", "mkdir")
BARRIER_OPS = ("fsync", "fsync_dir")


@dataclass
class Op:
    idx: int
    kind: str                      # create write trunc fsync rename unlink mkdir fsync_dir note
    path: str = ""                 # path (namespace ops) or current name of the inode (data ops)
    dst: str = ""                  # rename destination
    ino: int = 0                   # inode the op touches
    offset: int = 0
    data: bytes = b""
    size: int = 0                  # trunc target size
    label: str = ""                # note label
    info: Dict[str, object] = field(default_factory=dict)
    replaced: int = 0              # inode unlinked by rename-over / unlink
    thread: str = ""

    @property
    def mutating(self) -> bool:
        return self.kind != "note"

    def describe(self) -> str:
        k = self.kind
        if k == "write":
            return f"pwrite({self.path}, off={self.offset}, len={len(self.data)})"
        if k == "trunc":
            return f"ftruncate({self.path}, {self.size})"
        if k == "fsync":
            return f"fsync({self.path})"
        if k == "fsync_dir":
            return f"fsync(dir {self.path or '/'})"
        if k == "rename":
            return f"rename({self.path} -> {self.dst})"
        if k == "create":
            return f"creat({self.path})"
        if k == "unlink":
            return f"unlink({self.path})"
        if k == "mkdir":
            return f"mkdir({self.path})"
        return f"# {self.label}"

    def to_json(self, with_data: bool = False) -> Dict[str, object]:
        d: Dict[str, object] = {
            "idx": self.idx, "kind": self.kind, "path": self.path, "ino": self.ino,
            "text": self.describe(),
        }
        if self.kind == "rename":
            d["dst"] = self.dst
        if self.kind == "write":
            d["offset"] = self.offset
            d["len"] = len(self.data)
            if with_data:
                d["data"] = self.data.hex()
        if self.kind == "trunc":
            d["size"] = self.size
        if self.kind == "note":
            d["label"] = self.label
            if self.info:
                d["info"] = {k: v for k, v in self.info.items()
                             if isinstance(v, (str, int, float, bool))}
        return d


@dataclass
class BaseImage:
    """The durable disk state before the recorded workload started."""
    names: Dict[str, int]          # path -> inode
    contents: Dict[int, bytes]     # inode -> bytes
    dirs: List[str]


class RecordingVFS(VFS):
    def __init__(self, inner: VFS, crash_at: Optional[int] = None,
                 crash_at_label: Optional[str] = None,
                 on_crash: Optional[Callable[[Op], None]] = None,
                 partial_write: Optional[float] = None) -> None:
        """
        :param crash_at: crash *before* executing trace entry ``crash_at``.
        :param crash_at_label: crash when a ``note`` with this label is reached.
        :param on_crash: what "crash" means (default: raise :class:`CrashNow`).
        :param partial_write: if the crashing op is a write, first persist this
            fraction of its bytes (models a process killed inside ``write``).
        """
        super().__init__()
        self.inner = inner
        self.sector_size = inner.sector_size
        self.crash_at = crash_at
        self.crash_at_label = crash_at_label
        self.on_crash = on_crash
        self.partial_write = partial_write
        self._mu = threading.RLock()
        self.trace: List[Op] = []
        self.base: BaseImage = BaseImage({}, {}, [])
        self._names: Dict[str, int] = {}
        self._handles: Dict[int, int] = {}       # handle -> inode
        self._handle_paths: Dict[int, str] = {}  # handle -> path it was opened as
        self._next_ino = 1
        self.reset()

    # ------------------------------------------------------------- lifecycle
    def reset(self) -> None:
        """Forget the trace and treat the inner file system's current state as
        the durable base image (call after setting up a scenario)."""
        with self._mu:
            self.trace = []
            self._names = {}
            contents: Dict[int, bytes] = {}
            for path in self.inner.walk_files():
                ino = self._alloc_ino()
                self._names[path] = ino
                contents[ino] = self.inner.read_file(path) or b""
            self.base = BaseImage(dict(self._names), contents, self.inner.walk_dirs())
            # Handles opened before the reset (an engine that keeps its files
            # open) are re-attached to the fresh inode numbers.
            for h, path in self._handle_paths.items():
                if path in self._names:
                    self._handles[h] = self._names[path]

    def _alloc_ino(self) -> int:
        ino = self._next_ino
        self._next_ino += 1
        return ino

    def _name_of(self, ino: int) -> str:
        for p, i in self._names.items():
            if i == ino:
                return p
        return f"<inode {ino}>"

    def _emit(self, op: Op, handle: int = 0) -> Op:
        """Append ``op`` to the trace, crashing first if this is the crash point."""
        op.idx = len(self.trace)
        op.thread = threading.current_thread().name
        crash = (self.crash_at is not None and op.idx >= self.crash_at) or (
            self.crash_at_label is not None and op.kind == "note"
            and op.label == self.crash_at_label)
        if crash:
            if op.kind == "write" and self.partial_write:
                cut = int(len(op.data) * self.partial_write)
                if cut:  # the process died part-way through write(2)
                    self.inner.pwrite(handle, op.offset, op.data[:cut])
            self._crash(op)
        self.trace.append(op)
        return op

    def _crash(self, op: Op) -> None:
        self.crash_at = None
        self.crash_at_label = None
        if self.on_crash is not None:
            self.on_crash(op)
        raise CrashNow(op.describe())

    # -------------------------------------------------------------------- data
    def open(self, path: str, create: bool = False, truncate: bool = False) -> int:
        path = norm(path)
        with self._mu:
            existed = self.inner.exists(path)
            if not existed and create:
                ino = self._alloc_ino()
                self._emit(Op(0, "create", path=path, ino=ino))
                self._names[path] = ino
            h = self.inner.open(path, create=create, truncate=False)
            ino = self._names.get(path)
            if ino is None:  # file created behind our back; adopt it
                ino = self._alloc_ino()
                self._names[path] = ino
            self._handles[h] = ino
            self._handle_paths[h] = path
            if truncate and existed:
                self._emit(Op(0, "trunc", path=path, ino=ino, size=0))
                self.inner.ftruncate(h, 0)
            return h

    def close(self, h: int) -> None:
        with self._mu:
            self._handles.pop(h, None)
            self._handle_paths.pop(h, None)
            self.inner.close(h)

    def pread(self, h: int, offset: int, n: int) -> bytes:
        return self.inner.pread(h, offset, n)

    def pwrite(self, h: int, offset: int, data: bytes) -> None:
        with self._mu:
            ino = self._handles[h]
            self._emit(Op(0, "write", path=self._name_of(ino), ino=ino, offset=offset,
                          data=bytes(data)), handle=h)
            self.inner.pwrite(h, offset, data)
        self.stats.writes += 1
        self.stats.bytes_written += len(data)

    def size(self, h: int) -> int:
        return self.inner.size(h)

    def ftruncate(self, h: int, size: int) -> None:
        with self._mu:
            ino = self._handles[h]
            self._emit(Op(0, "trunc", path=self._name_of(ino), ino=ino, size=size))
            self.inner.ftruncate(h, size)

    def fsync(self, h: int) -> None:
        with self._mu:
            ino = self._handles[h]
            self._emit(Op(0, "fsync", path=self._name_of(ino), ino=ino))
            self.inner.fsync(h)
        self.stats.fsyncs += 1

    # --------------------------------------------------------------- namespace
    def exists(self, path: str) -> bool:
        return self.inner.exists(path)

    def isdir(self, path: str) -> bool:
        return self.inner.isdir(path)

    def listdir(self, path: str) -> List[str]:
        return self.inner.listdir(path)

    def mkdir(self, path: str) -> None:
        path = norm(path)
        with self._mu:
            self._emit(Op(0, "mkdir", path=path))
            self.inner.mkdir(path)

    def rename(self, src: str, dst: str) -> None:
        src, dst = norm(src), norm(dst)
        with self._mu:
            ino = self._names.get(src, 0)
            replaced = self._names.get(dst, 0)
            self._emit(Op(0, "rename", path=src, dst=dst, ino=ino, replaced=replaced))
            self.inner.rename(src, dst)
            self._names.pop(src, None)
            self._names[dst] = ino
        self.stats.renames += 1

    def unlink(self, path: str) -> None:
        path = norm(path)
        with self._mu:
            ino = self._names.get(path, 0)
            self._emit(Op(0, "unlink", path=path, ino=ino, replaced=ino))
            self.inner.unlink(path)
            self._names.pop(path, None)

    def fsync_dir(self, path: str) -> None:
        path = norm(path)
        with self._mu:
            self._emit(Op(0, "fsync_dir", path=path))
            self.inner.fsync_dir(path)
        self.stats.dir_fsyncs += 1

    def note(self, label: str, **info) -> None:
        with self._mu:
            self._emit(Op(0, "note", label=label, info=dict(info)))

    # ----------------------------------------------------------------- queries
    def mutating_ops(self) -> List[Op]:
        return [op for op in self.trace if op.mutating]

    def touched_dirs(self) -> Set[str]:
        out = set()
        for op in self.trace:
            if op.kind in ("create", "unlink", "mkdir"):
                out.add(parent(op.path))
            elif op.kind == "rename":
                out.add(parent(op.path))
                out.add(parent(op.dst))
        return out


__all__ = ["Op", "BaseImage", "RecordingVFS", "DATA_OPS", "NAMESPACE_OPS",
           "BARRIER_OPS", "CrashNow", "join"]
