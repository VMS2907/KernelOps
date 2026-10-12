"""Workloads the crash explorer runs against every engine.

A scenario is a starting state plus a list of commits.  That gives a chain of
states ``S0 -> S1 -> ... -> Sn``.  For a crash at any point, the only
acceptable recovered states are ``S_a ... S_b``, where ``a`` = commits
acknowledged before the crash (durability) and ``b`` = commits started
(you cannot recover something that was never written).  Anything else is a
violation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

State = Dict[str, Optional[bytes]]


def inventory(version: str, rows: int, seed: int) -> bytes:
    """A realistic CSV document.  Versions differ on every line, so a mixture
    of two versions is visible byte-for-byte."""
    items = ["water bottles", "food packets", "medical kits", "blankets", "torches",
             "batteries", "tents", "radios", "fuel cans", "rope coils", "masks", "gloves"]
    out = [f"# inventory {version}\nitem,quantity,location\n"]
    for i in range(rows):
        qty = (seed * 37 + i * 11) % 400 + 5
        out.append(f"{items[i % len(items)]} #{i:02d},{qty},shelf-{(i * seed) % 9 + 1}\n")
    return "".join(out).encode()


DOC_A = inventory("A (previous valid version)", 60, 3)      # ~2.0 KB
DOC_B = inventory("B (new version)", 44, 7)                  # ~1.5 KB, shorter than A on purpose


@dataclass
class Scenario:
    name: str
    title: str
    story: str
    initial: State
    steps: List[State]
    #: Per-engine option overrides that make the scenario exercise more code
    #: (e.g. checkpoint after every WAL commit so in-place home writes happen).
    engine_options: Dict[str, Dict[str, Any]] = field(default_factory=dict)

    @property
    def keys(self) -> List[str]:
        ks = set(self.initial)
        for s in self.steps:
            ks |= set(s)
        return sorted(ks)

    def states(self) -> List[State]:
        """``[S0, S1, ...]`` with every scenario key present (None = absent)."""
        cur: State = {k: self.initial.get(k) for k in self.keys}
        out = [dict(cur)]
        for step in self.steps:
            cur.update(step)
            out.append(dict(cur))
        return out

    def options_for(self, engine_name: str) -> Dict[str, Any]:
        return dict(self.engine_options.get(engine_name, {}))

    def to_json(self) -> Dict[str, Any]:
        return {"name": self.name, "title": self.title, "story": self.story,
                "keys": self.keys, "steps": len(self.steps)}


_COMMON = {"wal": {"checkpoint_every": 1}, "cow": {"block_size": 512},
           "naive": {"chunk": 512}}

SCENARIOS: Dict[str, Scenario] = {s.name: s for s in [
    Scenario(
        "update", "Overwrite a document",
        "inventory.csv holds version A. The user saves version B (shorter, every line "
        "changed) and the machine loses power somewhere during the save.",
        {"inventory.csv": DOC_A}, [{"inventory.csv": DOC_B}], _COMMON),
    Scenario(
        "transfer", "Multi-document transaction",
        "Move 30 units from alice to bob: both documents must change together. "
        "Total must stay 150: a crash may not create or destroy money.",
        {"alice": b"balance=100\n", "bob": b"balance=50\n"},
        [{"alice": b"balance=70\n", "bob": b"balance=80\n"}], _COMMON),
    Scenario(
        "create", "First-ever save",
        "report.md does not exist yet. The first save is interrupted. Valid outcomes: "
        "the document does not exist, or it is complete.",
        {}, [{"report.md": inventory("first draft", 30, 5)}], _COMMON),
    Scenario(
        "delete", "Delete a document",
        "inventory.csv is deleted. It must be either fully there or fully gone.",
        {"inventory.csv": DOC_A}, [{"inventory.csv": None}], _COMMON),
    Scenario(
        "sequence", "Three saves in a row",
        "Versions v1 -> v2 -> v3 are saved back to back. After a crash the document "
        "must be a version at least as new as the last acknowledged save.",
        {"notes.txt": b"v1: draft\n" * 20},
        [{"notes.txt": b"v2: reviewed\n" * 24}, {"notes.txt": b"v3: final\n" * 30}],
        # WAL checkpoints every 2nd commit here, so commit 1 is acknowledged
        # while it exists only in the log: this exercises the pure-log path.
        {**_COMMON, "wal": {"checkpoint_every": 2}}),
    Scenario(
        "large", "Large document (many blocks)",
        "A 20 KB document becomes 28 KB: dozens of sectors, multi-block extents, "
        "multi-chunk writes that can be torn anywhere.",
        {"report.csv": inventory("large A", 600, 11)},
        [{"report.csv": inventory("large B", 820, 13)}],
        {"wal": {"checkpoint_every": 1}, "cow": {"block_size": 4096}, "naive": {"chunk": 4096}}),
]}

DEFAULT_SCENARIOS = ["update", "transfer", "create", "delete", "sequence", "large"]
