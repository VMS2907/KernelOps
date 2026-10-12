"""The VFS layer: MemVFS must behave like the real file system, and the recorder
must track inode identity across renames."""
import os
import shutil
import tempfile
import unittest

from safewrite.vfs import CrashNow, MemVFS, OsVFS, RecordingVFS


POSIX = os.name != "nt"


def script(vfs):
    """A sequence touching every VFS operation; returns observations."""
    obs = []
    vfs.mkdir("d")
    h = vfs.open("d/a", create=True)
    vfs.pwrite(h, 0, b"hello")
    vfs.pwrite(h, 10, b"world")          # hole -> zeros
    obs.append(vfs.pread(h, 0, 100))
    vfs.fsync(h)
    if POSIX:
        vfs.rename("d/a", "d/b")         # handle keeps pointing at the inode
    else:                                # Windows cannot rename an open file
        vfs.close(h)
        vfs.rename("d/a", "d/b")
        h = vfs.open("d/b")
    vfs.pwrite(h, 0, b"HELLO")
    vfs.ftruncate(h, 12)
    obs.append(vfs.size(h))
    vfs.close(h)
    obs.append(vfs.read_file("d/b"))
    obs.append(vfs.exists("d/a"))
    h2 = vfs.open("d/c", create=True, truncate=True)
    vfs.pwrite(h2, 0, b"c")
    vfs.close(h2)
    vfs.rename("d/c", "d/b")             # rename over an existing file
    obs.append(vfs.read_file("d/b"))
    obs.append(vfs.listdir("d"))
    vfs.unlink("d/b")
    vfs.fsync_dir("d")
    obs.append(vfs.listdir("d"))
    h3 = vfs.open("d/t", create=True)
    vfs.pwrite(h3, 0, b"abcdef")
    vfs.close(h3)
    h3 = vfs.open("d/t", truncate=True)
    obs.append(vfs.size(h3))
    vfs.close(h3)
    with_missing = []
    try:
        vfs.open("d/missing")
    except FileNotFoundError:
        with_missing.append("FileNotFoundError")
    obs.append(with_missing)
    obs.append(vfs.walk_files())
    return obs


class Equivalence(unittest.TestCase):
    def test_memvfs_matches_real_filesystem(self):
        tmp = tempfile.mkdtemp()
        try:
            real = script(OsVFS(tmp))
            mem = script(MemVFS())
            self.assertEqual(real, mem)
        finally:
            shutil.rmtree(tmp)

    def test_recording_wrapper_is_transparent(self):
        self.assertEqual(script(RecordingVFS(MemVFS())), script(MemVFS()))


class Recorder(unittest.TestCase):
    def test_inode_identity_follows_rename(self):
        rec = RecordingVFS(MemVFS({"docs/x": b"old"}))
        h = rec.open("docs/x~1.tmp", create=True)
        rec.pwrite(h, 0, b"new")
        rec.close(h)
        rec.rename("docs/x~1.tmp", "docs/x")
        create, write, rename = rec.mutating_ops()
        self.assertEqual(create.ino, write.ino)
        self.assertEqual(rename.ino, write.ino)
        self.assertEqual(rename.replaced, rec.base.names["docs/x"])

    def test_crash_injection_by_syscall_number(self):
        rec = RecordingVFS(MemVFS(), crash_at=2)
        h = rec.open("f", create=True)               # syscall 1: creat
        with self.assertRaises(CrashNow):
            rec.pwrite(h, 0, b"x")                   # syscall 2: never happens
        self.assertEqual(rec.inner.read_file("f"), b"")

    def test_crash_injection_by_phase(self):
        rec = RecordingVFS(MemVFS(), crash_at_label="commit")
        rec.note("prepare")
        with self.assertRaises(CrashNow):
            rec.note("commit")

    def test_partial_write_models_a_kill_inside_write(self):
        rec = RecordingVFS(MemVFS(), crash_at=2, partial_write=0.5)
        h = rec.open("f", create=True)
        with self.assertRaises(CrashNow):
            rec.pwrite(h, 0, b"abcdefgh")
        self.assertEqual(rec.inner.read_file("f"), b"abcd")


class RealFsync(unittest.TestCase):
    def test_directory_fsync_works_on_this_platform(self):
        tmp = tempfile.mkdtemp()
        try:
            v = OsVFS(tmp)
            v.mkdir("x")
            v.fsync_dir("x")
            v.fsync_dir("")
            self.assertEqual(v.stats.dir_fsyncs, 2)
        finally:
            shutil.rmtree(tmp)

    def test_binary_safety(self):
        tmp = tempfile.mkdtemp()
        try:
            v = OsVFS(tmp)
            h = v.open("b", create=True)
            data = bytes(range(256)) * 4 + b"\r\n\n\r\x1a"
            v.pwrite(h, 0, data)
            v.close(h)
            with open(os.path.join(tmp, "b"), "rb") as f:
                self.assertEqual(f.read(), data)
        finally:
            shutil.rmtree(tmp)


if __name__ == "__main__":
    unittest.main()
