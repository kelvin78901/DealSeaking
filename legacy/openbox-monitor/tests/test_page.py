"""Load the rendered page in a real browser: any JS error (e.g. a duplicate
declaration that kills the whole script) fails the test."""
import pytest

from obm.config import load_config
from obm.dashboard import render_dashboard
from obm.models import Listing, ScopeResult
from obm.store import Store


try:  # dev-only dependency: pip install playwright && playwright install chromium
    import playwright  # noqa: F401
    HAVE_PW = True
except ImportError:
    HAVE_PW = False


@pytest.mark.skipif(not HAVE_PW, reason="playwright not installed")
def test_page_renders_without_js_errors(tmp_path):
    from playwright.sync_api import sync_playwright

    p = tmp_path / "config.yaml"
    p.write_text("location: {zip: '20852', radius_miles: 50}\ndatabase: d.db\nlog_file: d.log\n")
    cfg = load_config(p)
    st = Store(cfg.db_path)
    SSD, LAP = "SSD (Solid State Drives)", "Laptops/Notebooks"
    st.apply(ScopeResult("microcenter:Rockville", True, [
        Listing("microcenter", "Rockville", "9", "Samsung 9100 PRO 4TB SSD", "https://x/9", SSD, 1049.96, 1399.99),
        Listing("microcenter", "Rockville", "8", "WD Black SN850X 2TB SSD", "https://x/8", SSD, 139.96, 179.99),
        Listing("microcenter", "Rockville", "7", "Lenovo ThinkPad X1 Carbon", "https://x/7", LAP, 899.96, 1399.99)]))
    st.apply(ScopeResult("microcenter:Fairfax", False, error="BlockedError: 所有指纹都被拦"))
    st.apply(ScopeResult("microcenter:Brooklyn", True, []))  # not a watched store → no chip
    out = render_dashboard(cfg, st)

    with sync_playwright() as pw:
        b = pw.chromium.launch()
        pg = b.new_page()
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(out.as_uri())
        pg.wait_for_timeout(300)
        rows = pg.locator(".row").count()
        chips = pg.inner_text("#runs")
        tags = pg.inner_text("#cats")
        pg.click(f'#cats button[data-c="{LAP}"]')
        pg.wait_for_timeout(100)
        rows_laptop = pg.locator(".row").count()
        laptop_text = pg.inner_text("#list") if pg.locator("#list").count() else pg.content()
        mc = pg.inner_text("#mcBox")
        mc_links = pg.locator("#mcBox .mcl a").count()
        loc = pg.inner_text("#loc")
        settings_hidden = pg.eval_on_selector("#openSettings", "e => e.hidden")
        b.close()

    assert errors == []
    assert rows == 3
    assert SSD in tags and LAP in tags
    assert rows_laptop == 1 and "ThinkPad" in laptop_text      # tag filter works
    assert "Best Buy" not in chips
    assert "Rockville · 3 条" in chips and "Fairfax · 被拦截" in chips and "Brooklyn" not in chips
    assert "Rockville" in mc and "Parkville" in mc and mc_links == 3 * 7   # 'all' → 7 departments per store
    assert "20852" in loc and "Rockville, MD" in loc
    assert settings_hidden  # no daemon behind a file:// page → no settings button
