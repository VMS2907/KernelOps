"""Synchronization: locks, group commit, MVCC, and the lost-update race."""
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

from safewrite import SafeStore, StoreError
from safewrite.concurrency import GroupCommit, RWLock
from safewrite.race import run_race

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Primitives(unittest.TestCase):
    def test_rwlock_excludes_writers(self):
        lock = RWLock()
        inside = []
        overlap = []

        def writer():
            for _ in range(200):
                with lock.write():
                    inside.append(1)
                    if len(inside) > 1:
                        overlap.append(1)
                    inside.pop()

        def reader():
            for _ in range(200):
                with lock.read():
                    if inside:
                        overlap.append(1)
        ts = [threading.Thread(target=writer) for _ in range(4)] + [threading.Thread(target=reader) for _ in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(overlap, [])

    def test_group_commit_batches_and_orders(self):
        durable = []

        def flush(batch):
            time.sleep(0.002)
        gc = GroupCommit(flush, lambda batch: durable.extend(batch))
        ts = [threading.Thread(target=gc.submit, args=(i,)) for i in range(64)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        self.assertEqual(sorted(durable), list(range(64)))
        self.assertLess(gc.batches, 64)

    def test_group_commit_propagates_failure(self):
        def flush(batch):
            raise OSError("disk on fire")
        gc = GroupCommit(flush, lambda b: None)
        with self.assertRaises(OSError):
            gc.submit(1)


class Stores(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_lost_update_race_and_its_fix(self):
        r = run_race("cow", threads=6, increments=30, directory=self.tmp)
        self.assertEqual(r["synchronized"], r["expected"])
        self.assertLessEqual(r["unsynchronized"], r["expected"])

    def test_transactions_are_isolated(self):
        path = os.path.join(self.tmp, "s")
        with SafeStore.create(path, engine="wal") as s:
            s.put("alice", b"100")
            s.put("bob", b"100")

            def transfer(n):
                for _ in range(n):
                    with s.transaction() as tx:
                        a, b = int(tx.get("alice")), int(tx.get("bob"))
                        tx.put("alice", str(a - 1).encode())
                        tx.put("bob", str(b + 1).encode())
            ts = [threading.Thread(target=transfer, args=(20,)) for _ in range(5)]
            for t in ts:
                t.start()
            for t in ts:
                t.join()
            self.assertEqual(int(s.get("alice")) + int(s.get("bob")), 200)
            self.assertEqual(int(s.get("bob")), 200)

    def test_second_process_is_refused(self):
        path = os.path.join(self.tmp, "locked")
        with SafeStore.create(path, engine="cow"):
            code = ("import sys; sys.path.insert(0, %r)\n"
                    "from safewrite import SafeStore, StoreError\n"
                    "try:\n    SafeStore.open(%r)\nexcept StoreError as e:\n    print('REFUSED', e)\n") % (ROOT, path)
            out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30).stdout
            self.assertIn("REFUSED", out)

    @unittest.skipUnless(hasattr(signal, "SIGKILL"), "POSIX only")
    def test_lock_is_released_when_holder_is_killed(self):
        path = os.path.join(self.tmp, "killed")
        SafeStore.create(path, engine="cow").close()
        code = ("import sys, time; sys.path.insert(0, %r)\n"
                "from safewrite import SafeStore\n"
                "s = SafeStore.open(%r); print('HOLDING', flush=True); time.sleep(60)\n") % (ROOT, path)
        p = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
        self.assertEqual(p.stdout.readline().strip(), "HOLDING")
        with self.assertRaises(StoreError):
            SafeStore.open(path)
        os.kill(p.pid, signal.SIGKILL)
        p.wait()
        SafeStore.open(path).close()      # no stale lock: the kernel released it


if __name__ == "__main__":
    unittest.main()
