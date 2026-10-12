"""Redo write-ahead logging with checkpoints and group commit.

The design of ext4's jbd2 journal and of every database WAL, scaled down:

* **Commit** = append ``PUT``/``DEL`` records plus one ``COMMIT`` record to
  ``wal.log`` and ``fsync`` once.  The commit point is the moment the COMMIT
  record is durable.  It is a single sequential append and a single fsync,
  the cheapest durable commit of all four engines.
* **Group commit**: concurrent committers share that fsync.  One leader
  writes everyone's records and syncs; the followers sleep on a condition
  variable (see :class:`~safewrite.concurrency.GroupCommit`).
* **Checkpoint**: committed values live in memory until they are copied to
  their *home* files (``data/<key>``, overwritten in place, which may be torn
  by a crash), the homes are fsynced, and only then is the log reset.
* **Recovery (redo)**: scan the log from the last checkpoint, stop at the
  first record that is torn (CRC), out of sequence (LSN) or from an older log
  generation (salt).  *Re-apply* every transaction that has a COMMIT record
  and ignore the rest.  Redo is idempotent because every record holds the full
  new value.

So an interrupted commit recovers to the **new** version if its COMMIT
record reached the disk and to the old one otherwise.  An acknowledged commit
is always new.

Log layout (``wal.log``)::

    sector 0  header:  magic | checkpoint_lsn u64 | salt u32 | header_crc u32
    512..     records: magic "WREC" | salt | lsn | txid | type | keylen | vallen | crc | key | value
"""

from __future__ import annotations

import struct
from typing import Dict, List, Optional, Tuple

from ..concurrency import GroupCommit, RWLock
from ..format import (IntegrityError, crc32, decode_doc, encode_doc, key_to_name,
                      name_to_key, pad)
from ..vfs.base import join
from .base import Engine, RecoveryReport

DATA = "data"
LOG = "wal.log"
SECTOR = 512
MAGIC = b"SWWAL001"
_HDR = struct.Struct(">8sQI")                 # magic ckpt_lsn salt (+ crc)
_REC = struct.Struct(">4sIQQBHI")             # magic salt lsn txid type keylen vallen (+ crc)
REC_MAGIC = b"WREC"
PUT, DEL, COMMIT = 1, 2, 3
_MISSING = object()


def _log_header(ckpt_lsn: int, salt: int) -> bytes:
    head = _HDR.pack(MAGIC, ckpt_lsn, salt)
    return pad(head + struct.pack(">I", crc32(head)), SECTOR)


def _record(salt: int, lsn: int, txid: int, kind: int, key: bytes = b"",
            value: bytes = b"", count: int = 0) -> bytes:
    vallen = count if kind == COMMIT else len(value)
    head = _REC.pack(REC_MAGIC, salt, lsn, txid, kind, len(key), vallen)
    body = key + (b"" if kind == COMMIT else value)
    return head + struct.pack(">I", crc32(head, body)) + body


class WalEngine(Engine):
    name = "wal"
    title = "Redo write-ahead log"
    technique = "Append changes + COMMIT record to a log, fsync once; copy to home files at checkpoints"
    commit_point = "COMMIT record durable in the log (fsync)"
    recovers_to = "the NEW version if its COMMIT record is durable, else the old one (redo)"
    atomic_tx = True
    OPTIONS = {"checksums": True, "sync_commit": True,
               "checkpoint_every": 64, "checkpoint_bytes": 4 << 20}
    VARIANTS = {
        "no-checksum": {"options": {"checksums": False},
                        "breaks": "a torn log tail is replayed as if it were committed: garbage becomes a document"},
        "early-ack": {"options": {"sync_commit": False},
                      "breaks": "commit() returns before the log is fsynced: an acknowledged commit can vanish"},
    }

    def __init__(self, vfs, **options) -> None:
        super().__init__(vfs, **options)
        self._log = -1
        self._salt = 1
        self._ckpt_lsn = 0
        self._lsn = 0
        self._txid = 0
        self._tail = SECTOR
        self._mem: Dict[str, Optional[bytes]] = {}   # committed, not yet checkpointed
        self._since_ckpt = 0
        self._rw = RWLock()        # read: commits & home reads; write: checkpoint
        self._gc = GroupCommit(self._flush, self._apply)

    def _home(self, key: str) -> str:
        return join(DATA, key_to_name(key))

    @property
    def group_commit_stats(self) -> Dict[str, int]:
        return {"batches": self._gc.batches, "commits": self._gc.items}

    # --------------------------------------------------------------- format
    def format(self) -> None:
        vfs = self.vfs
        vfs.makedirs(DATA)
        h = vfs.open(LOG, create=True, truncate=True)
        try:
            vfs.pwrite(h, 0, _log_header(0, 1))
            vfs.fsync(h)
        finally:
            vfs.close(h)
        vfs.fsync_dir("")

    # ------------------------------------------------------------- recovery
    def _scan(self) -> Tuple[List[Tuple[int, List[Tuple[str, Optional[bytes]]]]], Dict[int, int], int, str]:
        """Read the log; return (committed txs, uncommitted {txid: nops}, end offset, stop reason)."""
        vfs = self.vfs
        size = vfs.size(self._log)
        off, expect = SECTOR, self._ckpt_lsn + 1
        pending: Dict[int, List[Tuple[str, Optional[bytes]]]] = {}
        committed: List[Tuple[int, List[Tuple[str, Optional[bytes]]]]] = []
        hsize = _REC.size + 4
        reason = "end of log"
        while True:
            if off >= size:
                break
            head = vfs.pread(self._log, off, hsize)
            if len(head) < hsize:
                reason = "torn record header at end of log"
                break
            magic, salt, lsn, txid, kind, klen, vlen = _REC.unpack_from(head)
            (crc,) = struct.unpack_from(">I", head, _REC.size)
            if magic != REC_MAGIC or salt != self._salt:
                reason = "no more records from this log generation"
                break
            if lsn != expect:
                reason = f"LSN {lsn} out of sequence (expected {expect})"
                break
            blen = klen + (0 if kind == COMMIT else vlen)
            body = vfs.pread(self._log, off + hsize, blen)
            if len(body) < blen:
                reason = f"record LSN {lsn} is truncated"
                break
            if self.opt["checksums"] and crc32(head[:_REC.size], body) != crc:
                reason = f"record LSN {lsn} fails its CRC (torn write)"
                break
            if kind in (PUT, DEL):
                key = body[:klen].decode("utf-8", "replace")
                value = body[klen:] if kind == PUT else None
                pending.setdefault(txid, []).append((key, value))
            elif kind == COMMIT:
                ops = pending.pop(txid, [])
                if len(ops) != vlen:
                    reason = f"COMMIT of tx {txid} expects {vlen} records, found {len(ops)}"
                    break
                committed.append((txid, ops))
            else:
                reason = f"unknown record type {kind}"
                break
            self._lsn = lsn
            self._txid = max(self._txid, txid)
            expect += 1
            off += hsize + blen
        return committed, {t: len(o) for t, o in pending.items()}, off, reason

    def recover(self, report: RecoveryReport) -> None:
        vfs = self.vfs
        if not vfs.exists(LOG):
            raise IntegrityError("wal.log is missing: not a WAL store (or creation never finished)")
        self._log = vfs.open(LOG)
        raw = vfs.pread(self._log, 0, SECTOR)
        head = raw[:_HDR.size]
        ok = len(raw) >= _HDR.size + 4
        if ok:
            magic, ckpt, salt = _HDR.unpack(head)
            ok = magic == MAGIC and crc32(head) == struct.unpack_from(">I", raw, _HDR.size)[0]
        if not ok:
            # The header is written only by a checkpoint, and only after every
            # home file is durable, so a torn header means the homes are complete.
            self.log("discard", "log header is torn: a checkpoint was in progress, and every home file "
                                "was already durable, so the log can be reset")
            report.discarded += 1
            self._ckpt_lsn, self._salt = 0, 1
            vfs.ftruncate(self._log, 0)
            vfs.fsync(self._log)
            vfs.pwrite(self._log, 0, _log_header(0, 1))
            vfs.fsync(self._log)
            self._tail = SECTOR
            return
        self._ckpt_lsn, self._salt, self._lsn = ckpt, salt, ckpt
        committed, uncommitted, end, reason = self._scan()
        size = vfs.size(self._log)
        self.log("info", f"log header OK (checkpoint LSN {ckpt}); scanned "
                         f"{end - SECTOR} bytes of records, stopped: {reason}")
        for txid, ops in committed:
            for key, value in ops:
                self._mem[key] = value
                report.replayed += 1
            what = ", ".join(f"{k} <- {'DELETE' if v is None else f'{len(v)} bytes'}" for k, v in ops)
            self.log("replay", f"REDO committed tx {txid}: {what}")
        for txid, n in uncommitted.items():
            report.discarded += 1
            self.log("discard", f"tx {txid} has {n} record(s) but no COMMIT record: never committed, ignored")
        if size > end:
            report.discarded += 1
            self.log("discard", f"{size - end} byte(s) of torn/stale log tail ignored")
        self._tail = end
        if committed or size > end or uncommitted:
            self._checkpoint(reason="recovery")
            self.log("ok", "home files rewritten and fsynced; log reset (new salt)")
        else:
            self.log("ok", "log is clean: nothing to redo")

    def close(self) -> None:
        if self._log != -1:
            if self._mem:
                self._checkpoint(reason="close")
            self.vfs.close(self._log)
            self._log = -1
        super().close()

    # ----------------------------------------------------------- checkpoint
    def checkpoint(self) -> None:
        self._checkpoint(reason="manual")

    def _checkpoint(self, reason: str) -> None:
        vfs = self.vfs
        with self._rw.write():
            vfs.note("checkpoint", reason=reason, docs=len(self._mem))
            dir_dirty = False
            for key, value in sorted(self._mem.items()):
                path = self._home(key)
                if value is None:
                    if vfs.exists(path):
                        vfs.unlink(path)
                        dir_dirty = True
                    continue
                if not vfs.exists(path):
                    dir_dirty = True
                rec = encode_doc(value)
                h = vfs.open(path, create=True)
                try:
                    vfs.pwrite(h, 0, rec)           # in place: may tear, the log covers it
                    if vfs.size(h) != len(rec):
                        vfs.ftruncate(h, len(rec))
                    vfs.fsync(h)
                finally:
                    vfs.close(h)
            if dir_dirty:
                vfs.fsync_dir(DATA)
            # Every home file is durable: only now may the log forget them.
            vfs.note("reset-log")
            self._ckpt_lsn = self._lsn
            self._salt = (self._salt + 1) & 0xFFFFFFFF or 1
            vfs.pwrite(self._log, 0, _log_header(self._ckpt_lsn, self._salt))
            vfs.fsync(self._log)
            vfs.ftruncate(self._log, SECTOR)
            self._tail = SECTOR
            self._mem.clear()
            self._since_ckpt = 0

    # -------------------------------------------------------------- reading
    def get(self, key: str) -> Optional[bytes]:
        v = self._mem.get(key, _MISSING)
        if v is not _MISSING:
            return v  # type: ignore[return-value]
        with self._rw.read():
            raw = self.vfs.read_file(self._home(key))
        return None if raw is None else decode_doc(raw)

    def keys(self) -> List[str]:
        mem = dict(self._mem)
        homes = {k for k in map(name_to_key, self.vfs.listdir(DATA)) if k}
        return sorted((homes | {k for k, v in mem.items() if v is not None})
                      - {k for k, v in mem.items() if v is None})

    # -------------------------------------------------------------- writing
    def commit(self, changes: Dict[str, Optional[bytes]]) -> None:
        changes = self._check(changes)
        if not changes:
            return
        with self._rw.read():
            self._gc.submit(changes)
        if (self._since_ckpt >= self.opt["checkpoint_every"]
                or self._tail >= self.opt["checkpoint_bytes"]):
            self._checkpoint(reason="threshold")

    def _flush(self, batch: List[object]) -> None:
        """Group-commit leader: append every queued transaction, fsync once."""
        vfs = self.vfs
        parts = []
        lsn, txid = self._lsn, self._txid
        for changes in batch:
            txid += 1
            for key, value in changes.items():  # type: ignore[attr-defined]
                lsn += 1
                kb = key.encode("utf-8")
                parts.append(_record(self._salt, lsn, txid, DEL if value is None else PUT, kb, value or b""))
            lsn += 1
            parts.append(_record(self._salt, lsn, txid, COMMIT, count=len(changes)))  # type: ignore[arg-type]
        data = b"".join(parts)
        vfs.note("append-log", txs=len(batch), bytes=len(data))
        vfs.pwrite(self._log, self._tail, data)
        if self.opt["sync_commit"]:
            vfs.note("fsync-log")
            vfs.fsync(self._log)
        self._tail += len(data)
        self._lsn, self._txid = lsn, txid
        vfs.note("commit", detail=f"{len(batch)} transaction(s) durable with one fsync")

    def _apply(self, batch: List[object]) -> None:
        for changes in batch:
            self._mem.update(changes)  # type: ignore[arg-type]
        self._since_ckpt += len(batch)
