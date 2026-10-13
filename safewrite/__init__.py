"""SafeWrite: crash-consistent document storage, proven rather than promised.

Quick start::

    from safewrite import SafeStore
    with SafeStore.open("mystore", engine="cow", create=True) as store:
        store.put("inventory.csv", b"item,qty\\nwater,120\\n")
        with store.transaction() as tx:
            tx.put("alice", b"70")
            tx.put("bob", b"80")
"""

__version__ = "1.0.0"

from .errors import CommitAborted, PayloadTooLarge, SaveError, VisibleNotDurable, WriteConflict  # noqa: E402
from .format import IntegrityError  # noqa: E402
from .store import SafeStore, StoreError  # noqa: E402

__all__ = ["SafeStore", "StoreError", "IntegrityError", "SaveError", "CommitAborted", "PayloadTooLarge",
           "WriteConflict", "VisibleNotDurable", "__version__"]
