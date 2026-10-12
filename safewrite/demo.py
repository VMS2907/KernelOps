"""``python -m safewrite demo``: a narrated, self-contained terminal walk-through.

The stage fallback if the browser misbehaves.  Every step runs real code:
real files, real ``os._exit`` crashes, the real simulator.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from typing import List

from .cli import bad, c, head, note, ok
from .crashsim import explore
from .crashsim.scenarios import DOC_A, DOC_B
from .race import run_race
from .store import SafeStore
from .torture import torture

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _pause(enabled: bool) -> None:
    if enabled and sys.stdin.isatty():
        input(c("\n  ⏎ press Enter to continue ", "grey"))


def _crash_cli(store: str, key: str, text: str, at: int) -> int:
    env = dict(os.environ, PYTHONPATH=ROOT + os.pathsep + os.environ.get("PYTHONPATH", ""),
               NO_COLOR="1", PYTHONIOENCODING="utf-8")
    p = subprocess.run([sys.executable, "-m", "safewrite", "crash", store, key, "--text", text, "--at", str(at)],
                       capture_output=True, env=env, timeout=60)
    return p.returncode


def _lines(data: bytes, n: int = 6) -> List[str]:
    return data.decode("utf-8", "replace").splitlines()[:n]


def run_demo(pause: bool = True) -> int:
    tmp = tempfile.mkdtemp(prefix="safewrite-demo-")
    A, B = DOC_A.decode(), DOC_B.decode()
    try:
        # ------------------------------------------------------------- 1
        head("1. The problem: a save is many syscalls, and a crash can stop it anywhere")
        naive = os.path.join(tmp, "naive")
        with SafeStore.create(naive, engine="naive", chunk=512) as s:
            s.put("inventory.csv", DOC_A)
        note("inventory.csv holds version A. We save version B with a plain overwrite")
        note("and REALLY kill the process (os._exit) after its 2nd write syscall...")
        code = _crash_cli(naive, "inventory.csv", B, 3)
        note(f"writer exit code: {code} (killed)")
        with open(os.path.join(naive, "docs", "inventory.csv"), "rb") as f:
            got = f.read()
        bad("what is on disk now is NEITHER version:")
        text = got.decode("utf-8", "replace")
        cut = text[:1024].count("\n")                 # the line where B stops and A shows through
        lines = text.splitlines()
        for i in range(0, 2):
            print(c("      B | ", "yellow") + lines[i])
        print(c("      ...", "grey"))
        for i in range(cut - 1, min(cut + 3, len(lines))):
            tag = "B | " if i < cut else ("B+A " if i == cut else "A | ")
            print(c("      " + tag, "yellow" if i < cut else "red" if i == cut else "blue") + lines[i])
        bad(f"{len(got)} bytes: the first 1024 from B, the rest from A. Silent corruption, no error raised.")
        _pause(pause)

        # ------------------------------------------------------------- 2
        head("2. The fix: copy-on-write. Crash it at EVERY syscall, for real")
        cow = os.path.join(tmp, "cow")
        with SafeStore.create(cow, engine="cow") as s:
            s.put("inventory.csv", DOC_A)
        for at in range(1, 6):
            code = _crash_cli(cow, "inventory.csv", B, at)
            with SafeStore.open(cow) as s:
                got = s.get("inventory.csv")
                which = "A (previous)" if got == DOC_A else "B (new)" if got == DOC_B else "CORRUPT"
                sel = [e.msg for e in s.recovery.events if e.kind == "select"]
                (ok if which != "CORRUPT" else bad)(
                    f"killed before syscall {at} (exit {code}) → recovered {which}   {c(sel[0][:60] if sel else '', 'grey')}")
                s.put("inventory.csv", DOC_A)
        note("the commit point is ONE superblock write; before it A, after it B, never a mix")
        _pause(pause)

        # ------------------------------------------------------------- 3
        head("3. The proof: every possible post-crash disk, not three hand-picked crash points")
        total = viol = 0
        for spec in ("shadow", "undo", "wal", "cow"):
            for sc in ("update", "transfer", "create", "delete", "sequence"):
                r = explore(spec, sc, "power")
                total += r["states"] + r["recovery_crash_states"]
                if r["verdict"] not in ("PASS", "LIMITATION"):
                    viol += r["violations"]
        ok(f"{total} power-loss crash states (lost, reordered, torn writes; crashes during recovery): "
           f"{viol} violations")
        _pause(pause)

        # ------------------------------------------------------------- 4
        head("4. Every line of the protocol has a counter-example")
        for spec, why in (("shadow/no-fsync", "skip fsync(tmp) before rename"),
                          ("shadow/no-dirsync", "skip fsync(dir) after rename"),
                          ("wal/no-checksum", "trust log records without a CRC"),
                          ("cow/no-barrier", "skip the fsync before the superblock flip")):
            proc = explore(spec, "update", "process", recovery_crashes=False)
            power = explore(spec, "update", "power", recovery_crashes=False)
            ex = power["examples"][0] if power["examples"] else {"code": "-", "detail": ""}
            print(f"  {c(spec, 'bold'):<28} {why}")
            note(f"kill -9 testing: {proc['violations']} failures  ←  this is why kill tests are not enough")
            bad(f"power-loss model: {power['violations']} failures, e.g. {ex['code']}: {ex['detail'][:80]}")
        _pause(pause)

        # ------------------------------------------------------------- 5
        head("5. Real kill -9, real files")
        for eng in ("naive/trunc", "cow"):
            r = torture(eng, trials=25, size=32 * 1024, directory=tmp)
            (ok if not r.violations else bad)(
                f"{eng:<12} {r.trials} SIGKILLs: {r.outcomes.get('OK_OLD', 0)} old, {r.outcomes.get('OK_NEW', 0)} new, "
                f"{r.violations} corrupt")
        _pause(pause)

        # ------------------------------------------------------------- 6
        head("6. Synchronization: atomic writes are not enough for read-modify-write")
        r = run_race("cow", threads=8, increments=40, directory=tmp)
        bad(f"get() then put() from 8 threads: counter = {r['unsynchronized']} of {r['expected']}")
        ok(f"store.update(fn) (lock held read→commit): counter = {r['synchronized']} of {r['expected']}")
        _pause(pause)

        # ------------------------------------------------------------- 7
        head("7. Why copy-on-write is the default")
        note("O(1) recovery: pick the newest valid superblock, with nothing to replay (WAL replay grows with its log)")
        note("version history and lock-free MVCC readers come for free")
        note("cost: 2 fsyncs per commit (WAL needs ~1 and wins small-write throughput)")
        print(c("\n  Open the Crash Lab for the visual version:  python -m safewrite serve\n", "bold"))
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
