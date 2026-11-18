"""Micro Center parsing + fingerprint rotation against a local stand-in site."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from obm.models import ScopeResult
from obm.scrape_http import BlockedError, RotatingFetcher
from obm.sources.microcenter import parse_page

FIX = Path(__file__).parent / "fixtures"


def test_parse_real_layout():
    items, rng = parse_page((FIX / "mc_rockville_ssd.html").read_text(), "Rockville", "其他")
    assert rng == (1, 3, 3) and len(items) == 3
    assert items[0].category == "SSD (Solid State Drives)"   # tag from the page's analytics data
    pro = items[2]                                           # not in analytics → fallback tag
    assert pro.category == "其他"
    assert pro.name.startswith("Samsung 9100 PRO 4TB") and pro.brand == "Samsung"
    assert pro.price_open_box == 1049.96 and pro.price_new == 1399.99 and pro.discount_pct == 25.0
    assert pro.stock == 2 and pro.capacity_gb is None       # unknown tag → not treated as storage
    assert pro.url.startswith("https://www.microcenter.com/product/691304/") and "ob=1" in pro.url
    items, _ = parse_page((FIX / "mc_rockville_ssd.html").read_text(), "Rockville", "其他", "085")
    assert items[2].url.endswith("?ob=1&storeid=085")   # opens with the store selected → its open box price shows
    # legacy storage key as the fallback still maps to Micro Center's name
    items, _ = parse_page((FIX / "mc_rockville_ssd.html").read_text(), "Rockville", "ssd")
    assert items[2].category == "SSD (Solid State Drives)" and items[2].capacity_gb == 4000


def _card(pid, name, ob, tag):
    return (f'<li class="product_wrapper"><h2><a href="/product/{pid}/x?ob=1" data-name="{name}" data-price="{ob + 100:.2f}" '
            f'data-brand="{name.split()[0]}">{name}</a></h2><div>1 IN STOCK</div><div>Open Box From ${ob:.2f}</div></li>',
            f"{{'name': '{name}', 'id': '{pid}', 'price': '{ob + 100:.2f}', 'brand': '{name.split()[0]}', 'category': '{tag}'}}")


ALL_ITEMS = [("1001", "Samsung 990 PRO 2TB", 139.96, "SSD (Solid State Drives)"),
             ("1002", "Lenovo ThinkPad X1", 899.96, "Laptops/Notebooks"),
             ("1003", "WD Elements 16TB", 219.96, "External Hard Drives"),
             ("1004", "ASUS RTX 5070 Ti", 649.96, "Graphics Cards"),
             ("1005", "LG 27GR93U Monitor", 399.96, "Computer Monitors")]


class AllOpenBox(BaseHTTPRequestHandler):
    """Full-store open box listing, 3 per page, Safari-only like the real site."""
    def log_message(self, *a):
        pass

    def do_GET(self):
        from urllib.parse import parse_qs, urlsplit
        ua = self.headers.get("User-Agent") or ""
        q = parse_qs(urlsplit(self.path).query)
        if "Chrome" in ua or "Firefox" in ua:
            self.send_response(403); self.end_headers(); return
        if self.path == "/":
            body = b"home"
        else:
            assert q["fq"] == ["Valuable Links:Open Box"], q   # 'all' scope → no category filter
            page = int(q.get("page", ["1"])[0])
            chunk = ALL_ITEMS[(page - 1) * 3: page * 3]
            cards, objs = zip(*[_card(*it) for it in chunk])
            body = (f"<p>{(page - 1) * 3 + 1} - {(page - 1) * 3 + len(chunk)} of {len(ALL_ITEMS)} items</p>"
                    f"<ul>{''.join(cards)}</ul><script>var dl=[{','.join(objs)}];</script>").encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def test_scrape_all_open_box_with_tags(tmp_path, monkeypatch):
    import obm.sources.microcenter as mc
    srv = ThreadingHTTPServer(("127.0.0.1", 0), AllOpenBox)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(mc, "SEARCH", f"http://127.0.0.1:{srv.server_address[1]}/search/search_results.aspx")
    cfg = {"sources": {"microcenter": {"categories": "all", "include_out_of_stock": False,
                                       "_stores": [("Rockville", "085")]}}}
    f = RotatingFetcher(tmp_path, delay=0, profiles=["chrome", "safari"])
    try:
        (res,) = list(mc.scrape(cfg, f))
    finally:
        f.close()
        srv.shutdown()
    assert res.ok and res.scope == "microcenter:Rockville" and len(res.listings) == 5   # both pages
    tags = {i.name: i.category for i in res.listings}
    assert tags["Lenovo ThinkPad X1"] == "Laptops/Notebooks" and tags["ASUS RTX 5070 Ti"] == "Graphics Cards"
    ssd = next(i for i in res.listings if i.product_id == "1001")
    assert ssd.price_per_tb == 69.98
    assert res.signature == "[null]"
    assert all(i.url.endswith("storeid=085") for i in res.listings)


class Site(BaseHTTPRequestHandler):
    """403 for Chrome/Firefox-looking clients, content for Safari — the pattern seen on Micro Center."""
    seen = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        ua = self.headers.get("User-Agent") or ""
        if self.path != "/":
            Site.seen.append(ua)
        ok = "Safari" in ua and "Chrome" not in ua and "Firefox" not in ua
        body = (FIX / "mc_rockville_ssd.html").read_bytes() if ok else b"<title>Access Denied</title>"
        self.send_response(200 if ok else 403)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def site():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def test_rotates_until_unblocked_and_remembers(site, tmp_path):
    f = RotatingFetcher(tmp_path, delay=0, profiles=["chrome", "edge", "safari", "firefox"])
    r = f.get(site + "/search/search_results.aspx", {"storeid": "085"})
    f.close()
    items, _ = parse_page(r.text, "Rockville", "ssd")
    assert len(items) == 3
    assert json.loads((tmp_path / "fetch_state.json").read_text())["profile"] == "safari"

    # next run starts with the remembered profile: a single request, no 403s
    Site.seen.clear()
    f2 = RotatingFetcher(tmp_path, delay=0, profiles=["chrome", "edge", "safari", "firefox"])
    assert f2.profiles[0] == "safari"
    f2.get(site + "/search/search_results.aspx")
    f2.close()
    assert len(Site.seen) == 1


def test_all_blocked_stops_for_the_round(site, tmp_path):
    f = RotatingFetcher(tmp_path, delay=0, profiles=["chrome", "firefox"])
    with pytest.raises(BlockedError):
        f.get(site + "/search/search_results.aspx")
    Site.seen.clear()
    with pytest.raises(BlockedError):
        f.get(site + "/search/search_results.aspx")
    assert Site.seen == []  # exhausted → no more requests this round
    assert not (tmp_path / "fetch_state.json").exists()
