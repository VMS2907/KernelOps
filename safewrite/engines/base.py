"""The contract every storage engine implements.

An engine owns a directory (through a :class:`~safewrite.vfs.base.VFS`) and
offers a tiny document-store API::

    engine.commit({"alice": b"70", "bob": b"80"})   # atomic (if atomic_tx)
    engine.get("alice")                             # -> b"70"
    engine.commit({"bob": None})                    # delete

Two promises define "crash consistent":

* **Atomicity**: after any crash, every document reads back as the version
  before a commit or the version after it, never a mixture.  For engines
  with ``atomic_tx = True`` this holds for *all documents of a commit
  together*.
* **Durability**: once ``commit()`` has returned, the new version survives
  any crash.

``open()`` is the recovery procedure.  It runs every time a store is opened,
because after a crash you cannot know whether the last shutdown was clean.
It must be idempotent and must itself survive crashes.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass, field
from typing import Any, ClassVar, Dict, List, Optional

from ..format import IntegrityError, check_key, sha256
from ..vfs.base import VFS


@dataclass
class Event:
    """One line of an engine's narrated protocol or recovery log."""
    kind: str      # info | ok | discard | replay | rollback | select | warn | error
    msg: str

    def to_json(self) -> Dict[str, str]:
        return {"kind": self.kind, "msg": self.msg}


@dataclass
class RecoveryReport:
    engine: str
    events: List[Event] = field(default_factory=list)
    seconds: float = 0.0
    replayed: int = 0
    rolled_back: int = 0
    discarded: int = 0

    def to_json(self) -> Dict[str, Any]:
        d = asdict(self)
        d["events"] = [e.to_json() for e in self.events]
        return d

    def summary(self) -> str:
        bits = []
        if self.replayed:
            bits.append(f"replayed {self.replayed} committed change(s)")
        if self.rolled_back:
            bits.append(f"rolled back {self.rolled_back} uncommitted change(s)")
        if self.discarded:
            bits.append(f"discarded {self.discarded} uncommitted item(s)")
        return "; ".join(bits) or "clean: nothing to repair"


@dataclass
class Version:
    key: str
    version: int
    length: int
    sha256: str
    timestamp: float
    deleted: bool = False

    def to_json(self) -> Dict[str, Any]:
        return asdict(self)


class Engine(ABC):
    #: Registry name, e.g. ``"cow"``.
    name: ClassVar[str] = "base"
    title: ClassVar[str] = ""
    technique: ClassVar[str] = ""
    #: The single event that makes a commit take effect.
    commit_point: ClassVar[str] = ""
    #: What recovery returns for an interrupted commit.
    recovers_to: ClassVar[str] = ""
    #: Whether a multi-document commit is all-or-nothing.
    atomic_tx: ClassVar[bool] = True
    #: Whether the engine is crash-consistent at all (False only for naive).
    safe: ClassVar[bool] = True
    #: Default option values.  Ablations flip one of them off.
    OPTIONS: ClassVar[Dict[str, Any]] = {}
    #: Named ablations: variant name -> (option overrides, what breaks).
    VARIANTS: ClassVar[Dict[str, Dict[str, Any]]] = {}
    #: Documented crash points: name -> where to crash (see ``ShadowEngine``).
    CRASH_POINTS: ClassVar[Dict[str, Dict[str, Any]]] = {}

    def __init__(self, vfs: VFS, **options: Any) -> None:
        unknown = set(options) - set(self.OPTIONS)
        if unknown:
            raise TypeError(f"{self.name}: unknown option(s) {sorted(unknown)}")
        self.vfs = vfs
        self.opt: Dict[str, Any] = {**self.OPTIONS, **options}
        self.events: List[Event] = []
        self.opened = False

    # ------------------------------------------------------------ lifecycle
    @classmethod
    def create(cls, vfs: VFS, **options: Any) -> "Engine":
        """Lay out an empty store durably, then open it."""
        eng = cls(vfs, **options)
        eng.format()
        eng.open()
        return eng

    @abstractmethod
    def format(self) -> None:
        """Write the initial on-disk layout and make it durable."""

    def open(self) -> RecoveryReport:
        """Run crash recovery and load in-memory state."""
        self.events = []
        report = RecoveryReport(self.name)
        t0 = time.perf_counter()
        self.recover(report)
        report.seconds = time.perf_counter() - t0
        report.events = list(self.events)
        self.opened = True
        return report

    @abstractmethod
    def recover(self, report: RecoveryReport) -> None: ...

    def close(self) -> None:
        self.opened = False

    # -------------------------------------------------------------- reading
    @abstractmethod
    def get(self, key: str) -> Optional[bytes]:
        """Current committed value, ``None`` if absent.  Raises
        :class:`IntegrityError` if the stored bytes are damaged."""

    @abstractmethod
    def keys(self) -> List[str]: ...

    def history(self, key: str) -> List[Version]:
        """Older versions of ``key``.  Only the COW engine keeps any."""
        value = self.get(key)
        if value is None:
            return []
        return [Version(key, 0, len(value), sha256(value), 0.0)]

    def verify(self) -> List[str]:
        """Scrub every document; return a list of problems (empty = healthy)."""
        problems = []
        for key in self.keys():
            try:
                self.get(key)
            except IntegrityError as e:
                problems.append(f"{key}: {e}")
        return problems

    # -------------------------------------------------------------- writing
    @abstractmethod
    def commit(self, changes: Dict[str, Optional[bytes]]) -> None:
        """Apply ``changes`` (``None`` = delete).  Durable when it returns."""

    def put(self, key: str, value: bytes) -> None:
        self.commit({key: value})

    def delete(self, key: str) -> None:
        self.commit({key: None})

    # ------------------------------------------------------------- helpers
    def log(self, kind: str, msg: str) -> None:
        self.events.append(Event(kind, msg))

    @staticmethod
    def _check(changes: Dict[str, Optional[bytes]]) -> Dict[str, Optional[bytes]]:
        out: Dict[str, Optional[bytes]] = {}
        for k, v in changes.items():
            check_key(k)
            if v is not None and not isinstance(v, (bytes, bytearray, memoryview)):
                raise TypeError(f"value for {k!r} must be bytes")
            out[k] = None if v is None else bytes(v)
        return out

    def layout(self) -> Dict[str, Any]:
        """Describe on-disk regions for the UI's disk map (engine-specific)."""
        return {}

    @classmethod
    def describe(cls) -> Dict[str, Any]:
        return {
            "name": cls.name, "title": cls.title, "technique": cls.technique,
            "commit_point": cls.commit_point, "recovers_to": cls.recovers_to,
            "atomic_tx": cls.atomic_tx, "safe": cls.safe,
            "options": dict(cls.OPTIONS),
            "variants": {k: {kk: vv for kk, vv in v.items()} for k, v in cls.VARIANTS.items()},
            "crash_points": {k: v["means"] for k, v in cls.CRASH_POINTS.items()},
        }
