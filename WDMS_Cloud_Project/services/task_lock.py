"""OS-backed process lock shared by all modes and output locations."""

from __future__ import annotations

import os
from pathlib import Path

from errors import TaskAlreadyRunning


class TaskLock:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._file = None
        self._locked = False

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._file = self.path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                self._file.seek(0, os.SEEK_END)
                if self._file.tell() == 0:
                    self._file.write(b"\0")
                    self._file.flush()
                self._file.seek(0)
                msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, BlockingIOError) as error:
            self._file.close()
            self._file = None
            raise TaskAlreadyRunning() from error
        self._locked = True

    def release(self) -> None:
        if not self._file:
            return
        try:
            if self._locked:
                if os.name == "nt":
                    import msvcrt

                    self._file.seek(0)
                    msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
        finally:
            self._locked = False
            self._file.close()
            self._file = None

    def __enter__(self) -> "TaskLock":
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()

    @staticmethod
    def is_locked(path: Path) -> bool:
        probe = TaskLock(path)
        try:
            probe.acquire()
        except TaskAlreadyRunning:
            return True
        else:
            probe.release()
            return False
