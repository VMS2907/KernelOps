"""SafeStore: the public, real-disk API.

Opening a store always does three things, in this order:

1. take the store's **process lock**, so no other process can write concurrently
   (kernel ``flock``, released automatically if this process dies);
2. read ``SAFEWRITE.json`` to learn which engine formatted the store;
3. run that engine's **recovery**.  Every open is treated as a possible
   restart after a crash.

The marker file is the last thing written when a store is created, with the
same temp + fsync + rename protocol as the ``shadow`` engine.  A crash during
creation therefore leaves a directory that is clearly "not a store yet",
never a half-made one.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

from .concurrency import ProcessLock, StoreLocked
from .engines import Engine, RecoveryReport, Version, make, parse_spec
from .vfs.base import VFS
from .vfs.osfs import OsVFS

META = "SAFEWRITE.json"
LOCK = "LOCK"


class StoreError(Exception):
    pass


def atomic_write(vfs: VFS, path: str, data: bytes) -> None:
    """Write a small file all-or-nothing (temp, fsync, rename, fsync dir)."""
    tmp = path + "~new"
    h = vfs.open(tmp, create=True, truncate=True)
    try:
        vfs.pwrite(h, 0, data)
        vfs.fsync(h)
    finally:
        vfs.close(h)
    vfs.rename(tmp, path)
    vfs.fsync_dir("")


class Transaction:
    """Buffers changes; ``commit`` applies them in one engine commit."""

    def __init__(self, store: "SafeStore") -> None:
        self._store = store
        self.changes: Dict[str, Optional[bytes]] = {}

    def put(self, key: str, value: bytes) -> None:
        self.changes[key] = bytes(value)

    def delete(self, key: str) -> None:
        self.changes[key] = None

    def get(self, key: str) -> Optional[bytes]:
        if key in self.changes:
            return self.changes[key]
        return self._store.get(key)


class SafeStore:
    def __init__(self, path: str, spec: str, engine: Engine, lock: ProcessLock,
                 report: RecoveryReport) -> None:
        self.path = path
        self.spec = spec
        self.engine = engine
        self.recovery = report
        self._lock = lock

    # ------------------------------------------------------------- opening
    @classmethod
    def create(cls, path: str, engine: str = "cow", **options: Any) -> "SafeStore":
        parse_spec(engine)
        os.makedirs(path, exist_ok=True)
        if os.path.exists(os.path.join(path, META)):
            raise StoreError(f"{path} is already a SafeWrite store")
        lock = cls._lock_for(path)
        try:
            vfs = OsVFS(path)
            leftovers = [n for n in vfs.listdir("") if n != LOCK]
            if leftovers:
                raise StoreError(f"{path} is not empty ({', '.join(leftovers[:5])}); "
                                 "refusing to create a store over existing files")
            eng = make(engine, vfs, **options)
            eng.format()
            meta = {"format": 1, "engine": engine, "options": options}
            atomic_write(vfs, META, json.dumps(meta, indent=2).encode())
            report = eng.open()
        except BaseException:
            lock.release()
            raise
        return cls(path, engine, eng, lock, report)

    @classmethod
    def open(cls, path: str, engine: Optional[str] = None, create: bool = False,
             **options: Any) -> "SafeStore":
        meta_path = os.path.join(path, META)
        if not os.path.exists(meta_path):
            if create:
                return cls.create(path, engine or "cow", **options)
            raise StoreError(f"{path} is not a SafeWrite store (no {META}); use init/create first")
        with open(meta_path, "rb") as f:
            meta = json.loads(f.read().decode())
        spec = meta["engine"]
        if engine and engine != spec:
            raise StoreError(f"{path} was created with engine {spec!r}, not {engine!r}")
        lock = cls._lock_for(path)
        try:
            eng = make(spec, OsVFS(path), **{**meta.get("options", {}), **options})
            report = eng.open()
        except BaseException:
            lock.release()
            raise
        return cls(path, spec, eng, lock, report)

    @staticmethod
    def _lock_for(path: str) -> ProcessLock:
        lock = ProcessLock(os.path.join(path, LOCK))
        try:
            lock.acquire()
        except StoreLocked as e:
            raise StoreError(str(e)) from None
        return lock

    def close(self) -> None:
        try:
            self.engine.close()
        finally:
            self._lock.release()

    def __enter__(self) -> "SafeStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---------------------------------------------------------------- data
    def get(self, key: str) -> Optional[bytes]:
        return self.engine.get(key)

    def put(self, key: str, value: bytes) -> None:
        self.engine.commit({key: bytes(value)})

    def delete(self, key: str) -> None:
        self.engine.commit({key: None})

    def keys(self) -> List[str]:
        return self.engine.keys()

    def history(self, key: str) -> List[Version]:
        return self.engine.history(key)

    def verify(self) -> List[str]:
        return self.engine.verify()

    @contextmanager
    def transaction(self) -> Iterator[Transaction]:
        """All-or-nothing multi-document update (if the engine supports it)."""
        tx = Transaction(self)
        yield tx
        if tx.changes:
            self.engine.commit(tx.changes)

    def info(self) -> Dict[str, Any]:
        return {
            "path": os.path.abspath(self.path),
            "engine": self.spec,
            "title": self.engine.title,
            "commit_point": self.engine.commit_point,
            "recovers_to": self.engine.recovers_to,
            "atomic_tx": self.engine.atomic_tx,
            "documents": len(self.keys()),
            "recovery": self.recovery.to_json(),
        }
