"""Ported from agy-router/file_lock.py (verbatim behavior).

Coordinated process-level (fcntl.flock) and thread-level (threading.RLock)
re-entrant lock. Used by the adapter's in-process key pool to protect key
state under concurrent requests.

Kept dependency-free on purpose: no agy-router imports.
"""

import os
import fcntl
import threading
from typing import Callable, Any


class _PathLockState:
    """Shared state per canonical lock path."""

    def __init__(self, canonical_path: str):
        self.canonical_path = canonical_path
        self.rlock = threading.RLock()
        self.local = threading.local()


_PATH_LOCK_STATES: dict = {}
_PATH_LOCK_GUARD = threading.Lock()


def get_path_lock_state(lock_path: str) -> _PathLockState:
    canonical = os.path.abspath(lock_path)
    with _PATH_LOCK_GUARD:
        state = _PATH_LOCK_STATES.get(canonical)
        if state is None:
            state = _PathLockState(canonical)
            _PATH_LOCK_STATES[canonical] = state
        return state


def get_shared_thread_lock(lock_path: str) -> threading.RLock:
    return get_path_lock_state(lock_path).rlock


class FileAndThreadLock:
    """Process + thread re-entrant lock (fcntl.flock + threading.RLock)."""

    def __init__(self, lock_path: str):
        self.lock_path = os.path.abspath(lock_path)
        self._state = get_path_lock_state(self.lock_path)

    @property
    def rlock(self) -> threading.RLock:
        return self._state.rlock

    def __enter__(self):
        self._state.rlock.acquire()

        depth = getattr(self._state.local, "depth", 0)
        if depth == 0:
            lock_dir = os.path.dirname(self.lock_path)
            if lock_dir and not os.path.exists(lock_dir):
                os.makedirs(lock_dir, mode=0o700, exist_ok=True)
            fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            self._state.local.fd = fd
            self._state.local.depth = 1
        else:
            self._state.local.depth = depth + 1
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            depth = getattr(self._state.local, "depth", 1)
            if depth <= 1:
                fd = getattr(self._state.local, "fd", None)
                if fd is not None:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    except OSError:
                        pass
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    self._state.local.fd = None
                self._state.local.depth = 0
            else:
                self._state.local.depth = depth - 1
        finally:
            self._state.rlock.release()

    def run(self, func: Callable[[], Any]) -> Any:
        with self:
            return func()
