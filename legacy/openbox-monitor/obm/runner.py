"""One monitoring pass: scrape → diff → notify → render page."""
from __future__ import annotations

import datetime as dt
import logging
import time
from contextlib import contextmanager
from typing import Iterable

from .config import Config
from .dashboard import render_dashboard
from .models import ScopeResult
from .notify import Notifier, format_event, format_summary
from .rules import priority, should_alert
from .scrape_http import RotatingFetcher
from .sources import bestbuy, microcenter
from .store import Event, Store

log = logging.getLogger(__name__)



def in_quiet_hours(cfg: Config, now: dt.datetime | None = None) -> bool:
    qh = cfg.get("quiet_hours") or []
    if len(qh) != 2:
        return False
    now = now or dt.datetime.now()
    start, end = (dt.datetime.strptime(x, "%H:%M").time() for x in qh)
    t = now.time()
    return (start <= t < end) if start < end else (t >= start or t < end)


@contextmanager
def run_lock(cfg: Config):
    """Prevent overlapping runs (e.g. launchd firing while a manual run is going)."""
    lock_path = cfg.db_path.parent / ".run.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    f = open(lock_path, "w")
    try:
        import fcntl

        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("另一个检查正在运行，跳过本轮")
        yield
    finally:
        f.close()


def _fail_alert_due(cfg: Config) -> bool:
    marker = cfg.db_path.parent / ".last_fail_alert"
    now = time.time()
    try:
        if now - float(marker.read_text()) < 86400:
            return False
    except Exception:
        pass
    marker.write_text(str(now))
    return True


def _one_profile(v) -> str:
    """Best Buy uses a single fingerprint; older configs may still hold a list."""
    if isinstance(v, (list, tuple)):
        v = v[0] if v else None
    return str(v or "safari")


def collect(cfg: Config, store: Store | None = None) -> Iterable[ScopeResult]:
    mc = cfg["sources"]["microcenter"]
    if mc["enabled"] and mc.get("scrape", True):
        f = RotatingFetcher(cfg.data_dir, delay=float(mc.get("request_delay_seconds", 3.0)),
                            profiles=mc.get("impersonate") or None)
        log.info("Micro Center: %s via %s", ", ".join(n for n, _ in mc["_stores"]), f.backend)
        try:
            yield from microcenter.scrape(cfg, f)
        finally:
            f.close()

    bb = cfg["sources"]["bestbuy"]
    if bb.get("enabled") and bb.get("searches"):
        first = store is not None and not any(store.scope_seen_before(bestbuy.scope_name(q)) for q in bb["searches"])
        # one fingerprint, no rotation: if Best Buy starts refusing, stop for this round
        f = RotatingFetcher(cfg.data_dir, delay=float(bb.get("request_delay_seconds", 3.0)),
                            profiles=[_one_profile(bb.get("impersonate"))], state_name="fetch_state_bestbuy.json")
        log.info("Best Buy: %s via %s%s", ", ".join(bb["searches"]), f.backend, "（首轮）" if first else "")
        try:
            yield from bestbuy.scrape(cfg, f, first_run=first)
        except Exception as e:  # blocked mid-way: report it instead of failing the whole run
            yield ScopeResult(bestbuy.scope_name(bb["searches"][0]), False, error=f"{type(e).__name__}: {e}"[:300])
        finally:
            f.close()


def run_once(
    cfg: Config,
    *,
    force: bool = False,
    notify: bool = True,
    results: Iterable[ScopeResult] | None = None,
    notifier: Notifier | None = None,
) -> dict:
    if not force and in_quiet_hours(cfg):
        log.info("静默时段，跳过（用 --force 强制）")
        return {"skipped": "quiet_hours"}

    with run_lock(cfg):
        t0 = time.time()
        store = Store(cfg.db_path)
        first_ever = not store.has_successful_run()
        notifier = notifier or Notifier(cfg)

        all_events: list[Event] = []
        alerts: list[Event] = []
        failures: list[ScopeResult] = []
        n_items = 0
        for res in (results if results is not None else collect(cfg, store)):
            baseline = not store.scope_seen_before(res.scope)
            if res.ok and res.signature and store.scope_signature_changed(res.scope, res.signature):
                baseline = True
            events = store.apply(res)
            n_items += len(res.listings)
            if not res.ok:
                failures.append(res)
                continue
            all_events.extend(events)
            for ev in events:
                if baseline and ev.kind == "new":
                    continue  # first look at this store/category: establish baseline quietly
                if ev.kind == "new" and ev.listing.get("key") in res.quiet:
                    continue  # the source knows this item was already there (e.g. it just got a price)
                if should_alert(ev, cfg):
                    alerts.append(ev)

        alerts.sort(key=lambda e: priority(e, cfg))
        sent = 0
        if notify:
            cap = max(1, int(cfg["alerts"].get("max_per_run") or 6))
            if len(alerts) > cap:
                for ev in alerts[: cap - 1]:
                    notifier.send(format_event(ev))
                notifier.send(format_summary(len(alerts) - (cap - 1), str(cfg.dashboard_path)))
                sent = cap
            else:
                for ev in alerts:
                    notifier.send(format_event(ev))
                sent = len(alerts)
            if first_ever and store.has_successful_run():
                notifier.send({
                    "title": "Open Box 监控已启动",
                    "body": f"已记录 {n_items} 条 open box 作为基线，之后有新货/降价会推送。",
                    "url": "",
                })
            if failures and not n_items and _fail_alert_due(cfg):
                # everything failed (blocked / offline / bad key) — tell the user, at most once a day
                notifier.send({
                    "title": "Open Box 监控：抓取失败",
                    "body": "; ".join(f"{r.scope}: {r.error}" for r in failures)[:300],
                    "url": "",
                })

        render_dashboard(cfg, store)
        stats = {
            "items": n_items,
            "events": {k: sum(1 for e in all_events if e.kind == k) for k in ("new", "restock", "price_drop", "price_up", "gone")},
            "alerts": len(alerts),
            "notified": sent,
            "failed_scopes": [f"{r.scope}: {r.error}" for r in failures],
            "seconds": round(time.time() - t0, 1),
        }
        log.info("run done: %s", stats)
        return stats
