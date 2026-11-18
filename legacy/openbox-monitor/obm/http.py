"""Plain HTTP helpers: cancellation, polite sleeps, a simple requests-based fetcher."""
from __future__ import annotations

import json
import logging
import random
import threading
import time
from dataclasses import dataclass

import requests

log = logging.getLogger(__name__)

# Set by the daemon on SIGTERM so an in-progress check stops within seconds.
CANCEL = threading.Event()


class Cancelled(BaseException):
    """Raised inside a check when shutdown was requested. BaseException so the
    per-scope `except Exception` handlers don't swallow it."""


def _sleep(seconds: float) -> None:
    if CANCEL.wait(max(0.0, seconds)):
        raise Cancelled()


@dataclass
class Resp:
    status_code: int
    text: str
    url: str = ""

    def json(self):
        return json.loads(self.text)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class HttpFetcher:
    def __init__(self, delay: float = 0.4, timeout: float = 15.0, retries: int = 3):
        self.delay, self.timeout, self.retries = delay, timeout, retries
        self._last = 0.0
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "openbox-monitor/0.3 (personal price alerts)",
                                     "Accept": "application/json"})

    def _pace(self) -> None:
        _sleep(self.delay + random.uniform(0, self.delay * 0.5) - (time.time() - self._last))
        self._last = time.time()

    def get(self, url: str, params: dict | None = None, **_kw) -> Resp:
        last_exc: Exception | None = None
        for attempt in range(1, self.retries + 1):
            self._pace()
            try:
                r = self.session.get(url, params=params, timeout=self.timeout)
                if r.status_code in (429, 500, 502, 503, 504):
                    raise RuntimeError(f"HTTP {r.status_code}")
                return Resp(r.status_code, r.text, r.url)
            except Exception as e:
                last_exc = e
                # never log the URL: it carries the API key
                log.warning("request failed (attempt %d/%d): %s", attempt, self.retries, type(e).__name__ if isinstance(e, requests.RequestException) else e)
                _sleep(2 * attempt)
        assert last_exc is not None
        raise RuntimeError(f"请求失败：{type(last_exc).__name__}" if isinstance(last_exc, requests.RequestException) else str(last_exc))

    def close(self) -> None:
        self.session.close()


Fetcher = HttpFetcher
