"""Engine registry.

An engine *spec* is ``"name"`` or ``"name/variant"``, e.g. ``"shadow/no-fsync"``.
Variants are ablations: the same engine with exactly one safeguard switched
off, so the crash explorer can show what that safeguard protects against.
"""

from __future__ import annotations

from typing import Any, Dict, List, Tuple, Type

from ..vfs.base import VFS
from .base import Engine, Event, RecoveryReport, Version
from .cow import CowEngine
from .naive import NaiveEngine
from .shadow import ShadowEngine
from .undo import UndoEngine
from .wal import WalEngine

ENGINES: Dict[str, Type[Engine]] = {
    cls.name: cls for cls in (NaiveEngine, ShadowEngine, UndoEngine, WalEngine, CowEngine)
}

#: The engines that are supposed to be crash-consistent.
SAFE = ["shadow", "undo", "wal", "cow"]


def parse_spec(spec: str) -> Tuple[Type[Engine], Dict[str, Any], str]:
    """``"wal/no-checksum"`` -> (WalEngine, {"checksums": False}, "no-checksum")."""
    name, _, variant = spec.partition("/")
    if name not in ENGINES:
        raise ValueError(f"unknown engine {name!r}; choose from {', '.join(ENGINES)}")
    cls = ENGINES[name]
    if not variant:
        return cls, {}, ""
    if variant not in cls.VARIANTS:
        raise ValueError(f"engine {name!r} has no variant {variant!r}; "
                         f"choose from {', '.join(cls.VARIANTS) or 'none'}")
    return cls, dict(cls.VARIANTS[variant]["options"]), variant


def make(spec: str, vfs: VFS, **options: Any) -> Engine:
    cls, opts, _ = parse_spec(spec)
    return cls(vfs, **{**opts, **options})


def all_specs(include_variants: bool = True) -> List[str]:
    out = []
    for name, cls in ENGINES.items():
        out.append(name)
        if include_variants:
            out.extend(f"{name}/{v}" for v in cls.VARIANTS)
    return out


def spec_info(spec: str) -> Dict[str, Any]:
    cls, opts, variant = parse_spec(spec)
    info = cls.describe()
    info["spec"] = spec
    info["variant"] = variant
    info["ablation"] = bool(variant)
    info["expected_safe"] = cls.safe and not variant
    info["breaks"] = cls.VARIANTS[variant]["breaks"] if variant else ""
    return info


__all__ = ["Engine", "Event", "RecoveryReport", "Version", "ENGINES", "SAFE",
           "parse_spec", "make", "all_specs", "spec_info",
           "NaiveEngine", "ShadowEngine", "UndoEngine", "WalEngine", "CowEngine"]
