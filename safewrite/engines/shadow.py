"""Shadow file + atomic rename.  Our round-1 SafeWrite design, hardened.

Protocol for ``put(key, B)``::

    prepare (the candidate)                      nothing visible changes
    1  tmp = docs/<key>~<n>.tmp           unique name: concurrent writers can't collide
    2  write(tmp, header + B)             header = magic, length, CRC-32
    3  fsync(tmp)                         B's bytes are durable BEFORE anyone can see them
    4  read back + validate tmp           catches I/O errors before commit
    publish (under the commit lock)
    5  rename(tmp, docs/<key>)            COMMIT POINT: the name now means B, atomically
    6  fsync(docs/)                       the rename itself is durable -> ack

Recovery: validate each document, then delete every ``*~*.tmp`` (never
renamed, so never committed).  A temp whose destination is *damaged* is not
deleted and not promoted: it is moved to ``quarantine/`` for forensics.  A
valid checksum proves a candidate is intact, never that it was committed.

Why each step exists (the explorer has a counter-example for each):

* without (3) the rename can reach the disk *before* B's data does.  After a
  power cut the name points at an empty inode and A is gone too.  This is the
  2009 ext4 "zero-length file" bug.
* without (6) ``put`` returns, then a power cut silently brings back A.  That
  is a durability violation.

Error contract (``safewrite.errors``): a failure in steps 1-4, or a rename
that did not happen, raises :class:`CommitAborted` (A untouched, temp removed,
store usable).  A failure in step 6 raises :class:`VisibleNotDurable`: B is
already visible, but not confirmed durable.

Documented crash points (``CRASH_POINTS``; ``python -m safewrite crash --phase``):
``during_write`` (inside step 2), ``after_validation`` (between 4 and 5) and
``after_replace`` (between 5 and 6).

Limitation, shown on purpose: a multi-document commit is a *sequence* of
renames, so a crash between them leaves some documents new and some old.
Atomic multi-document commits need a journal (undo/wal) or a single root
pointer (cow).
"""

from __future__ import annotations

import errno as _errno
import itertools
import threading
from contextlib import nullcontext
from dataclasses import dataclass
from typing import Any, ContextManager, Dict, List, Optional

from ..errors import CommitAborted, PayloadTooLarge, VisibleNotDurable, WriteConflict
from ..format import IntegrityError, check_key, decode_doc, encode_doc, key_to_name, name_to_key, sha256
from ..vfs.base import join
from .base import Engine, RecoveryReport

DOCS = "docs"
QUARANTINE = "quarantine"
#: The declared payload cap: 1 MiB.  A bigger document is refused before
#: anything is written (a save needs room for the old and the new copy at once).
MAX_PAYLOAD = 1 << 20
#: ``put(..., expect=ANY)``: unconditional save (last writer wins).
ANY = object()


@dataclass
class Candidate:
    """A prepared save: the new version is durable in ``tmp`` but not visible."""
    key: str
    tmp: str
    dest: str
    size: int          # payload bytes
    sha256: str        # of the payload


def _mib(n: int) -> str:
    return f"{n:,} bytes ({n / (1 << 20):.2f} MiB)"


class ShadowEngine(Engine):
    name = "shadow"
    title = "Shadow file + rename"
    technique = "Write a complete temp copy, fsync it, then atomically rename it over the original"
    commit_point = "rename(tmp, doc)"
    recovers_to = "old or new version of each document (per-document atomicity)"
    atomic_tx = False
    OPTIONS = {"fsync_file": True, "fsync_dir": True, "verify": True,
               "max_payload": MAX_PAYLOAD, "temp_names": "unique"}
    VARIANTS = {
        "no-fsync": {"options": {"fsync_file": False},
                     "breaks": "rename may persist before the data: the name points at an empty file, A and B both lost"},
        "no-dirsync": {"options": {"fsync_dir": False},
                       "breaks": "put() returns but the rename is not durable: an acknowledged write is lost"},
    }
    #: The round-1 test plan's named crash points.  ``phase`` = crash when that
    #: protocol note is reached; ``op`` = crash inside the first such syscall
    #: after the ``after`` note, persisting ``partial`` of its bytes first.
    CRASH_POINTS = {
        "during_write": {"op": "write", "after": "write-temp", "partial": 0.5,
                         "means": "killed inside write(2) of the temp file: half of B's record reached it"},
        "after_validation": {"phase": "rename",
                             "means": "temp complete, fsynced and validated; rename not yet issued"},
        "after_replace": {"phase": "commit",
                          "means": "rename done (B visible under the real name); directory not yet fsynced"},
    }

    def __init__(self, vfs, **options) -> None:
        super().__init__(vfs, **options)
        if self.opt["temp_names"] not in ("unique", "fixed"):
            raise ValueError("temp_names must be 'unique' or 'fixed'")
        self._seq = itertools.count(1)
        self._wlock = threading.Lock()
        #: Serialises publish (rename + fsync dir) across writers.  A no-op for
        #: one engine instance (``_wlock`` already serialises it); the store's
        #: multi-writer mode installs a cross-process file lock here.
        self.commit_lock: ContextManager[Any] = nullcontext()
        #: Multi-writer mode: a per-session token in temp names, so writers in
        #: different processes can never pick the same temp file.
        self.writer_id: Optional[str] = None
        #: Multi-writer mode, set before ``open()`` when other writers may be
        #: live: recovery must not touch temp files (they may be saves in flight).
        self.live_peers = False
        #: Documents that failed validation at the last recovery: name -> error.
        self.damaged: Dict[str, str] = {}

    def format(self) -> None:
        self.vfs.makedirs(DOCS)
        self.vfs.fsync_dir("")

    def _path(self, key: str) -> str:
        return join(DOCS, key_to_name(key))

    def _temp(self, key: str) -> str:
        name = key_to_name(key)
        if self.opt["temp_names"] == "fixed":       # the round-1 single-writer layout
            return join(DOCS, f"{name}~.tmp")
        wid = f"{self.writer_id}-" if self.writer_id else ""
        return join(DOCS, f"{name}~{wid}{next(self._seq)}.tmp")

    # ------------------------------------------------------------- recovery
    def recover(self, report: RecoveryReport) -> None:
        names = self.vfs.listdir(DOCS)
        temps = [n for n in names if n.endswith(".tmp") and "~" in n]
        docs = [n for n in names if n not in temps]
        # Integrity first: whether a destination is intact decides what may be
        # done with its leftover temp files.
        self.damaged = {}
        for n in docs:
            try:
                decode_doc(self.vfs.read_file(join(DOCS, n)))
            except IntegrityError as e:
                self.damaged[n] = str(e)
        if self.live_peers and temps:
            self.log("info", f"left {len(temps)} temp file(s) alone: other writers have the store open, "
                             "so a temp may be a save in flight (only an exclusive recovery sweeps them)")
        elif temps:
            kept = []
            for n in temps:
                owner = n.split("~", 1)[0]
                if owner in self.damaged:
                    kept.append(n)
                    continue
                self.vfs.unlink(join(DOCS, n))
                report.discarded += 1
                self.log("discard", f"deleted uncommitted temp file {n} (it was never renamed, so never committed)")
            if kept:
                self._quarantine(kept)
            self.vfs.fsync_dir(DOCS)
        else:
            self.log("info", "no leftover temp files")
        for n, err in self.damaged.items():
            self.log("error", f"{n}: committed document failed validation ({err}); reads of it are refused, "
                              "its bytes are left exactly as found")
        if docs and not self.damaged:
            self.log("ok", f"{len(docs)} committed document(s) validated (length + CRC-32)")
        # Continue the temp-name sequence past anything we just saw.
        self._seq = itertools.count(len(names) + 1)

    def _quarantine(self, temps: List[str]) -> None:
        """Move uncommitted candidates of damaged documents aside, byte for byte."""
        vfs = self.vfs
        if not vfs.exists(QUARANTINE):
            vfs.mkdir(QUARANTINE)
            vfs.fsync_dir("")
        for n in temps:
            target = self._free_name(n)
            vfs.rename(join(DOCS, n), join(QUARANTINE, target))
            owner = n.split("~", 1)[0]
            try:
                decode_doc(vfs.read_file(join(QUARANTINE, target)))
                looks = "passes its checksum"
            except IntegrityError as e:
                looks = f"is itself invalid ({e})"
            self.log("warn", f"kept {n}: an uncommitted candidate for damaged {owner}. It {looks}, but it "
                             f"was never renamed, so it was never committed and is NOT promoted. "
                             f"Moved to {QUARANTINE}/{target} for forensics")
        vfs.fsync_dir(QUARANTINE)

    def _free_name(self, name: str) -> str:
        target, i = name, 0
        while self.vfs.exists(join(QUARANTINE, target)):
            i += 1
            target = f"{name}.{i}"
        return target

    # -------------------------------------------------------------- reading
    def get(self, key: str) -> Optional[bytes]:
        raw = self.vfs.read_file(self._path(key))
        if raw is None:
            return None
        return decode_doc(raw)

    def keys(self) -> List[str]:
        return sorted(k for k in map(name_to_key, self.vfs.listdir(DOCS)) if k)

    def layout(self) -> Dict[str, Any]:
        q = self.vfs.listdir(QUARANTINE) if self.vfs.isdir(QUARANTINE) else []
        return {"damaged": dict(self.damaged), "quarantine": q}

    # -------------------------------------------------------------- writing
    def commit(self, changes: Dict[str, Optional[bytes]]) -> None:
        changes = self._check(changes)
        for key, value in changes.items():          # refuse oversize before writing anything
            if value is not None:
                self._check_size(key, value)
        with self._wlock:
            done: List[str] = []
            for key, value in changes.items():
                try:
                    if value is None:
                        self._delete(key)
                    else:
                        self.publish(self.prepare(key, value))
                except (CommitAborted, VisibleNotDurable) as e:
                    e.already_committed = done
                    raise
                done.append(key)

    def put(self, key: str, value: bytes, expect: Any = ANY) -> None:
        """Save ``key``.  ``expect`` makes it conditional: the sha256 of the
        payload the caller based its edit on (``None`` = must not exist yet).
        If the document changed meanwhile, :class:`WriteConflict` is raised."""
        if expect is ANY:
            self.commit({key: value})
            return
        value = self._check({key: value})[key]
        with self._wlock:
            self.publish(self.prepare(key, value), expect=expect)

    def _check_size(self, key: str, value: bytes) -> None:
        cap = self.opt["max_payload"]
        if cap is not None and len(value) > cap:
            raise PayloadTooLarge(
                f"{key!r} is {_mib(len(value))}, over this store's declared cap of {_mib(cap)}. "
                f"Nothing was written; the committed version is unchanged. Split or compress the "
                f"document, or create the store with a larger max_payload.",
                key=key, phase="size-check", errno=_errno.EFBIG, size=len(value), cap=cap)

    def prepare(self, key: str, value: bytes) -> Candidate:
        """Steps 1-4: make the new version durable in a temp file, invisibly."""
        check_key(key)
        value = bytes(value)
        self._check_size(key, value)
        vfs = self.vfs
        dest = self._path(key)
        tmp = self._temp(key)
        record = encode_doc(value)
        vfs.note("write-temp", key=key, tmp=tmp)
        phase = "write-temp"
        try:
            h = vfs.open(tmp, create=True, truncate=True)
        except OSError as e:
            self._discard(tmp)
            raise self._aborted(key, phase, e, tmp, 0, len(record)) from e
        failure: Optional[OSError] = None
        written = 0
        intact = True
        try:
            vfs.pwrite(h, 0, record)
            if self.opt["fsync_file"]:
                phase = "fsync-temp"
                vfs.note("fsync-temp")
                vfs.fsync(h)
            if self.opt["verify"]:
                phase = "validate-temp"
                vfs.note("validate-temp")
                intact = vfs.pread(h, 0, len(record) + 1) == record
        except OSError as e:
            failure = e
            try:
                written = vfs.size(h)
            except (OSError, ValueError):
                written = 0
        finally:
            vfs.close(h)
        if failure is not None:
            self._discard(tmp)
            raise self._aborted(key, phase, failure, tmp, written, len(record)) from failure
        if not intact:
            self._discard(tmp)
            raise CommitAborted(f"read-back of {tmp} did not match what was written; nothing was "
                                f"committed, {key!r} is unchanged and the temp file was removed",
                                key=key, phase="validate-temp", errno=_errno.EIO)
        return Candidate(key, tmp, dest, len(value), sha256(value))

    def publish(self, cand: Candidate, expect: Any = ANY) -> None:
        """Steps 5-6 under the commit lock: rename (commit point), fsync dir."""
        vfs = self.vfs
        with self.commit_lock:
            if expect is not ANY:
                self._check_expected(cand, expect)
            name = cand.dest.rsplit("/", 1)[-1]
            if name in self.damaged:
                self._preserve_damaged(name)
            vfs.note("rename", key=cand.key)
            try:
                vfs.rename(cand.tmp, cand.dest)
            except OSError as e:
                if e.errno != _errno.ENOENT and not vfs.exists(cand.tmp) and self._dest_holds(cand):
                    raise self._not_durable(cand.key, "rename", e) from e
                self._discard(cand.tmp)
                why = ("the temp file vanished (removed by another writer or by a recovery)"
                       if e.errno == _errno.ENOENT else f"rename failed ({e})")
                raise CommitAborted(f"save of {cand.key!r} aborted before its commit point: {why}. "
                                    f"Nothing was committed; the previous version is unchanged.",
                                    key=cand.key, phase="rename", errno=e.errno) from e
            vfs.note("commit", key=cand.key, detail="rename done: the name now refers to the new version")
            if self.opt["fsync_dir"]:
                vfs.note("fsync-dir")
                try:
                    vfs.fsync_dir(DOCS)
                except OSError as e:
                    raise self._not_durable(cand.key, "fsync-dir", e) from e

    def _delete(self, key: str) -> None:
        path = self._path(key)
        if not self.vfs.exists(path):
            return
        with self.commit_lock:
            self.vfs.note("unlink", key=key)
            try:
                self.vfs.unlink(path)
            except OSError as e:
                raise CommitAborted(f"delete of {key!r} failed before its commit point ({e}); "
                                    f"the document is unchanged", key=key, phase="unlink", errno=e.errno) from e
            self.vfs.note("commit", key=key, detail="unlink done")
            if self.opt["fsync_dir"]:
                try:
                    self.vfs.fsync_dir(DOCS)
                except OSError as e:
                    raise self._not_durable(key, "fsync-dir", e, deleted=True) from e

    # ------------------------------------------------------------- helpers
    def _discard(self, tmp: str) -> None:
        """Best-effort removal of an aborted candidate (recovery sweeps leftovers)."""
        try:
            if self.vfs.exists(tmp):
                self.vfs.unlink(tmp)
        except OSError:
            pass

    def _aborted(self, key: str, phase: str, e: OSError, tmp: str, written: int,
                 needed: int) -> CommitAborted:
        where = {"write-temp": "writing the temporary copy", "fsync-temp": "flushing the temporary copy",
                 "validate-temp": "reading back the temporary copy"}.get(phase, phase)
        msg = (f"save of {key!r} aborted before its commit point: {e.strerror or e} while {where} "
               f"({tmp}). Nothing was committed: {key!r} still reads as its previous version, "
               f"and the temporary file was removed.")
        if e.errno == _errno.ENOSPC:
            msg += (f" Wrote {written:,} of {needed:,} bytes before the device filled up. This save "
                    f"needs {_mib(needed)} free in the store's directory, because the new version is "
                    f"written in full beside the old one before replacing it. Free at least "
                    f"{max(needed - written, 1):,} more bytes and retry.")
        else:
            msg += " Retrying is safe once the cause is fixed."
        return CommitAborted(msg, key=key, phase=phase, errno=e.errno, written=written, needed=needed)

    def _not_durable(self, key: str, phase: str, e: OSError, deleted: bool = False) -> VisibleNotDurable:
        what = "deletion" if deleted else "new version"
        return VisibleNotDurable(
            f"{key!r} was {'deleted' if deleted else 'replaced'}: the {what} is ALREADY VISIBLE to "
            f"readers, but {'fsync(dir)' if phase == 'fsync-dir' else phase} failed "
            f"({e.strerror or e}), so it is not confirmed durable. After a power loss the document "
            f"may come back as either the previous or the new version (each intact). Do not report "
            f"this save as failed-and-undone, and do not retry fsync (the kernel may already have "
            f"dropped the error). Reopen the store, then save again for a durable acknowledgement.",
            key=key, phase=phase, errno=e.errno)

    def _current_sha(self, dest: str) -> Optional[str]:
        raw = self.vfs.read_file(dest)
        return None if raw is None else sha256(decode_doc(raw))

    def _check_expected(self, cand: Candidate, expect: Optional[str]) -> None:
        try:
            current = self._current_sha(cand.dest)
        except IntegrityError:
            current = "damaged"
        if current != expect:
            self._discard(cand.tmp)
            now = "absent" if current is None else current if current == "damaged" else f"sha256 {current[:12]}"
            was = "absent" if expect is None else f"sha256 {str(expect)[:12]}"
            raise WriteConflict(f"conflict: {cand.key!r} changed after this writer read it (expected "
                                f"{was}, found {now}). This save was not committed and its temp file "
                                f"was removed; re-read the document, merge, and save again.",
                                key=cand.key, phase="conflict", expected=expect, found=current)

    def _dest_holds(self, cand: Candidate) -> bool:
        try:
            return self._current_sha(cand.dest) == cand.sha256
        except (IntegrityError, OSError):
            return False

    def _preserve_damaged(self, name: str) -> None:
        """Before overwriting a damaged document, move its bytes to quarantine."""
        path = join(DOCS, name)
        try:
            decode_doc(self.vfs.read_file(path))
            self.damaged.pop(name, None)       # repaired meanwhile (another writer)
            return
        except IntegrityError:
            pass
        if not self.vfs.exists(path):
            self.damaged.pop(name, None)
            return
        if not self.vfs.exists(QUARANTINE):
            self.vfs.mkdir(QUARANTINE)
            self.vfs.fsync_dir("")
        target = self._free_name(name + ".damaged")
        self.vfs.rename(path, join(QUARANTINE, target))
        self.vfs.fsync_dir(QUARANTINE)
        self.damaged.pop(name, None)
        self.log("warn", f"moved damaged {name} to {QUARANTINE}/{target} before replacing it")
