"""Micro Center: per-store Open Box search pages, plus quick links for the page.

URL shape: /search/search_results.aspx?fq=category:<Name>|<id>,Valuable Links:Open Box&storeid=085
Each result card shows "<n> IN STOCK at <Store> Store" (new-item stock), "Regular price $X"
and "Open Box From $Y". Open box stock is per store, so each store is queried.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Iterable
from urllib.parse import urlencode, urljoin

from bs4 import BeautifulSoup

from ..models import Listing, ScopeResult, money

log = logging.getLogger(__name__)

BASE = "https://www.microcenter.com"
SEARCH = BASE + "/search/search_results.aspx"

# Department-level open box scopes (from microcenter.com's Clearance & Refurb → Open Box menu)
MC_DEPARTMENTS = {
    "computers": "Computers|92",
    "apple": "Apple|93",
    "computer_parts": "Computer Parts|94",
    "electronics": "Electronics|95",
    "networking": "Networking` Modems|96",
    "gaming": "Gaming|98",
    "accessories": "Accessories|103",
}
DEPARTMENT_LABELS = {
    "computers": "电脑（笔记本/台式）", "apple": "Apple", "computer_parts": "电脑配件（CPU/显卡/存储…）",
    "electronics": "电子产品（显示器/电视…）", "networking": "网络设备", "gaming": "游戏", "accessories": "配件",
}

# Finer storage scopes
MC_CATEGORIES = {
    "ssd": "SSD (Solid State Drives)|512",
    "hdd_internal": "Internal Hard Drives|511",
    "hdd_external": "External Hard Drives|514",
    "nas": "NAS (Network Attached Storage)|203",
    "usb_flash": "USB Flash Drives|586",
    "memory_card": "Flash Memory|585",
    "enclosure": "Enclosures/Trays|156",
}
LINK_LABELS = {
    "ssd": "SSD", "hdd_internal": "内置机械盘", "hdd_external": "移动机械盘", "nas": "NAS",
    "usb_flash": "U 盘", "memory_card": "存储卡", "enclosure": "硬盘盒",
}

_PRODUCT_HREF = re.compile(r"/product/(\d+)/")
_RANGE = re.compile(r"(\d+)\s*-\s*(\d+)\s+of\s+([\d,]+)\s+items", re.I)
_OPENBOX = re.compile(r"Open\s*Box(?:\s*From)?\s*:?\s*\$\s*([\d,]+\.\d{2})", re.I)
_REGULAR = re.compile(r"Regular\s*price\s*:?\s*\$\s*([\d,]+\.\d{2})", re.I)
_STOCK = re.compile(r"(\d+)\s*(\+)?\s*IN\s*STOCK", re.I)
_SKU = re.compile(r"SKU:?\s*(\d{5,7})", re.I)
# analytics objects at the bottom of the page:
#   {'name': '...', 'id': '661858', 'price': '59.99', 'brand': 'Inland', 'category': 'SSD (Solid State Drives)', ...}
_OBJ = re.compile(r"\{\s*'name':.*?\}", re.S)
_KV = re.compile(r"'(\w+)':\s*'((?:[^'\\]|\\.)*)'")


def _analytics(html: str) -> dict[str, dict]:
    out = {}
    for m in _OBJ.finditer(html):
        kv = {k: v.replace("\\'", "'") for k, v in _KV.findall(m.group(0))}
        if kv.get("id", "").isdigit():
            out[kv["id"]] = kv
    return out


def scope_fqs(categories) -> list[tuple[str | None, str]]:
    """config categories → [(fq category filter or None for all, fallback tag)]."""
    if categories in (None, "", "all") or (isinstance(categories, list) and "all" in categories):
        return [(None, "其他")]
    out = []
    for c in categories:
        fq = MC_DEPARTMENTS.get(c) or MC_CATEGORIES.get(c) or (c if "|" in str(c) else None)
        if fq is None:
            log.warning("未知的 Micro Center 分类 %r，已跳过", c)
            continue
        out.append((fq, fq.split("|")[0].replace("`", ",")))
    return out


# ───────────────────────── links (always shown on the page) ─────────────────────────
def open_box_url(category_key: str, store_id: str) -> str:
    return SEARCH + "?" + urlencode({
        "fq": f"category:{MC_CATEGORIES[category_key]},Valuable Links:Open Box",
        "storeid": store_id,
        "sortby": "pricelow",
    })


def store_links(cfg: dict) -> list[dict]:
    """[{store, id, distance, all_url, links:[{key,label,url}]}] for the configured stores."""
    mc = cfg["sources"]["microcenter"]
    if not mc.get("enabled", True):
        return []
    dist = cfg["location"].get("_distances") or {}
    cats = mc["categories"]
    if cats in (None, "", "all") or (isinstance(cats, list) and "all" in cats):
        pairs = [(k, DEPARTMENT_LABELS[k], v) for k, v in MC_DEPARTMENTS.items()]
    else:
        pairs = [(c, LINK_LABELS.get(c) or DEPARTMENT_LABELS.get(c) or c.split("|")[0],
                  MC_CATEGORIES.get(c) or MC_DEPARTMENTS.get(c) or c) for c in cats if c in MC_CATEGORIES or c in MC_DEPARTMENTS or "|" in str(c)]

    def url(fq, sid):
        return SEARCH + "?" + urlencode({"fq": f"category:{fq},Valuable Links:Open Box", "storeid": sid, "sortby": "pricelow"})

    return [{
        "store": name, "id": sid, "distance": dist.get(name),
        "all_url": SEARCH + "?" + urlencode({"fq": "Valuable Links:Open Box", "storeid": sid}),
        "links": [{"key": k, "label": lab.split("（")[0], "url": url(fq, sid)} for k, lab, fq in pairs],
    } for name, sid in mc["_stores"]]


# ───────────────────────── scraping ─────────────────────────
def build_params(fq: str | None, store_id: str, page: int, include_oos: bool) -> dict:
    """fq: a category filter like 'SSD (Solid State Drives)|512', a key from MC_CATEGORIES, or None for all."""
    fq = MC_CATEGORIES.get(fq, fq) if fq else None
    return {
        "fq": f"category:{fq},Valuable Links:Open Box" if fq else "Valuable Links:Open Box",
        "storeid": store_id,
        "rpp": 96,
        "page": page,
        "myStore": "false" if include_oos else "true",
    }


def _cards(soup: BeautifulSoup) -> list:
    cards = soup.select("li.product_wrapper")
    if cards:
        return cards
    # Fallback if class names change: group by the product link's <li>/<article> container.
    seen, out = set(), []
    for a in soup.find_all("a", href=_PRODUCT_HREF):
        pid = _PRODUCT_HREF.search(a["href"]).group(1)
        if pid in seen:
            continue
        box = a.find_parent("li") or a.find_parent("article")
        if box is None:
            node = a
            for _ in range(8):
                node = node.parent
                if node is None:
                    break
                if "IN STOCK" in node.get_text(" ").upper() or _OPENBOX.search(node.get_text(" ")):
                    box = node
                    break
        if box is not None:
            seen.add(pid)
            out.append(box)
    return out


def product_url(href: str, store_id: str = "") -> str:
    """Product link that opens with the store selected — without storeid the page shows only the
    new price and hides that store's open box offers."""
    url = urljoin(BASE, href)
    if "ob=1" not in url:
        url += ("&" if "?" in url else "?") + "ob=1"
    if store_id and "storeid=" not in url:
        url += f"&storeid={store_id}"
    return url


def parse_page(html: str, store_name: str, fallback_tag: str, store_id: str = "") -> tuple[list[Listing], tuple[int, int, int] | None]:
    """fallback_tag is used when the page's analytics data has no category for an item.
    For backwards compatibility a storage key like 'ssd' maps to its Micro Center name."""
    soup = BeautifulSoup(html, "html.parser")
    meta = _analytics(html)
    if fallback_tag in MC_CATEGORIES:
        fallback_tag = MC_CATEGORIES[fallback_tag].split("|")[0]

    listings: list[Listing] = []
    for card in _cards(soup):
        text = re.sub(r"\s+", " ", card.get_text(" ", strip=True))
        link = card.find("a", href=_PRODUCT_HREF)
        if not link:
            continue
        pid = _PRODUCT_HREF.search(link["href"]).group(1)
        ob = _OPENBOX.search(text)
        if not ob:
            continue  # not an open box offer
        price_ob = money(ob.group(1))

        info = meta.get(pid, {})
        tagged = card.find(attrs={"data-name": True})
        brand = (tagged.get("data-brand") if tagged else None) or info.get("brand", "")
        name = tagged.get("data-name") if tagged is not None else None
        if not name:
            h = card.find(["h2", "h3"])
            name = h.get_text(" ", strip=True) if h else ""
        if not name:
            name = max((a.get_text(" ", strip=True) for a in card.find_all("a", href=_PRODUCT_HREF)), key=len, default="")
        name = name.replace("\\'", "'").strip()
        if not brand and name:
            brand = name.split()[0]
        if brand and not name.lower().startswith(brand.lower()):
            name = f"{brand} {name}"

        price_new = money(tagged.get("data-price")) if tagged is not None and tagged.get("data-price") else None
        if price_new is None:
            m = _REGULAR.search(text)
            price_new = money(m.group(1)) if m else money(info.get("price"))

        stock, stock_text = None, ""
        m = _STOCK.search(text)
        if m:
            stock = int(m.group(1))
            stock_text = f"{m.group(1)}{'+' if m.group(2) else ''} in stock"
        elif "SOLD OUT" in text.upper():
            stock, stock_text = 0, "sold out"

        sku_m = _SKU.search(text)
        url = product_url(link["href"], store_id)

        listings.append(Listing(
            source="microcenter", store=store_name, product_id=pid, sku=sku_m.group(1) if sku_m else "",
            name=name, brand=brand, url=url, category=((tagged.get("data-category") if tagged is not None else None) or info.get("category") or fallback_tag).strip(),
            price_open_box=price_ob, price_new=price_new, condition="open box",
            stock=stock, stock_text=stock_text,
        ))

    rng = None
    m = _RANGE.search(soup.get_text(" "))
    if m:
        rng = (int(m.group(1)), int(m.group(2)), int(m.group(3).replace(",", "")))
    return listings, rng


def _short(e: Exception) -> str:
    return f"{type(e).__name__}: {str(e)[:150]}"


MAX_PAGES = 50  # 96 per page → up to 4,800 open box items per store per scope


def scrape(cfg: dict, fetcher) -> Iterable[ScopeResult]:
    """One ScopeResult per store. If any page for a store fails, the whole store is
    reported failed so nothing there gets wrongly marked as sold."""
    mc = cfg["sources"]["microcenter"]
    targets = scope_fqs(mc["categories"])
    consecutive_fail = 0
    for store_name, store_id in mc["_stores"]:
        scope = f"microcenter:{store_name}"
        if consecutive_fail >= 2:
            yield ScopeResult(scope, False, error="skipped: 前面连续失败")
            continue
        try:
            found: dict[str, Listing] = {}
            for fq, tag in targets:
                page = 1
                while True:
                    r = fetcher.get(SEARCH, params=build_params(fq, store_id, page, mc["include_out_of_stock"]))
                    items, rng = parse_page(r.text, store_name, tag, store_id)
                    for it in items:
                        found[it.key] = it
                    log.info("%s %s page %d: %d items (range=%s)", scope, tag if fq else "all", page, len(items), rng)
                    if not rng or rng[1] >= rng[2] or not items or page >= MAX_PAGES:
                        break
                    page += 1
            consecutive_fail = 0
            yield ScopeResult(scope, True, list(found.values()), signature=json.dumps([fq for fq, _ in targets]))
        except Exception as e:
            consecutive_fail += 1
            log.error("%s failed: %s", scope, e)
            yield ScopeResult(scope, False, error=_short(e))
