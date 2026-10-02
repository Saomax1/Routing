"""
Tiny JSON-over-HTTPS transport (stdlib only) with retry + exponential backoff.

* Retries 429 and 5xx (and network errors) honouring ``Retry-After``; gives up after ``max_attempts``.
* Error messages contain only the HTTP status and the URL *path* - never headers, query strings
  or bodies - so API keys and customer data cannot leak into logs. When the path itself carries data
  (a Mapbox geocoding address, routing coordinates) pass ``label`` and that is logged instead.
"""

from __future__ import annotations

import json
import logging
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional

log = logging.getLogger("routing.http")


class HttpError(Exception):
    def __init__(self, status: int, path: str, message: str = ""):
        self.status, self.path = status, path
        super().__init__(f"HTTP {status} for {path}" + (f": {message}" if message else ""))


class UrllibTransport:
    def __init__(self, timeout: float = 30.0, max_attempts: int = 5, base_delay: float = 1.0,
                 sleep: Callable[[float], None] = time.sleep):
        self.timeout, self.max_attempts, self.base_delay, self._sleep = timeout, max_attempts, base_delay, sleep

    def request(self, method: str, url: str, headers: Optional[dict] = None, params: Optional[list] = None,
                json_body: Any = None, label: Optional[str] = None) -> Any:
        """``params`` is a list of (key, value) tuples so repeated keys (work_status[]) work."""
        full = url
        if params:
            full += ("&" if "?" in url else "?") + urllib.parse.urlencode(params, doseq=True)
        path = label or urllib.parse.urlparse(url).path
        data = json.dumps(json_body).encode() if json_body is not None else None
        hdrs = {"Accept": "application/json", **(headers or {})}
        if data is not None:
            hdrs.setdefault("Content-Type", "application/json")

        last: Optional[Exception] = None
        for attempt in range(1, self.max_attempts + 1):
            req = urllib.request.Request(full, data=data, headers=hdrs, method=method)
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    body = resp.read()
                    return json.loads(body) if body else None
            except urllib.error.HTTPError as e:
                if e.code in (429, 500, 502, 503, 504) and attempt < self.max_attempts:
                    delay = self._retry_after(e) or self.base_delay * (2 ** (attempt - 1))
                    log.warning("HTTP %s from %s; retry %d/%d in %.1fs", e.code, path, attempt, self.max_attempts - 1, delay)
                    self._sleep(delay)
                    last = HttpError(e.code, path)
                    continue
                raise HttpError(e.code, path) from None
            except (urllib.error.URLError, socket.timeout, ConnectionError) as e:
                last = HttpError(0, path, type(e).__name__)
                if attempt < self.max_attempts:
                    self._sleep(self.base_delay * (2 ** (attempt - 1)))
                    continue
                raise last from None
        raise last or HttpError(0, path, "failed")

    @staticmethod
    def _retry_after(e: urllib.error.HTTPError) -> Optional[float]:
        try:
            v = e.headers.get("Retry-After")
            return min(float(v), 60.0) if v else None
        except (TypeError, ValueError):
            return None
