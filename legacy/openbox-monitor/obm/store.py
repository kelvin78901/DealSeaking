"""SQLite state: current listings, price history, events, run log."""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from .models import Listing, ScopeResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
  key TEXT PRIMARY KEY,
  scope TEXT NOT NULL,
  source TEXT, store TEXT, product_id TEXT, sku TEXT,
  name TEXT, brand TEXT, url TEXT, category TEXT, condition TEXT,
  capacity_gb REAL, price_new REAL, price_ob REAL, stock INTEGER, stock_text TEXT,
  first_seen REAL, last_seen REAL, last_change REAL,
  active INTEGER DEFAULT 1,
  low_price REAL
);
CREATE INDEX IF NOT EXISTS idx_listings_scope ON listings(scope);
CREATE TABLE IF NOT EXISTS price_history (
  key TEXT, ts REAL, price_ob REAL, stock INTEGER
);
CREATE INDEX IF NOT EXISTS idx_hist_key ON price_history(key, ts);
CREATE TABLE IF NOT EXISTS events (
  ts REAL, key TEXT, kind TEXT, old_price REAL, new_price REAL, payload TEXT, notified INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
CREATE TABLE IF NOT EXISTS runs (
  ts REAL, scope TEXT, ok INTEGER, n INTEGER, error TEXT
);
"""


@dataclass
class Event:
    kind: str            # new | restock | price_drop | price_up | gone
    listing: dict        # Listing.to_dict() (or last known row for 'gone')
    old_price: float | None = None
    new_price: float | None = None

    @property
    def drop_pct(self) -> float | None:
        if self.old_price and self.new_price is not None:
            return round((1 - self.new_price / self.old_price) * 100, 1)
        return None


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path))
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def has_successful_run(self) -> bool:
        return self.db.execute("SELECT 1 FROM runs WHERE ok=1 LIMIT 1").fetchone() is not None

    def scope_seen_before(self, scope: str) -> bool:
        return self.db.execute("SELECT 1 FROM runs WHERE scope=? AND ok=1 LIMIT 1", (scope,)).fetchone() is not None

    def scope_signature_changed(self, scope: str, signature: str) -> bool:
        """True if what a scope covers changed since last time (e.g. you widened from SSD to all
        open box). The first run after such a change is a quiet baseline, so you don't get
        hundreds of 'new' alerts for items that were always there."""
        self.db.execute("CREATE TABLE IF NOT EXISTS scope_sig (scope TEXT PRIMARY KEY, sig TEXT)")
        row = self.db.execute("SELECT sig FROM scope_sig WHERE scope=?", (scope,)).fetchone()
        self.db.execute("INSERT OR REPLACE INTO scope_sig VALUES (?,?)", (scope, signature))
        self.db.commit()
        return row is None or row[0] != signature

    def apply(self, result: ScopeResult, now: float | None = None) -> list[Event]:
        """Merge one scope's scrape into the DB and return the change events."""
        now = now or time.time()
        events: list[Event] = []
        cur = self.db.cursor()
        cur.execute(
            "INSERT INTO runs VALUES (?,?,?,?,?)",
            (now, result.scope, int(result.ok), len(result.listings), result.error),
        )
        if not result.ok:
            self.db.commit()
            return events

        seen = set()
        for it in result.listings:
            seen.add(it.key)
            row = cur.execute("SELECT * FROM listings WHERE key=?", (it.key,)).fetchone()
            d = it.to_dict()
            if row is None:
                events.append(Event("new", d, None, it.price_open_box))
                cur.execute(
                    """INSERT INTO listings (key, scope, source, store, product_id, sku, name, brand, url, category,
                       condition, capacity_gb, price_new, price_ob, stock, stock_text, first_seen, last_seen,
                       last_change, active, low_price) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?)""",
                    (it.key, result.scope, it.source, it.store, it.product_id, it.sku, it.name, it.brand, it.url,
                     it.category, it.condition, it.capacity_gb, it.price_new, it.price_open_box, it.stock,
                     it.stock_text, now, now, now, it.price_open_box),
                )
                cur.execute("INSERT INTO price_history VALUES (?,?,?,?)", (it.key, now, it.price_open_box, it.stock))
                continue

            old_price = row["price_ob"]
            changed = False
            if not row["active"]:
                events.append(Event("restock", d, old_price, it.price_open_box))
                changed = True
            elif old_price is not None and abs(it.price_open_box - old_price) >= 0.01:
                kind = "price_drop" if it.price_open_box < old_price else "price_up"
                events.append(Event(kind, d, old_price, it.price_open_box))
                changed = True
            if changed or row["stock"] != it.stock:
                cur.execute("INSERT INTO price_history VALUES (?,?,?,?)", (it.key, now, it.price_open_box, it.stock))
            cur.execute(
                """UPDATE listings SET scope=?, name=?, brand=?, url=?, category=?, capacity_gb=?, price_new=?,
                   price_ob=?, stock=?, stock_text=?, last_seen=?, active=1,
                   last_change=CASE WHEN ? THEN ? ELSE last_change END,
                   low_price=MIN(COALESCE(low_price, ?), ?) WHERE key=?""",
                (result.scope, it.name, it.brand, it.url, it.category, it.capacity_gb, it.price_new,
                 it.price_open_box, it.stock, it.stock_text, now, int(changed), now,
                 it.price_open_box, it.price_open_box, it.key),
            )

        # Anything previously active in this scope that we did not see is gone.
        # (a store-wide scope also owns rows left under older per-category scopes like "microcenter:Fairfax:ssd")
        for row in cur.execute(
            "SELECT * FROM listings WHERE (scope=? OR scope LIKE ?) AND active=1", (result.scope, result.scope + ":%")
        ).fetchall():
            if row["key"] not in seen:
                events.append(Event("gone", _row_to_listing_dict(row), row["price_ob"], None))
                self.db.execute("UPDATE listings SET active=0, last_change=? WHERE key=?", (now, row["key"]))

        for ev in events:
            self.db.execute(
                "INSERT INTO events (ts, key, kind, old_price, new_price, payload) VALUES (?,?,?,?,?,?)",
                (now, ev.listing["key"], ev.kind, ev.old_price, ev.new_price, json.dumps(ev.listing)),
            )
        self.db.commit()
        return events

    def prune_source(self, source: str, max_age: float, now: float | None = None) -> int:
        """Delete a source's stored content older than max_age: price history points,
        events, and listings that haven't been seen (refreshed) within the window."""
        cutoff = (now or time.time()) - max_age
        like = f"{source}:%"
        cur = self.db.cursor()
        n = cur.execute("DELETE FROM price_history WHERE key LIKE ? AND ts < ?", (like, cutoff)).rowcount
        n += cur.execute("DELETE FROM events WHERE key LIKE ? AND ts < ?", (like, cutoff)).rowcount
        n += cur.execute("DELETE FROM listings WHERE source = ? AND last_seen < ?", (source, cutoff)).rowcount
        # the all-time low is derived content too: recompute it from what's left
        cur.execute(
            """UPDATE listings SET low_price = MIN(price_ob, COALESCE(
                 (SELECT MIN(h.price_ob) FROM price_history h WHERE h.key = listings.key), price_ob))
               WHERE source = ?""", (source,))
        self.db.commit()
        return n

    # ── read side for the dashboard ──────────────────────────────
    def listings(self, include_inactive_days: float = 3) -> list[dict]:
        cutoff = time.time() - include_inactive_days * 86400
        rows = self.db.execute(
            "SELECT * FROM listings WHERE active=1 OR last_change>=? ORDER BY last_change DESC", (cutoff,)
        ).fetchall()
        return [_row_to_listing_dict(r) | {"active": bool(r["active"]), "first_seen": r["first_seen"],
                                            "last_seen": r["last_seen"], "last_change": r["last_change"],
                                            "low_price": r["low_price"]} for r in rows]

    def history(self, keys: Iterable[str], days: float) -> dict[str, list[list[float]]]:
        cutoff = time.time() - days * 86400
        out: dict[str, list[list[float]]] = {}
        keys = list(keys)
        for i in range(0, len(keys), 500):
            chunk = keys[i:i + 500]
            q = f"SELECT key, ts, price_ob FROM price_history WHERE ts>=? AND key IN ({','.join('?' * len(chunk))}) ORDER BY ts"
            for r in self.db.execute(q, [cutoff, *chunk]):
                out.setdefault(r["key"], []).append([round(r["ts"]), r["price_ob"]])
        return out

    def recent_events(self, days: float = 7, limit: int = 300) -> list[dict]:
        cutoff = time.time() - days * 86400
        rows = self.db.execute(
            "SELECT ts, key, kind, old_price, new_price, payload FROM events WHERE ts>=? ORDER BY ts DESC LIMIT ?",
            (cutoff, limit),
        ).fetchall()
        out = []
        for r in rows:
            p = json.loads(r["payload"])
            out.append({"ts": r["ts"], "key": r["key"], "kind": r["kind"], "old_price": r["old_price"],
                        "new_price": r["new_price"], "name": p.get("name"), "store": p.get("store"),
                        "source": p.get("source"), "url": p.get("url")})
        return out

    def tag_counts(self) -> dict[str, int]:
        """Tags (store categories) among active listings, most common first."""
        rows = self.db.execute(
            "SELECT category, COUNT(*) n FROM listings WHERE active=1 GROUP BY category ORDER BY n DESC").fetchall()
        return {r["category"]: r["n"] for r in rows if r["category"]}

    def last_runs(self) -> list[dict]:
        rows = self.db.execute(
            """SELECT r.scope, r.ts, r.ok, r.n, r.error FROM runs r
               JOIN (SELECT scope, MAX(ts) mts FROM runs GROUP BY scope) m ON r.scope=m.scope AND r.ts=m.mts
               ORDER BY r.scope"""
        ).fetchall()
        return [dict(r) for r in rows]


def _row_to_listing_dict(r: sqlite3.Row) -> dict:
    it = Listing(
        source=r["source"], store=r["store"], product_id=r["product_id"], sku=r["sku"] or "",
        name=r["name"], brand=r["brand"] or "", url=r["url"], category=r["category"],
        condition=r["condition"], capacity_gb=r["capacity_gb"], price_new=r["price_new"],
        price_open_box=r["price_ob"], stock=r["stock"], stock_text=r["stock_text"] or "",
    )
    return it.to_dict()
