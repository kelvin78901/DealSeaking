"""HTTP client for Micro Center pages: rotates curl_cffi browser fingerprints.

User-confirmed choice (Oct 2026): Micro Center 403s Chrome/Edge fingerprints and
headless Chromium; the Safari fingerprint passes. Rotate through profiles, remember
the one that works in data/fetch_state.json. Personal, low-volume use only.
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

from .http import Resp, _sleep

log = logging.getLogger(__name__)

DEFAULT_PROFILES = ["safari", "safari_ios", "firefox", "chrome", "edge"]


class BlockedError(RuntimeError):
    pass


def looks_blocked(status: int, html: str) -> bool:
    head = html[:6000].lower()
    return status == 403 or ("access denied" in head and "product_wrapper" not in html)


class RotatingFetcher:
    def __init__(self, data_dir: Path, delay: float = 3.0, profiles: list[str] | None = None, timeout: float = 15.0,
                 state_name: str = "fetch_state.json"):
        from curl_cffi import requests as creq  # type: ignore

        self._creq = creq
        self.delay, self.timeout = delay, timeout
        self.state_file = data_dir / state_name  # one per site: what works for one may not for another
        profiles = list(profiles or DEFAULT_PROFILES)
        good = self._state().get("profile")
        if good in profiles:
            profiles.remove(good)
            profiles.insert(0, good)
        self.profiles, self.idx = profiles, 0
        self.session = None
        self._last = 0.0
        self.exhausted = False

    def _state(self) -> dict:
        try:
            return json.loads(self.state_file.read_text())
        except Exception:
            return {}

    def _remember(self, profile: str) -> None:
        if self._state().get("profile") != profile:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            self.state_file.write_text(json.dumps({"profile": profile, "since": time.time()}))

    def _session(self, url: str):
        if self.session is None:
            self.session = self._creq.Session(impersonate=self.profiles[self.idx])
            self.session.headers.update({"Accept-Language": "en-US,en;q=0.9"})
            host = f"{urlsplit(url).scheme}://{urlsplit(url).netloc}/"
            try:  # land on the homepage first, like a person would
                self._pace()
                self.session.get(host, timeout=self.timeout)
            except Exception:
                pass
        return self.session

    def _pace(self) -> None:
        _sleep(self.delay + random.uniform(0, self.delay * 0.5) - (time.time() - self._last))
        self._last = time.time()

    @property
    def backend(self) -> str:
        return f"curl_cffi/{self.profiles[self.idx]}"

    def get(self, url: str, params: dict | None = None) -> Resp:
        if self.exhausted:
            raise BlockedError("所有指纹本轮都被拦了")
        while self.idx < len(self.profiles):
            s = self._session(url)
            self._pace()
            try:
                r = s.get(url, params=params, timeout=self.timeout,
                          headers={"Referer": f"{urlsplit(url).scheme}://{urlsplit(url).netloc}/"})
            except Exception as e:  # network error: not a block, don't burn through fingerprints
                detail = re.sub(r"\s+", " ", str(e) or repr(e)).split(" See https://curl.se")[0][:160]
                if type(e).__name__ == "HTTPError" and ("HTTP/2" in detail or "stream" in detail.lower() or not str(e)):
                    detail = "连接被服务器中途断开（多见于反爬拦截）" + (f"：{detail}" if str(e) else "")
                raise RuntimeError(f"网络错误：{type(e).__name__}: {detail}") from None
            if not looks_blocked(r.status_code, r.text):
                self._remember(self.profiles[self.idx])
                return Resp(r.status_code, r.text, str(r.url))
            log.warning("%s 被拦 (HTTP %s)，换下一种指纹", self.backend, r.status_code)
            s.close()
            self.session = None
            self.idx += 1
        self.exhausted = True
        raise BlockedError("所有指纹都被拦：可能 IP 被临时限制，调大检查间隔或稍后再试")

    def close(self) -> None:
        if self.session is not None:
            self.session.close()
