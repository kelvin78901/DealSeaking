"""Best Buy open box, read from the public website (Best Buy's API isn't open to individuals).

A search page (`/site/searchpage.jsp?st=…&qp=condition_facet=Condition~Open-Box`, or any category
URL you copy from the browser with the Open-Box filter on) server-renders, in its Apollo SSR data:

  search.numFound, search.requestInfo           → total open box results, applied category
  search.withBestMedia.placements[].documentsGridView.sponsoredDocuments[].product
                                                 → the grid (18 per page). Only the first few are
                                                   complete (name, price, link); the rest are stubs
                                                   (sku, bsin, tags) the browser fills in later
  bsinBuyingOptions.priceAndAvailabilityInfo[]   → per-condition open box prices (Excellent/Good/Fair)
                                                   for those first few

Product pages refuse automated requests, so we don't fetch them: only search-page prices are used.
Prices seen before are cached (data/bestbuy_offers.json), so an item that later shows up as a stub
keeps its last known price instead of being reported as sold.
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Iterable, Iterator
import hashlib
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit, urlunsplit

from ..models import Listing, ScopeResult

log = logging.getLogger(__name__)

BASE = "https://www.bestbuy.com"
SEARCH = BASE + "/site/searchpage.jsp"
OPEN_BOX_QP = "condition_facet=Condition~Open-Box"
PAGE_SIZE = 18
COND_ORDER = {"excellent": 0, "good": 1, "fair": 2}

_PUSH = re.compile(
    r'<script[^>]*>\s*\(window\[Symbol\.for\("ApolloSSRDataTransport"\)\]\s*\?\?=\s*\[\]\)\.push\((.*?)\);?\s*</script>', re.S)
_UNDEF = re.compile(r'([:,\[]\s*)undefined(?=\s*[,}\]])')  # JS literal inside otherwise-JSON payloads


# ───────────────────────── page parsing ─────────────────────────
def apollo_payloads(html: str) -> list[dict]:
    out = []
    for m in _PUSH.finditer(html):
        try:
            out.append(json.loads(_UNDEF.sub(r"\1null", m.group(1))))
        except ValueError as e:
            log.debug("skip unparsable Apollo push: %s", e)
    return out


def _datas(payloads: list[dict]) -> Iterator[dict]:
    for p in payloads:
        for ev in p.get("events") or []:
            d = (ev.get("value") or {}).get("data") if ev.get("type") == "next" else None
            if isinstance(d, dict):
                yield d


def _walk(o) -> Iterator[dict]:
    if isinstance(o, dict):
        yield o
        for v in o.values():
            yield from _walk(v)
    elif isinstance(o, list):
        for v in o:
            yield from _walk(v)


def _num(v) -> float | None:
    try:
        return round(float(v), 2) if v is not None else None
    except (TypeError, ValueError):
        return None


def parse_offers(payloads: list[dict]) -> dict[str, dict]:
    """{sku: {"new": price, "offers": [{"condition", "price", "url"}]}} from every bsinBuyingOptions
    found (search pages render a few, a product's open box page renders its own)."""
    out: dict[str, dict] = {}
    for d in _datas(payloads):
        for node in _walk(d):
            infos = node.get("priceAndAvailabilityInfo") if node.get("__typename") == "BsinBuyingOptions" else None
            for i in infos or []:
                sku, cond = str(i.get("skuId") or ""), (i.get("condition") or "").strip()
                prices = i.get("prices") or {}
                price = _num(prices.get("customerPrice"))
                if not sku or price is None:
                    continue
                e = out.setdefault(sku, {"new": None, "offers": []})
                if cond.lower() == "new":
                    e["new"] = e["new"] or _num(prices.get("currentPrice")) or price
                    continue
                if prices.get("openBoxCondition") is None and cond.lower() not in COND_ORDER:
                    continue
                e["new"] = e["new"] or _num(prices.get("currentPrice"))
                if not any(o["condition"] == cond.lower() for o in e["offers"]):
                    e["offers"].append({"condition": cond.lower(), "price": price, "url": i.get("pdpUrl") or ""})
    for e in out.values():
        e["offers"].sort(key=lambda o: COND_ORDER.get(o["condition"], 9))
    return out


def parse_search(html: str) -> dict:
    """→ {"num_found", "category", "products": [...], "offers": parse_offers(...)}.
    Each product: sku, name, brand, price_new, tags, url, ob_codes (open box condition codes on offer)."""
    payloads = apollo_payloads(html)
    num_found, category, products, seen = None, "", [], set()
    for d in _datas(payloads):
        s = d.get("search")
        if not isinstance(s, dict):
            continue
        num_found = s.get("numFound", num_found)
        category = ((s.get("requestInfo") or {}).get("categoryFacet") or {}).get("categoryName") or category
        for pl in ((s.get("withBestMedia") or {}).get("placements") or []):
            grid = pl.get("documentsGridView") or {}
            for doc in grid.get("sponsoredDocuments") or []:
                p = doc.get("product") or {}
                sku = str(p.get("skuId") or "")
                name = (p.get("name") or {}).get("short") if isinstance(p.get("name"), dict) else p.get("name")
                if not sku or sku in seen:
                    continue
                # only the first few grid items are fully rendered; the rest are stubs (sku, bsin, tags)
                # whose name and prices come from their product page
                codes = set()
                for b in p.get("buyingOptions") or []:
                    for o in (b.get("product") or {}).get("openBoxOptions") or []:
                        codes.add(str(o.get("code")))
                seen.add(sku)
                url = (p.get("url") or {}).get("skuSpecificUrl") or (p.get("url") or {}).get("pdp") or ""
                products.append({
                    "sku": sku, "bsin": p.get("bsin") or "", "name": _clean(name),
                    "brand": (p.get("brand") or "").replace("™", "").strip(),
                    "price_new": _num((p.get("price") or {}).get("customerPrice")),
                    "tags": [t for t in (p.get("whatItIs") or []) if t],
                    "url": url, "ob_codes": sorted(codes),
                })
    return {"num_found": num_found, "category": category, "products": products, "offers": parse_offers(payloads)}


def _clean(name) -> str:
    return (name or "").replace("\u2122", "").replace("\u00ae", "").strip()


def search_params(query: str, page: int) -> dict:
    params = {"st": query, "qp": OPEN_BOX_QP}
    if page > 1:
        params["cp"] = page
    return params


def openbox_url(product: dict) -> str:
    if product.get("url"):
        return product["url"].rstrip("/") + "/openbox"
    return f"{BASE}/site/searchpage.jsp?" + urlencode({"st": product["sku"]})


# ───────────────────────── offer cache ─────────────────────────
class OfferCache:
    """Last known open box prices per SKU (search pages only price a few items per page)."""

    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "bestbuy_offers.json"
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError):
            raw = {}
        # {"offers": {sku: entry}, "seen": {sku: first_seen_ts}}; older files were just {sku: entry}
        self.data: dict[str, dict] = raw.get("offers", {}) if "offers" in raw else raw
        self.seen: dict[str, float] = raw.get("seen", {}) if "offers" in raw else {}

    def first_seen(self, sku: str, now: float) -> float:
        return self.seen.setdefault(sku, now)

    def put(self, p: dict, entry: dict, now: float | None = None) -> None:
        self.data[p["sku"]] = {"ts": now or time.time(), **entry}

    def get(self, sku: str) -> dict | None:
        return self.data.get(sku)

    def save(self, keep: Iterable[str]) -> None:
        keep = set(keep)
        self.data = {k: v for k, v in self.data.items() if k in keep}
        self.seen = {k: v for k, v in self.seen.items() if k in keep}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"offers": self.data, "seen": self.seen}))
        tmp.replace(self.path)


# ───────────────────────── scraping ─────────────────────────
def to_listings(p: dict, entry: dict) -> list[Listing]:
    tags = p.get("tags") or entry.get("tags") or []
    name = p.get("name") or entry.get("name")
    if not name:
        return []
    out = []
    for o in entry.get("offers") or []:
        out.append(Listing(
            source="bestbuy", store="online", product_id=p["sku"], name=name, url=o.get("url") or openbox_url(p),
            category=tags[0] if tags else "Best Buy", price_open_box=o["price"],
            price_new=entry.get("new") or p.get("price_new") or entry.get("price_new"),
            condition=o["condition"], brand=p.get("brand") or entry.get("brand") or "", sku=p["sku"],
        ))
    return out


def scope_name(search: str) -> str:
    if search.startswith("http"):
        q = parse_qs(urlsplit(search).query)
        label = (q.get("st") or q.get("id") or [hashlib.sha1(search.encode()).hexdigest()[:8]])[0]
        return f"bestbuy:online:url:{label}"
    return f"bestbuy:online:{search}"


def page_request(search: str, page: int) -> tuple[str, dict | None]:
    """A search term, or a Best Buy URL copied from the browser (category + Open-Box filter)."""
    if not search.startswith("http"):
        return SEARCH, search_params(search, page)
    u = urlsplit(search)
    if not u.netloc.endswith("bestbuy.com"):
        raise ValueError(f"不是 Best Buy 的链接：{search}")
    q = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True) if k != "cp"]
    if page > 1:
        q.append(("cp", str(page)))
    return urlunsplit((u.scheme, u.netloc, u.path, urlencode(q), "")), None


def scrape(cfg: dict, fetcher, first_run: bool = False) -> Iterator[ScopeResult]:
    """One ScopeResult per search ('bestbuy:online:<search>'). Search pages only."""
    bb = cfg["sources"]["bestbuy"]
    cache = OfferCache(cfg.data_dir)
    stale = float(bb.get("keep_price_days", 7)) * 86400
    max_pages = int(bb.get("max_pages", 15))
    keep: set[str] = set()
    try:
        for search in bb.get("searches") or []:
            scope = scope_name(search)
            products: dict[str, dict] = {}
            offers: dict[str, dict] = {}
            try:
                page = 1
                while True:
                    url, params = page_request(search, page)
                    res = parse_search(fetcher.get(url, params=params).text)
                    new = [p for p in res["products"] if p["sku"] not in products]
                    for p in new:
                        products[p["sku"]] = p
                    offers.update(res["offers"])
                    log.info("%s page %d: %d products (of %s, category %s)", scope, page, len(new),
                             res["num_found"], res["category"] or "-")
                    if (not new or page >= max_pages or res["num_found"] is None
                            or page * PAGE_SIZE >= res["num_found"]):
                        break
                    page += 1
            except Exception as e:
                yield ScopeResult(scope, False, error=f"{type(e).__name__}: {e}"[:300])
                continue

            listings: list[Listing] = []
            quiet: set[str] = set()
            now = time.time()
            for p in products.values():
                keep.add(p["sku"])
                was_there = cache.first_seen(p["sku"], now) < now  # in the grid on an earlier round
                had_price = cache.get(p["sku"]) is not None
                if p.get("name") and offers.get(p["sku"], {}).get("offers"):
                    cache.put(p, {**offers[p["sku"]], "name": p["name"], "brand": p["brand"],
                                  "tags": p["tags"], "url": p["url"]}, now)
                entry = cache.get(p["sku"])
                if entry and now - entry.get("ts", 0) < stale:
                    items = to_listings({**p, "url": p.get("url") or entry.get("url", "")}, entry)
                    listings.extend(items)
                    if was_there and not had_price:
                        # it only just landed in the few priced slots of a page; not a new arrival
                        quiet.update(i.key for i in items)
            priced = len({i.product_id for i in listings})
            log.info("%s: %d products, %d with open box prices", scope, len(products), priced)
            yield ScopeResult(scope, True, listings, signature=json.dumps(bb.get("searches")), quiet=quiet)
    finally:
        cache.save(keep)
