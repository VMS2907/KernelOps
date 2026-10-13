"""The error contract of a save.

A failed save must tell the caller *which side of the commit point* it failed
on, because the correct reaction is opposite on the two sides:

=====================  ========================  ==============================================
exception              the document now reads as  what the caller should do
=====================  ========================  ==============================================
``CommitAborted``      the previous version       nothing changed and the store stays usable:
                                                  fix the cause (free space, shrink the
                                                  document, re-read after a conflict), retry
``VisibleNotDurable``  the NEW version            do not report "not saved" and do not retry
                                                  ``fsync``: reopen the store and save again to
                                                  get a durable acknowledgement
=====================  ========================  ==============================================

Both are :class:`StoreError` *and* :class:`OSError` (with the original
``errno``), so existing ``except OSError`` / ``except StoreError`` handlers keep
working while new code can branch on ``committed``.
"""

from __future__ import annotations

from typing import Any, ClassVar, List, Optional


class StoreError(Exception):
    """A store-level problem (not a SafeWrite store, locked, refused write...)."""


class SaveError(StoreError, OSError):
    """A save failed.  ``committed`` says whether its commit point was passed."""

    #: Whether the commit point (shadow: the rename) was passed.
    committed: ClassVar[bool] = False
    #: What readers see now: ``"previous"`` or ``"new"``.
    visible: ClassVar[str] = "previous"

    def __init__(self, message: str, *, key: str = "", phase: str = "",
                 errno: Optional[int] = None, **detail: Any) -> None:
        if errno is None:
            super().__init__(message)
        else:
            super().__init__(errno, message)
        self.message = message
        self.key = key
        #: Protocol phase that failed, e.g. ``"write-temp"`` or ``"fsync-dir"``.
        self.phase = phase
        self.detail = detail
        #: For a multi-document commit: documents committed before this one failed.
        self.already_committed: List[str] = []

    def __str__(self) -> str:
        return self.message


class CommitAborted(SaveError):
    """Pre-commit abort: the destination was never touched; the old version is
    still the committed one, temp space was released, the store stays usable."""


class PayloadTooLarge(CommitAborted):
    """The document exceeds the store's declared payload cap (nothing written)."""


class WriteConflict(CommitAborted):
    """A conditional save lost: the document changed after the caller read it."""


class VisibleNotDurable(SaveError):
    """Post-commit failure: the new version is already visible to readers, but
    its durability could not be confirmed (e.g. ``fsync(dir)`` returned EIO).
    After a power loss either version may come back, both intact."""

    committed = True
    visible = "new"


__all__ = ["StoreError", "SaveError", "CommitAborted", "PayloadTooLarge", "WriteConflict",
           "VisibleNotDurable"]
