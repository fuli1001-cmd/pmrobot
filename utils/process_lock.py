"""Cross-platform advisory lock used to prevent duplicate bot instances."""

import os
from pathlib import Path
from typing import IO, Optional


class AlreadyRunningError(RuntimeError):
    """Raised when another process already owns the instance lock."""


class SingleInstanceLock:
    """Hold an OS-backed lock for the lifetime of one pmrobot process."""

    def __init__(self, path: str):
        self.path = Path(path)
        self._handle: Optional[IO[str]] = None

    def acquire(self) -> None:
        if self._handle is not None:
            return

        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="ascii")
        try:
            self._lock(handle)
        except (BlockingIOError, OSError) as exc:
            handle.seek(0)
            owner = handle.read().strip() or "unknown"
            handle.close()
            raise AlreadyRunningError(
                f"lock={self.path} owner_pid={owner}"
            ) from exc

        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return

        self._handle = None
        try:
            self._unlock(handle)
        finally:
            handle.close()

    @staticmethod
    def _lock(handle: IO[str]) -> None:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            if not handle.read(1):
                handle.seek(0)
                handle.write("0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return

        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock(handle: IO[str]) -> None:
        if os.name == "nt":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            return

        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def __enter__(self) -> "SingleInstanceLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()
