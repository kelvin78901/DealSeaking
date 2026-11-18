"""Long-running mode: periodic checks + a tiny web server in one process.

Only three routes are served — never the directory — so config.yaml (API keys,
tokens) sitting next to dashboard.html can't leak:
  GET  /           the dashboard page
  GET  /healthz    JSON status; 503 if the check loop has stalled
  POST /run        trigger a check now
"""
from __future__ import annotations

import json
import logging
import signal
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from .config import Config, apply_update, load_config, public_view, save_raw
from .dashboard import render_dashboard
from .http import CANCEL, Cancelled
from .locations import STORES, geocode_zip, stores_by_distance, stores_near
from .sources.microcenter import DEPARTMENT_LABELS, LINK_LABELS, MC_CATEGORIES, MC_DEPARTMENTS
from .notify import Notifier
from .runner import in_quiet_hours, run_once
from .store import Store

log = logging.getLogger(__name__)


class State:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.started = time.time()
        self.running_since: float | None = None
        self.waiting_until: float | None = None
        self.last_finished: float | None = None
        self.last_stats: dict | None = None
        self.last_error: str = ""
        self.editable = False  # settings API only in daemon mode
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.lock = threading.Lock()

    def healthy(self) -> bool:
        now = time.time()
        if self.running_since:  # a check is in progress; allow it 30 min
            return now - self.running_since < 1800
        if self.waiting_until:  # sleeping until the next check; allow 15 min of slack
            return now < self.waiting_until + 900
        return now - self.started < 600  # still starting up

    def snapshot(self) -> dict:
        return {
            "ok": self.healthy(),
            "running": self.running_since is not None,
            "last_finished": self.last_finished,
            "next_check": self.waiting_until,
            "interval_minutes": self.cfg["interval_minutes"],
            "last_stats": self.last_stats,
            "last_error": self.last_error,
        }


def make_handler(state: State):
    class Handler(BaseHTTPRequestHandler):
        server_version = "obm"

        def log_message(self, fmt, *args):  # keep docker logs readable
            log.debug("http %s - " + fmt, self.address_string(), *args)

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path in ("/", "/index.html", "/" + state.cfg.dashboard_path.name):
                try:
                    body = state.cfg.dashboard_path.read_bytes()
                except FileNotFoundError:
                    body = "<meta http-equiv=refresh content=10><p>第一轮检查还没跑完，稍等…</p>".encode()
                return self._send(200, body, "text/html; charset=utf-8")
            if path == "/healthz":
                snap = state.snapshot()
                return self._send(200 if snap["ok"] else 503,
                                  json.dumps(snap, ensure_ascii=False).encode(), "application/json")
            if path == "/api/config" and state.editable:
                return self._json(200, settings_payload(state.cfg))
            if path == "/api/stores" and state.editable:
                q = parse_qs(urlsplit(self.path).query)
                try:
                    return self._json(200, stores_payload(q.get("zip", [""])[0], float(q.get("radius", ["50"])[0])))
                except ValueError as e:
                    return self._json(400, {"error": str(e)})
            self._send(404, b"not found", "text/plain")

        do_HEAD = do_GET

        # ── writes ──
        def _same_origin(self) -> bool:
            """Block other websites from POSTing here through your browser (CSRF)."""
            origin = self.headers.get("Origin")
            return not origin or urlsplit(origin).netloc == self.headers.get("Host")

        def _json(self, code: int, obj) -> None:
            self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def _read_json(self):
            if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                raise ValueError("需要 application/json")
            n = int(self.headers.get("Content-Length") or 0)
            if n > 200_000:
                raise ValueError("请求太大")
            return json.loads(self.rfile.read(n) or b"{}")

        def do_POST(self):
            path = self.path.split("?", 1)[0]
            if not self._same_origin():
                return self._send(403, b"cross-origin request refused", "text/plain")
            if path == "/run":
                if state.running_since:
                    return self._send(409, "正在检查中".encode(), "text/plain; charset=utf-8")
                state.wake.set()
                return self._send(202, "已开始检查".encode(), "text/plain; charset=utf-8")
            if not state.editable:
                return self._send(404, b"not found", "text/plain")
            if path == "/api/config":
                try:
                    body = self._read_json()
                    with state.lock:
                        raw = apply_update(state.cfg, body.get("config") or {})
                        save_raw(state.cfg, raw)
                        state.cfg = load_config(state.cfg.path)
                    render_dashboard(state.cfg, Store(state.cfg.db_path))
                    started = False
                    if body.get("run_now") and not state.running_since:
                        state.wake.set()
                        started = True
                    return self._json(200, {"ok": True, "run_started": started, **settings_payload(state.cfg)})
                except Exception as e:
                    return self._json(400, {"ok": False, "error": str(e)})
            if path == "/api/test-notify":
                results = Notifier(state.cfg).send({
                    "title": "Open Box 监控 · 测试", "body": "推送渠道配置正常 ✅",
                    "url": "https://www.microcenter.com/site/products/open-box.aspx"})
                return self._json(200, {"results": results})
            self._send(404, b"not found", "text/plain")

    return Handler


def stores_payload(zip_code: str, radius: float) -> dict:
    zip_code = (zip_code or "").strip()
    if not zip_code:
        return {"label": "", "stores": [{"name": s.name, "id": s.id, "state": s.state, "address": s.address,
                                          "distance": None, "in_range": False} for s in STORES]}
    _, _, label = geocode_zip(zip_code)
    near = {s.name for s, _ in stores_near(zip_code, radius)}
    return {"label": label, "stores": [
        {"name": s.name, "id": s.id, "state": s.state, "address": s.address, "distance": d, "in_range": s.name in near}
        for s, d in stores_by_distance(zip_code)]}


def tag_options(cfg: Config) -> list[dict]:
    """Tags seen in the data so far, plus any already chosen for alerts, plus the storage defaults."""
    counts = Store(cfg.db_path).tag_counts()
    names = list(counts)
    for t in (cfg["filters"].get("tags") or []) + [v.split("|")[0] for v in MC_CATEGORIES.values()]:
        if t not in names:
            names.append(t)
    return [{"tag": t, "n": counts.get(t, 0)} for t in names]


def settings_payload(cfg: Config) -> dict:
    loc = cfg["location"]
    try:
        stores = stores_payload(loc.get("zip") or "", float(loc.get("radius_miles") or 50))
    except ValueError:
        stores = stores_payload("", 50)
    return {
        "config": public_view(cfg),
        "watching": [n for n, _ in cfg["sources"]["microcenter"]["_stores"]],
        "stores": stores,
        "scopes": {
            "departments": [{"key": k, "label": DEPARTMENT_LABELS[k]} for k in MC_DEPARTMENTS],
            "storage": [{"key": k, "label": LINK_LABELS[k]} for k in MC_CATEGORIES],
        },
        "tags": tag_options(cfg),
    }


def serve_only(cfg: Config, host: str, port: int) -> None:
    """`python -m obm serve`: just the page, no checks."""
    state = State(cfg)
    state.waiting_until = float("inf")  # no loop here; always healthy
    render_dashboard(cfg, Store(cfg.db_path))
    srv = ThreadingHTTPServer((host, port), make_handler(state))
    print(f"http://{'localhost' if host == '0.0.0.0' else host}:{port}/  (Ctrl-C 退出)")
    srv.serve_forever()


def daemon(config_path: str, host: str, port: int, notify: bool = True) -> None:
    cfg = load_config(config_path)
    state = State(cfg)
    state.editable = True
    render_dashboard(cfg, Store(cfg.db_path))  # page exists before the first check finishes

    try:
        srv = ThreadingHTTPServer((host, port), make_handler(state))
    except OSError as e:
        raise SystemExit(f"端口 {port} 被占用（{e.strerror}）：是不是已经有一个监控在跑？换端口用 --port") from None
    threading.Thread(target=srv.serve_forever, name="http", daemon=True).start()
    log.info("页面：http://%s:%d/ · 每 %s 分钟检查一次", host, port, cfg["interval_minutes"])

    def _stop(signum, _frame):
        log.info("收到信号 %s，退出中…", signum)
        state.stop.set()
        state.wake.set()
        CANCEL.set()  # abort an in-progress check at the next request/sleep

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    manual = False
    while not state.stop.is_set():
        # pick up edits to config.yaml (by hand or from the settings page) without restarting
        try:
            with state.lock:
                new_cfg = load_config(config_path)
            if new_cfg["interval_minutes"] != cfg["interval_minutes"]:
                log.info("检查间隔改为 %s 分钟", new_cfg["interval_minutes"])
            cfg = state.cfg = new_cfg
        except Exception as e:
            log.error("config.yaml 有误，继续用上一次的配置：%s", e)
            state.last_error = f"config: {e}"

        if not manual and in_quiet_hours(cfg):
            log.info("静默时段，跳过")
        else:
            state.waiting_until = None
            state.running_since = time.time()
            try:
                state.last_stats = run_once(cfg, force=manual, notify=notify)
                state.last_error = ""
            except Cancelled:
                log.info("检查已中断（正在退出）")
                break
            except Exception as e:
                log.exception("check failed: %s", e)
                state.last_error = str(e)[:300]
            finally:
                state.running_since = None
                state.last_finished = time.time()

        interval = max(5, int(cfg["interval_minutes"])) * 60
        state.waiting_until = time.time() + interval
        state.wake.clear()
        manual = state.wake.wait(timeout=interval) and not state.stop.is_set()

    srv.shutdown()
    log.info("已退出")
