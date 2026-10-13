"""Live technical challenges KO-16-C1 ... C5, as runnable, narrated demonstrations.

    python -m safewrite challenge                # all five
    python -m safewrite challenge c3 --keep      # one, keeping its files to inspect

Every challenge runs on real files in a scratch directory (never anyone's real
data), prints the actual disk state before and after, the protocol, resource
or scheduling trace, and a list of checks.  Faults are simulated by a
controlled adapter (:class:`~safewrite.vfs.faulty.FaultyVFS`) between the
engine and the real disk: nothing fills the host disk or touches hardware.
Exit status 0 means every check passed.
"""

from __future__ import annotations

import errno
import multiprocessing
import os
import queue as _queue
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from .cli import bad, c, head, note, ok
from .engines import make
from .engines.shadow import ANY, ShadowEngine
from .errors import CommitAborted, PayloadTooLarge, StoreError, VisibleNotDurable, WriteConflict
from .format import IntegrityError, decode_doc, encode_doc, sha256
from .store import SafeStore
from .vfs import FaultyVFS, OsVFS, RecordingVFS

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
A64, B64, C64 = b"\x41" * 64, b"\x42" * 64, b"\x43" * 64

# One clock for every process on the machine, for the C5 timeline.
# ``time.perf_counter`` is per-process on some platforms and versions (macOS,
# Windows with Python 3.9), so read the system-wide counters directly.
if os.name == "nt":  # pragma: no cover - Windows
    import ctypes
    _QPF = ctypes.c_int64()
    ctypes.windll.kernel32.QueryPerformanceFrequency(ctypes.byref(_QPF))

    def _now() -> float:
        t = ctypes.c_int64()
        ctypes.windll.kernel32.QueryPerformanceCounter(ctypes.byref(t))
        return t.value / _QPF.value
elif hasattr(time, "clock_gettime") and hasattr(time, "CLOCK_MONOTONIC"):
    def _now() -> float:
        return time.clock_gettime(time.CLOCK_MONOTONIC)
else:  # pragma: no cover
    _now = time.time


# ----------------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------------
class Checks:
    """The pass/fail list of one challenge."""

    def __init__(self, cid: str) -> None:
        self.cid = cid
        self.items: List[Tuple[bool, str]] = []

    def check(self, cond: Any, msg: str) -> bool:
        self.items.append((bool(cond), msg))
        (ok if cond else bad)(msg)
        return bool(cond)

    @property
    def passed(self) -> bool:
        return bool(self.items) and all(r for r, _ in self.items)


def shape(data: Optional[bytes]) -> str:
    if data is None:
        return "absent"
    if data and data == data[:1] * len(data):
        return f"{len(data):,} × 0x{data[0]:02X}"
    return f"{len(data):,} bytes, sha256 {sha256(data)[:12]}"


def label(data: Optional[bytes], known: Dict[str, bytes]) -> str:
    for name, v in known.items():
        if data == v:
            return f"{name} ({shape(v)})"
    return shape(data)


def hexview(data: Optional[bytes]) -> str:
    if data is None:
        return "(absent)"
    head8 = " ".join(f"{b:02x}" for b in data[:8])
    return f"{head8}{' …' if len(data) > 8 else ''} ({len(data):,} bytes)"


def show_dir(root: str, known: Dict[str, bytes], title: str, raw_sha: bool = False) -> Dict[str, bytes]:
    """Print every file under docs/ and quarantine/ with its validity; return their bytes."""
    print(c(f"  {title}", "bold"))
    seen: Dict[str, bytes] = {}
    for sub in ("docs", "quarantine"):
        d = os.path.join(root, sub)
        if not os.path.isdir(d):
            continue
        for n in sorted(os.listdir(d)):
            with open(os.path.join(d, n), "rb") as f:
                raw = f.read()
            seen[f"{sub}/{n}"] = raw
            try:
                status = c("valid", "green") + ", payload " + label(decode_doc(raw), known)
            except IntegrityError as e:
                status = c("INVALID", "red") + f" ({e})"
            extra = f"  raw sha256 {sha256(raw)[:12]}" if raw_sha else ""
            print(f"    {sub + '/' + n:<34} {len(raw):>10,} B  {status}{extra}")
    if not seen:
        print("    (empty)")
    return seen


def events(report: Any, prefix: str = "recovery: ") -> None:
    icon = {"ok": "✓", "discard": "✂", "warn": "!", "error": "✗", "info": "·"}
    for ev in report.events:
        print(c(f"    {icon.get(ev.kind, '·')} {prefix}{ev.msg}", "grey" if ev.kind == "info" else
                {"error": "red", "warn": "yellow", "discard": "magenta"}.get(ev.kind, "green")))


def explain(mechanism: str, assumptions: str) -> None:
    print(c("  OS mechanism: ", "bold") + mechanism)
    print(c("  Assumptions:  ", "bold") + assumptions)


def _sim_outcomes(old: bytes, new: bytes, layout: str = "unique") -> Dict[Tuple[str, str], Dict[str, int]]:
    """Power-loss view of the three crash points: every disk state the model allows."""
    from .crashsim.explorer import record, recover_and_judge, window
    from .crashsim.model import MODELS, build_image, enumerate_states
    from .crashsim.scenarios import Scenario
    sc = Scenario("challenge", "challenge", "", {"doc": old}, [{"doc": new}],
                  {"shadow": {"temp_names": layout}})
    rec = record("shadow", sc)
    trace = rec.trace
    first = {k: next(op for op in trace if op.kind == k) for k in ("write", "fsync", "rename")}
    points = {"during_write": first["write"].idx + 1, "after_validation": first["fsync"].idx + 1,
              "after_replace": first["rename"].idx + 1}
    out: Dict[Tuple[str, str], Dict[str, int]] = {}
    for name, point in points.items():
        for model in ("process", "power"):
            # process model: the call *in flight* is the one after the crash point
            p = point - 1 if model == "process" and name == "during_write" else point
            states, _ = enumerate_states(trace, rec.base, p, MODELS[model])
            a, b = window(trace, p)
            tally = {"A": 0, "B": 0, "bad": 0}
            for st in states:
                img = build_image(trace, rec.base, st)
                o, _, _ = recover_and_judge(rec, img.files, img.dirs, a, b)
                tally["bad" if not o.ok else "A" if o.version == 0 else "B"] += 1
            out[(name, model)] = tally
    return out


def _tally(t: Dict[str, int]) -> str:
    bits = [f"A×{t['A']}" if t["A"] else "", f"B×{t['B']}" if t["B"] else ""]
    s = " ".join(b for b in bits if b) or "-"
    return s + (c(f"  corrupt×{t['bad']}", "red") if t["bad"] else "  corrupt×0")


def _child_env() -> Dict[str, str]:
    return dict(os.environ, PYTHONPATH=ROOT + os.pathsep + os.environ.get("PYTHONPATH", ""),
                NO_COLOR="1", PYTHONIOENCODING="utf-8")


# ----------------------------------------------------------------------------
# C1: SafeWrite crash points (Easy)
# ----------------------------------------------------------------------------
def c1(work: str, chk: Checks) -> None:
    head("C1 · SafeWrite crash points  [KO-16-C1, Easy]")
    print("  A = 64 bytes of 0x41 is committed.  Then B = 64 bytes of 0x42 is saved and the saving\n"
          "  process is REALLY killed (os._exit in a child process) at each documented crash point.\n"
          "  Recovery runs, and the recovered payload is compared byte for byte.")
    fb = os.path.join(work, "B.bin")
    with open(fb, "wb") as f:
        f.write(B64)
    known = {"A": A64, "B": B64}
    expected = {"during_write": "A", "after_validation": "A", "after_replace": "B"}
    real: Dict[Tuple[str, str], str] = {}
    for layout in ("fixed", "unique"):
        store = os.path.join(work, f"c1-{layout}-temp")
        SafeStore.create(store, engine="shadow", temp_names=layout).close()
        verbose = layout == "fixed"
        if verbose:
            print(c("\n  ── round-1 layout: one fixed temp name per document (docs/doc~.tmp) ──", "cyan"))
        for point, cp in ShadowEngine.CRASH_POINTS.items():
            with SafeStore.open(store) as s:
                s.put("doc", A64)
            p = subprocess.run([sys.executable, "-m", "safewrite", "crash", store, "doc", "--file", fb,
                                "--phase", point], capture_output=True, encoding="utf-8", errors="replace",
                               env=_child_env(), timeout=120)
            if verbose:
                print(c(f"\n  crash point {point}", "bold") + c(f": {cp['means']}", "grey"))
                crash = next((ln.strip() for ln in p.stdout.splitlines() if "CRASH" in ln), "")
                note(crash.replace("⚡ ", "") or f"child exit status {p.returncode}")
                show_dir(store, known, "disk right after the crash (before recovery):")
            with SafeStore.open(store) as s:
                report, got = s.recovery, s.get("doc")
            if verbose:
                events(report)
            want = A64 if expected[point] == "A" else B64
            real[(layout, point)] = "A" if got == A64 else "B" if got == B64 else "OTHER"
            chk.check(p.returncode == 77 and got == want,
                      f"[{layout} temp] {point}: process killed (exit {p.returncode}); recovered payload is "
                      f"exactly {expected[point]}: {hexview(got)}")
    print(c("\n  power loss instead of a process crash (simulator: every disk state the model allows)", "cyan"))
    sim = _sim_outcomes(A64, B64, "fixed")
    print(c(f"    {'crash point':<18}{'real kill -9 (both layouts)':<30}{'power loss (all states)':<34}", "bold"))
    for point in ShadowEngine.CRASH_POINTS:
        r = {real[(lay, point)] for lay in ("fixed", "unique")}
        print(f"    {point:<18}{'/'.join(sorted(r)):<30}{_tally(sim[(point, 'power')])}")
    chk.check(all(sim[(p, "power")]["bad"] == 0 for p in ShadowEngine.CRASH_POINTS),
              "power loss at every crash point: never a mixture or garbage")
    chk.check(sim[("after_validation", "power")]["B"] == 0,
              "a complete, valid temp is never promoted: before the rename, B cannot come back")
    note("after_replace under power loss: the directory was not fsynced yet, so A or B may come back; both are "
         "allowed because put() had not returned (no acknowledgement)")
    explain("rename(2) atomically switches which inode the name refers to; fsync(file) before it makes B's\n"
            "                bytes durable first, fsync(dir) after it makes the switch itself durable. The temp\n"
            "                file is never the committed state: only the rename commits.",
            "process crash = the kernel survives (page cache kept); journaled metadata (ext4/XFS/APFS/NTFS).")


# ----------------------------------------------------------------------------
# C2: invalid destination refusal (Medium)
# ----------------------------------------------------------------------------
def c2(work: str, chk: Checks) -> None:
    head("C2 · Invalid destination refusal  [KO-16-C2, Medium]")
    print("  The destination's header is truncated (damaged after commit: bit rot, a bad sector, an\n"
          "  outside tool), and a valid-looking but UNCOMMITTED temp with B is lying next to it. There is\n"
          "  no backup. Recovery must neither promote the temp nor destroy any of the evidence.")
    store = os.path.join(work, "c2")
    SafeStore.create(store, engine="shadow").close()
    with SafeStore.open(store) as s:
        s.put("doc", A64)
    dest, tmp = os.path.join(store, "docs", "doc"), os.path.join(store, "docs", "doc~9.tmp")
    with open(dest, "r+b") as f:
        f.truncate(10)                      # header is 24 bytes: cut it to 10
    with open(tmp, "wb") as f:
        f.write(encode_doc(B64))            # a complete, checksum-valid record of B
    known = {"A": A64, "B": B64}
    before = show_dir(store, known, "disk before recovery:", raw_sha=True)
    print(c("  why not promote the temp? ", "bold") + "its checksum proves the bytes are INTACT, not that they\n"
          "  were COMMITTED. Only the rename commits; this temp was never renamed. It may be a save that\n"
          "  failed, was cancelled, lost a conflict, or is older than the damaged version.")
    with SafeStore.open(store) as s:
        report = s.recovery
        events(report)
        after = show_dir(store, known, "disk after recovery:", raw_sha=True)
        try:
            got: Optional[bytes] = s.get("doc")
            refused = ""
        except IntegrityError as e:
            got, refused = None, str(e)
        problems = s.verify()
    chk.check(refused, f"reading doc is refused with an integrity error ({refused})")
    chk.check(got != B64, "the uncommitted B temp was NOT promoted")
    chk.check(after.get("docs/doc") == before["docs/doc"], "damaged destination left byte-identical in place")
    chk.check(after.get("quarantine/doc~9.tmp") == before["docs/doc~9.tmp"],
              "temp preserved byte-identical in quarantine/ (forensic content kept)")
    chk.check(problems and "doc" in problems[0], f"verify reports it: {problems[0] if problems else ''}")
    SafeStore.open(store).close()
    chk.check(_files(store) == after, "recovery is idempotent: a second recovery changes nothing")
    print(c("\n  operator repair: save a known-good version (here: A again)", "cyan"))
    with SafeStore.open(store) as s:
        s.put("doc", A64)
        for msg in [ev.msg for ev in s.engine.events if ev.kind == "warn"]:
            note(msg)
        fixed = s.get("doc")
    final = show_dir(store, known, "disk after the repair:", raw_sha=True)
    chk.check(fixed == A64, "the repair commits normally")
    chk.check(final.get("quarantine/doc.damaged") == before["docs/doc"] and
              final.get("quarantine/doc~9.tmp") == before["docs/doc~9.tmp"],
              "nothing that was on disk was destroyed: both originals are in quarantine/")
    explain("integrity (CRC-32 over header and payload) is checked separately from commitment (the rename\n"
            "                is the only commit point). rename(2) moves evidence aside without copying a byte.",
            "damage after commit is out of the protocol's control (media/tools), so it is reported, never repaired by guessing.")


def _files(store: str) -> Dict[str, bytes]:
    out = {}
    for sub in ("docs", "quarantine"):
        d = os.path.join(store, sub)
        if os.path.isdir(d):
            for n in os.listdir(d):
                with open(os.path.join(d, n), "rb") as f:
                    out[f"{sub}/{n}"] = f.read()
    return out


# ----------------------------------------------------------------------------
# C3: space / size constraint (Medium)
# ----------------------------------------------------------------------------
def c3(work: str, chk: Checks) -> None:
    head("C3 · Space / size constraint  [KO-16-C3, Medium]")
    store = os.path.join(work, "c3")
    SafeStore.create(store, engine="shadow").close()
    s = SafeStore.open(store)
    try:
        s.put("doc", A64)
        cap = s.engine.opt["max_payload"]
        B = b"\x42" * cap
        rec_len = len(encode_doc(B))
        print(f"  Declared payload cap: {cap:,} bytes (1 MiB), enforced before anything is written.\n"
              f"  A = 64 × 0x41 is committed. B = {cap:,} × 0x42 (exactly the cap; {rec_len:,} bytes on disk).\n"
              f"  A controlled adapter gives the store {rec_len // 2:,} bytes of free space, so the temp\n"
              f"  write runs out halfway. The host disk itself never fills.")
        known = {"A": A64, "B": B}
        print(c("\n  1) a document over the cap", "cyan"))
        try:
            s.put("doc", B + b"\x42")
            err: Optional[BaseException] = None
        except PayloadTooLarge as e:
            err = e
            note(str(e))
        chk.check(isinstance(err, PayloadTooLarge) and err.errno == errno.EFBIG,
                  "refused up front with PayloadTooLarge (EFBIG); nothing was written")
        fv = FaultyVFS(s.engine.vfs, free_bytes=rec_len // 2)
        s.engine.vfs = fv
        print(c("\n  2) B at the cap, device fills up halfway through the temp file", "cyan"))
        try:
            s.put("doc", B)
            err = None
        except CommitAborted as e:
            err = e
        print(c("  resource trace (controlled adapter):", "bold"))
        for ev in fv.events:
            print(c(f"    {ev['kind']:<7} {ev['op']:<8} {ev['path']:<22} {ev['detail']}  (free now {ev['free']:,})",
                    "red" if ev["kind"] == "enospc" else "grey"))
        print(c("  the error the caller gets:", "bold"))
        print("    " + str(err).replace(". ", ".\n    "))
        chk.check(isinstance(err, CommitAborted) and err.errno == errno.ENOSPC and not err.committed,
                  "CommitAborted with errno ENOSPC, committed=False (a pre-commit abort)")
        chk.check(s.get("doc") == A64, f"A retained, exact bytes: {hexview(s.get('doc'))}")
        listing = show_dir(store, known, "disk after the failure:")
        chk.check(set(listing) == {"docs/doc"}, "the partial temp was removed; its space was given back")
        chk.check(s.failed is None, "the store is still writable (nothing was committed, nothing is poisoned)")
        print(c("\n  3) the operator frees space and retries", "cyan"))
        fv.free_bytes = 4 << 20
        s.put("doc", B)
        chk.check(s.get("doc") == B, "retry commits B (exactly the cap) once space is available")
    finally:
        s.close()
    with SafeStore.open(store) as s2:
        chk.check(s2.get("doc") == B and not s2.verify(), "after reopening: B, every checksum valid")
    explain("write(2) returns a short count and then ENOSPC when the file system cannot allocate blocks;\n"
            "                unlink(2) of the temp returns them. The new copy is written in full BESIDE the old one,\n"
            "                so a save needs free space ≥ the new document; A is never touched before the rename.",
            "the adapter models a full device faithfully (short write, then ENOSPC); the cap is per document.")


# ----------------------------------------------------------------------------
# C4: post-rename flush error (Hard)
# ----------------------------------------------------------------------------
def c4(work: str, chk: Checks) -> None:
    head("C4 · Post-rename flush error  [KO-16-C4, Hard]")
    print("  A is committed. B is prepared and validated, the rename succeeds, and then fsync(dir)\n"
          "  fails with EIO (injected). B is ALREADY VISIBLE, so the error must not look like the\n"
          "  pre-commit abort that leaves A in place.")
    store = os.path.join(work, "c4")
    SafeStore.create(store, engine="shadow").close()
    s = SafeStore.open(store)
    try:
        s.put("doc", A64)
        rec = RecordingVFS(s.engine.vfs)
        fv = FaultyVFS(rec)
        s.engine.vfs = fv
        fv.fail("fsync_dir", errno.EIO)
        try:
            s.put("doc", B64)
            err: Optional[BaseException] = None
        except StoreError as e:
            err = e
        print(c("  protocol trace:", "bold"))
        for op in rec.trace:
            if op.kind == "note":
                if op.label == "commit":
                    print(c("          ── COMMIT POINT: rename done, B visible ──", "green"))
                continue
            print(f"    [{op.info.get('syscall'):>2}] {op.describe()}")
        print(c(f"    [{rec.syscalls + 1:>2}] fsync(dir docs)  ✗ EIO (injected by the adapter)", "red"))
        print(c("  the error contract:", "bold"))
        print(c("    before:  ", "grey") + "any OSError → StoreError('write failed …'): the caller cannot tell\n"
              "             whether doc is still A or already B, and typically reports \"not saved\".")
        if isinstance(err, VisibleNotDurable):
            print(c("    now:     ", "grey") + f"{type(err).__name__}(committed={err.committed}, visible={err.visible!r}, "
                  f"phase={err.phase!r}, errno={errno.errorcode.get(err.errno or 0)})")
            print("             " + str(err).replace(". ", ".\n             "))
        chk.check(isinstance(err, VisibleNotDurable) and err.committed and err.visible == "new"
                  and err.errno == errno.EIO and err.phase == "fsync-dir",
                  "VisibleNotDurable: committed=True, visible='new', phase='fsync-dir', errno EIO")
        chk.check(isinstance(err, OSError) and isinstance(err, StoreError),
                  "still an OSError and a StoreError, so existing handlers keep working")
        chk.check(s.get("doc") == B64, f"readers already see B: {hexview(s.get('doc'))}")
        try:
            s.put("doc", C64)
            refused = False
        except StoreError:
            refused = True
        chk.check(refused, "further writes are refused until reopen (no fsync retry: fsyncgate)")
    finally:
        s.close()
    with SafeStore.open(store) as s:
        events(s.recovery)
        chk.check(s.get("doc") == B64, "after reopening (= recovery) the visible version is B, intact")
        s.put("doc", B64)
        chk.check(s.get("doc") == B64, "saving B again returns normally: now a durable acknowledgement")
    print(c("\n  contrast: the same EIO one step earlier, on fsync(temp), BEFORE the commit point", "cyan"))
    with SafeStore.open(store) as s:
        s.put("doc", A64)
        fv = FaultyVFS(s.engine.vfs)
        s.engine.vfs = fv
        fv.fail("fsync", errno.EIO)
        try:
            s.put("doc", B64)
            err = None
        except StoreError as e:
            err = e
        if err is not None:
            note(f"{type(err).__name__}(committed={getattr(err, 'committed', None)}, "
                 f"visible={getattr(err, 'visible', None)!r}, phase={getattr(err, 'phase', None)!r})")
        chk.check(isinstance(err, CommitAborted) and not err.committed and err.visible == "previous",
                  "pre-commit failure → CommitAborted: committed=False, visible='previous'")
        chk.check(s.get("doc") == A64, "readers still see A")
        s.put("doc", B64)
        chk.check(s.get("doc") == B64, "the store stayed writable; the retry commits B")
    sim = _sim_outcomes(A64, B64)
    t = sim[("after_replace", "power")]
    print(c("\n  power loss right after the failed fsync(dir) (simulator, every disk state): ", "cyan") + _tally(t))
    chk.check(t["bad"] == 0 and t["A"] and t["B"],
              "after a power loss either version may return, each intact: exactly what the error says")
    explain("rename(2) changes the in-memory directory at once (visible to every reader); only fsync(dir)\n"
            "                forces it to the journal. A failed fsync cannot be retried safely (Linux may drop the\n"
            "                error after reporting it once), so the store goes read-only until reopened.",
            "EIO from fsync(dir) is reported once; reopening + saving again re-establishes durability.")


# ----------------------------------------------------------------------------
# C5: concurrent writers (Hard)
# ----------------------------------------------------------------------------
class _TracedVFS(FaultyVFS):
    """Pass-through VFS that reports the commit-point syscalls to the timeline."""

    def __init__(self, inner: Any, emit: Callable[..., None]) -> None:
        super().__init__(inner)
        self._emit = emit

    def rename(self, src: str, dst: str) -> None:
        super().rename(src, dst)
        self._emit(f"rename({src.rsplit('/', 1)[-1]} → {dst.rsplit('/', 1)[-1]})  ★ commit point")

    def fsync_dir(self, path: str) -> None:
        super().fsync_dir(path)
        self._emit(f"fsync(dir {path or '/'}): the rename is durable")


class _TracedLock:
    """The COMMIT lock, plus clock-independent evidence: a mutual-exclusion
    witness (an ``O_EXCL`` marker file) and the commit order, appended to a
    file while the lock is held."""

    def __init__(self, inner: Any, emit: Callable[..., None], prefix: str, who: str) -> None:
        self.inner, self.emit, self.who = inner, emit, who
        self.witness, self.order = prefix + ".inside", prefix + ".order"
        self.fd: Optional[int] = None

    def __enter__(self) -> "_TracedLock":
        self.emit("waits for the COMMIT lock")
        t = _now()
        self.inner.__enter__()
        self.emit(f"holds the COMMIT lock (waited {(_now() - t) * 1000:.1f} ms)", lock="acquired")
        try:
            self.fd = os.open(self.witness, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            self.emit("!! another writer is inside the commit section", overlap=True)
        with open(self.order, "a") as f:
            f.write(self.who + "\n")
        return self

    def __exit__(self, *exc: object) -> None:
        if self.fd is not None:
            os.close(self.fd)
            os.unlink(self.witness)
            self.fd = None
        self.emit("releases the COMMIT lock", lock="released")
        self.inner.__exit__(*exc)


def _writer_proc(path: str, who: str, payload: bytes, expect: Any, barrier: Any, q: Any,
                 prefix: str) -> None:
    """One writer process: open shared, prepare, barrier, commit, close."""
    def emit(msg: str, **kw: Any) -> None:
        q.put({"t": _now(), "who": who, "msg": msg, **kw})
    live = f"{prefix}.live-{who}"      # exists exactly while this writer's session is open
    try:
        open(live, "w").close()
        s = SafeStore.open(path, multi_writer=True)
        emit(f"opened the store (session lock SHARED, writer id {s.engine.writer_id})")
        eng = s.engine
        eng.vfs = _TracedVFS(eng.vfs, emit)
        eng.commit_lock = _TracedLock(eng.commit_lock, emit, prefix, who)
        cand = eng.prepare("doc", payload)
        emit(f"prepared {cand.tmp.rsplit('/', 1)[-1]}: written, fsynced, validated (invisible)")
        barrier.wait(timeout=120)      # every candidate is prepared
        barrier.wait(timeout=120)      # go
        try:
            s._guarded(lambda: eng.publish(cand, expect=ANY if expect == "*" else expect))
            emit("put() returned: committed and durable", result="committed")
        except WriteConflict as e:
            emit(f"WriteConflict: {e.message.split('.')[0]}; temp removed, nothing committed", result="conflict")
        emit("closes the store (releases its session lock)", closed=True)
        os.unlink(live)                # gone BEFORE the session lock is released
        s.close()
    except BaseException as e:  # report, never hang the barrier
        emit(f"ERROR {type(e).__name__}: {e}", result="error")
        try:
            barrier.abort()
        except Exception:
            pass


def _recovery_proc(path: str, q: Any, prefix: str) -> None:
    def emit(msg: str, **kw: Any) -> None:
        q.put({"t": _now(), "who": "R", "msg": msg, **kw})
    try:
        emit("wants the session lock EXCLUSIVELY (a sweeping recovery)", wants=True)
        with SafeStore.open(path, wait=120) as s:
            folder, base = os.path.split(prefix)
            live = [n for n in os.listdir(folder) if n.startswith(base + ".live-")]
            emit(f"got it: {len(live)} writer session(s) still live; recovery runs", lock="exclusive",
                 live_writers=len(live))
            for ev in s.recovery.events:
                emit("recovery: " + ev.msg, swept=ev.kind == "discard")
            data = s.get("doc")
            emit(f"reads doc = {shape(data)}", payload=data)
    except BaseException as e:
        emit(f"ERROR {type(e).__name__}: {e}", result="error")


def _c5_round(work: str, name: str, cas: bool, with_recovery: bool, chk: Checks) -> None:
    store = os.path.join(work, name)
    SafeStore.create(store, engine="shadow").close()
    with SafeStore.open(store) as s:
        s.put("doc", A64)
    ctx = multiprocessing.get_context("spawn")
    q = ctx.Queue()
    barrier = ctx.Barrier(3)
    prefix = os.path.join(work, name)
    expect = sha256(A64) if cas else "*"
    t0 = _now()
    writers = [ctx.Process(target=_writer_proc, args=(store, who, data, expect, barrier, q, prefix))
               for who, data in (("B", B64), ("C", C64))]
    log: List[Dict[str, Any]] = []
    for p in writers:
        p.start()
    rproc = None
    try:
        barrier.wait(timeout=120)
        show_dir(store, {"A": A64, "B": B64, "C": C64}, "disk at the barrier (both candidates prepared):")
        if with_recovery:
            rproc = ctx.Process(target=_recovery_proc, args=(store, q, prefix))
            rproc.start()
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                try:
                    ev = q.get(timeout=1)
                except _queue.Empty:
                    continue
                log.append(ev)
                if ev["who"] == "R":
                    break
            time.sleep(0.2)            # let it sit visibly blocked on the lock
        barrier.wait(timeout=120)
    except Exception as e:  # BrokenBarrierError, timeouts
        bad(f"coordination failed: {type(e).__name__}: {e}")
    for p in writers + ([rproc] if rproc else []):
        p.join(timeout=120)
    while True:
        try:
            log.append(q.get(timeout=0.5))
        except _queue.Empty:
            break
    log.sort(key=lambda e: e["t"])
    who_name = {"B": c("writer B", "blue"), "C": c("writer C", "magenta"), "R": c("recovery", "yellow")}
    print(c("  timeline (one clock, all processes):", "bold"))
    for ev in log:
        print(f"    +{(ev['t'] - t0) * 1000:8.1f} ms  {who_name[ev['who']]:<8} │ {ev['msg']}")
    by = {w: [e for e in log if e["who"] == w] for w in "BCR"}

    def first(w: str, **kw: Any) -> Optional[Dict[str, Any]]:
        return next((e for e in by[w] if all(e.get(k) == v for k, v in kw.items())), None)
    errors = [e for e in log if e.get("result") == "error"]
    chk.check(not errors, "no process failed" + (f": {errors[0]['msg']}" if errors else ""))
    try:
        with open(prefix + ".order") as f:
            order = f.read().split()              # written inside the lock: the true commit order
    except OSError:
        order = []
    if sorted(order) == ["B", "C"]:
        w1, w2 = order
        chk.check(not any(e.get("overlap") for e in log),
                  f"commit sections never overlap: {w1} committed, then {w2} (O_EXCL witness never tripped)")
    else:
        order = ["B", "C"]
        chk.check(False, "both writers reached the commit section")
    results = {w: (first(w, result="committed") and "committed") or (first(w, result="conflict") and "conflict")
               for w in "BC"}
    final = SafeStore.open(store)
    try:
        doc = final.get("doc")
        leftovers = [n for n in os.listdir(os.path.join(store, "docs")) if n.endswith(".tmp")]
    finally:
        final.close()
    payload = {"B": B64, "C": C64}
    if not cas:
        chk.check(results == {"B": "committed", "C": "committed"},
                  "both saves returned normally (no lost candidate, no wrong answer)")
        chk.check(doc == payload[order[-1]], f"doc = {shape(doc)}: exactly the later committer's version "
                                            f"({order[-1]}), never a mixture")
    else:
        chk.check(results.get(order[0]) == "committed" and results.get(order[1]) == "conflict",
                  f"{order[0]} committed; {order[1]} got WriteConflict (its edit was based on A, which "
                  f"{order[0]} had replaced): no lost update")
        chk.check(doc == payload[order[0]], f"doc = {shape(doc)}: the winner's version, intact")
    chk.check(not leftovers, "no temp files left behind")
    if with_recovery:
        racq = first("R", lock="exclusive")
        chk.check(racq is not None and racq.get("live_writers") == 0,
                  "the exclusive recovery waited until both writers had closed (recovery ⟂ live writes)")
        chk.check(not any(e.get("swept") for e in by["R"]),
                  "it found nothing to sweep: no live save was ever deleted")


def c5(work: str, chk: Checks) -> None:
    head("C5 · Concurrent writers extension  [KO-16-C5, Hard]")
    print("  Two writers, B and C, save the same document. The single-writer design breaks in two\n"
          "  ways; the extension (unique temps + COMMIT lock + shared/exclusive session lock) fixes both.")
    known = {"A": A64, "B": B64, "C": C64}
    print(c("\n  1) the single-writer design with two writers (fixed temp name, no lock), controlled order", "cyan"))
    store = os.path.join(work, "c5-unsafe")
    SafeStore.create(store, engine="shadow", temp_names="fixed").close()
    with SafeStore.open(store) as s:
        s.put("doc", A64)
    eb = make("shadow", OsVFS(store), temp_names="fixed")
    ec = make("shadow", OsVFS(store), temp_names="fixed")
    eb.open()
    ec.open()
    cb = eb.prepare("doc", B64)
    note(f"B prepares {cb.tmp}")
    cc = ec.prepare("doc", C64)
    note(f"C prepares {cc.tmp}: same name, O_TRUNC wipes B's candidate")
    eb.publish(cb)
    told_b = "committed"
    try:
        ec.publish(cc)
        told_c = "committed"
    except CommitAborted as e:
        told_c = f"aborted ({e.message.split(':')[1].split('.')[0].strip()})"
    doc = eb.get("doc")
    print(c(f"    B is told: {told_b:<10} but doc = {label(doc, known)}", "red"))
    print(c(f"    C is told: {told_c}, but its bytes ARE the document", "red"))
    chk.check(doc == C64 and told_c.startswith("aborted"),
              "hazard reproduced: a shared temp name gives both writers the wrong answer")
    print(c("\n  2) a recovery while a save is in flight (single-writer rule: open = sweep temps)", "cyan"))
    store = os.path.join(work, "c5-sweep")
    SafeStore.create(store, engine="shadow").close()
    with SafeStore.open(store) as s:
        s.put("doc", A64)
    eb = make("shadow", OsVFS(store))
    eb.open()
    cb = eb.prepare("doc", B64)
    note(f"B prepares {cb.tmp}")
    er = make("shadow", OsVFS(store))
    rep = er.open()
    events(rep, "a second opener's recovery: ")
    try:
        eb.publish(cb)
        lost = False
    except CommitAborted as e:
        lost = True
        print(c(f"    B: {e.message.split('.')[0]}", "red"))
    chk.check(lost, "hazard reproduced: recovery deleted a live save")
    print(c("\n  3) the extension, same interleavings, in-process (deterministic)", "cyan"))
    store = os.path.join(work, "c5-safe")
    SafeStore.create(store, engine="shadow").close()
    with SafeStore.open(store) as s:
        s.put("doc", A64)
    sb = SafeStore.open(store, multi_writer=True)
    sc = SafeStore.open(store, multi_writer=True)
    try:
        cb = sb.engine.prepare("doc", B64)                  # type: ignore[attr-defined]
        cc = sc.engine.prepare("doc", C64)                  # type: ignore[attr-defined]
        note(f"B prepares {cb.tmp}; C prepares {cc.tmp}: different files")
        try:
            SafeStore.open(store).close()
            single = "opened"
        except StoreError as e:
            single = f"refused ({e})"
        note(f"a single-writer open (exclusive): {single}")
        sr = SafeStore.open(store, multi_writer=True)
        events(sr.recovery, "a third writer's recovery: ")
        sr.close()
        chk.check(single.startswith("refused") and os.path.exists(os.path.join(store, cb.tmp))
                  and os.path.exists(os.path.join(store, cc.tmp)),
                  "while writers are live, nobody sweeps: both candidates survive other openers")
        sb.engine.publish(cb)                                # type: ignore[attr-defined]
        sc.engine.publish(cc)                                # type: ignore[attr-defined]
        chk.check(sb.get("doc") == C64, "B then C commit; both told the truth; doc = C, intact")
    finally:
        sb.close()
        sc.close()
    print(c("\n  4) real processes: two writers + a barrier after preparation + a recovery that must wait", "cyan"))
    _c5_round(work, "c5-processes", cas=False, with_recovery=True, chk=chk)
    print(c("\n  5) the same race with conditional saves (expect = sha256 of A): no lost update", "cyan"))
    _c5_round(work, "c5-cas", cas=True, with_recovery=False, chk=chk)
    explain("flock(2) (LockFileEx on Windows): writers hold LOCK shared for their session, a sweeping\n"
            "                recovery needs it exclusive; COMMIT is an exclusive lock around rename + fsync(dir).\n"
            "                The kernel releases both if a holder dies (kill -9), so no lock is ever stale.",
            "local file system (flock is unreliable over NFS); the shadow engine (others keep per-process state).")


# ----------------------------------------------------------------------------
# runner
# ----------------------------------------------------------------------------
CHALLENGES: Dict[str, Tuple[str, Callable[[str, Checks], None]]] = {
    "c1": ("SafeWrite crash points (Easy)", c1),
    "c2": ("Invalid destination refusal (Medium)", c2),
    "c3": ("Space/size constraint (Medium)", c3),
    "c4": ("Post-rename flush error (Hard)", c4),
    "c5": ("Concurrent writers extension (Hard)", c5),
}


def run(names: Optional[List[str]] = None, directory: Optional[str] = None, keep: bool = False) -> int:
    names = [n.lower() for n in (names or list(CHALLENGES))]
    if "all" in names:
        names = list(CHALLENGES)
    unknown = [n for n in names if n not in CHALLENGES]
    if unknown:
        raise SystemExit(f"unknown challenge(s) {unknown}; choose from {', '.join(CHALLENGES)} or all")
    base = directory or tempfile.mkdtemp(prefix="safewrite-challenges-")
    os.makedirs(base, exist_ok=True)
    results: List[Checks] = []
    try:
        for n in names:
            work = os.path.join(base, n)
            shutil.rmtree(work, ignore_errors=True)
            os.makedirs(work)
            chk = Checks(n)
            try:
                CHALLENGES[n][1](work, chk)
            except Exception as e:  # a crash in the harness is a failed challenge, not a traceback
                chk.check(False, f"challenge raised {type(e).__name__}: {e}")
            results.append(chk)
    finally:
        if keep or directory:
            note(f"files kept in {base}")
        else:
            shutil.rmtree(base, ignore_errors=True)
    head("summary")
    for chk in results:
        n_ok = sum(1 for r, _ in chk.items if r)
        line = f"{chk.cid.upper()}  {CHALLENGES[chk.cid][0]:<40} {n_ok}/{len(chk.items)} checks"
        (ok if chk.passed else bad)(line)
    return 0 if all(chk.passed for chk in results) else 1
