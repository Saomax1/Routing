"""Password hashing (stdlib scrypt), roles, and a tiny login rate limiter."""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import threading
import time
from collections import defaultdict, deque

ROLES = ("admin", "dispatcher")
MIN_PASSWORD_LENGTH = 10

_N, _R, _P = 2 ** 14, 8, 1


def hash_password(password: str) -> str:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters")
    salt = os.urandom(16)
    dk = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return "scrypt${}${}${}${}${}".format(
        _N, _R, _P, base64.b64encode(salt).decode(), base64.b64encode(dk).decode())


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt, expected = base64.b64decode(salt_b64), base64.b64decode(hash_b64)
        dk = hashlib.scrypt(password.encode(), salt=salt, n=int(n), r=int(r), p=int(p), dklen=len(expected))
        return hmac.compare_digest(dk, expected)
    except Exception:
        return False


# A hash to burn CPU on when the email is unknown (keeps timing similar for known/unknown users).
_DUMMY_HASH = hash_password("dummy-password-for-timing")


def burn_verify(password: str) -> None:
    verify_password(password, _DUMMY_HASH)


class LoginLimiter:
    """Allow at most ``max_attempts`` failed logins per key within ``window`` seconds."""

    MAX_KEYS = 10_000

    def __init__(self, max_attempts: int = 5, window: int = 600):
        self.max_attempts, self.window = max_attempts, window
        self._fails = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> deque:
        """The recent failures for ``key``. A key with none left is forgotten, and merely asking about a key never
        creates an entry, so a flood of different emails or addresses cannot grow this table."""
        q = self._fails.get(key)
        if q is None:
            return deque()
        while q and now - q[0] > self.window:
            q.popleft()
        if not q:
            del self._fails[key]
        return q

    def blocked(self, key: str) -> bool:
        with self._lock:
            return len(self._prune(key, time.time())) >= self.max_attempts

    def record_failure(self, key: str) -> None:
        with self._lock:
            now = time.time()
            if len(self._fails) >= self.MAX_KEYS:        # forget every key whose failures have all expired
                for k in [k for k, q in self._fails.items() if not q or now - q[-1] > self.window]:
                    del self._fails[k]
            self._prune(key, now)
            self._fails[key].append(now)

    def reset(self, key: str) -> None:
        with self._lock:
            self._fails.pop(key, None)
