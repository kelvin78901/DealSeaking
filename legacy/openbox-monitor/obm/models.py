"""Shared data model + helpers for parsing names."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Optional

_CAP_RE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*(TB|GB)\b", re.I)


def parse_capacity_gb(name: str) -> Optional[float]:
    """Largest capacity mentioned in a product name, in GB (1TB = 1000GB).

    Skips memory-ish mentions like '16GB Memory' / 'RAM' that show up in
    bundle names, and DRAM cache mentions.
    """
    best = None
    for m in _CAP_RE.finditer(name):
        tail = name[m.end(): m.end() + 12].lower()
        if any(w in tail for w in ("memory", "ram", "dram", "cache", "ddr")):
            continue
        val = float(m.group(1)) * (1000 if m.group(2).upper() == "TB" else 1)
        best = val if best is None else max(best, val)
    return best


def classify(name: str, hint: str = "") -> str:
    """Map a product to one of our category keys using the source hint + name."""
    n = name.lower()
    h = hint.lower()
    # cards and flash drives first: "microSDXC Card with Adapter" is a card, not an adapter
    if h == "memory_card" or re.search(r"\b(micro)?sd(xc|hc|uc)?\b.*(card|express)|cfexpress|memory card", n):
        return "memory_card"
    if h == "usb_flash" or "flash drive" in n or "thumb drive" in n:
        return "usb_flash"
    standalone_adapter = re.search(r"\badapter\b", n) and not re.search(r"\b(with|w/|includes?)\s+(an?\s+)?(\w+\s+)?adapter", n)
    if any(w in n for w in ("enclosure", "docking", "dock ", "drive bay")) or standalone_adapter:
        return "enclosure"
    if h == "nas" or re.search(r"\bnas\b|diskstation|terramaster|qnap ts-|ugreen nasync", n):
        if "hard drive" not in n and "hdd" not in n and "ssd" not in n:
            return "nas"
    name_ssd = "ssd" in n or "solid state" in n or "nvme" in n
    name_hdd = ("hard drive" in n or "hdd" in n or "rpm" in n) and not name_ssd
    external = any(w in n for w in ("portable", "external", "usb 3", "usb-c", "thunderbolt", "usb4"))
    # what the name says beats which listing page it came from
    is_ssd = name_ssd or (h.startswith("ssd") and not name_hdd)
    if is_ssd:
        return "ssd_external" if external or h == "ssd_external" else "ssd_internal"
    if name_hdd or h.startswith("hdd"):
        if h == "hdd_external" or external or "desktop drive" in n:
            return "hdd_external"
        return "hdd_internal"
    if h in ("hdd_internal", "hdd_external", "ssd_internal", "ssd_external", "nas", "enclosure"):
        return h
    return "other"


_LEGACY_STORAGE = {"ssd_internal", "ssd_external", "hdd_internal", "hdd_external", "nas", "usb_flash", "memory_card"}
_STORAGE_WORDS = ("ssd", "solid state", "hard drive", "flash", "memory card", "sd card", "nas", "storage")


def is_storage(tag: str) -> bool:
    """Is this retailer category a storage one (so capacity and $/TB make sense)?"""
    t = (tag or "").lower()
    if t in _LEGACY_STORAGE:
        return True
    if "enclosure" in t or "ram" in t.split("/") or "memory/ram" in t:
        return False
    return any(w in t for w in _STORAGE_WORDS)


@dataclass
class Listing:
    source: str                 # "microcenter" | "bestbuy"
    store: str                  # store name, or "online"
    product_id: str             # retailer's product id / sku
    name: str
    url: str
    category: str               # tag: the retailer's own category, e.g. "SSD (Solid State Drives)"
    price_open_box: float
    price_new: Optional[float] = None
    condition: str = "open box"
    brand: str = ""
    sku: str = ""
    stock: Optional[int] = None  # None = unknown
    stock_text: str = ""
    capacity_gb: Optional[float] = field(default=None)

    def __post_init__(self) -> None:
        if self.capacity_gb is None and is_storage(self.category):
            self.capacity_gb = parse_capacity_gb(self.name)

    @property
    def key(self) -> str:
        cond = re.sub(r"\W+", "-", self.condition.lower()).strip("-")
        return f"{self.source}:{self.store}:{self.product_id}:{cond}"

    @property
    def discount_pct(self) -> Optional[float]:
        if self.price_new and self.price_new > 0:
            return round((1 - self.price_open_box / self.price_new) * 100, 1)
        return None

    @property
    def price_per_tb(self) -> Optional[float]:
        if self.capacity_gb and is_storage(self.category):
            return round(self.price_open_box / (self.capacity_gb / 1000), 2)
        return None

    def to_dict(self) -> dict:
        d = asdict(self)
        d.update(key=self.key, discount_pct=self.discount_pct, price_per_tb=self.price_per_tb)
        return d


@dataclass
class ScopeResult:
    """Outcome of scraping one (source, store, category) slice.

    Only slices that succeeded are used to mark listings as gone, so a failed
    request never looks like everything sold out.
    """

    scope: str
    ok: bool
    listings: list = field(default_factory=list)
    error: str = ""
    signature: str = ""  # what this scope covered; a change → quiet baseline run
    quiet: set = field(default_factory=set)  # listing keys whose "new" event shouldn't alert


def money(text: str) -> Optional[float]:
    if text is None:
        return None
    m = re.search(r"\$?\s*([\d,]+(?:\.\d{1,2})?)", str(text))
    return float(m.group(1).replace(",", "")) if m else None
