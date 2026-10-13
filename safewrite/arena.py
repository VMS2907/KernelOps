"""Crash Arena: the audience crashes SafeWrite live from their phones.

Every tap on a phone becomes one *pair* of real crashes.  A writer process
saving the normal way (``naive/trunc``, what ``open(path, "w")`` does) and a
writer saving with SafeWrite (``cow``) are killed with SIGKILL
(``TerminateProcess`` on Windows) at the same moment, after the same random
delay.  Each store is then opened in this process, which runs the engine's real
recovery, and the torture harness's judge decides what is on disk.

Nothing is simulated.  Every number on the projector is a count of verdicts,
and a verdict exists only after a process died and its files were recovered.

Process and thread model
------------------------
* ``pool`` writer *slots* per side.  Each slot owns a temp store and a thread
  that keeps a *gated* writer warm: spawned, store opened, waiting on stdin.
  A semaphore caps how many writers start at once, so a burst of respawns
  cannot starve the web server of CPU.
* ``lanes`` threads take taps from a fair queue (round-robin across phones).
  For each tap a lane takes one ready writer per side, releases both, waits
  until each has acknowledged one save, sleeps **one** random delay, kills
  both back to back (alternating which dies first), judges both, records one
  pair, and hands both slots back to be respawned.
* HTTP threads only touch counters and the queue under one lock.  They never
  wait for a kill or a recovery.

Scenarios
---------
What a writer saves and how a crash is judged is a :class:`Scenario`.  Today
there is one, :class:`DocumentScenario` (the torture document).  A scenario
with a different invariant (for example a bank transfer, where the total
money must never change) plugs in by implementing the same methods; the
engine, the counters, the pages and the tests only see :class:`Verdict`.
"""

from __future__ import annotations

import atexit
import codecs
import heapq
import os
import queue
import random
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
from collections import OrderedDict, deque
from dataclasses import dataclass
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from .engines import spec_info
from .store import SafeStore
from .torture import (KEY, _judge, _read_acks, _send_kill, _spawn, _wait_ready, calibrate,
                      content_for, version_of)

LOBBY, LIVE, FROZEN = "LOBBY", "LIVE", "FROZEN"
SIDES = ("naive", "safe")
MAX_CLIENTS = 5000
#: A phone that has not polled or tapped for this long gives its seat to a newcomer
#: (open phones poll every 1-5 s, so only closed tabs go quiet).
IDLE_SEAT_SECONDS = 60.0
CLIENT_ID = re.compile(r"^[A-Za-z0-9_-]{8,64}$")


# ----------------------------------------------------------------------------
# scenarios: what the writers do and how a crash is judged
# ----------------------------------------------------------------------------
class ArenaFull(ValueError):
    """Every seat is taken by an active phone."""


@dataclass
class Verdict:
    """The judged outcome of one real crash on one side."""
    code: str                   # OLD, NEW, EMPTY, TRUNCATED, CORRUPT, LOST, ...
    ok: bool                    # did the scenario's invariant hold?
    detail: str                 # plain English, e.g. "empty file (0 bytes)"
    acked: int                  # last step the writer acknowledged before it died
    recovered: Optional[int]    # step found after recovery, None if damaged
    raw: str = ""               # the underlying judge code (OK_OLD, CORRUPT, ...)

    def to_json(self) -> Dict[str, Any]:
        return {"verdict": self.code, "ok": self.ok, "detail": self.detail, "acked": self.acked,
                "inflight": self.acked + 1, "recovered": self.recovered, "raw": self.raw}


class Scenario:
    """The contract between the arena and one kind of workload.

    A writer is a gated child process that prints ``R`` once its store is open,
    waits for one line on stdin, then commits step after step and prints
    ``A <n>`` after each commit returns.  ``n`` is the scenario's progress
    counter: a document version here, a transfer count for a bank.
    """

    name = "abstract"

    def meta(self) -> Dict[str, Any]:
        """UI copy for the pages: ``{"document": ..., "verdicts": {code: {ok, short, story}}}``."""
        raise NotImplementedError

    def prepare(self, path: str, spec: str) -> int:
        """Create a fresh store at ``path`` with engine ``spec``; return the starting step."""
        raise NotImplementedError

    def seconds_per_step(self, path: str) -> float:
        """How long one commit takes on this machine (sets the kill-delay range)."""
        raise NotImplementedError

    def spawn(self, path: str, state: int) -> subprocess.Popen:
        """Start a gated writer that continues from step ``state``."""
        raise NotImplementedError

    def judge(self, path: str, state: int, acked: int) -> Verdict:
        """Open (= recover) the store the dead writer left and classify it."""
        raise NotImplementedError

    def repair(self, path: str, verdict: Verdict) -> int:
        """Make the store valid again after a verdict; return the step to continue from."""
        raise NotImplementedError


class DocumentScenario(Scenario):
    """One document saved over and over: the kill -9 torture workload, unchanged."""

    name = "document"
    DOCUMENT = "Final_Assignment.txt"
    VERDICTS = {
        "OLD": (True, "recovered the last saved version",
                "the save that was cut off never happened; the previous version is intact"),
        "NEW": (True, "recovered the new version", "the save finished a moment before the kill"),
        "EMPTY": (False, "file is EMPTY", "the assignment is gone"),
        "TRUNCATED": (False, "file is cut short", "the end of the assignment is gone"),
        "CORRUPT": (False, "file is scrambled", "a mixture of two versions that matches neither"),
        "LOST": (False, "a saved version vanished", "work the app said was saved is gone"),
        "DETECTED": (False, "damaged, no valid version left", "a checksum caught the damage"),
        "UNRECOVERABLE": (False, "store cannot be opened", "recovery itself failed"),
    }

    def __init__(self, size: int = 65536) -> None:
        self.size = size

    def meta(self) -> Dict[str, Any]:
        return {"name": self.name, "document": self.DOCUMENT, "size": self.size,
                "verdicts": {k: {"ok": ok, "short": short, "story": story}
                             for k, (ok, short, story) in self.VERDICTS.items()}}

    def prepare(self, path: str, spec: str) -> int:
        shutil.rmtree(path, ignore_errors=True)
        with SafeStore.create(path, engine=spec) as s:
            s.put(KEY, content_for(0, self.size))
        return 0

    def seconds_per_step(self, path: str) -> float:
        return calibrate(path, 0, self.size)

    def spawn(self, path: str, state: int) -> subprocess.Popen:
        return _spawn(path, state, self.size, gated=True)

    def judge(self, path: str, state: int, acked: int) -> Verdict:
        last = acked if acked >= 0 else state
        raw, detail, recovered = _judge(path, state, last, self.size)
        code = {"OK_OLD": "OLD", "OK_NEW": "NEW"}.get(raw, raw)
        if raw == "CORRUPT":
            code = ("EMPTY" if detail.startswith("empty file") else
                    "TRUNCATED" if detail.startswith("truncated") else "CORRUPT")
        return Verdict(code, raw.startswith("OK"), detail, last, recovered, raw)

    def repair(self, path: str, verdict: Verdict) -> int:
        if verdict.recovered is not None:
            return verdict.recovered
        # The document is gone or damaged: put a valid version back so the next
        # crash starts from a known state (exactly what torture() does).
        with SafeStore.open(path) as s:
            s.put(KEY, content_for(verdict.acked, self.size))
            assert version_of(s.get(KEY) or b"", self.size) == verdict.acked
        return verdict.acked


# ----------------------------------------------------------------------------
# small pieces: fair queue, rate limiter, nicknames
# ----------------------------------------------------------------------------
@dataclass
class _Token:
    id: int
    client: str
    nickname: str
    source: str          # "phone" | "auto"
    t: float             # time.time() when the tap was received
    epoch: int           # reset generation the tap belongs to


class FairQueue:
    """A bounded queue served round-robin across clients.

    One fast tapper cannot fill the line: each lane takes the oldest tap of
    the next client in turn, and each client may have at most ``per_client``
    taps waiting, so a tap is never crashed long after it was made.
    """

    FULL, BACKLOG = "full", "backlog"

    def __init__(self, maxlen: int, per_client: int = 0) -> None:
        self.maxlen = max(1, maxlen)
        self.per_client = per_client
        self._by_client: "OrderedDict[str, Deque[_Token]]" = OrderedDict()
        self._n = 0
        self._cv = threading.Condition(threading.Lock())

    def __len__(self) -> int:
        return self._n

    def put(self, client: str, item: _Token) -> Optional[str]:
        """Queue ``item``; return None, or why it was refused (FULL or BACKLOG)."""
        with self._cv:
            if self._n >= self.maxlen:
                return self.FULL
            dq = self._by_client.get(client)
            if self.per_client and dq is not None and len(dq) >= self.per_client:
                return self.BACKLOG
            self._by_client.setdefault(client, deque()).append(item)
            self._n += 1
            self._cv.notify()
            return None

    def get(self, timeout: float) -> Optional[_Token]:
        with self._cv:
            if not self._n:
                self._cv.wait(timeout)
            if not self._n:
                return None
            client, dq = next(iter(self._by_client.items()))
            item = dq.popleft()
            self._n -= 1
            if dq:
                self._by_client.move_to_end(client)
            else:
                del self._by_client[client]
            return item

    def clear(self) -> int:
        with self._cv:
            n, self._n = self._n, 0
            self._by_client.clear()
            return n

    def wake_all(self) -> None:
        with self._cv:
            self._cv.notify_all()


class RateLimiter:
    """Token bucket per key: ``rate`` taps per second, bursts of up to ``burst``."""

    def __init__(self, rate: float, burst: Optional[float] = None) -> None:
        self.rate = float(rate)
        self.burst = float(burst if burst is not None else rate)
        self._b: Dict[str, Tuple[float, float]] = {}

    def allow(self, key: str, now: float) -> bool:
        tokens, last = self._b.get(key, (self.burst, now))
        tokens = min(self.burst, tokens + (now - last) * self.rate)
        ok = tokens >= 1.0
        self._b[key] = (tokens - 1.0 if ok else tokens, now)
        if len(self._b) > 4 * MAX_CLIENTS:      # forget the quietest half
            for k in sorted(self._b, key=lambda k: self._b[k][1])[: len(self._b) // 2]:
                del self._b[k]
        return ok


# ROT13 so the word list does not appear on screen if someone opens this file
# during the demo.  Long words match anywhere; short ones only as whole words.
_BLOCK_ANYWHERE = codecs.decode(
    "shpx ohyyfuvg fuvgurnq ovgpu phag nffubyr onfgneq fyhg juber avttre avttn snttbg ergneq cravf "
    "intvan puhgvln puhgvn znqnepubq orurapubq ourapubq oubfqv oubfqn tnaqh zbgureshpxre", "rot13").split()
_BLOCK_WORDS = set(codecs.decode(
    "nff qvpx pbpx gvg gvgf phz frk frkl fuvg encr cbea enaqv ynhqn unenzv jgs fgsh op zp ofqx zxp yhaq tnaq",
    "rot13").split())
_LEET = str.maketrans("013457@$!|", "oieastasii")


def is_profane(name: str) -> bool:
    low = unicodedata.normalize("NFKD", name).lower().translate(_LEET)
    words = re.findall(r"[a-z]+", low)
    joined = "".join(words)
    if any(w in _BLOCK_WORDS for w in words):
        return True
    return any(b in joined for b in _BLOCK_ANYWHERE)


_TAG = re.compile(r"<[^>]*>?")


def clean_nickname(raw: Any) -> str:
    """At most 16 printable characters, no HTML, no profanity ("" if nothing is left)."""
    s = _TAG.sub("", str(raw or ""))
    s = unicodedata.normalize("NFKC", s)
    s = "".join(ch for ch in s if ch.isprintable() and ch not in "<>&\"'`\\")
    s = " ".join(s.split())[:16].strip()
    if s and is_profane(s):
        return "Anonymous"
    return s


def lan_addresses() -> List[str]:
    """This machine's IPv4 addresses that phones could reach, most likely first.

    A UDP ``connect`` sends no packet; it only asks the kernel which local
    address routes there, so this works on a hotspot with no internet.
    """
    import socket
    found: List[str] = []

    def add(ip: str) -> None:
        if ip and not ip.startswith(("127.", "0.", "169.254.")) and ip not in found:
            found.append(ip)

    for probe in ("8.8.8.8", "192.168.254.254", "10.254.254.254", "172.31.254.254"):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((probe, 9))
            add(s.getsockname()[0])
        except OSError:
            pass
        finally:
            s.close()
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            add(ip)
    except OSError:
        pass
    return found


def _percentile(xs: List[float], p: float) -> Optional[float]:
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100.0 * (len(xs) - 1))))]


# ----------------------------------------------------------------------------
# the engine
# ----------------------------------------------------------------------------
class _Slot:
    def __init__(self, side: str, index: int, path: str) -> None:
        self.side = side
        self.index = index
        self.path = path
        self.state = 0
        self.proc: Optional[subprocess.Popen] = None
        self.release = threading.Event()


class _Client:
    __slots__ = ("nickname", "taps", "crashes", "joined", "seen", "last")

    def __init__(self, nickname: str, now: float) -> None:
        self.nickname = nickname
        self.taps = 0
        self.crashes = 0
        self.joined = now
        self.seen = now
        self.last: Optional[Dict[str, Any]] = None


class ArenaEngine:
    """Pools of warm writers, paired crash lanes, honest counters."""

    def __init__(self, naive: str = "naive/trunc", safe: str = "cow", pool: int = 4,
                 lanes: Optional[int] = None, scenario: Optional[Scenario] = None, size: int = 65536,
                 queue_max: int = 500, per_phone: int = 3, rate: float = 8.0, autotap_rate: float = 10.0,
                 max_phones: int = 30,
                 max_spawns: Optional[int] = None, delay_max: Optional[float] = None,
                 seed: Optional[int] = None, directory: Optional[str] = None,
                 public_url: str = "") -> None:
        if not spec_info(safe)["expected_safe"]:
            raise ValueError(f"{safe!r} is not a crash-safe engine; the right side must be one")
        spec_info(naive)
        self.specs = {"naive": naive, "safe": safe}
        self.scenario = scenario or DocumentScenario(size)
        self.pool = max(1, int(pool))
        self.lanes = max(1, int(lanes or min(self.pool, 4)))
        self.queue_max = queue_max
        self.per_phone = per_phone
        self.max_phones = max(1, min(int(max_phones), MAX_CLIENTS))
        self.rate_limit = rate
        self.autotap_rate = max(0.1, float(autotap_rate))
        self.public_url = public_url
        self.delay_max = delay_max
        self.per_step: Dict[str, float] = {}
        self.base = os.path.abspath(directory or tempfile.mkdtemp(prefix="safewrite-arena-"))
        self._own_base = directory is None

        self._mu = threading.Lock()
        self._rng = random.Random(seed)
        self._queue = FairQueue(queue_max, per_phone)
        self._limiter = RateLimiter(rate)
        self._spawn_sem = threading.BoundedSemaphore(max_spawns or max(2, os.cpu_count() or 2))
        self._ready: Dict[str, "queue.Queue[_Slot]"] = {s: queue.Queue() for s in SIDES}
        self._slots: List[_Slot] = []
        self._threads: List[threading.Thread] = []
        self._procs: set = set()
        self._stop = threading.Event()
        self.on_spawn: Optional[Callable[[subprocess.Popen], None]] = None   # test hook
        self.started = False
        self.closed = False
        self.t0 = time.time()

        self.clients: Dict[str, _Client] = {}
        self.state = LOBBY
        self.autotap = False
        self.epoch = 0
        self.in_flight = 0
        self._tap_seq = 0
        self.spawned = 0
        self.seats_reclaimed = 0
        self.pool_errors = 0
        self.last_error = ""
        self._reset_stats_locked()

    # ------------------------------------------------------------ lifecycle
    def _reset_stats_locked(self) -> None:
        self.taps = {src: {"received": 0, "accepted": 0, "rate_limited": 0, "queue_full": 0, "backlog": 0,
                           "not_live": 0}
                     for src in ("phone", "auto")}
        self.processed = 0
        self.failed = 0
        self.discarded = 0
        self.stale = 0
        self._crash_seq = 0
        self.sides = {s: {"crashes": 0, "corrupted": 0, "verdicts": {}} for s in SIDES}
        self.records: List[Dict[str, Any]] = []
        self.feed: Deque[Dict[str, Any]] = deque(maxlen=300)
        self.gallery: Dict[str, Deque[Dict[str, Any]]] = {s: deque(maxlen=8) for s in SIDES}
        self._rate_window: Deque[float] = deque(maxlen=20000)
        self._latency: Deque[float] = deque(maxlen=2000)
        self.live_since: Optional[float] = None
        self.frozen_at: Optional[float] = None
        for c in self.clients.values():
            c.taps = c.crashes = 0
            c.last = None

    def start(self) -> "ArenaEngine":
        """Calibrate, create the stores and start every thread (writers warm up in the lobby)."""
        if self.started:
            return self
        self.started = True
        atexit.register(self.shutdown)
        os.makedirs(self.base, exist_ok=True)
        if self.delay_max is None:
            for side in SIDES:
                path = os.path.join(self.base, f"calibrate-{side}")
                self.scenario.prepare(path, self.specs[side])
                self.per_step[side] = self.scenario.seconds_per_step(path)
                shutil.rmtree(path, ignore_errors=True)
            # One delay range for both sides, wide enough for three of the slower saves.
            self.delay_max = 3 * max(self.per_step.values())
        for side in SIDES:
            for i in range(self.pool):
                slot = _Slot(side, i, os.path.join(self.base, f"{side}-{i}"))
                slot.state = self.scenario.prepare(slot.path, self.specs[side])
                self._slots.append(slot)
                self._thread(self._slot_loop, f"arena-{side}-{i}", slot)
        for i in range(self.lanes):
            self._thread(self._lane_loop, f"arena-lane-{i}")
        self._thread(self._autotap_loop, "arena-autotap")
        return self

    def _thread(self, fn: Callable[..., None], name: str, *args: Any) -> None:
        t = threading.Thread(target=fn, args=args, name=name, daemon=True)
        t.start()
        self._threads.append(t)

    def wait_warm(self, timeout: float = 60.0) -> bool:
        """Block until every writer is warm (used by tests and the CLI banner)."""
        end = time.time() + timeout
        while time.time() < end:
            if all(self._ready[s].qsize() >= self.pool for s in SIDES):
                return True
            time.sleep(0.02)
        return False

    def wait_idle(self, timeout: float = 60.0) -> bool:
        """Block until the queue is empty and no crash is in flight."""
        end = time.time() + timeout
        while time.time() < end:
            with self._mu:
                if not len(self._queue) and not self.in_flight:
                    return True
            time.sleep(0.02)
        return False

    def shutdown(self) -> None:
        """Kill every writer, stop every thread, delete every temp store.  Idempotent."""
        with self._mu:
            if self.closed:
                return
            self.closed = True
            self._stop.set()
            procs = list(self._procs)
        try:
            atexit.unregister(self.shutdown)
        except Exception:  # pragma: no cover
            pass
        self._queue.wake_all()
        for slot in self._slots:
            slot.release.set()
        for p in procs:                        # kill first: blocked reads in our threads then see EOF
            try:
                if p.poll() is None:
                    _send_kill(p)
            except OSError:  # pragma: no cover
                pass
        for t in self._threads:
            t.join(timeout=10)
        with self._mu:
            procs = list(self._procs)          # anything registered while we were stopping
            self._procs.clear()
        for p in procs:
            self._kill_quietly(p)
        if self._own_base:
            for _ in range(50):                # Windows may hold a handle for a moment
                shutil.rmtree(self.base, ignore_errors=True)
                if not os.path.exists(self.base):
                    break
                time.sleep(0.1)

    @staticmethod
    def _kill_quietly(p: subprocess.Popen) -> None:
        try:
            if p.poll() is None:
                _send_kill(p)
            p.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):  # pragma: no cover
            pass
        for f in (p.stdin, p.stdout, p.stderr):
            try:
                if f is not None:
                    f.close()
            except OSError:  # pragma: no cover
                pass

    # ------------------------------------------------------------ writers
    def _register(self, proc: subprocess.Popen) -> bool:
        with self._mu:
            if self._stop.is_set():
                stopping = True
            else:
                stopping = False
                self._procs.add(proc)
                self.spawned += 1
        if stopping:
            self._kill_quietly(proc)
            return False
        if self.on_spawn:
            self.on_spawn(proc)
        return True

    def _unregister(self, proc: subprocess.Popen) -> None:
        with self._mu:
            self._procs.discard(proc)

    def _slot_loop(self, slot: _Slot) -> None:
        failures = 0
        while not self._stop.is_set():
            try:
                with self._spawn_sem:
                    if self._stop.is_set():
                        break
                    proc = self.scenario.spawn(slot.path, slot.state)
                    if not self._register(proc):
                        break
                    slot.proc = proc
                    _wait_ready(proc)
            except Exception as e:
                failures += 1
                if not self._stop.is_set():
                    with self._mu:
                        self.pool_errors += 1
                        self.last_error = f"{slot.side}-{slot.index}: {type(e).__name__}: {e}"[-400:]
                if slot.proc is not None:
                    self._kill_quietly(slot.proc)
                    self._unregister(slot.proc)
                    slot.proc = None
                if self._stop.wait(min(2.0, 0.05 * 2 ** min(failures, 6))):
                    break
                continue
            failures = 0
            slot.release.clear()
            self._ready[slot.side].put(slot)
            slot.release.wait()
        if slot.proc is not None:
            self._kill_quietly(slot.proc)
            self._unregister(slot.proc)
            slot.proc = None

    def _take(self, side: str) -> Optional[_Slot]:
        while not self._stop.is_set():
            try:
                return self._ready[side].get(timeout=0.2)
            except queue.Empty:
                continue
        return None

    # ------------------------------------------------------------ crash lanes
    def _lane_loop(self) -> None:
        while not self._stop.is_set():
            tok = self._queue.get(timeout=0.2)
            if tok is None:
                continue
            with self._mu:
                self.in_flight += 1
            slots = []
            for side in SIDES:
                s = self._take(side)
                if s is not None:
                    slots.append(s)
            if len(slots) < 2:                 # shutting down
                with self._mu:
                    self.in_flight -= 1
                for s in slots:
                    s.release.set()
                return
            self._crash_pair(tok, slots[0], slots[1])

    @staticmethod
    def _first_ack(proc: subprocess.Popen) -> int:
        assert proc.stdout is not None
        line = proc.stdout.readline()
        if not line.startswith(b"A "):
            raise RuntimeError(f"writer stopped before its first save ({line!r})")
        return int(line[2:])

    def _settle(self, slot: _Slot, first: int) -> int:
        """Make sure the writer is dead and reaped; return its last acknowledged step."""
        p = slot.proc
        assert p is not None
        if p.poll() is None:
            _send_kill(p)
        p.wait()
        rest = _read_acks(p)                   # drains stdout to EOF, closes stdout/stderr
        if p.stdin is not None:
            try:
                p.stdin.close()
            except OSError:
                pass
        self._unregister(p)
        slot.proc = None
        return max(first, rest)

    def _crash_pair(self, tok: _Token, a: _Slot, b: _Slot) -> None:
        with self._mu:
            delay = self._rng.uniform(0.0, self.delay_max or 0.0)
        order = (a, b) if tok.id % 2 else (b, a)       # alternate who dies first
        first: Dict[str, int] = {}
        error = ""
        try:
            for s in (a, b):
                assert s.proc is not None and s.proc.stdin is not None
                s.proc.stdin.write(b"G\n")
                s.proc.stdin.flush()
            for s in (a, b):
                first[s.side] = self._first_ack(s.proc)  # type: ignore[arg-type]
            time.sleep(delay)
            for s in order:
                _send_kill(s.proc)  # type: ignore[arg-type]
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
        verdicts: Dict[str, Verdict] = {}
        for s in (a, b):
            try:
                acked = self._settle(s, first.get(s.side, -1))
                v = self.scenario.judge(s.path, s.state, acked)
                s.state = self.scenario.repair(s.path, v)
                verdicts[s.side] = v
            except Exception as e:
                error = error or f"{s.side}: {type(e).__name__}: {e}"
                try:
                    s.state = self.scenario.prepare(s.path, self.specs[s.side])
                except Exception as e2:  # pragma: no cover
                    error += f"; rebuild failed: {e2}"
        self._record(tok, verdicts, delay, order[0].side, error, {s.side: s.index for s in (a, b)})
        for s in (a, b):
            s.release.set()

    def _record(self, tok: _Token, verdicts: Dict[str, Verdict], delay: float, first_killed: str,
                error: str, slots: Dict[str, int]) -> None:
        now = time.time()
        with self._mu:
            self.in_flight -= 1
            if tok.epoch != self.epoch:
                self.stale += 1                 # tapped before a reset: real, but not this session's
                return
            if error or len(verdicts) != len(SIDES):
                self.failed += 1
                self.last_error = error[-400:]
                return
            self.processed += 1
            latency = (now - tok.t) * 1000.0
            pair: Dict[str, Any] = {
                "tap": tok.id, "ts": round(now, 3), "nickname": tok.nickname, "source": tok.source,
                "delay_ms": round(delay * 1000.0, 3), "first_killed": first_killed,
                "latency_ms": round(latency, 1), "sides": {}}
            for side in SIDES:
                v = verdicts[side]
                self._crash_seq += 1
                rec = {"id": self._crash_seq, "side": side, "engine": self.specs[side],
                       "slot": slots[side], **v.to_json()}
                pair["sides"][side] = rec
                st = self.sides[side]
                st["crashes"] += 1
                st["verdicts"][v.code] = st["verdicts"].get(v.code, 0) + 1
                if not v.ok:
                    st["corrupted"] += 1
                    self.gallery[side].appendleft({"tap": tok.id, "ts": pair["ts"], "nickname": tok.nickname,
                                                   "source": tok.source, **rec})
            self.records.append(pair)
            if len(self.records) > 200000:
                del self.records[:50000]
            self.feed.append(pair)
            self._rate_window.append(now)
            self._latency.append(latency)
            c = self.clients.get(tok.client)
            if c is not None:
                c.crashes += 1
                c.last = {"tap": tok.id, "sides": {s: {k: pair["sides"][s][k] for k in (
                    "verdict", "ok", "detail", "acked", "inflight", "recovered")} for s in SIDES}}

    def _autotap_loop(self) -> None:
        while not self._stop.wait(1.0 / self.autotap_rate):
            if self.autotap and self.state == LIVE:
                self._enqueue("~auto", "auto-tap", "auto")

    # ------------------------------------------------------------ public API (HTTP threads)
    def _enqueue(self, client: str, nickname: str, source: str) -> Tuple[bool, str]:
        """Count one tap and queue it if allowed.  Never blocks."""
        with self._mu:
            st = self.taps[source]
            st["received"] += 1
            if self.state != LIVE:
                st["not_live"] += 1
                return False, "not_live"
            if source == "phone" and not self._limiter.allow(client, time.monotonic()):
                st["rate_limited"] += 1
                return False, "rate_limited"
            self._tap_seq += 1
            tok = _Token(self._tap_seq, client, nickname, source, time.time(), self.epoch)
            refused = self._queue.put(client, tok)
            if refused == FairQueue.FULL:
                st["queue_full"] += 1
                return False, "busy"
            if refused == FairQueue.BACKLOG:
                st["backlog"] += 1
                return False, "backlog"
            st["accepted"] += 1
            return True, "queued"

    def join(self, client_id: str, nickname: Any) -> Dict[str, Any]:
        if not CLIENT_ID.match(str(client_id or "")):
            raise ValueError("bad client id")
        name = clean_nickname(nickname) or "Anonymous"
        now = time.time()
        with self._mu:
            c = self.clients.get(client_id)
            if c is None:
                if len(self.clients) >= self.max_phones:
                    idle = min(self.clients, key=lambda k: self.clients[k].seen)
                    if now - self.clients[idle].seen < IDLE_SEAT_SECONDS:
                        raise ArenaFull(f"the arena is full: {self.max_phones} phones are playing")
                    del self.clients[idle]         # a closed tab gives its seat to a newcomer
                    self.seats_reclaimed += 1
                c = self.clients[client_id] = _Client(name, now)
            c.nickname = name
            c.seen = now
            return {"nickname": name, "state": self.state}

    def tap(self, client_id: str, nickname: Any = None) -> Dict[str, Any]:
        if not CLIENT_ID.match(str(client_id or "")):
            raise ValueError("bad client id")
        with self._mu:
            known = client_id in self.clients
        if not known:
            self.join(client_id, nickname)
        with self._mu:
            c = self.clients[client_id]
            c.taps += 1
            c.seen = time.time()
            name = c.nickname
        accepted, reason = self._enqueue(client_id, name, "phone")
        with self._mu:
            return {"accepted": accepted, "reason": reason, "your_crashes": c.crashes,
                    "your_taps": c.taps, "state": self.state}

    def me(self, client_id: str) -> Dict[str, Any]:
        with self._mu:
            c = self.clients.get(client_id)
            if c is not None:
                c.seen = time.time()
            return {"state": self.state, "known": c is not None,
                    "nickname": c.nickname if c else None, "your_crashes": c.crashes if c else 0,
                    "your_taps": c.taps if c else 0, "last": c.last if c else None,
                    "phones": len(self.clients)}

    def control(self, action: str) -> Dict[str, Any]:
        with self._mu:
            if action == "start":
                if self.state != LIVE:
                    self.state = LIVE
                    self.live_since = self.live_since or time.time()
                    self.frozen_at = None
            elif action == "freeze":
                if self.state == LIVE:
                    self.state = FROZEN
                    self.frozen_at = time.time()
                    self.discarded += self._queue.clear()
            elif action == "reset":
                self.epoch += 1
                self._queue.clear()
                self.state = LOBBY
                self.autotap = False
                self._reset_stats_locked()
            elif action in ("autotap_on", "autotap_off"):
                self.autotap = action == "autotap_on"
            else:
                raise ValueError(f"unknown action {action!r}")
        return self.snapshot()

    def meta(self) -> Dict[str, Any]:
        titles = {s: spec_info(self.specs[s])["title"] for s in SIDES}
        return {"scenario": self.scenario.meta(), "specs": dict(self.specs), "titles": titles,
                "public_url": self.public_url, "pool": self.pool, "lanes": self.lanes,
                "queue_max": self.queue_max, "per_phone": self.per_phone, "rate_limit": self.rate_limit,
                "max_phones": self.max_phones,
                "autotap_rate": self.autotap_rate,
                "delay_max_ms": round((self.delay_max or 0.0) * 1000.0, 3),
                "per_step_ms": {s: round(v * 1000.0, 3) for s, v in self.per_step.items()},
                "platform": sys.platform,
                "kill": "SIGKILL" if hasattr(signal, "SIGKILL") else "TerminateProcess"}

    def snapshot(self) -> Dict[str, Any]:
        now = time.time()
        with self._mu:
            while self._rate_window and self._rate_window[0] < now - 5.0:
                self._rate_window.popleft()
            span = min(5.0, max(0.5, now - (self.live_since or now)))
            lat = list(self._latency)
            top = heapq.nlargest(5, (c for c in self.clients.values() if c.crashes),
                                 key=lambda c: (c.crashes, -c.joined))
            active = sum(1 for c in self.clients.values() if now - c.seen < 15.0)
            participants = sum(1 for c in self.clients.values() if c.crashes)
            recent = [c.nickname for c in heapq.nlargest(14, self.clients.values(), key=lambda c: c.joined)]
            accepted = sum(t["accepted"] for t in self.taps.values())
            return {
                "state": self.state, "epoch": self.epoch, "autotap": self.autotap, "t": round(now, 3),
                "phones": {"joined": len(self.clients), "max": self.max_phones, "active": active,
                           "participants": participants, "full": len(self.clients) >= self.max_phones,
                           "recent": recent},
                "taps": {k: dict(v) for k, v in self.taps.items()},
                "accepted": accepted,
                "queue": {"pending": len(self._queue), "max": self.queue_max, "in_flight": self.in_flight},
                "processed": self.processed, "failed": self.failed, "discarded": self.discarded,
                "stale": self.stale,
                "rate": round(len(self._rate_window) / span, 2) if self._rate_window else 0.0,
                "latency_ms": {"p50": _percentile(lat, 50), "p95": _percentile(lat, 95), "n": len(lat)},
                "sides": {s: {"spec": self.specs[s], "crashes": v["crashes"], "corrupted": v["corrupted"],
                              "verdicts": dict(v["verdicts"]), "ready": self._ready[s].qsize(),
                              "pool": self.pool} for s, v in self.sides.items()},
                "gallery": {s: list(g)[:6] for s, g in self.gallery.items()},
                "leaderboard": [{"nickname": c.nickname, "crashes": c.crashes} for c in top],
                "pool": {"size": self.pool, "spawned": self.spawned, "errors": self.pool_errors,
                         "last_error": self.last_error},
                "live_seconds": round((self.frozen_at or now) - self.live_since, 1) if self.live_since else 0.0,
            }

    def feed_since(self, seq: int, limit: int = 60) -> Tuple[List[Dict[str, Any]], int]:
        """Crash pairs completed after tap-order position ``seq`` (by completion order)."""
        with self._mu:
            total = self.processed
            if seq > total:                   # a reset happened: start over
                seq = 0
            new = total - seq
            if new <= 0:
                return [], total
            items = list(self.feed)[-min(new, len(self.feed)):]
            return items[-limit:], total

    def export(self) -> Dict[str, Any]:
        with self._mu:
            pairs = list(self.records)
        return {"meta": self.meta(), "snapshot": self.snapshot(), "pairs": pairs}
