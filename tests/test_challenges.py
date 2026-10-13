"""The live technical challenges KO-16-C1..C5: crash points, invalid destination,
disk full, post-commit flush errors and concurrent writers."""
import errno
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest

from safewrite import (CommitAborted, PayloadTooLarge, SafeStore, StoreError, VisibleNotDurable,
                       WriteConflict)
from safewrite.cli import resolve_crash_point
from safewrite.concurrency import FileLock
from safewrite.engines import make
from safewrite.engines.shadow import MAX_PAYLOAD, ShadowEngine
from safewrite.format import IntegrityError, encode_doc, sha256
from safewrite.vfs import CrashNow, FaultyVFS, MemVFS, RecordingVFS

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
A, B, C = b"\x41" * 64, b"\x42" * 64, b"\x43" * 64


def shadow_with(doc, **opts):
    mem = MemVFS()
    e = make("shadow", mem, **opts)
    e.format()
    e.open()
    if doc is not None:
        e.put("doc", doc)
    return mem, e


def reopen(vfs, **opts):
    e = make("shadow", vfs, **opts)
    report = e.open()
    return e, report


class C1CrashPoints(unittest.TestCase):
    EXPECT = {"during_write": A, "after_validation": A, "after_replace": B}

    def test_documented_crash_points_recover_exact_bytes(self):
        for layout in ("fixed", "unique"):
            for name, cp in ShadowEngine.CRASH_POINTS.items():
                with self.subTest(layout=layout, point=name):
                    mem, _ = shadow_with(A, temp_names=layout)
                    plan_vfs = RecordingVFS(mem.clone())
                    dry = make("shadow", plan_vfs, temp_names=layout)
                    dry.open()
                    plan_vfs.reset()
                    dry.put("doc", B)
                    at, label, partial = resolve_crash_point(plan_vfs.trace, cp)
                    rvfs = RecordingVFS(mem, crash_at=at, crash_at_label=label, partial_write=partial)
                    eng = make("shadow", rvfs, temp_names=layout)
                    eng.open()
                    rvfs.reset()
                    rvfs.crash_at, rvfs.crash_at_label = at, label
                    with self.assertRaises(CrashNow):
                        eng.put("doc", B)
                    e2, _ = reopen(mem, temp_names=layout)
                    self.assertEqual(e2.get("doc"), self.EXPECT[name])
                    self.assertEqual([n for n in mem.listdir("docs") if n.endswith(".tmp")], [])

    def test_during_write_leaves_a_partial_temp(self):
        mem, _ = shadow_with(A, temp_names="fixed")
        rvfs = RecordingVFS(mem, crash_at=2, partial_write=0.5)
        eng = make("shadow", rvfs, temp_names="fixed")
        eng.open()
        rvfs.reset()
        rvfs.crash_at = 2
        with self.assertRaises(CrashNow):
            eng.put("doc", B)
        self.assertEqual(len(mem.read_file("docs/doc~.tmp")), len(encode_doc(B)) // 2)

    def test_crash_points_are_documented_for_the_cli(self):
        self.assertEqual(set(ShadowEngine.describe()["crash_points"]),
                         {"during_write", "after_validation", "after_replace"})


class C2InvalidDestination(unittest.TestCase):
    def damaged(self, temp_record=None):
        mem, _ = shadow_with(A)
        h = mem.open("docs/doc")
        mem.ftruncate(h, 10)                       # truncated header
        mem.close(h)
        h = mem.open("docs/doc~9.tmp", create=True)
        mem.pwrite(h, 0, temp_record if temp_record is not None else encode_doc(B))
        mem.close(h)
        return mem

    def test_temp_is_not_promoted_and_nothing_is_destroyed(self):
        mem = self.damaged()
        dest, tmp = mem.read_file("docs/doc"), mem.read_file("docs/doc~9.tmp")
        e, report = reopen(mem)
        with self.assertRaises(IntegrityError):
            e.get("doc")
        self.assertEqual(mem.read_file("docs/doc"), dest)
        self.assertEqual(mem.read_file("quarantine/doc~9.tmp"), tmp)
        self.assertFalse(mem.exists("docs/doc~9.tmp"))
        self.assertEqual(report.discarded, 0)
        self.assertTrue(any(ev.kind == "warn" and "NOT promoted" in ev.msg for ev in report.events))
        self.assertEqual(e.verify()[0].split(":")[0], "doc")
        self.assertIn("doc", e.layout()["damaged"])

    def test_recovery_is_idempotent(self):
        mem = self.damaged()
        reopen(mem)
        snap = mem.snapshot()
        reopen(mem)
        self.assertEqual(mem.snapshot(), snap)

    def test_invalid_temp_is_kept_too(self):
        mem = self.damaged(temp_record=encode_doc(B)[:30])
        reopen(mem)
        self.assertEqual(len(mem.read_file("quarantine/doc~9.tmp")), 30)

    def test_quarantine_never_overwrites(self):
        mem = self.damaged()
        reopen(mem)
        h = mem.open("docs/doc~9.tmp", create=True)
        mem.pwrite(h, 0, encode_doc(C))
        mem.close(h)
        reopen(mem)
        self.assertEqual(sorted(mem.listdir("quarantine")), ["doc~9.tmp", "doc~9.tmp.1"])

    def test_healthy_or_missing_destination_still_sweeps(self):
        mem, _ = shadow_with(A)
        h = mem.open("docs/doc~9.tmp", create=True)
        mem.pwrite(h, 0, encode_doc(B))
        mem.close(h)
        h = mem.open("docs/new~3.tmp", create=True)      # first save of 'new' interrupted
        mem.pwrite(h, 0, encode_doc(B))
        mem.close(h)
        e, report = reopen(mem)
        self.assertEqual(report.discarded, 2)
        self.assertEqual(e.get("doc"), A)
        self.assertIsNone(e.get("new"))
        self.assertFalse(mem.exists("quarantine"))

    def test_saving_over_a_damaged_document_preserves_it(self):
        mem = self.damaged()
        dest = mem.read_file("docs/doc")
        e, _ = reopen(mem)
        e.put("doc", C)
        self.assertEqual(e.get("doc"), C)
        self.assertEqual(mem.read_file("quarantine/doc.damaged"), dest)


class C3SpaceAndSize(unittest.TestCase):
    def test_payload_cap_is_declared_and_enforced_before_any_write(self):
        self.assertEqual(MAX_PAYLOAD, 1 << 20)
        mem, e = shadow_with(A)
        before = mem.snapshot()
        with self.assertRaises(PayloadTooLarge) as cm:
            e.commit({"other": b"x", "doc": b"\x42" * (MAX_PAYLOAD + 1)})
        self.assertEqual(cm.exception.errno, errno.EFBIG)
        self.assertEqual(mem.snapshot(), before)                  # not even 'other' was written
        e.put("doc", b"\x42" * MAX_PAYLOAD)                       # exactly the cap is fine

    def test_enospc_at_any_point_of_the_temp_write_keeps_a(self):
        big = b"\x42" * MAX_PAYLOAD
        need = len(encode_doc(big))
        for free in (0, 1, 23, 24, need // 2, need - 1):
            with self.subTest(free=free):
                mem, _ = shadow_with(A)
                fv = FaultyVFS(mem, free_bytes=free)
                e, _ = reopen(fv)
                with self.assertRaises(CommitAborted) as cm:
                    e.put("doc", big)
                err = cm.exception
                self.assertEqual(err.errno, errno.ENOSPC)
                self.assertFalse(err.committed)
                self.assertEqual(err.phase, "write-temp")
                self.assertEqual(err.detail["needed"], need)
                self.assertIn("Free at least", str(err))
                self.assertEqual(e.get("doc"), A)
                self.assertEqual(mem.listdir("docs"), ["doc"])        # temp removed
                self.assertEqual(fv.free_bytes, free)                # its space given back
                self.assertTrue(any(ev["kind"] == "enospc" for ev in fv.events))

    def test_store_stays_writable_after_enospc(self):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "s")
            with SafeStore.create(path, engine="shadow") as s:
                s.put("doc", A)
                fv = FaultyVFS(s.engine.vfs, free_bytes=100)
                s.engine.vfs = fv
                with self.assertRaises(CommitAborted):
                    s.put("doc", b"\x42" * 4096)
                self.assertIsNone(s.failed)
                fv.free_bytes = None
                s.put("doc", B)
                self.assertEqual(s.get("doc"), B)
        finally:
            shutil.rmtree(tmp)


class C4PostCommitFlushError(unittest.TestCase):
    def test_dir_fsync_failure_after_rename_reports_visible_not_durable(self):
        mem, _ = shadow_with(A)
        fv = FaultyVFS(mem)
        e, _ = reopen(fv)
        fv.fail("fsync_dir", errno.EIO)
        with self.assertRaises(VisibleNotDurable) as cm:
            e.put("doc", B)
        err = cm.exception
        self.assertTrue(err.committed)
        self.assertEqual((err.visible, err.phase, err.errno), ("new", "fsync-dir", errno.EIO))
        self.assertIsInstance(err, OSError)
        self.assertIsInstance(err, StoreError)
        self.assertEqual(e.get("doc"), B)

    def test_failures_before_the_rename_are_pre_commit_aborts(self):
        for op, skip in (("open", 0), ("pwrite", 0), ("fsync", 0), ("rename", 0)):
            with self.subTest(op=op):
                mem, _ = shadow_with(A)
                fv = FaultyVFS(mem)
                e, _ = reopen(fv)
                fv.fail(op, errno.EIO, skip=skip)
                with self.assertRaises(CommitAborted) as cm:
                    e.put("doc", B)
                self.assertFalse(cm.exception.committed)
                self.assertEqual(cm.exception.visible, "previous")
                self.assertEqual(e.get("doc"), A)
                self.assertEqual(mem.listdir("docs"), ["doc"])

    def test_store_contract(self):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "s")
            with SafeStore.create(path, engine="shadow") as s:
                s.put("doc", A)
                fv = FaultyVFS(s.engine.vfs)
                s.engine.vfs = fv
                fv.fail("fsync", errno.EIO)            # temp fsync: before the commit point
                with self.assertRaises(CommitAborted):
                    s.put("doc", B)
                self.assertIsNone(s.failed)
                fv.fail("fsync_dir", errno.EIO)        # after the rename
                with self.assertRaises(VisibleNotDurable):
                    s.put("doc", B)
                self.assertEqual(s.get("doc"), B)
                with self.assertRaises(StoreError):    # no blind retry: reopen first
                    s.put("doc", C)
            with SafeStore.open(path) as s:
                self.assertEqual(s.get("doc"), B)
                s.put("doc", B)
        finally:
            shutil.rmtree(tmp)

    def test_delete_and_multi_document_contracts(self):
        mem, _ = shadow_with(A)
        fv = FaultyVFS(mem)
        e, _ = reopen(fv)
        fv.fail("fsync_dir", errno.EIO)
        with self.assertRaises(VisibleNotDurable):
            e.delete("doc")
        self.assertIsNone(e.get("doc"))
        fv.fail("pwrite", errno.ENOSPC, skip=1)       # second document's temp
        with self.assertRaises(CommitAborted) as cm:
            e.commit({"x": A, "y": B})
        self.assertEqual(cm.exception.already_committed, ["x"])
        self.assertEqual((e.get("x"), e.get("y")), (A, None))


class C5ConcurrentWriters(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "s")
        with SafeStore.create(self.path, engine="shadow") as s:
            s.put("doc", A)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_file_lock_modes(self):
        p = os.path.join(self.tmp, "L")
        a, b, x = FileLock(p), FileLock(p), FileLock(p)
        self.assertTrue(a.acquire(shared=True))
        self.assertTrue(b.acquire(shared=True))
        self.assertFalse(x.acquire(blocking=False))
        a.release()
        b.release()
        self.assertTrue(x.acquire(blocking=False))
        self.assertFalse(a.acquire(shared=True, blocking=False))
        x.downgrade()
        self.assertTrue(a.acquire(shared=True, blocking=False))
        a.release()
        x.release()

    def test_commit_lock_excludes_threads(self):
        p = os.path.join(self.tmp, "COMMIT")
        inside, overlap = [], []

        def worker():
            for _ in range(30):
                with FileLock(p):
                    inside.append(1)
                    if len(inside) > 1:
                        overlap.append(1)
                    inside.pop()
        ts = [threading.Thread(target=worker) for _ in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(overlap, [])

    def test_sessions_share_and_recovery_leaves_live_temps_alone(self):
        sb = SafeStore.open(self.path, multi_writer=True)
        sc = SafeStore.open(self.path, multi_writer=True)
        try:
            cb, cc = sb.engine.prepare("doc", B), sc.engine.prepare("doc", C)
            self.assertNotEqual(cb.tmp, cc.tmp)
            with self.assertRaises(StoreError):
                SafeStore.open(self.path)                     # single-writer: excluded
            sr = SafeStore.open(self.path, multi_writer=True)
            self.assertTrue(sr.engine.live_peers)
            self.assertEqual(sr.recovery.discarded, 0)
            sr.close()
            sb.engine.publish(cb)
            sc.engine.publish(cc)
            self.assertEqual(sb.get("doc"), C)
        finally:
            sb.close()
            sc.close()
        with SafeStore.open(self.path) as s:                  # alone again: exclusive is fine
            self.assertEqual(s.get("doc"), C)
            self.assertEqual(s.recovery.discarded, 0)

    def test_orphans_of_dead_writers_are_swept_when_alone(self):
        s = SafeStore.open(self.path, multi_writer=True)
        s.engine.prepare("doc", B)                            # then "dies" without publishing
        s.close()
        s = SafeStore.open(self.path, multi_writer=True)
        self.assertEqual(s.recovery.discarded, 1)
        s.close()

    def test_conditional_put_detects_the_lost_update(self):
        sb = SafeStore.open(self.path, multi_writer=True)
        sc = SafeStore.open(self.path, multi_writer=True)
        try:
            base = sha256(sb.get("doc"))
            sb.put("doc", B, expect=base)
            with self.assertRaises(WriteConflict):
                sc.put("doc", C, expect=base)
            self.assertEqual(sc.get("doc"), B)
            self.assertEqual([n for n in os.listdir(os.path.join(self.path, "docs")) if n.endswith(".tmp")], [])
            with self.assertRaises(WriteConflict):
                sc.put("fresh", C, expect=sha256(A))
            sc.put("fresh", C, expect=None)
        finally:
            sb.close()
            sc.close()

    def test_update_across_sessions_loses_nothing(self):
        sessions = [SafeStore.open(self.path, multi_writer=True) for _ in range(3)]
        try:
            sessions[0].put("ctr", b"0")

            def inc(s):
                for _ in range(25):
                    s.update("ctr", lambda v: str(int(v) + 1).encode())
            ts = [threading.Thread(target=inc, args=(s,)) for s in sessions]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            self.assertEqual(sessions[0].get("ctr"), b"75")
        finally:
            for s in sessions:
                s.close()

    def test_only_shadow_supports_multi_writer(self):
        other = os.path.join(self.tmp, "cow")
        SafeStore.create(other, engine="cow").close()
        with self.assertRaises(StoreError):
            SafeStore.open(other, multi_writer=True)
        with SafeStore.open(other) as s:
            with self.assertRaises(StoreError):
                s.put("k", b"v", expect=None)


class ChallengeHarness(unittest.TestCase):
    def test_every_challenge_passes_end_to_end(self):
        env = dict(os.environ, PYTHONPATH=ROOT, NO_COLOR="1", PYTHONIOENCODING="utf-8")
        p = subprocess.run([sys.executable, "-m", "safewrite", "challenge"], capture_output=True,
                           encoding="utf-8", errors="replace", env=env, timeout=600)
        self.assertEqual(p.returncode, 0, p.stdout[-4000:] + p.stderr[-2000:])
        for cid in ("C1", "C2", "C3", "C4", "C5"):
            self.assertIn(f"✓ {cid}", p.stdout)


if __name__ == "__main__":
    unittest.main()
