"""Filtering + deciding which events deserve a push."""
from __future__ import annotations

from .store import Event


def _lower_list(xs) -> list[str]:
    return [str(x).lower() for x in (xs or []) if str(x).strip()]


def passes_filters(d: dict, f: dict) -> bool:
    name = (d.get("name") or "").lower()
    cap = d.get("capacity_gb") or 0
    price = d.get("price_open_box") or 0
    if f.get("min_capacity_gb") and cap and cap < f["min_capacity_gb"]:
        return False
    if f.get("min_capacity_gb") and not cap:
        return False
    if f.get("max_capacity_gb") and cap and cap > f["max_capacity_gb"]:
        return False
    if f.get("max_price") and price > f["max_price"]:
        return False
    if f.get("min_discount_pct"):
        disc = d.get("discount_pct")
        if disc is None or disc < f["min_discount_pct"]:
            return False
    if f.get("max_price_per_tb"):
        ppt = d.get("price_per_tb")
        if ppt is None or ppt > f["max_price_per_tb"]:
            return False
    tags = _lower_list(f.get("tags"))
    if tags and (d.get("category") or "").lower() not in tags:
        return False
    brands = _lower_list(f.get("brands"))
    if brands and (d.get("brand") or "").lower() not in brands and not any(b in name for b in brands):
        return False
    inc = _lower_list(f.get("include_keywords"))
    if inc and not any(k in name for k in inc):
        return False
    exc = _lower_list(f.get("exclude_keywords"))
    if exc and any(k in name for k in exc):
        return False
    return True


def on_watchlist(d: dict, alerts: dict) -> bool:
    name = (d.get("name") or "").lower()
    return any(w in name for w in _lower_list(alerts.get("watchlist")))


def should_alert(ev: Event, cfg: dict) -> bool:
    a, f = cfg["alerts"], cfg["filters"]
    if ev.kind not in ("new", "restock", "price_drop"):
        return False
    if on_watchlist(ev.listing, a):
        return True
    if not passes_filters(ev.listing, f):
        return False
    if ev.kind == "new":
        return bool(a.get("on_new"))
    if ev.kind == "restock":
        return bool(a.get("on_restock"))
    if ev.kind == "price_drop":
        return bool(a.get("on_price_drop")) and (ev.drop_pct or 0) >= float(a.get("min_drop_pct") or 0)
    return False


def priority(ev: Event, cfg: dict) -> tuple:
    """Sort key: watchlist first, then biggest discount, then cheapest $/TB."""
    d = ev.listing
    return (
        0 if on_watchlist(d, cfg["alerts"]) else 1,
        -(d.get("discount_pct") or 0),
        d.get("price_per_tb") or 1e9,
    )
