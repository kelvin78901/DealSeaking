"""Config loading: YAML file deep-merged over defaults, plus save-back for the settings page."""
from __future__ import annotations

import copy
import logging
import os
import time
from pathlib import Path
from typing import Any

import yaml

from .locations import find_store, geocode_zip, stores_by_distance, stores_near

log = logging.getLogger(__name__)

ALL_CATEGORIES = ["ssd", "hdd_internal", "hdd_external", "nas", "usb_flash", "memory_card", "enclosure"]
DEPARTMENTS = ["computers", "computer_parts", "electronics", "networking", "apple", "gaming", "accessories"]

DEFAULTS: dict[str, Any] = {
    "interval_minutes": 180,
    "quiet_hours": [],
    # Where you are. With `stores: auto`, every Micro Center within radius_miles is watched.
    "location": {"zip": "", "radius_miles": 50},
    "sources": {
        "microcenter": {
            "enabled": True,          # show these stores on the page
            "scrape": True,           # automatically check their open box listings
            "stores": "auto",
            # what to scrape: "all" = every open box item in the store, or a list of
            # departments (computers, computer_parts, …) / storage keys (ssd, …) / raw "Name|id"
            "categories": "all",
            "include_out_of_stock": False,
            "request_delay_seconds": 3.0,
            # browser fingerprints tried in order; the one that works is remembered
            "impersonate": ["safari", "safari_ios", "firefox", "chrome", "edge"],
        },
        "bestbuy": {
            "enabled": False,
            # search terms (run with the Open-Box filter; Best Buy picks a category by itself), or
            # Best Buy URLs copied from the browser after choosing a category + the Open-Box filter.
            # Online open box, not per store.
            "searches": ["ssd"],
            "max_pages": 15,              # 18 products per page
            "keep_price_days": 7,         # how long a last-seen price is reused (see bestbuy.py)
            "request_delay_seconds": 3.0,
            "impersonate": "safari",      # one fingerprint, no rotation
        },
    },
    "filters": {
        # push only items with these tags (the store's own category names); [] = every tag
        "tags": ["SSD (Solid State Drives)", "Internal Hard Drives", "External Hard Drives",
                 "NAS (Network Attached Storage)", "USB Flash Drives", "Flash Memory"],
        "min_capacity_gb": 0,
        "max_capacity_gb": 0,
        "max_price": 0,
        "min_discount_pct": 0,
        "max_price_per_tb": 0,
        "brands": [],
        "include_keywords": [],
        "exclude_keywords": [],
    },
    "alerts": {
        "on_new": True,
        "on_price_drop": True,
        "min_drop_pct": 3,
        "on_restock": True,
        "max_per_run": 6,
        "watchlist": [],
    },
    "notify": {
        "macos": True,
        "ntfy": {"enabled": False, "server": "https://ntfy.sh", "topic": ""},
        "bark": {"enabled": False, "server": "https://api.day.app", "key": ""},
        "telegram": {"enabled": False, "bot_token": "", "chat_id": ""},
    },
    "dashboard": {"path": "dashboard.html", "history_days": 30, "open_after_run": False},
    "database": "data/openbox.db",
    "log_file": "data/monitor.log",
}

# Category keys used across the tool -> human label
CATEGORY_LABELS: dict[str, str] = {
    "ssd_internal": "内置 SSD",
    "ssd_external": "移动 SSD",
    "hdd_internal": "内置机械盘",
    "hdd_external": "移动机械盘",
    "nas": "NAS",
    "usb_flash": "U 盘",
    "memory_card": "存储卡",
    "enclosure": "硬盘盒/托架",
    "other": "其他",
}

LEGACY_BB_KEYS = ("api_key", "category_ids", "categories", "conditions",
                  "max_detail_per_run", "max_detail_first_run", "refresh_hours")

# Secrets never sent to the browser
SECRET_PATHS = [
    ("notify", "ntfy", "topic"),
    ("notify", "bark", "key"),
    ("notify", "telegram", "bot_token"),
]


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def resolve_store(value: Any) -> tuple[str, str]:
    """Accept a store name ('Rockville') or ID ('085' / 85). Return (name, id)."""
    s = find_store(value)
    return s.name, s.id


class Config(dict):
    """Dict with a few helpers; `base_dir` resolves relative paths, `path` is the YAML file."""

    base_dir: Path
    path: Path | None = None

    def resolve_path(self, value: str) -> Path:
        p = Path(value).expanduser()
        return p if p.is_absolute() else (self.base_dir / p)

    @property
    def db_path(self) -> Path:
        return self.resolve_path(self["database"])

    @property
    def log_path(self) -> Path:
        return self.resolve_path(self["log_file"])

    @property
    def dashboard_path(self) -> Path:
        return self.resolve_path(self["dashboard"]["path"])

    @property
    def data_dir(self) -> Path:
        return self.db_path.parent


def _resolve_stores(cfg: dict) -> None:
    """Fill sources.microcenter._stores = [(name, id)] and location._* helpers."""
    mc = cfg["sources"]["microcenter"]
    loc = cfg["location"]
    zip_code = str(loc.get("zip") or "").strip()
    stores = mc.get("stores")
    distances: dict[str, float] = {}

    if zip_code:
        lat, lon, label = geocode_zip(zip_code)  # raises on a bad ZIP
        loc["_label"] = label
        distances = {s.name: d for s, d in stores_by_distance(zip_code)}

    if stores in (None, "", "auto", []) or (isinstance(stores, str) and stores.lower() == "auto"):
        if not zip_code:
            raise ValueError("stores 设为 auto 时必须填 location.zip（你的 5 位 ZIP），或者把 stores 改成门店列表")
        picked = stores_near(zip_code, float(loc.get("radius_miles") or 50))
        mc["_stores"] = [(s.name, s.id) for s, _ in picked]
        mc["_auto"] = True
    else:
        if isinstance(stores, str):
            stores = [x.strip() for x in stores.split(",") if x.strip()]
        mc["_stores"] = [resolve_store(s) for s in stores]
        mc["_auto"] = False
    loc["_distances"] = distances


def load_config(path: str | Path | None) -> Config:
    data: dict = {}
    base_dir = Path.cwd()
    p = None
    if path:
        p = Path(path).expanduser().resolve()
        if not p.exists():
            raise FileNotFoundError(f"找不到配置文件 {p}。先运行：cp config.example.yaml config.yaml")
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        base_dir = p.parent
    cfg = Config(_deep_merge(DEFAULTS, data))
    cfg.base_dir = base_dir
    cfg.path = p
    _resolve_stores(cfg)  # validate early so typos fail loudly
    return cfg


def public_view(cfg: dict) -> dict:
    """Config as the settings page sees it: no computed keys, secrets replaced by a flag."""
    out = _strip_private(copy.deepcopy(dict(cfg)))
    for k in LEGACY_BB_KEYS:  # leftovers of older versions
        out.get("sources", {}).get("bestbuy", {}).pop(k, None)
    for path in SECRET_PATHS:
        node = out
        for k in path[:-1]:
            node = node.get(k, {})
        val = node.get(path[-1], "")
        node[path[-1]] = ""
        node[path[-1] + "_set"] = bool(val)
    return out


def _strip_private(d):
    if isinstance(d, dict):
        return {k: _strip_private(v) for k, v in d.items() if not str(k).startswith("_")}
    if isinstance(d, list):
        return [_strip_private(v) for v in d]
    return d


def apply_update(cfg: Config, update: dict) -> dict:
    """Merge a settings-page update into the on-disk config and return the new raw dict.

    Empty secret fields mean "keep the current value". Validates by loading the result
    before anything is written.
    """
    raw = {}
    if cfg.path and cfg.path.exists():
        raw = yaml.safe_load(cfg.path.read_text(encoding="utf-8")) or {}
    update = copy.deepcopy(update)
    for path in SECRET_PATHS:
        node = update
        for k in path[:-1]:
            node = node.get(k) if isinstance(node, dict) else None
            if node is None:
                break
        if isinstance(node, dict):
            node.pop(path[-1] + "_set", None)
            if not node.get(path[-1]):
                node.pop(path[-1], None)  # keep existing secret
    # lists from the page replace lists on disk wholesale
    merged = _deep_merge(raw, _strip_private(update))
    for k in LEGACY_BB_KEYS:
        merged.get("sources", {}).get("bestbuy", {}).pop(k, None)
    # validate the merged result exactly like a normal load
    test = Config(_deep_merge(DEFAULTS, merged))
    test.base_dir = cfg.base_dir
    _resolve_stores(test)
    return merged


HEADER = """# Open Box Monitor 配置
# 这个文件可以手改，也可以在监控页面的「设置」里改（页面保存时会重写本文件，注释不会保留；
# 字段说明见 config.example.yaml）。上一版本自动备份为 config.yaml.bak。
"""


def save_raw(cfg: Config, raw: dict) -> None:
    if not cfg.path:
        raise RuntimeError("没有配置文件路径，无法保存")
    if cfg.path.exists():
        cfg.path.with_suffix(".yaml.bak").write_bytes(cfg.path.read_bytes())
    tmp = cfg.path.with_suffix(f".tmp{os.getpid()}")
    tmp.write_text(HEADER + yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8")
    tmp.replace(cfg.path)
    log.info("config saved from settings page at %s", time.strftime("%H:%M:%S"))
