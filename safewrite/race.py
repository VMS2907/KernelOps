"""The lost-update race, and its fix.

N threads each increment a counter document M times.

* **Unsynchronized**: ``v = get(); put(v + 1)``.  Two threads read the same
  value, both write value+1, and one increment is lost.  Every individual put
  is still atomic and durable; atomicity of a *single write* does not make a
  *read-modify-write sequence* safe.
* **Synchronized**: ``store.update(key, fn)`` holds the transaction lock from
  the read to the commit (mutual exclusion around the critical section).

The final counter must equal N x M.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
from typing import Dict, Optional

from .store import SafeStore

KEY = "counter"


def _inc(v: Optional[bytes]) -> bytes:
    return str(int(v or b"0") + 1).encode()


def run_race(engine: str = "cow", threads: int = 8, increments: int = 50,
             directory: Optional[str] = None) -> Dict[str, int]:
    base = directory or tempfile.mkdtemp(prefix="safewrite-race-")
    out: Dict[str, int] = {"expected": threads * increments}
    try:
        for mode in ("unsynchronized", "synchronized"):
            path = os.path.join(base, f"{engine.replace('/', '_')}-{mode}")
            shutil.rmtree(path, ignore_errors=True)
            with SafeStore.create(path, engine=engine) as store:
                store.put(KEY, b"0")
                barrier = threading.Barrier(threads)

                def worker() -> None:
                    barrier.wait()
                    for _ in range(increments):
                        if mode == "unsynchronized":
                            store.put(KEY, _inc(store.get(KEY)))      # race window
                        else:
                            store.update(KEY, _inc)                     # critical section

                ts = [threading.Thread(target=worker) for _ in range(threads)]
                for t in ts:
                    t.start()
                for t in ts:
                    t.join()
                out[mode] = int(store.get(KEY) or b"0")
    finally:
        if directory is None:
            shutil.rmtree(base, ignore_errors=True)
    return out
