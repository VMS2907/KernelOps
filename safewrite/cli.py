"""Command-line interface: ``python -m safewrite <command>``.

Store commands work on real directories with real system calls::

    init      create a store              put / get / rm / ls / history
    recover   open a store and print what recovery did
    verify    scrub every checksum         info   engine and layout details
    crash     really kill this process (os._exit) at any syscall of a save

Proof commands::

    trace     print an engine's exact syscall protocol for a scenario
    explore   exhaustive crash-state exploration (the matrix)
    torture   real kill -9 loop against real files
    race      lost-update race vs. synchronized read-modify-write
    bench     latency / fsyncs / write amplification / recovery time
    serve     the Crash Lab web UI
    demo      a narrated walk-through in the terminal
    challenge the live technical challenges KO-16-C1..C5
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional, Sequence

from . import __version__
from .engines import all_specs, make, spec_info
from .format import IntegrityError, sha256
from .store import COMMIT, LOCK, META, SafeStore, StoreError

# ----------------------------------------------------------------------------
# terminal styling
# ----------------------------------------------------------------------------
_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR") and os.name != "nt" or bool(
    os.environ.get("FORCE_COLOR"))


def c(text: str, *styles: str) -> str:
    if not _COLOR:
        return text
    codes = {"bold": "1", "dim": "2", "red": "31", "green": "32", "yellow": "33", "blue": "34",
             "magenta": "35", "cyan": "36", "grey": "90", "inv": "7"}
    return "".join(f"\033[{codes[s]}m" for s in styles) + text + "\033[0m"


def ok(msg: str) -> None:
    print(c("  ✓ ", "green", "bold") + msg)


def bad(msg: str) -> None:
    print(c("  ✗ ", "red", "bold") + msg)


def note(msg: str) -> None:
    print(c("  · ", "grey") + msg)


def head(msg: str) -> None:
    print("\n" + c(msg, "bold"))


def _read_value(args: argparse.Namespace) -> bytes:
    if getattr(args, "text", None) is not None:
        return args.text.encode()
    if getattr(args, "file", None):
        if args.file == "-":
            return sys.stdin.buffer.read()
        with open(args.file, "rb") as f:
            return f.read()
    raise SystemExit("give the new content with --text TEXT or --file PATH (or --file - for stdin)")


def _print_report(store: SafeStore) -> None:
    rep = store.recovery
    icon = {"ok": c("✓", "green"), "replay": c("↻", "cyan"), "rollback": c("↶", "yellow"),
            "discard": c("✂", "magenta"), "select": c("➜", "blue"), "warn": c("!", "yellow"),
            "error": c("✗", "red"), "info": c("·", "grey")}
    print(c(f"recovery ({store.spec}, {rep.seconds * 1000:.2f} ms): ", "bold") + rep.summary())
    for ev in rep.events:
        print(f"  {icon.get(ev.kind, '·')} {ev.msg}")


# ----------------------------------------------------------------------------
# store commands
# ----------------------------------------------------------------------------
def cmd_init(a: argparse.Namespace) -> int:
    opts: Dict[str, Any] = {}
    if a.block_size:
        opts["block_size"] = a.block_size
    if a.temp_names:
        if a.engine.split("/")[0] != "shadow":
            raise SystemExit("--temp-names applies to the shadow engine only")
        opts["temp_names"] = a.temp_names
    with SafeStore.create(a.store, engine=a.engine, **opts) as s:
        info = s.info()
    ok(f"created {a.engine} store at {info['path']}")
    note(f"commit point: {info['commit_point']}")
    note(f"after a crash recovers to: {info['recovers_to']}")
    return 0


def cmd_put(a: argparse.Namespace) -> int:
    value = _read_value(a)
    with SafeStore.open(a.store) as s:
        t0 = time.perf_counter()
        s.put(a.key, value)
        dt = time.perf_counter() - t0
    if s.engine.safe and "/" not in s.spec:
        ok(f"{a.key}: {len(value)} bytes committed and durable in {dt * 1000:.2f} ms "
           f"(sha256 {sha256(value)[:12]})")
    else:
        note(f"{a.key}: {len(value)} bytes written in {dt * 1000:.2f} ms with engine {s.spec}: "
             "NOT crash-safe (no commit point)")
    return 0


def cmd_get(a: argparse.Namespace) -> int:
    with SafeStore.open(a.store) as s:
        try:
            if a.version is not None:
                getv = getattr(s.engine, "get_version", None)
                if getv is None:
                    raise SystemExit(f"engine {s.spec} keeps no history; only cow does")
                data = getv(a.key, a.version)
            else:
                data = s.get(a.key)
        except IntegrityError as e:
            bad(f"{a.key}: {e}")
            return 2
    if data is None:
        bad(f"{a.key}: no such document")
        return 1
    if a.out:
        with open(a.out, "wb") as f:
            f.write(data)
        ok(f"wrote {len(data)} bytes to {a.out} (sha256 {sha256(data)[:12]})")
    else:
        sys.stdout.buffer.write(data)
        sys.stdout.flush()
    return 0


def cmd_rm(a: argparse.Namespace) -> int:
    with SafeStore.open(a.store) as s:
        s.delete(a.key)
    ok(f"{a.key} deleted")
    return 0


def cmd_ls(a: argparse.Namespace) -> int:
    with SafeStore.open(a.store) as s:
        for k in s.keys():
            try:
                v = s.get(k) or b""
                print(f"{len(v):>10}  {sha256(v)[:12]}  {k}")
            except IntegrityError as e:
                print(f"{'DAMAGED':>10}  {'':12}  {k}  ({e})")
    return 0


def cmd_history(a: argparse.Namespace) -> int:
    with SafeStore.open(a.store) as s:
        versions = s.history(a.key)
        if s.spec.split("/")[0] != "cow":
            note(f"engine {s.spec} keeps only the current version (use the cow engine for history)")
        for v in versions:
            when = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(v.timestamp)) if v.timestamp else ""
            if v.deleted:
                print(f"  gen {v.version:>4}  {when}  {c('deleted', 'red')}")
            else:
                print(f"  v{v.version:>4}  {when}  {v.length:>8} bytes  sha256 {v.sha256[:12]}")
    return 0


def cmd_verify(a: argparse.Namespace) -> int:
    with SafeStore.open(a.store) as s:
        problems = s.verify()
        n = len(s.keys())
    if problems:
        for p in problems:
            bad(p)
        return 2
    ok(f"all {n} document(s) verified (every checksum matches)")
    return 0


def cmd_recover(a: argparse.Namespace) -> int:
    try:
        with SafeStore.open(a.store) as s:
            _print_report(s)
            problems = s.verify()
            if problems:
                for p in problems:
                    bad(p)
                return 2
            ok(f"{len(s.keys())} document(s) readable and verified")
            if a.key:
                data = s.get(a.key)
                if data is None:
                    note(f"{a.key}: does not exist")
                else:
                    note(f"{a.key}: {len(data)} bytes, sha256 {sha256(data)[:12]}")
                    if a.out:
                        with open(a.out, "wb") as f:
                            f.write(data)
                        ok(f"wrote recovered {a.key} to {a.out}")
    except (StoreError, IntegrityError) as e:
        bad(f"RECOVERY ERROR: {e}")
        return 2
    return 0


def cmd_info(a: argparse.Namespace) -> int:
    with SafeStore.open(a.store) as s:
        info = s.info()
        info["layout"] = s.engine.layout()
    print(json.dumps(info, indent=2, default=str))
    return 0


# ----------------------------------------------------------------------------
# crash: a real process death at any syscall
# ----------------------------------------------------------------------------
def _print_trace(trace: Sequence[Any], upto: Optional[int] = None, mark: Optional[int] = None) -> None:
    for op in trace[:upto]:
        if op.kind == "note":
            if op.label in ("commit",):
                print(c(f"        ── COMMIT POINT ── {op.info.get('detail', '')}", "green", "bold"))
            elif op.label in ("begin", "ack"):
                print(c(f"        ── {op.label.upper()} {op.info.get('step', '')}", "grey"))
            else:
                print(c(f"        # {op.label}", "cyan"))
            continue
        style = {"fsync": ("yellow",), "fsync_dir": ("yellow",), "rename": ("magenta", "bold")}.get(op.kind, ())
        line = f"  [{op.info.get('syscall', op.idx):>3}] {op.describe()}"
        print(c(line, *style, "inv") if mark == op.idx else c(line, *style))


def cmd_crash(a: argparse.Namespace) -> int:
    from .vfs import MemVFS, OsVFS, RecordingVFS
    value = _read_value(a)
    path = a.store
    with open(os.path.join(path, META), "rb") as f:
        meta = json.loads(f.read().decode())
    spec, options = meta["engine"], meta.get("options", {})

    # Dry run on an in-memory copy to show the numbered syscall plan.
    snap = OsVFS(path).snapshot()
    snap.pop(LOCK, None)
    snap.pop(COMMIT, None)
    mem = MemVFS(snap, OsVFS(path).walk_dirs())
    rec = RecordingVFS(mem)
    eng = make(spec, rec, **options)
    eng.open()
    rec.reset()
    eng.commit({a.key: value})
    plan = rec.trace
    points = type(eng).CRASH_POINTS
    if a.list or (a.at is None and a.phase is None):
        head(f"syscall plan for put({a.key!r}) with engine {spec}:")
        _print_trace(plan)
        print("\nre-run with --at N to kill the process just before syscall [N] "
              "(or --phase NAME to kill at a protocol phase)")
        for name, cp in points.items():
            note(f"--phase {name}: {cp['means']}")
        return 0
    named = a.phase if a.phase in points else None
    if named:
        a.at, a.phase, a.partial = resolve_crash_point(plan, points[named], a.partial)

    lock = SafeStore._lock_for(path)
    rvfs = RecordingVFS(OsVFS(path))
    eng = make(spec, rvfs, **options)
    eng.open()
    rvfs.reset()

    def die(op: Any) -> None:
        sys.stdout.flush()
        if op.kind == "note":
            where = f"at phase '{op.label}'"
        elif op.kind == "write" and rvfs.partial_write and int(len(op.data) * rvfs.partial_write):
            where = (f"inside syscall [{op.info.get('syscall')}] {op.describe()}, after "
                     f"{int(len(op.data) * rvfs.partial_write)} of {len(op.data)} bytes")
        else:
            where = f"just before syscall [{op.info.get('syscall')}] {op.describe()}"
        print(c(f"\n  ⚡ CRASH: process killed with os._exit(77) {where}", "red", "bold"), flush=True)
        print(c(f"     run:  python -m safewrite recover {path} --key {a.key}", "grey"), flush=True)
        os._exit(77)

    head(f"put({a.key!r}) with engine {spec}; real crash armed")
    if named:
        note(f"crash point {named}: {points[named]['means']}")
    rvfs.on_crash = die
    rvfs.partial_write = a.partial
    if a.phase:
        rvfs.crash_at_label = a.phase
    else:
        rvfs.crash_at = a.at
    _orig_emit = rvfs._emit

    def echo(op: Any, handle: int = 0) -> Any:
        n = rvfs.syscalls + 1
        will_crash = (rvfs.crash_at is not None and op.mutating and n >= rvfs.crash_at) or (
            rvfs.crash_at_label and op.kind == "note" and op.label == rvfs.crash_at_label)
        if not will_crash:
            if op.kind == "note":
                print(c(f"        # {op.label}", "cyan"))
            else:
                print(f"  [{n:>3}] {op.describe()}")
        return _orig_emit(op, handle)

    rvfs._emit = echo  # type: ignore[method-assign]
    eng.commit({a.key: value})
    lock.release()
    ok("the crash point was never reached: the save completed normally")
    return 0


def resolve_crash_point(plan: Sequence[Any], cp: Dict[str, Any], partial: Optional[float] = None
                        ) -> "tuple[Optional[int], Optional[str], Optional[float]]":
    """A documented crash point -> (syscall number, phase label, partial fraction)."""
    if "phase" in cp:
        return None, cp["phase"], partial
    armed = False
    for op in plan:
        if op.kind == "note" and op.label == cp["after"]:
            armed = True
        elif armed and op.kind == cp["op"]:
            return op.info["syscall"], None, cp.get("partial") if partial is None else partial
    raise SystemExit(f"crash point not found in the plan: {cp}")


# ----------------------------------------------------------------------------
# proof commands
# ----------------------------------------------------------------------------
def cmd_trace(a: argparse.Namespace) -> int:
    from .crashsim.explorer import get_recording
    rec = get_recording(a.engine, a.scenario)
    info = spec_info(a.engine)
    head(f"{info['title']} [{a.engine}]: scenario '{a.scenario}'")
    note(info["technique"])
    note(f"commit point: {info['commit_point']}")
    if info["breaks"]:
        print(c(f"  ABLATION: {info['breaks']}", "red"))
    print()
    _print_trace(rec.trace)
    n_sync = sum(1 for op in rec.trace if op.kind in ("fsync", "fsync_dir"))
    n_ops = sum(1 for op in rec.trace if op.mutating)
    print(f"\n  {n_ops} mutating syscalls, {n_sync} of them fsync barriers")
    return 0


def _matrix_cell(r: Dict[str, Any]) -> str:
    v = r["violations"] + r["recovery_violations"]
    n = r["states"] + r["recovery_crash_states"]
    verdict = r["verdict"]
    if verdict == "PASS":
        return c(f"✓ {n:>5}", "green")
    if verdict == "LIMITATION":
        return c(f"◐ {v:>3}/{n:<4}", "yellow")
    if verdict == "BROKEN-AS-EXPECTED":
        return c(f"✗ {v:>3}/{n:<4}", "red")
    if verdict == "UNEXPECTED-PASS":
        return c(f"· {n:>5}", "grey")
    return c(f"!! {v}/{n}", "red", "bold", "inv")


def cmd_explore(a: argparse.Namespace) -> int:
    from .crashsim import DEFAULT_SCENARIOS, explore
    specs = a.engines.split(",") if a.engines else all_specs()
    scenarios = a.scenarios.split(",") if a.scenarios else DEFAULT_SCENARIOS
    models = a.models.split(",") if a.models else ["process", "power", "paranoid"]
    t0 = time.perf_counter()
    results: List[Dict[str, Any]] = []
    unexpected = 0
    for model in models:
        head(f"crash model: {model}")
        print(f"  {'engine':<22}" + "".join(f"{s:>13}" for s in scenarios))
        for spec in specs:
            cells = []
            for sc in scenarios:
                r = explore(spec, sc, model, recovery_crashes=not a.no_recovery_crashes)
                if not a.keep_details:
                    r = {k: v for k, v in r.items() if k != "timeline"}
                results.append(r)
                cells.append(_matrix_cell(r))
                if r["verdict"] == "FAIL":
                    unexpected += 1
            name = spec if "/" not in spec else "  └ " + spec.split("/", 1)[1]
            print(f"  {name:<22}" + "".join(f"{x:>22}" if _COLOR else f"{x:>13}" for x in cells))
    total = sum(r["states"] + r["recovery_crash_states"] for r in results)
    safe_states = sum(r["states"] + r["recovery_crash_states"] for r in results if r["expected_safe"])
    safe_viol = sum(r["violations"] + r["recovery_violations"] for r in results
                    if r["expected_safe"] and r["verdict"] != "LIMITATION")
    broken = {r["spec"] for r in results if r["verdict"] == "BROKEN-AS-EXPECTED"}
    ablations = {s for s in specs if not spec_info(s)["expected_safe"]}
    head("summary")
    print(f"  {total} crash states explored in {time.perf_counter() - t0:.1f} s")
    (ok if not safe_viol else bad)(f"safe engines: {safe_states} crash states, {safe_viol} violations")
    (ok if broken >= ablations else bad)(
        f"ablations caught: {len(broken & ablations)}/{len(ablations)} "
        "(each removed safeguard produces at least one counter-example)")
    note("legend: ✓ all states correct   ✗ violations/states (expected: safeguard removed)   "
         "◐ multi-doc limitation   · ablation not exercised by this scenario")
    if a.examples:
        head("counter-examples")
        for r in results:
            for ex in r["examples"][: a.examples]:
                print(f"  {c(r['spec'], 'bold')} [{r['scenario']}/{r['model']}] "
                      f"{c(ex['code'], 'red')}: {ex['detail']}")
                print(c(f"      crash after '{ex['phase']}': {ex['crash']}", "grey"))
    if a.json:
        with open(a.json, "w") as f:
            json.dump({"generated": time.strftime("%Y-%m-%d %H:%M:%S"), "results": results}, f, indent=1)
        note(f"results written to {a.json}")
    return 1 if unexpected else 0


def cmd_torture(a: argparse.Namespace) -> int:
    from .torture import torture
    engines = a.engines.split(",") if a.engines else ["naive", "naive/trunc", "shadow", "undo", "wal", "cow"]
    head(f"kill -9 torture: {a.trials} real crashes per engine, {a.size} byte documents")
    results = []
    bad_safe = 0
    for eng in engines:
        def tick(ev: Dict[str, Any]) -> None:
            if not _COLOR:
                return
            sym = c("•", "green") if str(ev["code"]).startswith("OK") else c("✗", "red")
            print(sym, end="", flush=True)
        print(f"  {eng:<14}", end="", flush=True)
        r = torture(eng, trials=a.trials, size=a.size, directory=a.dir, progress=tick)
        results.append(r.to_json())
        oc = r.outcomes
        summary = (f" {r.trials} kills: {oc.get('OK_OLD', 0)} old, {oc.get('OK_NEW', 0)} new, "
                   f"{r.violations} corrupt/lost  ({r.puts_per_sec:.0f} saves/s)")
        print(("\n" + " " * 16 if _COLOR else "") + (c(summary, "red") if r.violations else c(summary, "green")))
        for ex in r.examples[:2]:
            note(ex)
        if r.violations and spec_info(eng)["expected_safe"]:
            bad_safe += 1
    if a.json:
        with open(a.json, "w") as f:
            json.dump({"generated": time.strftime("%Y-%m-%d %H:%M:%S"), "results": results}, f, indent=1)
    return 1 if bad_safe else 0


def cmd_bench(a: argparse.Namespace) -> int:
    from .bench import run_bench, print_bench
    res = run_bench(engines=a.engines.split(",") if a.engines else None, n=a.n, size=a.size,
                    threads=[int(x) for x in a.threads.split(",")], directory=a.dir)
    print_bench(res, c)
    if a.json:
        with open(a.json, "w") as f:
            json.dump(res, f, indent=1)
    return 0


def cmd_race(a: argparse.Namespace) -> int:
    from .race import run_race
    head(f"lost-update race: {a.threads} threads x {a.increments} increments of one counter document")
    for eng in a.engines.split(","):
        r = run_race(eng, a.threads, a.increments)
        lost = r["expected"] - r["unsynchronized"]
        print(f"  {c(eng, 'bold'):<8}")
        bad(f"get() then put():      counter = {r['unsynchronized']:>5} of {r['expected']} "
            f"({lost} updates lost: each put is atomic, the read-modify-write is not)")
        (ok if r["synchronized"] == r["expected"] else bad)(
            f"store.update(fn):      counter = {r['synchronized']:>5} of {r['expected']} "
            "(transaction lock held from read to commit)")
    return 0


def cmd_serve(a: argparse.Namespace) -> int:
    from .server.app import serve
    serve(a.host, a.port, open_browser=not a.no_browser, store_dir=a.store)
    return 0


def cmd_challenge(a: argparse.Namespace) -> int:
    from .challenges import run
    return run(a.which, a.dir, a.keep)


def cmd_demo(a: argparse.Namespace) -> int:
    from .demo import run_demo
    return run_demo(pause=not a.no_pause)


# ----------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="safewrite", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"safewrite {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="create a store")
    s.add_argument("store")
    s.add_argument("--engine", default="cow", choices=all_specs())
    s.add_argument("--block-size", type=int, help="cow only")
    s.add_argument("--temp-names", choices=["unique", "fixed"],
                   help="shadow only: one fixed temp name per document (the round-1 layout) or unique ones")
    s.set_defaults(fn=cmd_init)

    for name, fn, hlp in (("put", cmd_put, "save a document (atomic + durable)"),):
        s = sub.add_parser(name, help=hlp)
        s.add_argument("store")
        s.add_argument("key")
        s.add_argument("--text")
        s.add_argument("--file", help="path, or - for stdin")
        s.set_defaults(fn=fn)

    s = sub.add_parser("get", help="read a document")
    s.add_argument("store")
    s.add_argument("key")
    s.add_argument("--version", type=int, help="an older version (cow)")
    s.add_argument("--out")
    s.set_defaults(fn=cmd_get)

    for name, fn, hlp in (("rm", cmd_rm, "delete a document"), ("history", cmd_history, "list versions")):
        s = sub.add_parser(name, help=hlp)
        s.add_argument("store")
        s.add_argument("key")
        s.set_defaults(fn=fn)

    for name, fn, hlp in (("ls", cmd_ls, "list documents"), ("verify", cmd_verify, "scrub checksums"),
                          ("info", cmd_info, "engine and layout details")):
        s = sub.add_parser(name, help=hlp)
        s.add_argument("store")
        s.set_defaults(fn=fn)

    s = sub.add_parser("recover", help="open the store (runs recovery) and report")
    s.add_argument("store")
    s.add_argument("--key")
    s.add_argument("--out")
    s.set_defaults(fn=cmd_recover)

    s = sub.add_parser("crash", help="save a document but really kill the process at syscall N")
    s.add_argument("store")
    s.add_argument("key")
    s.add_argument("--text")
    s.add_argument("--file")
    s.add_argument("--at", type=int, help="kill just before this syscall number (see --list)")
    s.add_argument("--phase", help="kill when this protocol phase starts (e.g. rename, flip-superblock), "
                   "or at a documented crash point (shadow: during_write, after_validation, after_replace)")
    s.add_argument("--partial", type=float, help="if the victim is a write, persist this fraction first")
    s.add_argument("--list", action="store_true", help="print the numbered syscall plan and exit")
    s.set_defaults(fn=cmd_crash)

    s = sub.add_parser("trace", help="print an engine's syscall protocol")
    s.add_argument("engine", choices=all_specs())
    s.add_argument("--scenario", default="update")
    s.set_defaults(fn=cmd_trace)

    s = sub.add_parser("explore", help="exhaustive crash-state exploration")
    s.add_argument("--engines", help="comma-separated specs (default: all, with ablations)")
    s.add_argument("--scenarios", help="comma-separated (default: all)")
    s.add_argument("--models", help="process,power,paranoid (default: all)")
    s.add_argument("--examples", type=int, default=0, help="print N counter-examples per cell")
    s.add_argument("--json", help="write full results to this file")
    s.add_argument("--keep-details", action="store_true", help="include per-crash-point timelines in --json")
    s.add_argument("--no-recovery-crashes", action="store_true")
    s.set_defaults(fn=cmd_explore)

    s = sub.add_parser("torture", help="real kill -9 crash loop")
    s.add_argument("--engines")
    s.add_argument("--trials", type=int, default=100)
    s.add_argument("--size", type=int, default=64 * 1024)
    s.add_argument("--dir", help="where to put the stores (default: a temp dir)")
    s.add_argument("--json")
    s.set_defaults(fn=cmd_torture)

    s = sub.add_parser("bench", help="measure the cost of crash consistency")
    s.add_argument("--engines")
    s.add_argument("--n", type=int, default=200, help="commits per measurement")
    s.add_argument("--size", type=int, default=4096)
    s.add_argument("--threads", default="1,4,16")
    s.add_argument("--dir")
    s.add_argument("--json")
    s.set_defaults(fn=cmd_bench)

    s = sub.add_parser("race", help="lost-update race vs. synchronized read-modify-write")
    s.add_argument("--engines", default="cow,wal")
    s.add_argument("--threads", type=int, default=8)
    s.add_argument("--increments", type=int, default=50)
    s.set_defaults(fn=cmd_race)

    s = sub.add_parser("serve", help="Crash Lab web UI")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--store", default="demo-store", help="real store for the Documents tab")
    s.add_argument("--no-browser", action="store_true")
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("challenge", help="live technical challenges KO-16-C1..C5 (all by default)")
    s.add_argument("which", nargs="*", help="c1 c2 c3 c4 c5 (default: all)")
    s.add_argument("--dir", help="work here and keep the files (default: a temp dir, removed)")
    s.add_argument("--keep", action="store_true", help="keep the temp dir and print its path")
    s.set_defaults(fn=cmd_challenge)

    s = sub.add_parser("demo", help="narrated terminal demo")
    s.add_argument("--no-pause", action="store_true")
    s.set_defaults(fn=cmd_demo)
    return p


def main(argv: Optional[List[str]] = None) -> int:
    # Windows pipes default to a legacy code page that cannot print ✓ / ✗.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure") and (stream.encoding or "").lower().replace("-", "") != "utf8":
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):
                pass
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args) or 0
    except StoreError as e:
        bad(str(e))
        return 2
    except KeyboardInterrupt:
        return 130

