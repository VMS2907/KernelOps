"""Functional behaviour of every engine on both the in-memory and the real FS."""
import os
import shutil
import tempfile
import threading
import unittest

from safewrite.engines import ENGINES, all_specs, make
from safewrite.format import IntegrityError
from safewrite.vfs import MemVFS, OsVFS, RecordingVFS, CrashNow

ALL = list(ENGINES)


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def backends(self, name):
        path = os.path.join(self.tmp, name.replace("/", "_"))
        os.makedirs(path, exist_ok=True)
        return [("mem", MemVFS()), ("os", OsVFS(path))]

    def fresh(self, spec, vfs, **opts):
        e = make(spec, vfs, **opts)
        e.format()
        e.open()
        return e


class Basics(Fixture):
    def test_crud_and_reopen(self):
        big = os.urandom(1 << 20)
        for spec in ALL:
            for kind, vfs in self.backends(spec):
                with self.subTest(engine=spec, fs=kind):
                    e = self.fresh(spec, vfs)
                    e.put("a", b"1")
                    e.put("ünï 文档", b"unicode key")
                    e.put("big", big)
                    e.put("empty", b"")
                    e.commit({"a": b"2", "b": b"3"})
                    e.delete("b")
                    self.assertEqual(e.get("a"), b"2")
                    self.assertIsNone(e.get("b"))
                    self.assertIsNone(e.get("never"))
                    e.close()
                    e2 = make(spec, vfs)
                    e2.open()
                    self.assertEqual(e2.get("a"), b"2")
                    self.assertEqual(e2.get("big"), big)
                    self.assertEqual(e2.get("empty"), b"")
                    self.assertEqual(e2.get("ünï 文档"), b"unicode key")
                    self.assertEqual(e2.keys(), sorted(["a", "big", "empty", "ünï 文档"]))
                    self.assertEqual(e2.verify(), [])
                    e2.close()

    def test_many_documents_and_overwrites(self):
        for spec in ALL:
            with self.subTest(engine=spec):
                e = self.fresh(spec, MemVFS())
                expect = {}
                for i in range(300):
                    k = f"doc{i % 37}"
                    v = (f"{i}:" * (i % 50 + 1)).encode()
                    e.put(k, v)
                    expect[k] = v
                e.close()
                e = make(spec, e.vfs)
                e.open()
                for k, v in expect.items():
                    self.assertEqual(e.get(k), v)

    def test_variants_are_constructible(self):
        for spec in all_specs():
            e = self.fresh(spec, MemVFS())
            e.put("k", b"v")
            self.assertEqual(e.get("k"), b"v")

    def test_rejects_bad_input(self):
        e = self.fresh("cow", MemVFS())
        with self.assertRaises(TypeError):
            e.put("k", "not bytes")
        with self.assertRaises(ValueError):
            e.put("", b"x")


class Recovery(Fixture):
    def crash_mid_commit(self, spec, at, initial=b"A" * 3000, new=b"B" * 2000, **opts):
        mem = MemVFS()
        e = self.fresh(spec, mem, **opts)
        e.put("doc", initial)
        e.close()
        rec = RecordingVFS(mem)
        e = make(spec, rec, **opts)
        e.open()
        rec.reset()
        rec.crash_at = at
        try:
            e.commit({"doc": new})
            crashed = False
        except CrashNow:
            crashed = True
        e2 = make(spec, mem, **opts)  # process crash: everything issued is on "disk"
        report = e2.open()
        return crashed, e2.get("doc"), report

    def test_process_crash_at_every_syscall(self):
        for spec in ("shadow", "undo", "wal", "cow"):
            for at in range(1, 30):
                with self.subTest(engine=spec, syscall=at):
                    crashed, got, _ = self.crash_mid_commit(spec, at)
                    self.assertIn(got, (b"A" * 3000, b"B" * 2000))
                    if not crashed:
                        self.assertEqual(got, b"B" * 2000)

    def test_naive_produces_a_mixture(self):
        _, got, _ = self.crash_mid_commit("naive", 2, b"A" * 3000, b"B" * 2000)
        self.assertNotIn(got, (b"A" * 3000, b"B" * 2000))
        self.assertTrue(got.startswith(b"B") and got.endswith(b"A"))

    def test_undo_rolls_back_to_previous(self):
        crashed, got, rep = self.crash_mid_commit("undo", 4)
        self.assertTrue(crashed)
        self.assertEqual(got, b"A" * 3000)
        self.assertEqual(rep.rolled_back, 1)

    def test_wal_rolls_forward_to_new(self):
        # syscalls: 1 append, 2 fsync(log) = COMMIT POINT, 3.. checkpoint
        crashed, got, rep = self.crash_mid_commit("wal", 3, checkpoint_every=1)
        self.assertTrue(crashed)
        self.assertEqual(got, b"B" * 2000)
        self.assertGreaterEqual(rep.replayed, 1)

    def test_recovery_is_idempotent(self):
        for spec in ("shadow", "undo", "wal", "cow"):
            mem = MemVFS()
            e = self.fresh(spec, mem)
            e.put("doc", b"A")
            rec = RecordingVFS(mem)
            e = make(spec, rec)
            e.open()
            rec.reset()
            rec.crash_at = 2
            try:
                e.put("doc", b"B")
            except CrashNow:
                pass
            states = []
            for _ in range(3):
                e2 = make(spec, mem)
                e2.open()
                states.append((e2.get("doc"), mem.snapshot()))
            self.assertEqual(states[0], states[1])
            self.assertEqual(states[1], states[2])


class CopyOnWrite(Fixture):
    def test_history_and_restore(self):
        e = self.fresh("cow", MemVFS(), keep_versions=8)
        for i in range(5):
            e.put("doc", f"v{i}".encode())
        hist = e.history("doc")
        self.assertEqual(len(hist), 5)
        self.assertEqual(e.get_version("doc", hist[-1].version), b"v0")
        e.delete("doc")
        self.assertTrue(e.history("doc")[0].deleted)

    def test_garbage_collection_reuses_blocks(self):
        e = self.fresh("cow", MemVFS(), keep_versions=2, block_size=512)
        for i in range(200):
            e.put("doc", bytes([i % 256]) * 1500)
        self.assertLess(e._high, 40, "freed blocks must be recycled, not leaked")

    def test_snapshot_isolation_survives_gc(self):
        e = self.fresh("cow", MemVFS(), keep_versions=2, block_size=512)
        e.commit({"a": b"a0" * 300, "b": b"b0" * 300})
        with e.snapshot() as snap:
            for i in range(1, 30):
                e.commit({"a": b"a%d" % i * 300, "b": b"b%d" % i * 300})
            # 29 newer generations later, the pinned snapshot is still consistent
            self.assertEqual(snap.get("a"), b"a0" * 300)
            self.assertEqual(snap.get("b"), b"b0" * 300)
        self.assertEqual(e.get("a"), b"a29" * 300)

    def test_torn_superblock_falls_back(self):
        mem = MemVFS()
        e = self.fresh("cow", mem)
        e.put("doc", b"good")
        e.put("doc", b"newer")
        e.close()
        raw = bytearray(mem.read_file("volume.img"))
        gen_slot = 0 if raw[24:32] > raw[4096 + 24:4096 + 32] else 1   # newest generation's slot
        off = gen_slot * 4096
        raw[off + 30] ^= 0xFF                                         # tear it
        h = mem.open("volume.img")
        mem.pwrite(h, 0, bytes(raw))
        mem.close(h)
        e2 = make("cow", mem)
        rep = e2.open()
        self.assertEqual(e2.get("doc"), b"good")
        self.assertTrue(any("ignored" in ev.msg for ev in rep.events))


class WriteAheadLog(Fixture):
    def test_group_commit_shares_fsyncs(self):
        vfs = MemVFS()
        e = self.fresh("wal", vfs, checkpoint_every=10 ** 9)
        slow = vfs.fsync

        def slow_fsync(h):
            threading.Event().wait(0.003)          # a disk that takes 3 ms per flush
            slow(h)
        vfs.fsync = slow_fsync
        errors = []

        def worker(w):
            try:
                for i in range(20):
                    e.put(f"w{w}-{i}", b"x" * 100)
            except Exception as ex:   # pragma: no cover
                errors.append(ex)
        ts = [threading.Thread(target=worker, args=(w,)) for w in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(errors, [])
        st = e.group_commit_stats
        self.assertEqual(st["commits"], 160)
        self.assertLess(st["batches"], 120, "concurrent commits should share fsyncs")
        e.close()
        e2 = make("wal", vfs)
        e2.open()
        self.assertEqual(len(e2.keys()), 160)

    def test_torn_tail_is_ignored(self):
        mem = MemVFS()
        e = self.fresh("wal", mem, checkpoint_every=10 ** 9)
        e.put("doc", b"committed")
        h = mem.open("wal.log")
        size = mem.size(h)
        mem.pwrite(h, size, b"WREC" + b"\xff" * 40)    # garbage after the last record
        mem.close(h)
        e2 = make("wal", mem)
        rep = e2.open()
        self.assertEqual(e2.get("doc"), b"committed")
        self.assertGreaterEqual(rep.discarded, 1)


if __name__ == "__main__":
    unittest.main()
