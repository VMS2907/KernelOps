#!/usr/bin/env python3
"""Load test for the Crash Arena: N simulated phones tapping at random rates.

Starts ``python -m safewrite arena`` in its own process (or targets ``--url``),
joins N phones, lets each one tap with its own random rate and bursts for
``--seconds``, then reports what happened and checks the invariants:

* every accepted tap is accounted for (crashed, waiting, dropped at freeze, failed);
* every crashed tap crashed exactly one writer on each side;
* the safe engine (cow) corrupted nothing.

Standard library only::

    python tools/arena_load.py                                   # 30 phones (the cap) x 60 s, pool 8
    python tools/arena_load.py --phones 60 --max-phones 60       # stress: twice the stage cap
    python tools/arena_load.py --sweep 4,8,16,32 --seconds 30    # pool-size table
    python tools/arena_load.py --phones 10 --min-rate 2 --max-rate 5   # 10-volunteer mode
    python tools/arena_load.py --url http://192.168.1.23:8765 --token TOKEN   # a running arena
"""

from __future__ import annotations

import argparse
import json
import os
import random
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _req(url: str, body: Optional[dict] = None, token: str = "", timeout: float = 30.0) -> Any:
    data = None if body is None else json.dumps(body).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Arena-Token"] = token
    with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=headers), timeout=timeout) as r:
        return json.loads(r.read())


def pct(xs: List[float], p: float) -> float:
    if not xs:
        return float("nan")
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100.0 * (len(xs) - 1))))]


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ----------------------------------------------------------------------------
# memory: the server and its writer children (Linux /proc only)
# ----------------------------------------------------------------------------
def _rss_kb(pid: int) -> int:
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1])
    except OSError:
        pass
    return 0


def memory(pid: int) -> Optional[Dict[str, float]]:
    if not os.path.isdir("/proc"):
        return None
    kids = []
    for d in os.listdir("/proc"):
        if d.isdigit():
            try:
                with open(f"/proc/{d}/stat") as f:
                    if int(f.read().rsplit(")", 1)[1].split()[1]) == pid:
                        kids.append(int(d))
            except (OSError, ValueError, IndexError):
                pass
    kid_rss = [_rss_kb(k) for k in kids]
    return {"server_mb": _rss_kb(pid) / 1024, "writers": len(kids), "writers_mb": sum(kid_rss) / 1024,
            "per_writer_mb": (sum(kid_rss) / len(kid_rss) / 1024) if kid_rss else 0.0}


# ----------------------------------------------------------------------------
# one simulated phone
# ----------------------------------------------------------------------------
def phone(i: int, base: str, end: float, seed: int, lo: float, hi: float, out: Dict[str, Any]) -> None:
    rng = random.Random(seed)
    cid = f"load-{i:04d}-{rng.getrandbits(32):08x}"
    st = {"sent": 0, "accepted": 0, "rate_limited": 0, "busy": 0, "backlog": 0, "not_live": 0, "errors": 0, "rtt": []}
    out[cid] = st
    try:
        _req(base + "/api/arena/join", {"client_id": cid, "nickname": f"phone {i}"})
    except urllib.error.HTTPError as e:
        st["errors" if e.code != 409 else "refused_full"] = 1      # 409: the arena is full
        return
    except (OSError, ValueError):
        st["errors"] += 1
        return
    rate = rng.uniform(lo, hi)                     # this phone's own tapping speed
    while time.perf_counter() < end:
        roll = rng.random()                        # bursts and pauses, like real thumbs
        r = 0.0 if roll < 0.1 else 2.0 * rate if roll < 0.3 else rate
        phase_end = min(end, time.perf_counter() + rng.uniform(0.5, 2.0))
        if r == 0.0:
            time.sleep(max(0.0, phase_end - time.perf_counter()))
            continue
        while time.perf_counter() < phase_end:
            t0 = time.perf_counter()
            try:
                d = _req(base + "/api/arena/tap", {"client_id": cid})
                st["sent"] += 1
                st["accepted" if d["accepted"] else d["reason"]] += 1
            except (OSError, ValueError, KeyError):
                st["errors"] += 1
            dt = time.perf_counter() - t0
            st["rtt"].append(dt)
            time.sleep(max(0.0, rng.expovariate(r) - dt))


# ----------------------------------------------------------------------------
# one run
# ----------------------------------------------------------------------------
def run(phones: int, seconds: float, pool: int, queue: int, per_phone: int, lo: float, hi: float,
        url: Optional[str], token: Optional[str], warmup: float, seed: int, size: int,
        max_phones: int) -> Dict[str, Any]:
    proc = None
    if url is None:
        port = free_port()
        token = "load-test-token"
        env = dict(os.environ, PYTHONPATH=ROOT + os.pathsep + os.environ.get("PYTHONPATH", ""))
        proc = subprocess.Popen([sys.executable, "-m", "safewrite", "arena", "--no-browser", "--host", "127.0.0.1",
                                 "--port", str(port), "--token", token, "--pool", str(pool),
                                 "--queue", str(queue), "--per-phone-queue", str(per_phone), "--size", str(size),
                                 "--max-phones", str(max_phones)],
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env, cwd=ROOT)
        assert proc.stdout is not None
        banner = []
        for line in iter(proc.stdout.readline, b""):
            banner.append(line.decode(errors="replace"))
            if b"Ctrl+C stops the arena" in line:
                break
        else:
            raise SystemExit("arena did not start:\n" + "".join(banner))
        threading.Thread(target=lambda: proc.stdout.read(), daemon=True).start()   # keep draining
        url = f"http://127.0.0.1:{port}"
    assert token is not None
    base = url.rstrip("/")
    ctl = lambda action: _req(base + "/api/arena/control", {"action": action}, token)  # noqa: E731
    try:
        snap = ctl("reset")
        snap = ctl("start")
        out: Dict[str, Any] = {}
        mem_peak: Dict[str, float] = {}
        t_start = time.perf_counter()
        end = t_start + seconds
        threads = [threading.Thread(target=phone, args=(i, base, end, seed * 1000 + i, lo, hi, out), daemon=True)
                   for i in range(phones)]
        for t in threads:
            t.start()
        samples = []
        stop_mem = threading.Event()

        def sample() -> None:
            while not stop_mem.wait(1.0):
                if proc is not None:
                    m = memory(proc.pid)
                    if m:
                        total = m["server_mb"] + m["writers_mb"]
                        if total > mem_peak.get("total_mb", 0):
                            mem_peak.update(m, total_mb=total)
                try:
                    s = ctl("autotap_off")
                    samples.append((time.perf_counter() - t_start, s["processed"], s["queue"]["pending"]))
                except (OSError, ValueError):
                    pass

        sampler = threading.Thread(target=sample, daemon=True)
        sampler.start()
        for t in threads:
            t.join()
        stop_mem.set()
        sampler.join()
        live = ctl("autotap_off")
        t_live = time.perf_counter() - t_start
        ctl("freeze")                                  # discard what is still waiting
        deadline = time.time() + 60
        while time.time() < deadline:
            final = ctl("autotap_off")
            if final["queue"]["in_flight"] == 0:
                break
            time.sleep(0.1)
        export = _req(base + "/api/arena/export", token=token, timeout=120)
    finally:
        if proc is not None:
            proc.terminate()                           # SIGTERM: the arena cleans up after itself
            try:
                proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                proc.kill()

    pairs = export["pairs"]
    rtts = [x for st in out.values() for x in st["rtt"]]
    sent = sum(st["sent"] for st in out.values())
    after = [s for s in samples if s[0] >= warmup]
    sustained = ((after[-1][1] - after[0][1]) / (after[-1][0] - after[0][0])) if len(after) >= 2 else float("nan")
    lat_all = [p["latency_ms"] for p in pairs]
    fin = export["snapshot"]
    tp = fin["taps"]["phone"]
    sides = fin["sides"]
    checks = {
        "every accepted tap accounted for": fin["accepted"] == (fin["processed"] + fin["failed"] + fin["stale"]
                                                               + fin["discarded"] + fin["queue"]["pending"]
                                                               + fin["queue"]["in_flight"]),
        "server saw every tap a phone sent": tp["received"] == sent,
        "one crash per side per crashed tap": all(set(p["sides"]) == {"naive", "safe"} for p in pairs)
        and sides["naive"]["crashes"] == sides["safe"]["crashes"] == fin["processed"] == len(pairs),
        "taps received >= crashes performed": tp["received"] >= sides["naive"]["crashes"],
        "SafeWrite corruptions == 0": sides["safe"]["corrupted"] == 0,
    }
    return {
        "phones": phones, "max_phones": max_phones,
        "refused_full": sum(st.get("refused_full", 0) for st in out.values()), "seconds": seconds, "pool": pool, "queue": queue, "per_phone": per_phone,
        "rate_range": [lo, hi],
        "cpus": os.cpu_count(), "platform": sys.platform, "python": sys.version.split()[0],
        "taps_sent": sent, "offered_per_sec": sent / max(t_live, 1e-9),
        "accepted": tp["accepted"], "rate_limited": tp["rate_limited"], "queue_full": tp["queue_full"],
        "backlog": tp["backlog"],
        "http_errors": sum(st["errors"] for st in out.values()),
        "rtt_ms": {"p50": pct(rtts, 50) * 1e3, "p95": pct(rtts, 95) * 1e3, "p99": pct(rtts, 99) * 1e3,
                   "max": max(rtts) * 1e3 if rtts else float("nan")},
        "crashes_per_side": sides["safe"]["crashes"], "crashes_live": live["processed"],
        "sustained_per_sec": sustained,
        "tap_to_verdict_ms": {"p50": pct(lat_all, 50), "p95": pct(lat_all, 95), "max": max(lat_all) if lat_all else 0},
        "mean_queue": (sum(s[2] for s in after) / len(after)) if after else 0.0,
        "naive": sides["naive"], "safe": sides["safe"], "discarded_at_freeze": fin["discarded"],
        "failed": fin["failed"], "pool_errors": fin["pool"]["errors"], "spawned": fin["pool"]["spawned"],
        "memory_peak": mem_peak or None, "checks": checks, "delay_max_ms": export["meta"]["delay_max_ms"],
    }


def report(r: Dict[str, Any]) -> str:
    n, s = r["naive"], r["safe"]
    mix = ", ".join(f"{k} {v:,}" for k, v in sorted(n["verdicts"].items(), key=lambda kv: -kv[1]))
    m = r["memory_peak"]
    mem = (f"server {m['server_mb']:.0f} MB + {m['writers']} writers x {m['per_writer_mb']:.1f} MB "
           f"= {m['total_mb']:.0f} MB peak" if m else "n/a (Linux only)")
    lines = [
        f"Crash Arena load test: {r['phones']} phones x {r['seconds']:g} s, pool {r['pool']} per side, "
        f"queue {r['queue']} ({r['per_phone'] or 'no'} per phone), tap rates {r['rate_range'][0]:g}-{r['rate_range'][1]:g}/s "
        f"({r['cpus']} CPUs, {r['platform']}, Python {r['python']})",
        f"  phones joined        {r['phones'] - r['refused_full']:>8,}   (cap {r['max_phones']}; "
        f"{r['refused_full']} refused: arena full)",
        f"  taps sent            {r['taps_sent']:>8,}   ({r['offered_per_sec']:.0f}/s offered)",
        f"  accepted             {r['accepted']:>8,}",
        f"  rate-limited         {r['rate_limited']:>8,}   (phones bursting above 8/s)",
        f"  dropped, busy        {r['queue_full'] + r['backlog']:>8,}   (counted, not crashed: queue full "
        f"{r['queue_full']:,}, phone already had {r['per_phone']} waiting {r['backlog']:,})",
        f"  HTTP errors          {r['http_errors']:>8,}",
        f"  tap round trip       p50 {r['rtt_ms']['p50']:.1f} ms · p95 {r['rtt_ms']['p95']:.1f} ms · "
        f"p99 {r['rtt_ms']['p99']:.1f} ms · max {r['rtt_ms']['max']:.0f} ms",
        f"  crashes performed    {r['crashes_per_side']:>8,} per side   (sustained {r['sustained_per_sec']:.1f}/s per side, "
        f"{2 * r['sustained_per_sec']:.0f} processes killed/s)",
        f"  tap -> verdict       p50 {r['tap_to_verdict_ms']['p50'] / 1e3:.2f} s · p95 {r['tap_to_verdict_ms']['p95'] / 1e3:.2f} s"
        f"   (mean queue {r['mean_queue']:.0f} waiting)",
        f"  {n['spec']:<20} {n['corrupted']:>8,} corrupted of {n['crashes']:,} "
        f"({100.0 * n['corrupted'] / max(1, n['crashes']):.0f}%): {mix}",
        f"  {s['spec']:<20} {s['corrupted']:>8,} corrupted of {s['crashes']:,}",
        f"  writers spawned      {r['spawned']:>8,}   (start-up errors {r['pool_errors']}, failed pairs {r['failed']}, "
        f"not run at freeze {r['discarded_at_freeze']:,})",
        f"  memory               {mem}",
    ]
    lines += [f"  {'OK  ' if ok else 'FAIL'} {name}" for name, ok in r["checks"].items()]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--phones", type=int, default=30)
    ap.add_argument("--seconds", type=float, default=60)
    ap.add_argument("--pool", type=int, default=8)
    ap.add_argument("--queue", type=int, default=500)
    ap.add_argument("--per-phone-queue", type=int, default=3)
    ap.add_argument("--max-phones", type=int, help="the arena's phone cap (default: --phones, so all can join)")
    ap.add_argument("--size", type=int, default=64 * 1024)
    ap.add_argument("--min-rate", type=float, default=1.0, help="slowest phone, taps/s")
    ap.add_argument("--max-rate", type=float, default=10.0, help="fastest phone, taps/s (bursts go to 2x)")
    ap.add_argument("--warmup", type=float, default=5.0, help="seconds excluded from the sustained rate")
    ap.add_argument("--sweep", help="comma-separated pool sizes: one run each, then a table")
    ap.add_argument("--url", help="target a running arena instead of starting one")
    ap.add_argument("--token", help="host token of the running arena (with --url)")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--json", help="write every run's results here")
    a = ap.parse_args()
    if a.url and not a.token:
        ap.error("--url needs --token (the host token printed by the arena)")
    pools = [int(x) for x in a.sweep.split(",")] if a.sweep else [a.pool]
    results = []
    for pool in pools:
        r = run(a.phones, a.seconds, pool, a.queue, a.per_phone_queue, a.min_rate, a.max_rate, a.url, a.token, a.warmup, a.seed, a.size,
                a.max_phones or a.phones)
        results.append(r)
        print(report(r), flush=True)
        print()
    if len(results) > 1:
        print("| pool | sustained crashes/s per side | p95 tap -> verdict | mean queue | p95 tap round trip | peak RAM |")
        print("|---:|---:|---:|---:|---:|---:|")
        for r in results:
            m = r["memory_peak"]
            print(f"| {r['pool']} | {r['sustained_per_sec']:.1f} | {r['tap_to_verdict_ms']['p95'] / 1e3:.2f} s "
                  f"| {r['mean_queue']:.0f} | {r['rtt_ms']['p95']:.1f} ms | "
                  f"{(str(round(m['total_mb'])) + ' MB') if m else 'n/a'} |")
    if a.json:
        with open(a.json, "w") as f:
            json.dump(results, f, indent=1)
    return 0 if all(all(r["checks"].values()) for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
