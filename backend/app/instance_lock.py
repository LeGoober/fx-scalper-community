"""One trading process per data directory.

Two backends on the same database would each run the engine and place the same order twice.
The first process takes an exclusive OS lock on `<data_dir>/backend.lock` for its lifetime; the
OS drops it if the process dies, so a crash never leaves a stale lock. A second process still
serves the API but refuses to start demo/real trading.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import IO

_handle: IO | None = None


def acquire(path: Path) -> bool:
    global _handle
    if _handle is not None:
        return True
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+")
    try:
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False
    _handle = handle
    return True


def release() -> None:
    global _handle
    if _handle is None:
        return
    try:
        if os.name == "nt":
            import msvcrt
            _handle.seek(0)
            msvcrt.locking(_handle.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    _handle.close()
    _handle = None


def owned() -> bool:
    return _handle is not None
