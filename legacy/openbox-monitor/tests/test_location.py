import pytest
import yaml

from obm.config import apply_update, load_config, public_view, save_raw
from obm.locations import find_store, stores_near


def names(zip_code, r):
    return [s.name for s, _ in stores_near(zip_code, r)]


def test_stores_near():
    assert names("20852", 30) == ["Rockville", "Fairfax"]
    assert names("20852", 50) == ["Rockville", "Fairfax", "Parkville"]
    assert set(names("10001", 25)) == {"Brooklyn", "Flushing", "Yonkers", "North Jersey", "Westbury"}
    assert names("94103", 50) == ["Santa Clara"]
    assert names("59718", 50) == ["Denver"]  # Bozeman, MT: nothing in range → nearest store
    assert find_store("085").name == "Rockville" and find_store("st louis park").id == "045"


def _cfg(tmp_path, text):
    p = tmp_path / "config.yaml"
    p.write_text(text)
    return load_config(p)


def test_auto_vs_explicit(tmp_path):
    c = _cfg(tmp_path, "location: {zip: '77429', radius_miles: 40}\n")
    assert [n for n, _ in c["sources"]["microcenter"]["_stores"]] == ["Houston"]
    assert c["location"]["_label"] == "Cypress, TX"
    c = _cfg(tmp_path, "location: {zip: '77429'}\nsources: {microcenter: {stores: [Dallas, '029']}}\n")
    assert c["sources"]["microcenter"]["_stores"] == [("Dallas", "131"), ("Shippable", "029")]
    assert c["location"]["_distances"]["Dallas"] > 200
    with pytest.raises(ValueError):
        _cfg(tmp_path, "sources: {microcenter: {stores: auto}}\n")  # auto needs a ZIP
    with pytest.raises(ValueError):
        _cfg(tmp_path, "location: {zip: '00000'}\n")


def test_settings_roundtrip_keeps_secrets(tmp_path):
    c = _cfg(tmp_path, "location: {zip: '20852'}\nnotify:\n  ntfy: {enabled: true, topic: secret-topic}\n"
                       "  telegram: {enabled: true, bot_token: TOKEN123, chat_id: '42'}\n")
    view = public_view(c)
    assert view["notify"]["ntfy"]["topic"] == "" and view["notify"]["ntfy"]["topic_set"] is True
    assert view["notify"]["telegram"]["bot_token"] == "" and "_stores" not in view["sources"]["microcenter"]
    assert "api_key" not in view["sources"]["bestbuy"]  # old API key never reaches the page

    # the page sends back blanks for untouched secrets, plus real changes
    view["location"] = {"zip": "07504", "radius_miles": 20}
    view["interval_minutes"] = 45
    raw = apply_update(c, view)
    save_raw(c, raw)
    c2 = load_config(tmp_path / "config.yaml")
    assert c2["notify"]["ntfy"]["topic"] == "secret-topic" and c2["notify"]["telegram"]["bot_token"] == "TOKEN123"
    assert c2["location"]["zip"] == "07504"  # stays a string with its leading zero
    assert [n for n, _ in c2["sources"]["microcenter"]["_stores"]][0] == "North Jersey"
    assert c2["interval_minutes"] == 45
    assert (tmp_path / "config.yaml.bak").exists()

    # invalid update is rejected before anything is written
    bad = public_view(c2)
    bad["location"]["zip"] = "abcde"
    before = (tmp_path / "config.yaml").read_text()
    with pytest.raises(ValueError):
        apply_update(c2, bad)
    assert (tmp_path / "config.yaml").read_text() == before
