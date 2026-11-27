"""Windows single-instance lock for the PAPER trading controller."""

import os
import msvcrt
from contextlib import contextmanager
from pathlib import Path


class AlreadyRunningError(RuntimeError):
    """Raised when another controller process owns the lock."""


@contextmanager
def single_instance_lock(lock_path: Path):
    """Hold an exclusive Windows byte-range lock until the context exits."""
    if os.name != "nt":
        raise RuntimeError("The PAPER controller process lock currently requires Windows.")

    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    handle = lock_path.open("a+b")
    locked = False

    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\\0")
            handle.flush()

        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            locked = True
        except OSError as exc:
            raise AlreadyRunningError(
                "Another PAPER trading controller is already running. "
                "Only one controller instance is allowed."
            ) from exc

        yield

    finally:
        if locked:
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            finally:
                handle.close()
        else:
            handle.close()
