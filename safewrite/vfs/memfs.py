"""An in-memory file system with real inode semantics.

The crash simulator rebuilds thousands of post-crash disk images and runs an
engine's recovery on each one.  Doing that on a real disk would be slow, so
this class models just enough of a POSIX file system: directories, names that
point to inodes, and handles that keep pointing to their inode after a
``rename``, exactly like a file descriptor does.

``tests/test_vfs.py`` checks that it behaves identically to :class:`OsVFS`.
"""

from __future__ import annotations

import threading
from typing import Dict, List, Optional

from .base import VFS, join, norm, parent


class Inode:
    __slots__ = ("ino", "data")

    def __init__(self, ino: int, data: bytes = b"") -> None:
        self.ino = ino
        self.data = bytearray(data)


class MemVFS(VFS):
    def __init__(self, files: Optional[Dict[str, bytes]] = None,
                 dirs: Optional[List[str]] = None) -> None:
        super().__init__()
        self._mu = threading.RLock()
        self._dirs = {""}
        self._names: Dict[str, Inode] = {}
        self._handles: Dict[int, Inode] = {}
        self._next_ino = 1
        self._next_h = 1
        for d in sorted(dirs or []):
            self.makedirs(d)
        for path, data in sorted((files or {}).items()):
            self.makedirs(parent(path))
            self._names[norm(path)] = self._new_inode(data)

    def _new_inode(self, data: bytes = b"") -> Inode:
        node = Inode(self._next_ino, data)
        self._next_ino += 1
        return node

    def _need_dir(self, path: str) -> None:
        if path not in self._dirs:
            raise FileNotFoundError(f"no such directory: {path!r}")

    # -------------------------------------------------------------------- data
    def open(self, path: str, create: bool = False, truncate: bool = False) -> int:
        path = norm(path)
        with self._mu:
            node = self._names.get(path)
            if node is None:
                if path in self._dirs:
                    raise IsADirectoryError(path)
                if not create:
                    raise FileNotFoundError(path)
                self._need_dir(parent(path))
                node = self._new_inode()
                self._names[path] = node
                self.stats.creates += 1
            if truncate:
                del node.data[:]
                self.stats.truncates += 1
            h = self._next_h
            self._next_h += 1
            self._handles[h] = node
            return h

    def close(self, h: int) -> None:
        with self._mu:
            del self._handles[h]

    def _node(self, h: int) -> Inode:
        try:
            return self._handles[h]
        except KeyError:
            raise ValueError(f"bad handle {h}") from None

    def pread(self, h: int, offset: int, n: int) -> bytes:
        with self._mu:
            data = bytes(self._node(h).data[offset:offset + n])
        self.stats.reads += 1
        self.stats.bytes_read += len(data)
        return data

    def pwrite(self, h: int, offset: int, data: bytes) -> None:
        with self._mu:
            buf = self._node(h).data
            if offset > len(buf):
                buf.extend(b"\0" * (offset - len(buf)))
            buf[offset:offset + len(data)] = data
        self.stats.writes += 1
        self.stats.bytes_written += len(data)

    def size(self, h: int) -> int:
        with self._mu:
            return len(self._node(h).data)

    def ftruncate(self, h: int, size: int) -> None:
        with self._mu:
            buf = self._node(h).data
            if size < len(buf):
                del buf[size:]
            else:
                buf.extend(b"\0" * (size - len(buf)))
        self.stats.truncates += 1

    def fsync(self, h: int) -> None:
        self._node(h)
        self.stats.fsyncs += 1

    # --------------------------------------------------------------- namespace
    def exists(self, path: str) -> bool:
        path = norm(path)
        with self._mu:
            return path in self._names or path in self._dirs

    def isdir(self, path: str) -> bool:
        with self._mu:
            return norm(path) in self._dirs

    def listdir(self, path: str) -> List[str]:
        path = norm(path)
        with self._mu:
            self._need_dir(path)
            out = set()
            for p in list(self._names) + list(self._dirs):
                if p and parent(p) == path:
                    out.add(p.rsplit("/", 1)[-1])
            return sorted(out)

    def mkdir(self, path: str) -> None:
        path = norm(path)
        with self._mu:
            if path in self._dirs or path in self._names:
                raise FileExistsError(path)
            self._need_dir(parent(path))
            self._dirs.add(path)

    def rename(self, src: str, dst: str) -> None:
        src, dst = norm(src), norm(dst)
        with self._mu:
            node = self._names.get(src)
            if node is None:
                raise FileNotFoundError(src)
            self._need_dir(parent(dst))
            if dst in self._dirs:
                raise IsADirectoryError(dst)
            del self._names[src]
            self._names[dst] = node
        self.stats.renames += 1

    def unlink(self, path: str) -> None:
        path = norm(path)
        with self._mu:
            if path not in self._names:
                raise FileNotFoundError(path)
            del self._names[path]
        self.stats.unlinks += 1

    def fsync_dir(self, path: str) -> None:
        with self._mu:
            self._need_dir(norm(path))
        self.stats.dir_fsyncs += 1

    # ----------------------------------------------------------------- helpers
    def clone(self) -> "MemVFS":
        """A deep copy (used to fork a scenario's base image)."""
        with self._mu:
            return MemVFS({p: bytes(n.data) for p, n in self._names.items()},
                          sorted(self._dirs - {""}))

    @classmethod
    def from_vfs(cls, other: VFS) -> "MemVFS":
        return cls(other.snapshot(), other.walk_dirs())

    def dirs(self) -> List[str]:
        with self._mu:
            return sorted(self._dirs - {""})


def copy_tree(src: VFS, dst: VFS, path: str = "") -> None:
    """Copy every directory and file from ``src`` into ``dst``."""
    for name in src.listdir(path):
        child = join(path, name)
        if src.isdir(child):
            if not dst.exists(child):
                dst.mkdir(child)
            copy_tree(src, dst, child)
        else:
            data = src.read_file(child) or b""
            h = dst.open(child, create=True, truncate=True)
            try:
                dst.pwrite(h, 0, data)
            finally:
                dst.close(h)
