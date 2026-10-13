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

**Multi-writer mode** (``open(path, multi_writer=True)``, shadow engine only):
several processes may save at once.  Each holds the session lock *shared* and
names its temp files with its own writer id; renames are serialised on the
``COMMIT`` lock; recovery sweeps temp files only while it holds the session
lock *exclusively*, so it can never delete a save that is still in flight.
"""

from __future__ import annotations

import json
import os
import threading
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, List, Optional

from .concurrency import FileLock, ProcessLock, StoreLocked
from .engines import Engine, RecoveryReport, Version, make, parse_spec
from .errors import CommitAborted, StoreError, VisibleNotDurable, WriteConflict
from .format import sha256
from .vfs.base import VFS
from .vfs.osfs import OsVFS

META = "SAFEWRITE.json"
LOCK = "LOCK"
#: Multi-writer stores serialise their commit points on this lock file.
COMMIT = "COMMIT"
_UNSET = object()


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
                 report: RecoveryReport, multi_writer: bool = False) -> None:
        self.path = path
        self.spec = spec
        self.engine = engine
        self.recovery = report
        self.multi_writer = multi_writer
        self._lock = lock
        #: Serialises transactions inside this process (isolation).  Plain
        #: reads do not take it: the cow engine serves them from MVCC snapshots.
        self._tx_lock = threading.RLock()
        #: Set when a write or fsync fails.  After a failed fsync the kernel may
        #: already have dropped the dirty pages (PostgreSQL's 2018 "fsyncgate"),
        #: so retrying is unsafe: the only correct move is to reopen = recover.
        self.failed: Optional[str] = None

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
             multi_writer: bool = False, wait: Optional[float] = None, **options: Any) -> "SafeStore":
        """Open (and recover) a store.  ``multi_writer``: share it with other
        writer processes (shadow engine).  ``wait``: seconds to wait for the
        session lock instead of failing at once."""
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
        if multi_writer:
            return cls._open_multi_writer(path, spec, {**meta.get("options", {}), **options},
                                          timeout=10.0 if wait is None else wait)
        lock = cls._lock_for(path, wait)
        try:
            eng = make(spec, OsVFS(path), **{**meta.get("options", {}), **options})
            report = eng.open()
        except BaseException:
            lock.release()
            raise
        return cls(path, spec, eng, lock, report)

    @classmethod
    def _open_multi_writer(cls, path: str, spec: str, options: Dict[str, Any],
                           timeout: float = 10.0) -> "SafeStore":
        if parse_spec(spec)[0].name != "shadow":
            raise StoreError(f"multi-writer mode needs the shadow engine; {spec!r} keeps per-process "
                             "state in memory (log position, catalog), so it allows one writer process")
        lock = ProcessLock(os.path.join(path, LOCK))
        eng = make(spec, OsVFS(path), **options)
        eng.writer_id = f"w{os.getpid()}-{os.urandom(2).hex()}"    # type: ignore[attr-defined]
        eng.commit_lock = FileLock(os.path.join(path, COMMIT))     # type: ignore[attr-defined]
        try:
            try:
                # Alone?  Then every temp file is an orphan of a dead writer, and an
                # exclusive (sweeping) recovery is safe.  Then share the store.
                lock.acquire(blocking=False)
                report = eng.open()
                lock.downgrade()
            except StoreLocked:
                # Other sessions are live: their temp files may be saves in flight.
                lock.acquire(shared=True, timeout=timeout)
                eng.live_peers = True                               # type: ignore[attr-defined]
                report = eng.open()
        except StoreLocked as e:
            raise StoreError(f"{e} (a single-writer session excludes multi-writer sessions)") from None
        except BaseException:
            lock.release()
            raise
        return cls(path, spec, eng, lock, report, multi_writer=True)

    @staticmethod
    def _lock_for(path: str, wait: Optional[float] = None) -> ProcessLock:
        lock = ProcessLock(os.path.join(path, LOCK))
        try:
            lock.acquire(timeout=wait)
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

    def _guarded(self, fn: Callable[[], None]) -> None:
        """Run an engine write under the store's error contract.

        * :class:`CommitAborted`: nothing changed and nothing is poisoned, so
          the store stays writable (fix the cause and retry).
        * :class:`VisibleNotDurable`: the new state is visible but durability is
          unknown; retrying fsync is unsafe (PostgreSQL's 2018 "fsyncgate"), so
          the store refuses writes until it is reopened (= recovered).
        * any other ``OSError`` (engines without the split contract): same as
          above, read-only until reopened.
        """
        if self.failed:
            raise StoreError(f"an earlier write failed ({self.failed}); reopen the store to recover "
                             "before writing again")
        try:
            fn()
        except CommitAborted:
            raise
        except VisibleNotDurable as e:
            self.failed = f"{e.phase}: {e.strerror or type(e).__name__}"
            raise
        except OSError as e:
            self.failed = f"{type(e).__name__}: {e}"
            raise StoreError(f"write failed, store is now read-only until reopened: {self.failed}") from e

    def _commit(self, changes: Dict[str, Optional[bytes]]) -> None:
        self._guarded(lambda: self.engine.commit(changes))

    def put(self, key: str, value: bytes, expect: Any = _UNSET) -> None:
        """Save ``key``.  With ``expect`` (shadow engine) the save is conditional:
        ``expect`` is the sha256 of the payload this edit is based on (``None``
        = the document must not exist); if another writer committed meanwhile,
        :class:`~safewrite.errors.WriteConflict` is raised and nothing changes."""
        if expect is _UNSET:
            self._commit({key: bytes(value)})
            return
        cond = getattr(self.engine, "prepare", None) and getattr(self.engine, "put")
        if not cond:
            raise StoreError(f"conditional put needs the shadow engine, not {self.spec!r}")
        self._guarded(lambda: cond(key, bytes(value), expect=expect))

    def delete(self, key: str) -> None:
        self._commit({key: None})

    def keys(self) -> List[str]:
        return self.engine.keys()

    def history(self, key: str) -> List[Version]:
        return self.engine.history(key)

    def verify(self) -> List[str]:
        return self.engine.verify()

    @contextmanager
    def transaction(self) -> Iterator[Transaction]:
        """All-or-nothing multi-document update (if the engine supports it).

        The store's transaction lock is held from the first read to the commit,
        so a read-modify-write inside a transaction can never lose another
        thread's update (serializable isolation within the process; other
        processes are already excluded by the flock).
        """
        with self._tx_lock:
            tx = Transaction(self)
            yield tx
            if tx.changes:
                self._commit(tx.changes)

    def update(self, key: str, fn: Callable[[Optional[bytes]], Optional[bytes]]) -> Optional[bytes]:
        """Atomic read-modify-write: ``new = fn(old)`` (``None`` deletes).

        In multi-writer mode the in-process lock cannot see other processes, so
        the write is conditional on what was read, and retried on conflict
        (optimistic concurrency): no update is ever lost.
        """
        if self.multi_writer:
            for _ in range(1000):
                with self._tx_lock:
                    old = self.get(key)
                    new = fn(old)
                    if new is None:
                        self.delete(key)
                        return None
                    try:
                        self.put(key, new, expect=None if old is None else sha256(old))
                        return new
                    except WriteConflict:
                        continue
            raise StoreError(f"update of {key!r} kept conflicting with other writers; giving up")
        with self.transaction() as tx:
            new = fn(tx.get(key))
            if new is None:
                tx.delete(key)
            else:
                tx.put(key, new)
        return new

    def info(self) -> Dict[str, Any]:
        return {
            "path": os.path.abspath(self.path),
            "engine": self.spec,
            "title": self.engine.title,
            "commit_point": self.engine.commit_point,
            "recovers_to": self.engine.recovers_to,
            "atomic_tx": self.engine.atomic_tx,
            "multi_writer": self.multi_writer,
            "documents": len(self.keys()),
            "recovery": self.recovery.to_json(),
        }
