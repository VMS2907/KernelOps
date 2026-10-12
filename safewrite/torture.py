"""Real crashes on a real file system: the kill -9 torture test.

The simulator reasons about *possible* disks.  This module produces *actual*
ones.  For each trial:

1. start a writer process that saves version n+1, n+2, ... of one document
   in a loop and reports ``A <n>`` on a pipe **after** each save returns;
2. after a random delay, ``SIGKILL`` it (``TerminateProcess`` on Windows).
   No cleanup handler runs, nothing is flushed, the lock is dropped by the kernel;
3. open the store in this process.  That runs the engine's recovery on the
   real files the dead process left behind;
4. the document must be **byte-identical** to the last acknowledged version or
   to the one being written (which may have committed just before the ack).

Every version is generated deterministically from its number with a
different length, so any mixture, truncation or stale read is caught.

A ``kill -9`` keeps the kernel page cache, so this test checks *atomicity*.
Durability against power loss is the simulator's job.  The two together are
the evidence.
"""

from __future__ import annotations

import os
import random
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .crashsim.provenance import describe_mixture
from .format import IntegrityError
from .store import SafeStore, StoreError

KEY = "torture.doc"
MAGIC = b"SAFEWRITE-TORTURE v="


def content_for(n: int, size: int) -> bytes:
    """Version ``n`` of the document: header + deterministic body of varying length."""
    rng = random.Random(n * 7919 + size)
    length = size // 2 + rng.randrange(size)
    head = MAGIC + b"%010d\n" % n
    body = rng.randbytes(length) if hasattr(rng, "randbytes") else bytes(
        rng.getrandbits(8) for _ in range(length))
    return head + body


def version_of(data: bytes, size: int) -> Optional[int]:
    """The version number if ``data`` is *exactly* some valid version, else None."""
    if not data.startswith(MAGIC):
        return None
    try:
        n = int(data[len(MAGIC):len(MAGIC) + 10])
    except ValueError:
        return None
    return n if content_for(n, size) == data else None


# ----------------------------------------------------------------------------
# the writer (child process)
# ----------------------------------------------------------------------------
def child_main(path: str, start: int, size: int) -> None:  # pragma: no cover - runs in a child
    store = SafeStore.open(path)
    out = sys.stdout.buffer
    out.write(b"R\n")
    out.flush()
    n = start
    while True:
        n += 1
        store.put(KEY, content_for(n, size))
        out.write(b"A %d\n" % n)
        out.flush()


def _spawn(path: str, start: int, size: int) -> subprocess.Popen:
    env = dict(os.environ)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env["PYTHONPATH"] = root + os.pathsep + env.get("PYTHONPATH", "")
    return subprocess.Popen([sys.executable, "-m", "safewrite.torture", "child", path, str(start), str(size)],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env)


def _kill(proc: subprocess.Popen) -> None:
    if hasattr(signal, "SIGKILL"):
        os.kill(proc.pid, signal.SIGKILL)
    else:  # pragma: no cover - Windows: TerminateProcess, equally abrupt
        proc.kill()
    proc.wait()


# ----------------------------------------------------------------------------
# the harness (parent)
# ----------------------------------------------------------------------------
@dataclass
class TortureResult:
    engine: str
    trials: int = 0
    outcomes: Dict[str, int] = field(default_factory=dict)
    examples: List[str] = field(default_factory=list)
    puts_per_sec: float = 0.0
    kills_mid_write: int = 0
    seconds: float = 0.0

    @property
    def violations(self) -> int:
        return sum(v for k, v in self.outcomes.items() if not k.startswith("OK"))

    def to_json(self) -> Dict[str, object]:
        return {"engine": self.engine, "trials": self.trials, "outcomes": self.outcomes,
                "violations": self.violations, "examples": self.examples,
                "puts_per_sec": round(self.puts_per_sec, 1), "seconds": round(self.seconds, 2)}


def _read_acks(proc: subprocess.Popen) -> int:
    """Highest version the dead writer acknowledged; closes its pipes."""
    assert proc.stdout is not None
    last = -1
    for line in proc.stdout.read().splitlines():
        if line.startswith(b"A "):
            last = int(line[2:])
    proc.stdout.close()
    if proc.stderr is not None:
        proc.stderr.close()
    return last


def _wait_ready(proc: subprocess.Popen, timeout: float = 15.0) -> None:
    assert proc.stdout is not None
    t0 = time.time()
    line = proc.stdout.readline()
    if line.strip() != b"R":
        err = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
        proc.kill()
        raise RuntimeError(f"writer failed to start: {line!r} {err[-800:]}")
    if time.time() - t0 > timeout:
        raise RuntimeError("writer took too long to start")


def calibrate(path: str, start: int, size: int, seconds: float = 0.4) -> float:
    """Measure how long one save takes, so kills land inside saves, not between runs."""
    proc = _spawn(path, start, size)
    _wait_ready(proc)
    time.sleep(seconds)
    _kill(proc)
    acks = _read_acks(proc)
    n = max(1, acks - start)
    return seconds / n


def torture(engine: str = "cow", trials: int = 100, size: int = 64 * 1024,
            directory: Optional[str] = None, seed: int = 1,
            progress: Optional[Callable[[Dict[str, object]], None]] = None,
            stop: Optional[Callable[[], bool]] = None) -> TortureResult:
    rng = random.Random(seed)
    base = directory or tempfile.mkdtemp(prefix="safewrite-torture-")
    path = os.path.join(base, engine.replace("/", "_"))
    shutil.rmtree(path, ignore_errors=True)
    t0 = time.perf_counter()
    with SafeStore.create(path, engine=engine) as s:
        s.put(KEY, content_for(0, size))
    version = 0
    res = TortureResult(engine)
    per_put = calibrate(path, version, size)
    with SafeStore.open(path) as s:          # calibration ran real saves; resync
        data = s.get(KEY) or b""
        version = version_of(data, size) or 0
    res.puts_per_sec = 1.0 / per_put if per_put else 0.0
    for trial in range(1, trials + 1):
        if stop and stop():
            break
        proc = _spawn(path, version, size)
        _wait_ready(proc)
        delay = rng.uniform(0, per_put * 3)
        time.sleep(delay)
        _kill(proc)
        acked = _read_acks(proc)
        last = acked if acked >= 0 else version
        code, detail, recovered = _judge(path, version, last, size)
        res.trials += 1
        res.outcomes[code] = res.outcomes.get(code, 0) + 1
        if not code.startswith("OK") and len(res.examples) < 10:
            res.examples.append(f"trial {trial}: {detail}")
        if progress:
            progress({"trial": trial, "trials": trials, "code": code, "detail": detail,
                      "acked": last, "recovered": recovered, "delay_ms": round(delay * 1000, 3),
                      "outcomes": dict(res.outcomes)})
        if recovered is None:
            # The document is gone or damaged: put a valid version back so the
            # next trial starts from a known state (only the unsafe engine needs this).
            with SafeStore.open(path) as s:
                s.put(KEY, content_for(last, size))
            version = last
        else:
            version = recovered
    res.seconds = time.perf_counter() - t0
    if directory is None:
        shutil.rmtree(base, ignore_errors=True)
    return res


def _judge(path: str, before: int, acked: int, size: int):
    """Open (= recover) the store the dead writer left and classify what we find."""
    try:
        store = SafeStore.open(path)
    except (StoreError, IntegrityError, OSError) as e:
        return "UNRECOVERABLE", f"recovery failed: {e}", None
    try:
        try:
            data = store.get(KEY)
        except IntegrityError as e:
            return "DETECTED", f"checksum caught damage, no valid version: {e}", None
    finally:
        store.close()
    if data is None:
        return "LOST", "document vanished", None
    v = version_of(data, size)
    if v is None:
        versions = [("acked", content_for(acked, size)), ("in-flight", content_for(acked + 1, size))]
        return "CORRUPT", describe_mixture(data, versions), None
    if v == acked:
        return "OK_OLD", f"v{v} = last acknowledged version", v
    if v == acked + 1:
        return "OK_NEW", f"v{v} = in-flight version (committed just before its ack)", v
    if v < acked:
        return "LOST", f"recovered v{v} but v{acked} had been acknowledged", v
    return "CORRUPT", f"recovered v{v}, which was never written", None


if __name__ == "__main__":  # pragma: no cover
    if len(sys.argv) == 5 and sys.argv[1] == "child":
        child_main(sys.argv[2], int(sys.argv[3]), int(sys.argv[4]))
