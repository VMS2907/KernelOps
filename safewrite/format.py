"""On-disk encodings shared by the engines.

Integrity has two layers:

* **CRC-32** frames every header, record and block run.  It is cheap and is
  exactly what a torn or half-written record fails.  ext4's jbd2 journal and
  most databases use the same idea for the same reason.
* **SHA-256** fingerprints whole documents for display and for comparing
  versions byte-for-byte in tests and in the UI.

A checksum can prove that bytes are *intact*.  It can never prove that they
were *committed*.  Commit is decided by each engine's commit point, and the
crash explorer contains counter-examples for engines that confuse the two.
"""

from __future__ import annotations

import hashlib
import struct
import zlib
from typing import Optional, Tuple


class IntegrityError(Exception):
    """Stored bytes failed validation (torn, truncated, or bit-flipped)."""


def crc32(*parts: bytes) -> int:
    c = 0
    for p in parts:
        c = zlib.crc32(p, c)
    return c & 0xFFFFFFFF


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def pad(data: bytes, size: int) -> bytes:
    if len(data) > size:
        raise ValueError(f"{len(data)} bytes do not fit in {size}")
    return data + b"\0" * (size - len(data))


def blocks_for(n: int, block_size: int) -> int:
    return max(1, -(-n // block_size))


# ---------------------------------------------------------------------------
# Document file: header + payload.  Used by shadow, undo and wal home files.
#
#   offset  size  field
#   0       8     magic  b"SWDOC\x00v1"
#   8       8     payload length (big-endian u64)
#   16      4     CRC-32 of payload
#   20      4     CRC-32 of bytes 0..19 (header self-check)
#   24      ...   payload
# ---------------------------------------------------------------------------
DOC_MAGIC = b"SWDOC\x00v1"
_DOC = struct.Struct(">8sQII")
DOC_HEADER = _DOC.size  # 24


def encode_doc(payload: bytes) -> bytes:
    head = struct.pack(">8sQI", DOC_MAGIC, len(payload), crc32(payload))
    return head + struct.pack(">I", crc32(head)) + payload


def decode_doc(raw: Optional[bytes]) -> bytes:
    """Validate a document file and return its payload, or raise IntegrityError."""
    if raw is None:
        raise IntegrityError("file is missing")
    if len(raw) < DOC_HEADER:
        raise IntegrityError(f"file is {len(raw)} bytes, shorter than the {DOC_HEADER}-byte header")
    magic, length, body_crc, head_crc = _DOC.unpack_from(raw)
    if magic != DOC_MAGIC:
        raise IntegrityError("bad magic: not a SafeWrite document")
    if crc32(raw[:20]) != head_crc:
        raise IntegrityError("header checksum mismatch")
    payload = raw[DOC_HEADER:]
    if len(payload) != length:
        raise IntegrityError(f"length mismatch: header says {length}, file has {len(payload)}")
    if crc32(payload) != body_crc:
        raise IntegrityError("payload checksum mismatch")
    return payload


# ---------------------------------------------------------------------------
# Key <-> file name.  Lower-case letters, digits and ``._-`` are kept; every
# other byte (including upper-case, so names are safe on case-insensitive
# file systems) is percent-encoded.  ``~`` can therefore never appear in an
# encoded key, so engines use it to mark their own temporary files.
# ---------------------------------------------------------------------------
_SAFE = set(b"abcdefghijklmnopqrstuvwxyz0123456789._-")
MAX_KEY_BYTES = 200


def check_key(key: str) -> bytes:
    if not isinstance(key, str) or not key:
        raise ValueError("document key must be a non-empty string")
    raw = key.encode("utf-8")
    if len(raw) > MAX_KEY_BYTES:
        raise ValueError(f"document key longer than {MAX_KEY_BYTES} bytes")
    return raw


def key_to_name(key: str) -> str:
    raw = check_key(key)
    out = []
    for i, b in enumerate(raw):
        if b in _SAFE and not (i == 0 and b == ord(".")):
            out.append(chr(b))
        else:
            out.append("%%%02X" % b)
    return "".join(out)


def name_to_key(name: str) -> Optional[str]:
    """Inverse of :func:`key_to_name`; ``None`` for names that are not keys."""
    if "~" in name:
        return None
    try:
        out = bytearray()
        i = 0
        while i < len(name):
            if name[i] == "%":
                out.append(int(name[i + 1:i + 3], 16))
                i += 3
            else:
                out.append(ord(name[i]))
                i += 1
        return out.decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None


def split_u64(data: bytes, offset: int) -> Tuple[int, int]:
    return struct.unpack_from(">Q", data, offset)[0], offset + 8
