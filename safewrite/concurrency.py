"""Synchronization primitives.

Three different problems need three different tools:

``RWLock``
    Threads in one process.  Many readers or one writer.  Used by engines that
    update files *in place* (naive, undo), where a reader could otherwise
    observe a half-written document.

``FileLock`` / ``ProcessLock``
    Processes.  An advisory ``flock`` (``LockFileEx`` on Windows) on
    ``<store>/LOCK``, shared or exclusive.  The kernel releases it
    automatically when the holding process dies, *including* by ``kill -9``,
    so a crash can never leave a stale lock behind.  A plain "lock file that
    exists" would.  Multi-writer stores also serialise commits on
    ``<store>/COMMIT``.

``GroupCommit``
    Coordination, not just exclusion.  Concurrent committers queue their log
    records.  One *leader* writes the whole batch and issues a single
    ``fsync``.  The *followers* sleep on a condition variable until the batch
    they joined is durable.  N commits share 1 fsync.
"""

from __future__ import annotations

import os
import threading
import time
from contextlib import contextmanager
from typing import Callable, Iterator, List, Optional, Tuple

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]
try:  # Windows
    import msvcrt  # type: ignore[import-not-found]
except ImportError:
    msvcrt = None  # type: ignore[assignment]


class RWLock:
    """Writer-preferring readers/writer lock built on one condition variable."""

    def __init__(self) -> None:
        self._cond = threading.Condition(threading.Lock())
        self._readers = 0
        self._writer = False
        self._waiting_writers = 0

    @contextmanager
    def read(self) -> Iterator[None]:
        with self._cond:
            while self._writer or self._waiting_writers:
                self._cond.wait()
            self._readers += 1
        try:
            yield
        finally:
            with self._cond:
                self._readers -= 1
                if not self._readers:
                    self._cond.notify_all()

    @contextmanager
    def write(self) -> Iterator[None]:
        with self._cond:
            self._waiting_writers += 1
            while self._writer or self._readers:
                self._cond.wait()
            self._waiting_writers -= 1
            self._writer = True
        try:
            yield
        finally:
            with self._cond:
                self._writer = False
                self._cond.notify_all()


class StoreLocked(Exception):
    pass


if os.name == "nt":  # pragma: no cover - Windows
    import ctypes
    from ctypes import wintypes

    class _OVERLAPPED(ctypes.Structure):
        _fields_ = [("Internal", ctypes.c_void_p), ("InternalHigh", ctypes.c_void_p),
                    ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                    ("hEvent", wintypes.HANDLE)]

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _k32.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                wintypes.DWORD, ctypes.POINTER(_OVERLAPPED)]
    _k32.LockFileEx.restype = wintypes.BOOL
    _k32.UnlockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                  ctypes.POINTER(_OVERLAPPED)]
    _k32.UnlockFileEx.restype = wintypes.BOOL
    _LOCKFILE_FAIL_IMMEDIATELY, _LOCKFILE_EXCLUSIVE_LOCK = 0x1, 0x2
    _ERROR_LOCK_VIOLATION = 33

    def _overlapped() -> "_OVERLAPPED":
        ov = _OVERLAPPED()
        ov.OffsetHigh = 1        # lock byte 4 GiB: far past the file's real contents
        return ov

    def _os_lock(fd: int, shared: bool, blocking: bool) -> bool:
        flags = (0 if shared else _LOCKFILE_EXCLUSIVE_LOCK) | (0 if blocking else _LOCKFILE_FAIL_IMMEDIATELY)
        h = wintypes.HANDLE(msvcrt.get_osfhandle(fd))
        if _k32.LockFileEx(h, flags, 0, 1, 0, ctypes.byref(_overlapped())):
            return True
        err = ctypes.get_last_error()
        if err == _ERROR_LOCK_VIOLATION and not blocking:
            return False
        raise ctypes.WinError(err)

    def _os_unlock(fd: int) -> None:
        h = wintypes.HANDLE(msvcrt.get_osfhandle(fd))
        if not _k32.UnlockFileEx(h, 0, 1, 0, ctypes.byref(_overlapped())):
            raise ctypes.WinError(ctypes.get_last_error())

    def _os_downgrade(fd: int) -> None:
        # A handle may add a shared lock over its own exclusive one; the first
        # unlock then drops the exclusive lock, so nobody can slip in between.
        if _os_lock(fd, shared=True, blocking=False):
            _os_unlock(fd)
            return
        _os_unlock(fd)                      # fallback: release, then wait for shared
        _os_lock(fd, shared=True, blocking=True)
else:
    def _os_lock(fd: int, shared: bool, blocking: bool) -> bool:
        flags = (fcntl.LOCK_SH if shared else fcntl.LOCK_EX) | (0 if blocking else fcntl.LOCK_NB)
        try:
            fcntl.flock(fd, flags)
        except BlockingIOError:
            return False
        return True

    def _os_unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)

    def _os_downgrade(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_SH)     # converts the lock in place


class FileLock:
    """An advisory file lock with shared and exclusive modes.

    POSIX ``flock(2)``; Windows ``LockFileEx`` on one byte far past the end of
    the file, so the file's contents stay readable.  Either way the *kernel*
    drops the lock when the holding process dies, ``kill -9`` included, so a
    crash can never leave a stale lock.  Every acquisition opens its own
    descriptor, so two threads of one process exclude each other as well.

    ``with lock:`` takes it exclusively and waits as long as needed.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._fd: Optional[int] = None
        self.mode = ""           # "shared" | "exclusive" while held

    @property
    def fd(self) -> Optional[int]:
        return self._fd

    def acquire(self, shared: bool = False, blocking: bool = True,
                timeout: Optional[float] = None) -> bool:
        """Take the lock; ``False`` if ``blocking`` is off (or ``timeout``
        seconds pass) while someone else holds a conflicting lock."""
        if self._fd is not None:
            raise RuntimeError(f"{self.path}: lock already held by this object")
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
        try:
            if timeout is None:
                got = _os_lock(fd, shared, blocking)
            else:
                deadline = time.monotonic() + timeout
                while not (got := _os_lock(fd, shared, False)) and time.monotonic() < deadline:
                    time.sleep(0.005)
        except BaseException:
            os.close(fd)
            raise
        if not got:
            os.close(fd)
            return False
        self._fd = fd
        self.mode = "shared" if shared else "exclusive"
        return True

    def downgrade(self) -> None:
        """Exclusive -> shared without letting a writer in between (Windows);
        on POSIX ``flock`` converts in place."""
        if self._fd is None or self.mode != "exclusive":
            raise RuntimeError(f"{self.path}: not held exclusively")
        _os_downgrade(self._fd)
        self.mode = "shared"

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            _os_unlock(self._fd)
        finally:
            os.close(self._fd)
            self._fd = None
            self.mode = ""

    def __enter__(self) -> "FileLock":
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


class ProcessLock:
    """The store's session lock, released by the kernel when the holder dies.

    A normal (single-writer) session holds it *exclusively*.  A multi-writer
    session (shadow engine only) holds it *shared*, so writers coexist with
    each other but never with a single-writer session or an exclusive recovery.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = FileLock(path)

    @property
    def mode(self) -> str:
        return self._lock.mode

    def acquire(self, blocking: bool = False, shared: bool = False,
                timeout: Optional[float] = None) -> None:
        if not self._lock.acquire(shared=shared, blocking=blocking, timeout=timeout):
            holder = ""
            try:
                with open(self.path, "rb") as f:
                    holder = f.read(32).split(b"\0")[0].decode("ascii", "replace").strip()
            except OSError:
                pass
            raise StoreLocked("store is in use by another process"
                              + (f" ({holder})" if holder else "")) from None
        if not shared:
            # Record the holder for humans.  Informational only.  The lock itself
            # is the kernel's, which disappears when this process dies.
            try:
                os.ftruncate(self._lock.fd, 0)            # type: ignore[arg-type]
                os.write(self._lock.fd, f"pid {os.getpid()}".encode())  # type: ignore[arg-type]
            except OSError:
                pass

    def downgrade(self) -> None:
        self._lock.downgrade()

    def release(self) -> None:
        self._lock.release()


class GroupCommit:
    """Batch concurrent log appends behind a single durable flush.

    ``flush(batch)`` is supplied by the engine: it must write every payload in
    ``batch`` (in order) and make them durable with one fsync.  It returns
    nothing.  ``on_durable(batch)`` runs under the lock right after, in the
    leader thread, so the effects become visible in commit order.
    """

    def __init__(self, flush: Callable[[List[object]], None],
                 on_durable: Callable[[List[object]], None]) -> None:
        self._flush = flush
        self._on_durable = on_durable
        self._cond = threading.Condition(threading.Lock())
        self._queue: List[object] = []
        self._enqueued = 0      # sequence number of the last item enqueued
        self._durable = 0       # sequence number of the last item made durable
        self._flushing = False
        self._error: Optional[Tuple[int, BaseException]] = None
        #: Statistics: how many fsync batches, how many items rode in them.
        self.batches = 0
        self.items = 0

    def submit(self, item: object) -> None:
        with self._cond:
            self._queue.append(item)
            self._enqueued += 1
            ticket = self._enqueued
            while self._durable < ticket:
                if self._error and self._error[0] >= ticket:
                    raise self._error[1]
                if not self._flushing:
                    # Become the leader: take everything queued so far.
                    self._flushing = True
                    batch, self._queue = self._queue, []
                    last = self._enqueued
                    self._cond.release()
                    try:
                        self._flush(batch)
                        err = None
                    except BaseException as e:  # includes CrashNow
                        err = e
                    finally:
                        self._cond.acquire()
                    self._flushing = False
                    if err is not None:
                        self._error = (last, err)
                        self._cond.notify_all()
                        raise err
                    self._on_durable(batch)
                    self._durable = last
                    self.batches += 1
                    self.items += len(batch)
                    self._cond.notify_all()
                else:
                    self._cond.wait()
            if self._error and self._error[0] >= ticket and self._durable < ticket:
                raise self._error[1]
