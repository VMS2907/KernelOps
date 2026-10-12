"""I/O errors: a full disk or a failing fsync must never corrupt committed data."""
import errno
import os
import shutil
import tempfile
import unittest

from safewrite import SafeStore, StoreError
from safewrite.engines import make
from safewrite.vfs import MemVFS


class FaultyVFS(MemVFS):
    """MemVFS that fails the n-th mutating call with ENOSPC (writes) or EIO (fsync)."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.fail_at = None
        self.calls = 0

    def _tick(self, err):
        self.calls += 1
        if self.fail_at is not None and self.calls == self.fail_at:
            raise OSError(err, os.strerror(err))

    def pwrite(self, h, offset, data):
        self._tick(errno.ENOSPC)
        super().pwrite(h, offset, data)

    def fsync(self, h):
        self._tick(errno.EIO)
        super().fsync(h)

    def rename(self, src, dst):
        self._tick(errno.EIO)
        super().rename(src, dst)


class IOErrors(unittest.TestCase):
    def test_error_at_every_syscall_never_corrupts(self):
        A, B = b"A" * 5000, b"B" * 7000
        for spec in ("shadow", "undo", "wal", "cow"):
            for n in range(1, 25):
                with self.subTest(engine=spec, failing_call=n):
                    vfs = FaultyVFS()
                    e = make(spec, vfs, **({"checkpoint_every": 1} if spec == "wal" else {}))
                    e.format()
                    e.open()
                    e.put("doc", A)
                    vfs.calls, vfs.fail_at = 0, n
                    try:
                        e.put("doc", B)
                    except OSError:
                        pass
                    vfs.fail_at = None
                    e2 = make(spec, vfs)          # reopen = recovery
                    e2.open()
                    self.assertIn(e2.get("doc"), (A, B))
                    self.assertEqual(e2.verify(), [])

    def test_store_refuses_writes_after_a_failure_until_reopened(self):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "s")
            s = SafeStore.create(path, engine="cow")
            s.put("doc", b"good")
            real_fsync = s.engine.vfs.fsync

            def broken(h):
                raise OSError(errno.EIO, "I/O error")
            s.engine.vfs.fsync = broken
            with self.assertRaises(StoreError):
                s.put("doc", b"new")
            s.engine.vfs.fsync = real_fsync
            with self.assertRaises(StoreError):       # fsyncgate: no retry on a poisoned handle
                s.put("doc", b"retry")
            self.assertEqual(s.get("doc"), b"good")
            s.close()
            with SafeStore.open(path) as s2:           # reopening runs recovery
                self.assertIn(s2.get("doc"), (b"good", b"new"))
                s2.put("doc", b"after")
                self.assertEqual(s2.get("doc"), b"after")
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
