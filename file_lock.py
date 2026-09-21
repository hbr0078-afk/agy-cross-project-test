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

_PATH_LOCK_STATES: dict[str, _PathLockState] = {}
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
    """
    Coordinated process-level (fcntl.flock) and thread-level (threading.RLock) re-entrant lock.
    Guarantees:
    1. Same process, same lock path -> serialized via shared threading.RLock per path.
    2. Across processes -> serialized via fcntl.flock(LOCK_EX).
    3. Re-entrancy supported on the same thread even across different FileAndThreadLock
       wrapper instances targeting the same file!
    4. Strict lock acquisition order:
       RLock acquire -> flock acquire -> critical section -> flock release -> RLock release.
    """
    def __init__(self, lock_path: str):
        self.lock_path = os.path.abspath(lock_path)
        self._state = get_path_lock_state(self.lock_path)

    @property
    def rlock(self) -> threading.RLock:
        return self._state.rlock

    def __enter__(self):
        # 1. Acquire thread-level RLock first
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
            # Always release thread-level RLock last
            self._state.rlock.release()

    def run(self, func: Callable[[], Any]) -> Any:
        with self:
            return func()
