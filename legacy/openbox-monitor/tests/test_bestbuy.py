"""Best Buy, search pages only: parse the Apollo data (trimmed from a real page), remember prices
of items that drift out of the few priced slots, and don't call an item 'new' just because it
got a price on this round. Product pages are never requested (they're behind bot protection)."""
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from obm.config import load_config
from obm.models import ScopeResult
from obm.runner import run_once
from obm.scrape_http import RotatingFetcher
from obm.sources import bestbuy as bb

FIX = Path(__file__).parent / "fixtures"
SEARCH_HTML = (FIX / "bb_search_ssd.html").read_text(encoding="utf-8")


def test_parse_search_page():
    res = bb.parse_search(SEARCH_HTML)
    assert res["num_found"] == 215 and res["category"] == "Computers & Tablets"
    prods = {p["sku"]: p for p in res["products"]}
    assert len(prods) == 18
    assert len([p for p in prods.values() if p["name"]]) == 4  # only the first few are fully rendered
    mac = prods["6566933"]
    assert mac["name"].startswith("Apple - Mac mini") and mac["price_new"] == 899.0
    assert mac["tags"][0] == "Desktop Computer Tower" and mac["url"].endswith("/sku/6566933")
    offers = res["offers"]["6566933"]
    assert [(o["condition"], o["price"]) for o in offers["offers"]] == [("excellent", 804.99), ("good", 795.99), ("fair", 785.99)]
    assert offers["offers"][0]["url"].endswith("/openbox?condition=excellent")
    stub = prods["6615874"]
    assert stub["name"] == "" and stub["tags"][0] == "Laptop Computer"
    assert "™" not in prods["6500317"]["name"]


def test_copied_url_and_scope_names():
    url = ("https://www.bestbuy.com/site/searchpage.jsp?id=pcat17071&qp=condition_facet%3DCondition~Open-Box"
           "&st=categoryid%24abcat0504001&cp=3")
    u, params = bb.page_request(url, 2)
    assert params is None and "cp=2" in u and "cp=3" not in u and "categoryid%24abcat0504001" in u
    assert bb.scope_name("ssd") == "bestbuy:online:ssd"
    assert bb.scope_name(url).startswith("bestbuy:online:url:")
    with pytest.raises(ValueError):
        bb.page_request("https://example.com/x", 1)


# ── a stand-in search page whose priced slots we control ──
def page(items, priced):
    """items: [(sku, name, new_price, tag)], priced: skus fully rendered with offers."""
    docs = []
    for sku, name, new, tag in items:
        p = {"__typename": "Product", "skuId": sku, "bsin": "B" + sku, "whatItIs": [tag]}
        if sku in priced:
            p.update({"name": {"short": name}, "brand": name.split()[0], "price": {"customerPrice": new},
                      "url": {"skuSpecificUrl": f"https://www.bestbuy.com/product/x/B{sku}/sku/{sku}"}})
        docs.append({"product": p})
    data = [{"search": {"numFound": len(items), "requestInfo": {"categoryFacet": {"categoryName": "Storage"}},
                        "withBestMedia": {"placements": [{"documentsGridView": {"sponsoredDocuments": docs}}]}}}]
    for sku, name, new, tag in items:
        if sku in priced:
            data.append({"bsinBuyingOptions": {"__typename": "BsinBuyingOptions", "bsin": "B" + sku, "priceAndAvailabilityInfo": [
                {"condition": "Excellent", "skuId": sku, "pdpUrl": f"https://www.bestbuy.com/x/{sku}/openbox?condition=excellent",
                 "prices": {"currentPrice": new, "customerPrice": round(new * 0.8, 2), "openBoxCondition": 2}}]}})
    push = lambda d: ('<script>(window[Symbol.for("ApolloSSRDataTransport")] ??= []).push('
                      + json.dumps({"rehydrate": {}, "events": [{"type": "next", "value": {"data": d}}]}) + ");</script>")
    return "<html><body>" + "".join(push(d) for d in data) + "</body></html>"


ITEMS = [("101", "Samsung 990 PRO 2TB SSD", 180.0, "Internal Solid State Drive"),
         ("102", "WD Black SN850X 4TB SSD", 300.0, "Internal Solid State Drive"),
         ("103", "Seagate 20TB Hard Drive", 350.0, "Internal Hard Drive"),
         ("104", "SanDisk 1TB Portable SSD", 99.0, "Portable Solid State Drive")]


class Site(BaseHTTPRequestHandler):
    body = ""
    hits = []

    def log_message(self, *a):
        pass

    def do_GET(self):
        u = urlsplit(self.path)
        if u.path != "/":
            Site.hits.append(self.path)
            assert u.path == "/site/searchpage.jsp", "only search pages may be requested"
            assert parse_qs(u.query)["qp"] == ["condition_facet=Condition~Open-Box"]
        b = (Site.body if u.path != "/" else "home").encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)


@pytest.fixture
def site(monkeypatch):
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Site)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(bb, "SEARCH", f"http://127.0.0.1:{srv.server_address[1]}/site/searchpage.jsp")
    Site.hits = []
    yield
    srv.shutdown()


@pytest.fixture
def cfg(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text("location: {zip: '20852'}\ndatabase: data/t.db\nlog_file: data/t.log\nnotify: {macos: false}\n"
                 "filters: {tags: []}\n"
                 "sources: {microcenter: {scrape: false}, bestbuy: {enabled: true, searches: [ssd], max_pages: 1}}\n")
    return load_config(p)


def scrape(cfg):
    f = RotatingFetcher(cfg.data_dir, delay=0, profiles=["safari"], state_name="fetch_state_bestbuy.json")
    try:
        return list(bb.scrape(cfg, f))
    finally:
        f.close()


def test_prices_are_remembered_and_late_priced_items_are_quiet(site, cfg):
    Site.body = page(ITEMS, priced={"101", "102"})
    (r1,) = scrape(cfg)
    assert {i.product_id for i in r1.listings} == {"101", "102"} and r1.quiet == set()
    assert Site.hits and all(h.startswith("/site/searchpage.jsp") for h in Site.hits)

    # next round the grid shifted: 103/104 got the priced slots, 101/102 didn't
    Site.body = page(ITEMS, priced={"103", "104"})
    (r2,) = scrape(cfg)
    assert {i.product_id for i in r2.listings} == {"101", "102", "103", "104"}  # 101/102 from memory
    assert r2.quiet == {"bestbuy:online:103:excellent", "bestbuy:online:104:excellent"}  # were already there

    # a SKU never seen before that shows up with a price is genuinely new
    Site.body = page(ITEMS + [("105", "Crucial 4TB SSD", 250.0, "Internal Solid State Drive")], priced={"105"})
    (r3,) = scrape(cfg)
    assert "bestbuy:online:105:excellent" not in r3.quiet

    # gone from the grid → no listing (sold), even though a price is remembered
    Site.body = page(ITEMS[1:], priced=set())
    (r4,) = scrape(cfg)
    assert {i.product_id for i in r4.listings} == {"102", "103", "104"}


def test_runner_alerts_only_real_arrivals(site, cfg):
    class N:
        sent = []

        def send(self, m):
            self.sent.append(m)
            return {}

    n = N()
    Site.body = page(ITEMS, priced={"101", "102"})
    run_once(cfg, force=True, notifier=n)  # first round: quiet baseline
    n.sent.clear()
    Site.body = page(ITEMS + [("105", "Crucial 4TB SSD", 250.0, "Internal Solid State Drive")], priced={"103", "105"})
    s = run_once(cfg, force=True, notifier=n)
    assert s["events"]["new"] == 2  # 103 (newly priced) and 105 (really new) both stored
    bodies = " ".join(m["title"] + m["body"] for m in n.sent)
    assert s["alerts"] == 1 and "Crucial" in bodies and "Seagate" not in bodies
