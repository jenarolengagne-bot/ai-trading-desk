"""Exclusive lock shared by threads and processes.

Windows byte-range locks (msvcrt.locking) start at the current file position
and must be unlocked at that same position and length. The e77cfbe lock wrote
a byte in append mode, so the lock sat at offset 1, then seek(0) + LK_UNLCK
tried to free offset 0 and raised PermissionError errno 13. Two processes
could also lock different offsets and fail to exclude each other.

This lock always covers the first byte, in binary mode, and closing the
handle is the release of last resort.
"""
from __future__ import annotations

import os
import time
import traceback
from pathlib import Path


class LockBusy(Exception):
    """Another holder has the region. Not a permissions failure."""


class FileLock:
    def __init__(self, path: Path | str, timeout: float):
        self.path = Path(path)
        self.timeout = timeout
        self._fh = None
        self._held = False

    def acquire(self):
        if self._held:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o666)
        self._fh = os.fdopen(fd, "r+b", buffering=0)
        try:
            self._ensure_region()
            deadline = time.monotonic() + self.timeout
            while True:
                try:
                    self._lock_region()
                    self._held = True
                    return
                except LockBusy:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"lock timeout: {self.path}")
                    time.sleep(0.01)
        except Exception:
            self._close_handle()
            raise

    def release(self):
        """Drop the region if this handle holds it, then close.

        Safe when acquire never succeeded: there is nothing to unlock.
        An unlock error does not skip close. Closing the handle releases
        any byte-range lock the handle still owns, on Windows and on POSIX.
        """
        fh = self._fh
        held = self._held
        self._fh = None
        self._held = False
        if fh is None:
            return
        try:
            if held:
                try:
                    fh.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
        finally:
            fh.close()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()
        return False

    def _ensure_region(self):
        self._fh.seek(0, os.SEEK_END)
        if self._fh.tell() == 0:
            self._fh.write(b"\0")
        self._fh.seek(0)

    def _lock_region(self):
        self._fh.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                # One byte at offset 0. LK_NBLCK does not move the pointer.
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                # 13 = EACCES (region busy), 36 = EDEADLK (lock timed out).
                if getattr(exc, "errno", None) in (13, 36):
                    raise LockBusy from exc
                raise
        else:
            import fcntl
            try:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise LockBusy from exc

    def _close_handle(self):
        fh = self._fh
        self._fh = None
        self._held = False
        if fh is not None:
            fh.close()


def child_main(command: str, arg1: str, arg2: str, arg3: str = "") -> None:
    """Entry point for a spawned interpreter. Strings only, no inherited lock."""
    err_path = Path(arg1).with_name(f"{Path(arg1).name}.{command}.{arg2}.err")
    try:
        _child(command, arg1, arg2, arg3)
    except Exception:
        err_path.write_text(traceback.format_exc(), encoding="utf-8")
        raise


def _child(command: str, arg1: str, arg2: str, arg3: str) -> None:
    if command == "buy":
        from datetime import datetime, timezone

        from desk.broker import PaperBroker
        now = datetime(2026, 10, 26, 12, 0, tzinfo=timezone.utc)
        broker = PaperBroker(Path(arg1))
        broker.buy(arg2, arg2, 20, 1.0, now=now, key=arg2)
        return
    if command == "hold":
        ready = Path(arg3)
        with FileLock(Path(arg1), timeout=5):
            ready.write_text("held", encoding="utf-8")
            time.sleep(float(arg2))
        return
    raise ValueError(command)
