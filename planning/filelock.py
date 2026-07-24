"""OS-level advisory file locking, shared by the plan store and the approval store.

Both stores are written by several server processes at once (restarts leave old MCP
server processes alive on the same state directory), so `threading.Lock` is not enough:
it only serializes threads inside one process.

Reads deliberately take no lock. Every write goes through a temp file plus an atomic
rename, so a reader either sees the whole previous version or the whole new one - never
a torn file. Locking is therefore only needed between writers.
"""

from __future__ import annotations

import contextlib
import logging
import os
import time
from pathlib import Path
from typing import Iterator

log = logging.getLogger("planning-mcp.filelock")

DEFAULT_TIMEOUT = 20.0

if os.name == "nt":
    import msvcrt

    def _acquire(fh) -> None:
        msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)

    def _release(fh) -> None:
        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _acquire(fh) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX)

    def _release(fh) -> None:
        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def exclusive(path: Path, timeout: float = DEFAULT_TIMEOUT) -> Iterator[bool]:
    """Hold an exclusive lock on `path`. Yields whether it was actually acquired.

    On timeout it yields False rather than raising: blocking the user's workflow is a
    worse outcome than a rare unserialized write, but the caller is expected to log it.
    """
    fh = None
    deadline = time.monotonic() + timeout
    while True:
        try:
            fh = open(path, "a+b")
        except OSError as exc:
            log.warning("Cannot open lock file %s (%s); proceeding unserialized", path, exc)
            yield False
            return
        try:
            _acquire(fh)
            break
        except OSError:
            fh.close()
            fh = None
            if time.monotonic() >= deadline:
                log.error(
                    "Could not take %s within %.0fs; proceeding UNSERIALIZED - another "
                    "planning-mcp instance may be using this state directory",
                    path.name,
                    timeout,
                )
                yield False
                return
            time.sleep(0.05)
    try:
        yield True
    finally:
        try:
            _release(fh)
        except OSError:
            pass
        finally:
            fh.close()
