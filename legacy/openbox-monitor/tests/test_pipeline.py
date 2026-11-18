"""End-to-end with Micro Center-shaped data: diff across runs → tag-based alerts → dashboard."""
import copy

import pytest

from obm.config import load_config
from obm.models import Listing, ScopeResult, is_storage, parse_capacity_gb
from obm.rules import passes_filters
from obm.runner import run_once
from obm.store import Store

SSD = "SSD (Solid State Drives)"
LAPTOP = "Laptops/Notebooks"


class FakeNotifier:
    def __init__(self):
        self.sent = []

    def send(self, msg):
        self.sent.append(msg)
        return {}


@pytest.fixture
def cfg(tmp_path):
    p = tmp_path / "config.yaml"
    p.write_text(
        "interval_minutes: 60\nquiet_hours: []\nlocation: {zip: '20852', radius_miles: 30}\n"
        "alerts: {min_drop_pct: 3, watchlist: ['ThinkPad']}\n"
        "dashboard: {path: out/dashboard.html}\n"
        "database: data/test.db\nlog_file: data/test.log\n"
    )
    return load_config(p)


def L(pid, name, price, tag, new=None, store="Rockville"):
    return Listing("microcenter", store, pid, name, f"https://www.microcenter.com/product/{pid}/x?ob=1", tag, price, new)


def test_capacity_only_for_storage():
    assert parse_capacity_gb("Samsung 9100 PRO 4TB ...") == 4000
    assert parse_capacity_gb("Mac mini M6 16GB Memory 512GB SSD") == 512
    ssd = L("1", "Samsung 990 PRO 2TB", 150.0, SSD)
    laptop = L("2", "ASUS Vivobook 16GB RAM 1TB SSD Laptop", 500.0, LAPTOP)
    assert ssd.price_per_tb == 75.0
    assert laptop.capacity_gb is None and laptop.price_per_tb is None  # no $/TB for a laptop
    assert is_storage("Flash Memory") and not is_storage("Enclosures/Trays")


def test_filters_by_tag():
    d = L("1", "Inland 2TB SSD", 100.0, SSD, 150.0).to_dict()
    assert passes_filters(d, {"tags": [SSD]})
    assert not passes_filters(d, {"tags": [LAPTOP]})
    assert passes_filters(d, {"tags": []})  # empty = all tags
    assert not passes_filters(d, {"max_price_per_tb": 40})


def test_multi_run_alerts_follow_tags(cfg):
    n = FakeNotifier()
    scope = "microcenter:Rockville"
    base = [L("1", "Samsung 990 PRO 2TB", 150.0, SSD, 180.0),
            L("2", "Lenovo ThinkPad X1 Carbon", 999.0, LAPTOP, 1399.0),
            L("3", "ASUS Vivobook 15", 399.0, LAPTOP, 549.0)]

    s1 = run_once(cfg, results=[ScopeResult(scope, True, base, signature="all")], notifier=n, force=True)
    assert s1["events"]["new"] == 3 and s1["alerts"] == 0 and "已启动" in n.sent[0]["title"]

    # new SSD (storage tag → push), new laptop (not a push tag → silent), ThinkPad drop (watchlist → push)
    n.sent.clear()
    r2 = copy.deepcopy(base)
    r2[1].price_open_box = 899.0
    r2 += [L("4", "WD Black SN850X 4TB", 239.96, SSD, 299.99), L("5", "HP Pavilion 14", 299.0, LAPTOP)]
    s2 = run_once(cfg, results=[ScopeResult(scope, True, r2, signature="all")], notifier=n, force=True)
    assert s2["events"]["new"] == 2 and s2["events"]["price_drop"] == 1
    bodies = "\n".join(m["body"] for m in n.sent)
    assert s2["alerts"] == 2 and "SN850X" in bodies and "ThinkPad" in bodies and "Pavilion" not in bodies

    # a failed run must not mark anything sold
    s3 = run_once(cfg, results=[ScopeResult(scope, False, error="BlockedError")], notifier=n, force=True)
    assert s3["events"]["gone"] == 0

    html = cfg.dashboard_path.read_text()
    assert "SN850X" in html and "Pavilion" in html  # everything is stored and shown, alerts are filtered


def test_widening_scope_is_a_quiet_baseline(cfg):
    n = FakeNotifier()
    scope = "microcenter:Rockville"
    ssd_only = [L("1", "Samsung 990 PRO 2TB", 150.0, SSD)]
    run_once(cfg, results=[ScopeResult(scope, True, ssd_only, signature='["ssd"]')], notifier=n, force=True)
    n.sent.clear()
    # user switches to "all open box": 40 storage items appear that were always there → no push storm
    wider = ssd_only + [L(str(100 + i), f"Drive {i} 2TB SSD", 99.0, SSD) for i in range(40)]
    s = run_once(cfg, results=[ScopeResult(scope, True, wider, signature='["all"]')], notifier=n, force=True)
    assert s["events"]["new"] == 40 and s["alerts"] == 0 and n.sent == []
    # next run with the same scope: alerts work normally again
    s = run_once(cfg, results=[ScopeResult(scope, True, wider + [L("999", "New 4TB SSD", 199.0, SSD)], signature='["all"]')],
                 notifier=n, force=True)
    assert s["alerts"] == 1


def test_old_per_category_rows_migrate_to_store_scope(tmp_path):
    st = Store(tmp_path / "t.db")
    old = Listing("microcenter", "Fairfax", "707193", "Crucial P310 1TB", "u", "ssd_internal", 159.96, 199.99)
    gone_later = Listing("microcenter", "Fairfax", "656521", "Samsung T7 1TB", "u", "ssd_external", 183.96, 229.99)
    st.apply(ScopeResult("microcenter:Fairfax:ssd", True, [old, gone_later]), now=1000)

    new = copy.deepcopy(old)
    new.category = SSD  # now tagged with the store's own category name
    ev = st.apply(ScopeResult("microcenter:Fairfax", True, [new]), now=2000)
    rows = {r["key"]: dict(r) for r in st.db.execute("SELECT * FROM listings")}
    assert rows[new.key]["scope"] == "microcenter:Fairfax" and rows[new.key]["category"] == SSD
    assert [e.kind for e in ev] == ["gone"] and not rows[gone_later.key]["active"]


def test_max_per_run_summary(cfg):
    n = FakeNotifier()
    run_once(cfg, results=[ScopeResult("microcenter:Rockville", True, [], signature="all")], notifier=n, force=True)
    many = [L(str(1000 + k), f"Drive {k} 2TB SSD", 100.0 + k, SSD) for k in range(10)]
    n.sent.clear()
    run_once(cfg, results=[ScopeResult("microcenter:Rockville", True, many, signature="all")], notifier=n, force=True)
    assert len(n.sent) == 6 and "还有 5 条" in n.sent[-1]["title"]
