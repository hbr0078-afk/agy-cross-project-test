import os
import fcntl
import threading
from typing import Callable, Any

_THREAD_LOCKS: dict[str, threading.RLock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()

def get_shared_thread_lock(lock_path: str) -> threading.RLock:
    canonical = os.path.abspath(lock_path)
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(canonical)
        if lock is None:
            lock = threading.RLock()
            _THREAD_LOCKS[canonical] = lock
        return lock

class FileAndThreadLock:
    """
    Coordinated process-level (fcntl.flock) and thread-level (threading.RLock) re-entrant lock.
    Guarantees:
    1. Same process, same lock path -> serialized via shared threading.RLock.
    2. Across processes -> serialized via fcntl.flock(LOCK_EX).
    3. Re-entrancy supported on the same thread without deadlock.
    4. Strict lock acquisition order:
       RLock acquire -> flock acquire -> critical section -> flock release -> RLock release.
    """
    def __init__(self, lock_path: str):
        self.lock_path = os.path.abspath(lock_path)
        self.rlock = get_shared_thread_lock(self.lock_path)
        self._local = threading.local()

    def __enter__(self):
        # 1. Acquire thread-level RLock first
        self.rlock.acquire()

        depth = getattr(self._local, "depth", 0)
        if depth == 0:
            lock_dir = os.path.dirname(self.lock_path)
            if lock_dir and not os.path.exists(lock_dir):
                os.makedirs(lock_dir, mode=0o700, exist_ok=True)
            fd = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            self._local.fd = fd
            self._local.depth = 1
        else:
            self._local.depth = depth + 1
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            depth = getattr(self._local, "depth", 1)
            if depth <= 1:
                fd = getattr(self._local, "fd", None)
                if fd is not None:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    except OSError:
                        pass
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    self._local.fd = None
                self._local.depth = 0
            else:
                self._local.depth = depth - 1
        finally:
            # Always release thread-level RLock last
            self.rlock.release()

    def run(self, func: Callable[[], Any]) -> Any:
        with self:
            return func()
