"""Render the self-contained monitoring page (dashboard.html)."""
from __future__ import annotations

import json
import time
from pathlib import Path

from .config import CATEGORY_LABELS, Config
from .rules import on_watchlist, passes_filters
from .sources.bestbuy import scope_name as bestbuy_scope
from .sources.microcenter import store_links

TEMPLATE = Path(__file__).with_name("dashboard_template.html")


def build_data(cfg: Config, store) -> dict:
    listings = store.listings(include_inactive_days=3)
    for d in listings:
        d["match"] = passes_filters(d, cfg["filters"])
        d["watch"] = on_watchlist(d, cfg["alerts"])
    hist = store.history([d["key"] for d in listings], days=float(cfg["dashboard"]["history_days"]))
    mc = cfg["sources"]["microcenter"]
    loc = cfg["location"]
    dist = loc.get("_distances") or {}
    return {
        "generated_at": time.time(),
        "interval_minutes": cfg["interval_minutes"],
        "quiet_hours": cfg.get("quiet_hours") or [],
        "location": {"zip": loc.get("zip") or "", "label": loc.get("_label", ""),
                     "radius": loc.get("radius_miles"), "auto": bool(mc.get("_auto"))},
        "stores": [{"name": n, "distance": dist.get(n)} for n, _ in mc["_stores"]] if mc["enabled"] else [],
        "mc_links": store_links(cfg),
        "distances": dist,
        "bestbuy_enabled": bool(cfg["sources"]["bestbuy"].get("enabled")),
        "bb_scopes": [bestbuy_scope(q) for q in cfg["sources"]["bestbuy"].get("searches") or []],
        "mc_scrape": bool(mc["enabled"] and mc.get("scrape", True)),
        "filters": cfg["filters"],
        "watchlist": cfg["alerts"].get("watchlist") or [],
        "categories": CATEGORY_LABELS,
        "listings": listings,
        "history": hist,
        "events": store.recent_events(days=7),
        "runs": store.last_runs(),
    }


def render_dashboard(cfg: Config, store, out: Path | None = None) -> Path:
    data = build_data(cfg, store)
    blob = json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    html = TEMPLATE.read_text(encoding="utf-8").replace("/*__OBM_DATA__*/null", blob)
    out = out or cfg.dashboard_path
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".tmp")
    tmp.write_text(html, encoding="utf-8")
    tmp.replace(out)  # atomic so an open browser never reads half a file
    return out
