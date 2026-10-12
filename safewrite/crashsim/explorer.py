"""Record an engine, crash it everywhere, recover, and judge the result.

Pipeline for one (engine, scenario, model)::

    record   run the scenario on an in-memory FS through RecordingVFS -> trace
    for each crash point in the trace:
        for each disk state the model allows (model.enumerate_states):
            build that disk image
            open the engine on it          <- the engine's real recovery code
            read every document back
            classify  OK / LOST / TORN_TX / CORRUPT / DETECTED / UNRECOVERABLE
            check     recovery is idempotent (recover twice, same answer)
            crash the recovery itself at every point, recover again, re-check

Outcome codes:

``OK``             the state is one the spec allows (old, new, or in between for sequences)
``LOST``           an *acknowledged* commit is missing (durability violation)
``TORN_TX``        some documents of one transaction are new, others old (atomicity violation)
``CORRUPT``        a document's bytes match no version: silent corruption, the worst case
``DETECTED``       a checksum caught damage, but neither version is available (data loss)
``UNRECOVERABLE``  recovery itself failed: the store cannot be opened
``NONIDEMPOTENT``  running recovery twice gave different answers
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..engines import Engine, parse_spec, spec_info
from ..format import IntegrityError, sha256
from ..vfs.base import CrashNow
from ..vfs.memfs import MemVFS
from ..vfs.recording import BaseImage, Op, RecordingVFS
from .model import MODELS, CrashState, Image, Model, build_image, enumerate_states, phase_at
from .provenance import describe_mixture, provenance
from .scenarios import SCENARIOS, Scenario, State

VIOLATIONS = ("LOST", "TORN_TX", "CORRUPT", "DETECTED", "UNRECOVERABLE", "NONIDEMPOTENT")
SEVERITY = {"OK": 0, "LOST": 2, "TORN_TX": 3, "DETECTED": 3, "NONIDEMPOTENT": 3,
            "UNRECOVERABLE": 4, "CORRUPT": 5}


# ----------------------------------------------------------------------------
# recording
# ----------------------------------------------------------------------------
@dataclass
class Recording:
    spec: str
    scenario: Scenario
    options: Dict[str, Any]
    trace: List[Op]
    base: BaseImage
    states: List[State]
    error: str = ""

    def engine(self, vfs) -> Engine:
        cls, _, _ = parse_spec(self.spec)
        return cls(vfs, **self.options)


def record(spec: str, scenario: Scenario) -> Recording:
    cls, opts, _ = parse_spec(spec)
    options = {**scenario.options_for(cls.name), **opts}
    mem = MemVFS()
    eng = cls(mem, **options)
    eng.format()
    eng.open()
    if scenario.initial:
        eng.commit(dict(scenario.initial))
    eng.close()
    rec = RecordingVFS(mem)
    eng = cls(rec, **options)
    eng.open()
    rec.reset()          # the setup above is the durable starting point
    error = ""
    try:
        for i, step in enumerate(scenario.steps, 1):
            rec.note("begin", step=i)
            eng.commit(dict(step))
            rec.note("ack", step=i)
    except Exception as e:  # an engine refusing the workload is itself a finding
        error = f"{type(e).__name__}: {e}"
    return Recording(spec, scenario, options, list(rec.trace), rec.base, scenario.states(), error)


def crash_points(trace: Sequence[Op]) -> List[int]:
    """0, after every mutating op, and after every acknowledgement."""
    pts = [0]
    for i, op in enumerate(trace):
        if op.mutating or (op.kind == "note" and op.label == "ack"):
            pts.append(i + 1)
    return sorted(set(pts))


def window(trace: Sequence[Op], point: int) -> Tuple[int, int]:
    """(acknowledged commits, started commits) before ``point``."""
    a = sum(1 for op in trace[:point] if op.kind == "note" and op.label == "ack")
    b = sum(1 for op in trace[:point] if op.kind == "note" and op.label == "begin")
    return a, b


# ----------------------------------------------------------------------------
# judging one recovered store
# ----------------------------------------------------------------------------
@dataclass
class Outcome:
    code: str
    detail: str
    version: Optional[int] = None
    state: Optional[State] = None
    errors: Optional[Dict[str, str]] = None

    @property
    def ok(self) -> bool:
        return self.code == "OK"


def version_label(j: int, n: int) -> str:
    if j == 0:
        return "previous version"
    if j == n:
        return "new version"
    return f"intermediate version S{j}"


def read_state(eng: Engine, keys: Sequence[str]) -> Tuple[State, Dict[str, str], List[str]]:
    state: State = {}
    errors: Dict[str, str] = {}
    for k in keys:
        try:
            state[k] = eng.get(k)
        except IntegrityError as e:
            errors[k] = str(e)
    try:
        extra = [k for k in eng.keys() if k not in keys]
    except Exception:  # listing itself failed
        extra = []
    return state, errors, extra


def judge(state: State, errors: Dict[str, str], extra: List[str], states: List[State],
          a: int, b: int) -> Outcome:
    n = len(states) - 1
    if errors:
        k, e = next(iter(errors.items()))
        return Outcome("DETECTED", f"{k}: {e}", None, state, errors)
    if extra:
        return Outcome("CORRUPT", f"phantom document(s) appeared: {', '.join(extra)}", None, state)
    for j in range(b, a - 1, -1):
        if state == states[j]:
            return Outcome("OK", f"recovered the {version_label(j, n)}", j, state)
    for j in range(a):
        if state == states[j]:
            return Outcome("LOST", f"commit was acknowledged, but recovery returned the {version_label(j, n)}",
                           j, state)
    candidates = {k: [s[k] for s in states[:b + 1]] for k in state}
    if all(state[k] in candidates[k] for k in state):
        parts = []
        for k in state:
            j = max(i for i, s in enumerate(states[:b + 1]) if s[k] == state[k])
            parts.append(f"{k}={'new' if j == n else 'old' if j == 0 else f'S{j}'}")
        return Outcome("TORN_TX", "transaction half-applied: " + ", ".join(parts), None, state)
    bad = [k for k in state if state[k] not in candidates[k]]
    k = bad[0]
    versions = [("old", states[0][k]), ("new", states[-1][k])]
    if state[k] is None:
        return Outcome("CORRUPT", f"{k}: document vanished", None, state)
    return Outcome("CORRUPT", f"{k}: {describe_mixture(state[k], versions)}", None, state)


def recover_and_judge(rec: Recording, files: Dict[str, bytes], dirs: List[str], a: int, b: int,
                      check_idempotent: bool = True) -> Tuple[Outcome, Optional[RecordingVFS], List[Dict[str, str]]]:
    """Open the engine on a crash image; returns (outcome, recovery recorder, recovery events)."""
    mem = MemVFS(files, dirs)
    rvfs = RecordingVFS(mem)
    eng = rec.engine(rvfs)
    try:
        report = eng.open()
    except Exception as e:
        return Outcome("UNRECOVERABLE", f"recovery failed: {e}"), rvfs, [
            *(ev.to_json() for ev in eng.events), {"kind": "error", "msg": str(e)}]
    events = [ev.to_json() for ev in report.events]
    state, errors, extra = read_state(eng, rec.scenario.keys)
    out = judge(state, errors, extra, rec.states, a, b)
    try:
        eng.close()
    except Exception:
        pass
    if check_idempotent and out.code not in ("UNRECOVERABLE",):
        eng2 = rec.engine(mem)
        try:
            eng2.open()
            state2, errors2, _ = read_state(eng2, rec.scenario.keys)
            if state2 != state or set(errors2) != set(errors):
                out = Outcome("NONIDEMPOTENT", "a second recovery returned a different state", None, state2)
        except Exception as e:
            out = Outcome("NONIDEMPOTENT", f"second recovery failed: {e}")
    return out, rvfs, events


# ----------------------------------------------------------------------------
# exploring
# ----------------------------------------------------------------------------
def explore(spec: str, scenario: str = "update", model: str = "power",
            recovery_crashes: bool = True, max_examples: int = 12,
            progress: Optional[Callable[[int, int], None]] = None) -> Dict[str, Any]:
    t0 = time.perf_counter()
    sc = SCENARIOS[scenario]
    mdl = MODELS[model]
    rec = record(spec, sc)
    trace = rec.trace
    points = crash_points(trace)
    cache: Dict[Tuple[str, int, int], Tuple[Outcome, Optional[RecordingVFS], Image]] = {}
    counts: Dict[str, int] = {}
    timeline: List[Dict[str, Any]] = []
    examples: List[Dict[str, Any]] = []
    total = 0
    exhaustive_all = True
    rec_states = rec_viol = 0
    nested_budget = 40
    nested_done = set()
    seen_codes: Dict[str, int] = {}

    for pi, point in enumerate(points):
        states, exhaustive = enumerate_states(trace, rec.base, point, mdl)
        exhaustive_all &= exhaustive
        a, b = window(trace, point)
        pc: Dict[str, int] = {}
        for si, st in enumerate(states):
            img = build_image(trace, rec.base, st)
            key = (img.fingerprint(), a, b)
            if key not in cache:
                out, rvfs, _ = recover_and_judge(rec, img.files, img.dirs, a, b)
                cache[key] = (out, rvfs, img)
            out, rvfs, img = cache[key]
            code = out.code
            # Crash the recovery itself (once per unique image whose recovery writes).
            if (recovery_crashes and out.ok and rvfs is not None and key[0] not in nested_done
                    and len(nested_done) < nested_budget and any(op.mutating for op in rvfs.trace)):
                nested_done.add(key[0])
                ns, nv, nex = _crash_recovery(rec, rvfs, mdl, a, b)
                rec_states += ns
                rec_viol += nv
                if nex and len(examples) < max_examples:
                    examples.append({**nex, "point": point, "phase": phase_at(trace, point),
                                     "state_index": si, "crash": st.describe(trace)})
            total += 1
            counts[code] = counts.get(code, 0) + 1
            pc[code] = pc.get(code, 0) + 1
            if code != "OK" and len(examples) < max_examples and seen_codes.get(code, 0) < 4:
                seen_codes[code] = seen_codes.get(code, 0) + 1
                examples.append({"point": point, "phase": phase_at(trace, point), "state_index": si,
                                 "code": code, "detail": out.detail, "crash": st.describe(trace)})
        last = trace[point - 1] if point else None
        timeline.append({"point": point, "phase": phase_at(trace, point),
                         "op": last.describe() if last else "(before the first operation)",
                         "acked": a, "started": b, "states": len(states),
                         "violations": sum(v for k, v in pc.items() if k != "OK"),
                         "outcomes": pc, "exhaustive": exhaustive})
        if progress:
            progress(pi + 1, len(points))

    violations = sum(v for k, v in counts.items() if k != "OK")
    info = spec_info(spec)
    multi_doc = any(len(step) > 1 for step in sc.steps)
    limitation = (multi_doc and not rec.engine(MemVFS()).atomic_tx and info["expected_safe"]
                  and set(counts) <= {"OK", "TORN_TX"} and not rec_viol)
    return {
        "spec": spec, "engine": info["name"], "variant": info["variant"],
        "expected_safe": info["expected_safe"], "breaks": info["breaks"],
        "scenario": scenario, "model": model, "record_error": rec.error,
        "trace_len": len([op for op in trace if op.mutating]),
        "fsyncs": sum(1 for op in trace if op.kind in ("fsync", "fsync_dir")),
        "crash_points": len(points), "states": total, "unique_images": len({k[0] for k in cache}),
        "exhaustive": exhaustive_all, "outcomes": counts, "violations": violations,
        "recovery_crash_states": rec_states, "recovery_violations": rec_viol,
        "verdict": "LIMITATION" if limitation else _verdict(violations + rec_viol, info["expected_safe"]),
        "timeline": timeline, "examples": examples,
        "seconds": round(time.perf_counter() - t0, 3),
    }


def _verdict(violations: int, expected_safe: bool) -> str:
    if violations == 0:
        return "PASS" if expected_safe else "UNEXPECTED-PASS"
    return "FAIL" if expected_safe else "BROKEN-AS-EXPECTED"


def _crash_recovery(rec: Recording, rvfs: RecordingVFS, mdl: Model, a: int, b: int
                    ) -> Tuple[int, int, Optional[Dict[str, Any]]]:
    """Crash the recovery run recorded in ``rvfs`` at every point; recover again."""
    trace = rvfs.trace
    n = viol = 0
    example = None
    for point in [0] + [i + 1 for i, op in enumerate(trace) if op.mutating][:-1]:
        states, _ = enumerate_states(trace, rvfs.base, point, mdl)
        for st in states[:60]:
            img = build_image(trace, rvfs.base, st)
            out, _, _ = recover_and_judge(rec, img.files, img.dirs, a, b, check_idempotent=False)
            n += 1
            if not out.ok:
                viol += 1
                if example is None:
                    example = {"code": "RECOVERY_CRASH/" + out.code,
                               "detail": f"crash during recovery after "
                                         f"{trace[point - 1].describe() if point else 'nothing'}: {out.detail}"}
    return n, viol, example


# ----------------------------------------------------------------------------
# the Lab: one crash state, fully explained
# ----------------------------------------------------------------------------
_REC_CACHE: Dict[Tuple[str, str], Recording] = {}


def get_recording(spec: str, scenario: str) -> Recording:
    key = (spec, scenario)
    if key not in _REC_CACHE:
        _REC_CACHE[key] = record(spec, SCENARIOS[scenario])
    return _REC_CACHE[key]


def trace_view(spec: str, scenario: str, model: str = "power") -> Dict[str, Any]:
    rec = get_recording(spec, scenario)
    mdl = MODELS[model]
    points = []
    for p in crash_points(rec.trace):
        states, exhaustive = enumerate_states(rec.trace, rec.base, p, mdl)
        a, b = window(rec.trace, p)
        points.append({"point": p, "states": len(states), "exhaustive": exhaustive,
                       "phase": phase_at(rec.trace, p), "acked": a, "started": b})
    return {
        "spec": spec, "info": spec_info(spec), "scenario": rec.scenario.to_json(),
        "model": mdl.to_json(), "error": rec.error,
        "trace": [op.to_json() for op in rec.trace],
        "points": points,
        "base_files": {p: len(rec.base.contents[i]) for p, i in rec.base.names.items()},
        "versions": [{k: (None if v is None else {"len": len(v), "sha256": sha256(v)[:12]})
                      for k, v in s.items()} for s in rec.states],
    }


def _doc_view(key: str, value: Optional[bytes], err: Optional[str], states: List[State]) -> Dict[str, Any]:
    n = len(states) - 1
    versions = [("old", states[0].get(key)), ("new", states[-1].get(key))]
    for j in range(1, n):
        versions.append((f"S{j}", states[j].get(key)))
    d: Dict[str, Any] = {"key": key}
    if err:
        d.update(status="detected", text=err)
        return d
    if value is None:
        match = [j for j, s in enumerate(states) if s.get(key) is None]
        d.update(status="absent", match=match, text="document does not exist")
        return d
    match = [j for j, s in enumerate(states) if s.get(key) == value]
    d.update(status="match" if match else "corrupt", match=match, len=len(value),
             sha256=sha256(value)[:12], preview=value[:600].decode("utf-8", "replace"),
             provenance=provenance(value, versions) if not match else None,
             text=(f"identical to the {version_label(match[-1], n)}" if match
                   else describe_mixture(value, versions)))
    return d


def inspect(spec: str, scenario: str, model: str, point: int, index: int) -> Dict[str, Any]:
    """Everything the Lab shows for one crash state."""
    rec = get_recording(spec, scenario)
    mdl = MODELS[model]
    trace = rec.trace
    states, exhaustive = enumerate_states(trace, rec.base, point, mdl)
    if not states:
        raise ValueError("no crash states at this point")
    index = max(0, min(index, len(states) - 1))
    st = states[index]
    img = build_image(trace, rec.base, st, track=True)
    a, b = window(trace, point)

    status: Dict[int, str] = {}
    for op in trace[:point]:
        if not op.mutating:
            continue
        if op.kind in ("fsync", "fsync_dir"):
            status[op.idx] = "barrier"
        elif op.idx in st.ns_lost or op.idx in st.lost:
            status[op.idx] = "lost"
        elif op.idx in st.forced:
            status[op.idx] = "durable"
        elif op.idx in st.lucky:
            status[op.idx] = "survived"
        else:
            status[op.idx] = "persisted"
    if st.tear is not None:
        status[st.tear.op] = "torn"

    out, rvfs, events = recover_and_judge(rec, img.files, img.dirs, a, b)
    mem = MemVFS(img.files, img.dirs)
    eng = rec.engine(mem)
    docs = []
    try:
        eng.open()
        state, errors, extra = read_state(eng, rec.scenario.keys)
        for k in rec.scenario.keys:
            docs.append(_doc_view(k, state.get(k), errors.get(k), rec.states))
        layout = eng.layout()
    except Exception as e:
        layout = {}
        docs.append({"key": "*", "status": "unrecoverable", "text": str(e)})
    n = len(rec.states) - 1
    return {
        "point": point, "index": index, "count": len(states), "exhaustive": exhaustive,
        "phase": phase_at(trace, point), "acked": a, "started": b,
        "crash": st.to_json(trace), "op_status": {str(k): v for k, v in status.items()},
        "disk": [{"path": p, "size": len(img.files[p]), "owners": img.owners.get(p, [])}
                 for p in sorted(img.files)],
        "recovery": {"events": events,
                     "writes": [op.to_json() for op in (rvfs.trace if rvfs else []) if op.mutating]},
        "outcome": {"code": out.code, "detail": out.detail,
                    "label": version_label(out.version, n) if out.version is not None else None},
        "documents": docs, "layout": layout,
    }


def find_state(spec: str, scenario: str, model: str, want: str = "violation",
               seed: Optional[int] = None) -> Tuple[int, int]:
    """Pick a (point, index) for the Lab's "pull the plug" button.

    ``want`` = "violation" finds a failing state if one exists, "random" picks any.
    """
    import random as _r
    rec = get_recording(spec, scenario)
    mdl = MODELS[model]
    rng = _r.Random(seed)
    points = crash_points(rec.trace)
    if want == "random":
        p = rng.choice(points[1:] or points)
        states, _ = enumerate_states(rec.trace, rec.base, p, mdl)
        return p, rng.randrange(len(states))
    order = points[:]
    rng.shuffle(order)
    for p in order:
        states, _ = enumerate_states(rec.trace, rec.base, p, mdl)
        a, b = window(rec.trace, p)
        idxs = list(range(len(states)))
        rng.shuffle(idxs)
        for i in idxs[:80]:
            img = build_image(rec.trace, rec.base, states[i])
            out, _, _ = recover_and_judge(rec, img.files, img.dirs, a, b, check_idempotent=False)
            if not out.ok:
                return p, i
    return find_state(spec, scenario, model, "random", seed)


__all__ = ["record", "explore", "inspect", "trace_view", "find_state", "crash_points",
           "Recording", "Outcome", "VIOLATIONS", "SEVERITY", "CrashNow", "CrashState"]
