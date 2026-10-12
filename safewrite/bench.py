"""What does crash consistency cost?  Measured, not guessed.

For each engine, on the real file system:

* **latency** of a durable save (p50 / p99)
* **fsyncs per commit**: the dominant cost on real disks
* **write amplification**: bytes written to disk / bytes of document saved
* **recovery time** after an unclean shutdown
* **throughput under concurrency**, where WAL group commit shines
* the same saves *without* fsync, to show what durability itself costs
"""

from __future__ import annotations

import os
import shutil
import statistics
import tempfile
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from .engines import make
from .vfs.osfs import OsVFS

ENGINES = ["naive", "shadow", "undo", "wal", "cow"]


def _payload(i: int, size: int) -> bytes:
    stamp = b"%08d|" % i
    return (stamp * (size // len(stamp) + 1))[:size]


def _fresh(spec: str, path: str, durable: bool = True, **opts: Any):
    shutil.rmtree(path, ignore_errors=True)
    os.makedirs(path)
    vfs = OsVFS(path, durable=durable)
    eng = make(spec, vfs, **opts)
    eng.format()
    eng.open()
    vfs.stats.reset()
    return eng, vfs


def _pct(xs: List[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(p * len(xs)))]


def bench_engine(spec: str, base: str, n: int, size: int, threads: List[int]) -> Dict[str, Any]:
    path = os.path.join(base, spec.replace("/", "_"))
    out: Dict[str, Any] = {"engine": spec}

    # 1. sequential durable saves
    eng, vfs = _fresh(spec, path)
    lat = []
    for i in range(n):
        t0 = time.perf_counter()
        eng.put(f"doc{i % 16}", _payload(i, size))
        lat.append(time.perf_counter() - t0)
    st = vfs.stats.snapshot()
    out.update({
        "p50_ms": round(_pct(lat, 0.50) * 1e3, 3),
        "p99_ms": round(_pct(lat, 0.99) * 1e3, 3),
        "mean_ms": round(statistics.mean(lat) * 1e3, 3),
        "saves_per_s": round(n / sum(lat), 1),
        "fsyncs_per_commit": round((st["fsyncs"] + st["dir_fsyncs"]) / n, 2),
        "syscalls_per_commit": round((st["writes"] + st["fsyncs"] + st["dir_fsyncs"] + st["renames"]
                                      + st["unlinks"] + st["creates"] + st["truncates"]) / n, 2),
        "write_amplification": round(st["bytes_written"] / (n * size), 2),
    })

    # 2. recovery after an unclean shutdown: never close `eng`, open a second instance
    t0 = time.perf_counter()
    eng2 = make(spec, OsVFS(path))
    rep = eng2.open()
    out["recovery_ms"] = round((time.perf_counter() - t0) * 1e3, 3)
    out["recovery_summary"] = rep.summary()
    eng2.close()

    # 3. the price of durability: identical saves with fsync disabled
    eng, vfs = _fresh(spec, path, durable=False)
    t0 = time.perf_counter()
    for i in range(n):
        eng.put(f"doc{i % 16}", _payload(i, size))
    out["no_fsync_saves_per_s"] = round(n / (time.perf_counter() - t0), 1)

    # 4. concurrency
    out["throughput"] = {}
    for t in threads:
        eng, vfs = _fresh(spec, path)
        per = max(1, n // t)
        errors: List[BaseException] = []

        def worker(w: int) -> None:
            try:
                for i in range(per):
                    eng.put(f"t{w}-doc{i % 4}", _payload(i, size))
            except BaseException as e:  # pragma: no cover
                errors.append(e)

        ths = [threading.Thread(target=worker, args=(w,)) for w in range(t)]
        t0 = time.perf_counter()
        for th in ths:
            th.start()
        for th in ths:
            th.join()
        dt = time.perf_counter() - t0
        st = vfs.stats.snapshot()
        entry = {"commits_per_s": round(per * t / dt, 1),
                 "fsyncs_per_commit": round((st["fsyncs"] + st["dir_fsyncs"]) / (per * t), 2)}
        gc = getattr(eng, "group_commit_stats", None)
        if gc:
            entry["avg_group_size"] = round(gc["commits"] / max(1, gc["batches"]), 2)
        out["throughput"][str(t)] = entry
    return out


def recovery_scaling(base: str, sizes: List[int]) -> Dict[str, Dict[str, float]]:
    """Recovery time vs. amount of un-checkpointed work: WAL replays, COW doesn't."""
    res: Dict[str, Dict[str, float]] = {}
    for spec, opts in (("wal", {"checkpoint_every": 10 ** 9, "checkpoint_bytes": 1 << 40}), ("cow", {})):
        res[spec] = {}
        for n in sizes:
            path = os.path.join(base, f"scale-{spec}")
            eng, _ = _fresh(spec, path, durable=False, **opts)
            for i in range(n):
                eng.put(f"doc{i % 64}", _payload(i, 512))
            t0 = time.perf_counter()
            make(spec, OsVFS(path), **opts).open()
            res[spec][str(n)] = round((time.perf_counter() - t0) * 1e3, 3)
    return res


def run_bench(engines: Optional[List[str]] = None, n: int = 200, size: int = 4096,
              threads: Optional[List[int]] = None, directory: Optional[str] = None,
              progress: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    engines = engines or ENGINES
    threads = threads or [1, 4, 16]
    base = directory or tempfile.mkdtemp(prefix="safewrite-bench-")
    try:
        results = []
        for spec in engines:
            if progress:
                progress(spec)
            results.append(bench_engine(spec, base, n, size, threads))
        scaling = recovery_scaling(base, [100, 1000, 5000])
    finally:
        if directory is None:
            shutil.rmtree(base, ignore_errors=True)
    return {"generated": time.strftime("%Y-%m-%d %H:%M:%S"), "n": n, "size": size,
            "threads": threads, "platform": os.uname().sysname if hasattr(os, "uname") else os.name,
            "results": results, "recovery_scaling": scaling}


def print_bench(res: Dict[str, Any], c: Callable[..., str] = lambda s, *a: s) -> None:
    print(c(f"\n{res['n']} durable saves of {res['size']} bytes per engine ({res['platform']})", "bold"))
    hdr = (f"  {'engine':<8}{'p50 ms':>9}{'p99 ms':>9}{'saves/s':>10}{'fsync/commit':>14}"
           f"{'write amp':>11}{'recovery ms':>13}{'no-fsync/s':>12}")
    print(hdr)
    for r in res["results"]:
        print(f"  {r['engine']:<8}{r['p50_ms']:>9}{r['p99_ms']:>9}{r['saves_per_s']:>10}"
              f"{r['fsyncs_per_commit']:>14}{r['write_amplification']:>11}{r['recovery_ms']:>13}"
              f"{r['no_fsync_saves_per_s']:>12}")
    print(c("\n  concurrent commits/s (threads -> rate, fsyncs per commit, WAL group size)", "bold"))
    for r in res["results"]:
        cells = []
        for t, e in r["throughput"].items():
            g = f" grp {e['avg_group_size']}" if "avg_group_size" in e else ""
            cells.append(f"{t}T: {e['commits_per_s']:>7}/s ({e['fsyncs_per_commit']} fs{g})")
        print(f"  {r['engine']:<8}" + "   ".join(cells))
    print(c("\n  recovery time vs un-checkpointed commits (ms)", "bold"))
    for spec, row in res["recovery_scaling"].items():
        print(f"  {spec:<8}" + "  ".join(f"{n:>5} commits: {ms:>8}" for n, ms in row.items()))
