"""Undo (rollback) journaling.  Overwrite in place, but keep the old bytes first.

This is SQLite's classic rollback-journal design.  Documents are updated
*in place*, which by itself is the unsafe naive strategy, but only after the
old version has been made durable in a journal::

    1  journal <- old bytes of every document in the transaction (+ CRC)
    2  fsync(journal)                      the undo information is durable
    3  overwrite documents in place        may be torn by a crash: that's fine
    4  fsync(documents) [+ fsync(dir)]
    5  journal header <- "inactive"        COMMIT POINT once durable
    6  fsync(journal)                      -> ack

Recovery: if the journal is *active and intact*, a transaction was cut off
somewhere in steps 3-5, so copy the old bytes back (roll back) and deactivate
the journal.  If the journal is torn or incomplete, the crash happened in steps
1-2 and the documents were never touched, so just deactivate it.

So an interrupted transaction always recovers to the **previous** version.
That is the first of the two outcomes the problem statement allows.  (The redo
WAL engine shows the other one.)  Multi-document transactions are atomic
because one journal covers them all.

Journal layout (``undo.journal``)::

    sector 0  header: magic | seq | nrec | body_len | body_crc | header_crc
    512..     body:   [keylen u16 | existed u8 | oldlen u64 | key | old file bytes] * nrec
"""

from __future__ import annotations

import struct
from typing import Dict, List, Optional, Tuple

from ..concurrency import RWLock
from ..format import (IntegrityError, crc32, decode_doc, encode_doc, key_to_name,
                      name_to_key, pad)
from ..vfs.base import join
from .base import Engine, RecoveryReport

DOCS = "docs"
JOURNAL = "undo.journal"
SECTOR = 512
_HDR = struct.Struct(">8sQIQI")          # magic seq nrec body_len body_crc
_REC = struct.Struct(">HBQ")             # keylen existed oldlen
MAGIC = b"SWUNDO01"


def _header(seq: int, nrec: int, body: bytes) -> bytes:
    head = _HDR.pack(MAGIC, seq, nrec, len(body), crc32(body) if nrec else 0)
    return pad(head + struct.pack(">I", crc32(head)), SECTOR)


class UndoEngine(Engine):
    name = "undo"
    title = "Undo journal (rollback)"
    technique = "Save old bytes to a journal, fsync, then overwrite in place"
    commit_point = "journal marked inactive (fsync)"
    recovers_to = "the PREVIOUS version (interrupted transactions are rolled back)"
    atomic_tx = True
    OPTIONS = {"journal_sync": True}
    VARIANTS = {
        "no-journal-sync": {"options": {"journal_sync": False},
                            "breaks": "documents are overwritten before the undo data is durable: torn file, nothing to roll back to"},
    }

    def __init__(self, vfs, **options) -> None:
        super().__init__(vfs, **options)
        self._rw = RWLock()
        self._seq = 0
        self._j = -1

    def _path(self, key: str) -> str:
        return join(DOCS, key_to_name(key))

    # ------------------------------------------------------------- format
    def format(self) -> None:
        vfs = self.vfs
        vfs.makedirs(DOCS)
        h = vfs.open(JOURNAL, create=True, truncate=True)
        try:
            vfs.pwrite(h, 0, _header(0, 0, b""))
            vfs.fsync(h)
        finally:
            vfs.close(h)
        vfs.fsync_dir("")

    # ------------------------------------------------------------ journal io
    def _read_journal(self) -> Tuple[str, int, List[Tuple[str, Optional[bytes]]]]:
        """Return (state, seq, records); state in {inactive, torn, incomplete, active}."""
        raw = self.vfs.pread(self._j, 0, SECTOR)
        if len(raw) < _HDR.size + 4:
            return "torn", self._seq, []
        head = raw[:_HDR.size]
        (hcrc,) = struct.unpack_from(">I", raw, _HDR.size)
        magic, seq, nrec, body_len, body_crc = _HDR.unpack(head)
        if magic != MAGIC or crc32(head) != hcrc:
            return "torn", self._seq, []
        if nrec == 0:
            return "inactive", seq, []
        body = self.vfs.pread(self._j, SECTOR, body_len)
        if len(body) != body_len or crc32(body) != body_crc:
            return "incomplete", seq, []
        records: List[Tuple[str, Optional[bytes]]] = []
        off = 0
        for _ in range(nrec):
            klen, existed, olen = _REC.unpack_from(body, off)
            off += _REC.size
            key = body[off:off + klen].decode("utf-8")
            off += klen
            old = body[off:off + olen] if existed else None
            off += olen
            records.append((key, old))
        return "active", seq, records

    def _deactivate(self, seq: int) -> None:
        self.vfs.pwrite(self._j, 0, _header(seq, 0, b""))
        self.vfs.fsync(self._j)

    # ------------------------------------------------------------- recovery
    def recover(self, report: RecoveryReport) -> None:
        vfs = self.vfs
        if not vfs.exists(JOURNAL):
            raise IntegrityError("undo.journal is missing: not an undo store (or creation never finished)")
        self._j = vfs.open(JOURNAL)
        state, seq, records = self._read_journal()
        self._seq = seq
        if state == "inactive":
            self.log("ok", "journal inactive: the last transaction completed; nothing to undo")
            return
        if state in ("torn", "incomplete"):
            what = "header is torn" if state == "torn" else "body checksum does not match"
            self.log("discard", f"journal {what}: it never became durable, so no document was touched yet")
            report.discarded += 1
            self._deactivate(seq)
            self.log("ok", "journal deactivated")
            return
        self.log("rollback", f"ACTIVE journal for transaction #{seq} with {len(records)} document(s): "
                             "the crash interrupted it, so roll back")
        self._restore(records)
        report.rolled_back += len(records)
        self._deactivate(seq)
        self.log("ok", "old versions restored and fsynced; journal deactivated")

    def _restore(self, records: List[Tuple[str, Optional[bytes]]]) -> None:
        vfs = self.vfs
        dir_dirty = False
        for key, old in records:
            path = self._path(key)
            if old is None:
                if vfs.exists(path):
                    vfs.unlink(path)
                    dir_dirty = True
                self.log("rollback", f"{key}: did not exist before; removed")
                continue
            if not vfs.exists(path):
                dir_dirty = True
            h = vfs.open(path, create=True)
            try:
                vfs.pwrite(h, 0, old)
                vfs.ftruncate(h, len(old))
                vfs.fsync(h)
            finally:
                vfs.close(h)
            self.log("rollback", f"{key}: restored previous version ({len(old)} bytes)")
        if dir_dirty:
            vfs.fsync_dir(DOCS)

    def close(self) -> None:
        if self._j != -1:
            self.vfs.close(self._j)
            self._j = -1
        super().close()

    # -------------------------------------------------------------- reading
    def get(self, key: str) -> Optional[bytes]:
        with self._rw.read():
            raw = self.vfs.read_file(self._path(key))
        return None if raw is None else decode_doc(raw)

    def keys(self) -> List[str]:
        return sorted(k for k in map(name_to_key, self.vfs.listdir(DOCS)) if k)

    # -------------------------------------------------------------- writing
    def commit(self, changes: Dict[str, Optional[bytes]]) -> None:
        changes = self._check(changes)
        vfs = self.vfs
        with self._rw.write():
            self._seq += 1
            seq = self._seq
            # 1. journal the old bytes of every document we are about to touch
            parts = []
            for key in changes:
                old = vfs.read_file(self._path(key))
                kb = key.encode("utf-8")
                parts.append(_REC.pack(len(kb), old is not None, len(old or b"")) + kb + (old or b""))
            body = b"".join(parts)
            vfs.note("journal-old-versions", seq=seq, docs=len(changes))
            vfs.pwrite(self._j, SECTOR, body)
            vfs.pwrite(self._j, 0, _header(seq, len(changes), body))
            # 2. make the undo information durable before touching any document
            if self.opt["journal_sync"]:
                vfs.note("fsync-journal")
                vfs.fsync(self._j)
            # 3-4. overwrite in place
            vfs.note("write-in-place")
            dir_dirty = False
            for key, value in changes.items():
                path = self._path(key)
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
                    vfs.pwrite(h, 0, rec)
                    if vfs.size(h) != len(rec):
                        vfs.ftruncate(h, len(rec))
                    vfs.fsync(h)
                finally:
                    vfs.close(h)
            if dir_dirty:
                vfs.fsync_dir(DOCS)
            # 5-6. deactivate the journal: this is the commit point
            vfs.note("deactivate-journal")
            self._deactivate(seq)
            vfs.note("commit", seq=seq, detail="journal inactive: the new versions are final")
