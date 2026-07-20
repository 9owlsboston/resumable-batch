"""Cross-OS run()-scope exclusive lock (design §4.3).

A single OS-owned exclusive lock on a stable lockfile that lives OUTSIDE the cache
tree (so a ``fresh`` wipe never touches the lock inode). Acquire ONCE at run()
scope, thread the held lock into every store; stores never self-acquire. The lock
is an **OS-owned handle lock** on both platforms (POSIX ``flock`` / Windows
``msvcrt.locking``) that releases automatically on process death — so there is no
TTL takeover and thus no two-writer window.
"""

from __future__ import annotations

import logging
import os
import socket
import time
from pathlib import Path
from typing import Callable

from .errors import CacheLockedError

logger = logging.getLogger(__name__)

_IS_WINDOWS = os.name == "nt"


class LockProvider:
    """Base class: ``acquire()`` / ``release()`` / ``held`` with the
    :class:`CacheLockedError` contention contract. Also a context manager."""

    def __init__(self, lockfile: os.PathLike | str, *,
                 timeout: float = 30.0, poll: float = 0.5,
                 sleep: Callable[[float], None] = time.sleep,
                 monotonic: Callable[[], float] = time.monotonic):
        self.lockfile = Path(lockfile)
        self.timeout = timeout
        self.poll = poll
        self._sleep = sleep
        self._monotonic = monotonic
        self._fd: int | None = None
        self.held = False

    # -- subclass hooks -----------------------------------------------------

    def _try_lock(self, fd: int) -> bool:
        """Attempt a non-blocking exclusive lock on ``fd``. Return True if
        acquired, False if held by another process. Raise on hard errors."""
        raise NotImplementedError

    def _unlock(self, fd: int) -> None:
        raise NotImplementedError

    # -- shared machinery ---------------------------------------------------

    def _read_holder(self) -> str:
        try:
            return self.lockfile.read_text().strip() or "unknown"
        except OSError:
            return "unknown"

    def acquire(self) -> "LockProvider":
        self.lockfile.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.lockfile, os.O_RDWR | os.O_CREAT, 0o644)
        deadline = self._monotonic() + self.timeout
        while True:
            if self._try_lock(fd):
                break
            if self._monotonic() >= deadline:
                holder = self._read_holder()
                os.close(fd)
                raise CacheLockedError(
                    f"cache lock {self.lockfile} held by {holder}; "
                    f"gave up after {self.timeout}s")
            self._sleep(self.poll)
        # Acquired — record holder identity for diagnostics (best-effort).
        try:
            os.ftruncate(fd, 0)
            os.write(fd, f"{os.getpid()}@{socket.gethostname()}".encode())
            os.fsync(fd)
        except OSError:
            pass
        self._fd = fd
        self.held = True
        return self

    def release(self) -> None:
        if self._fd is not None:
            try:
                self._unlock(self._fd)
            finally:
                os.close(self._fd)
                self._fd = None
                self.held = False

    def __enter__(self) -> "LockProvider":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()


class PosixFlockLock(LockProvider):
    """``fcntl.flock(LOCK_EX)`` — releases on process death (POSIX)."""

    def _try_lock(self, fd: int) -> bool:
        import fcntl
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(self, fd: int) -> None:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)


class WindowsLock(LockProvider):
    """``msvcrt.locking(LK_NBLCK)`` on a byte of a held handle.

    An OS-owned handle lock that the kernel releases when the process dies — no
    lease/TTL takeover, so no two-writer window (design §4.3 / closes B5)."""

    _LOCK_BYTES = 1

    def _try_lock(self, fd: int) -> bool:
        import msvcrt
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, self._LOCK_BYTES)
            return True
        except OSError:
            return False

    def _unlock(self, fd: int) -> None:
        import msvcrt
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, self._LOCK_BYTES)
        except OSError:
            pass


def make_lock(lockfile: os.PathLike | str, **kwargs) -> LockProvider:
    """Return the OS-appropriate :class:`LockProvider` for this platform."""
    cls = WindowsLock if _IS_WINDOWS else PosixFlockLock
    return cls(lockfile, **kwargs)
