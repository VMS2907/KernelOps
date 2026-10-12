"""Synchronization primitives.

Three different problems need three different tools:

``RWLock``
    Threads in one process.  Many readers or one writer.  Used by engines that
    update files *in place* (naive, undo), where a reader could otherwise
    observe a half-written document.

``ProcessLock``
    Processes.  An advisory ``flock`` on ``<store>/LOCK``.  The kernel releases
    it automatically when the holding process dies, *including* by
    ``kill -9``, so a crash can never leave a stale lock behind.  A plain
    "lock file that exists" would.

``GroupCommit``
    Coordination, not just exclusion.  Concurrent committers queue their log
    records.  One *leader* writes the whole batch and issues a single
    ``fsync``.  The *followers* sleep on a condition variable until the batch
    they joined is durable.  N commits share 1 fsync.
"""

from __future__ import annotations

import os
import threading
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


class ProcessLock:
    """Exclusive inter-process lock on a file, released by the kernel on death."""

    def __init__(self, path: str) -> None:
        self.path = path
        self._fd: Optional[int] = None

    def acquire(self, blocking: bool = False) -> None:
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o644)
        try:
            if fcntl is not None:
                flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
                fcntl.flock(fd, flags)
            elif msvcrt is not None:  # pragma: no cover - Windows
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK, 1)
        except OSError:
            holder = ""
            try:
                os.lseek(fd, 0, os.SEEK_SET)
                holder = os.read(fd, 32).split(b"\0")[0].decode("ascii", "replace").strip()
            except OSError:
                pass
            os.close(fd)
            raise StoreLocked("store is in use by another process"
                              + (f" ({holder})" if holder else "")) from None
        # Record the holder for humans.  Informational only.  The lock itself is
        # the kernel's flock, which disappears when this process dies.
        if fcntl is not None:
            try:
                os.ftruncate(fd, 0)
                os.write(fd, f"pid {os.getpid()}".encode())
            except OSError:
                pass
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            elif msvcrt is not None:  # pragma: no cover - Windows
                os.lseek(self._fd, 0, os.SEEK_SET)
                msvcrt.locking(self._fd, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(self._fd)
            self._fd = None


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
