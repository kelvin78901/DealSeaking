"""Push channels: macOS notification, ntfy, Bark, Telegram."""
from __future__ import annotations

import logging
import platform
import shutil
import subprocess

import requests

from .config import CATEGORY_LABELS
from .store import Event

log = logging.getLogger(__name__)

KIND_LABEL = {"new": "🆕 新上架", "restock": "🔁 重新上架", "price_drop": "📉 降价"}
SOURCE_LABEL = {"microcenter": "Micro Center", "bestbuy": "Best Buy"}


def format_event(ev: Event) -> dict:
    d = ev.listing
    where = SOURCE_LABEL.get(d["source"], d["source"])
    if d["store"] != "online":
        where += f" {d['store']}"
    price = f"${d['price_open_box']:,.2f}"
    if ev.kind == "price_drop" and ev.old_price:
        price = f"${ev.old_price:,.2f} → {price} (-{ev.drop_pct}%)"
    elif d.get("discount_pct") is not None:
        price += f"（比新品 -{d['discount_pct']:.0f}%）"
    extras = []
    if d.get("price_per_tb"):
        extras.append(f"${d['price_per_tb']:,.0f}/TB")
    if d.get("price_new"):
        extras.append(f"新品 ${d['price_new']:,.2f}")
    if d.get("condition") and d["source"] == "bestbuy":
        extras.append(d["condition"])
    extras.append(CATEGORY_LABELS.get(d.get("category")) or d.get("category") or "")
    return {
        "title": f"{KIND_LABEL.get(ev.kind, ev.kind)} · {where}",
        "body": f"{price}\n{d['name']}\n{' · '.join(x for x in extras if x)}",
        "url": d["url"],
    }


def format_summary(n_more: int, dashboard_hint: str) -> dict:
    return {"title": f"Open Box：还有 {n_more} 条变化", "body": f"打开监控页面查看：{dashboard_hint}", "url": ""}


class Notifier:
    def __init__(self, cfg: dict):
        self.cfg = cfg["notify"]

    def send(self, msg: dict) -> dict:
        """Send to every enabled channel. Returns {channel: 'ok' | 'off' | error text}."""
        results = {}
        for name, fn in (
            ("macos", self._macos),
            ("ntfy", self._ntfy),
            ("bark", self._bark),
            ("telegram", self._telegram),
        ):
            try:
                results[name] = "ok" if fn(msg) is not False else "off"
            except Exception as e:
                log.error("notify via %s failed: %s", name, e)
                results[name] = str(e)[:200]
        return results

    # ── channels ────────────────────────────────────────────────
    def _macos(self, msg: dict) -> None:
        if not self.cfg.get("macos") or platform.system() != "Darwin":
            return False
        title, body = msg["title"], msg["body"].replace("\n", " · ")
        tn = shutil.which("terminal-notifier")
        if tn:
            args = [tn, "-title", title, "-message", body, "-group", "openbox-" + (msg.get("url") or title)[-40:]]
            if msg.get("url"):
                args += ["-open", msg["url"]]
            subprocess.run(args, check=False, timeout=10)
        else:
            esc = lambda s: s.replace("\\", "\\\\").replace('"', '\\"')
            script = f'display notification "{esc(body)}" with title "{esc(title)}" sound name "Glass"'
            subprocess.run(["osascript", "-e", script], check=False, timeout=10)

    def _ntfy(self, msg: dict) -> None:
        c = self.cfg.get("ntfy") or {}
        if not c.get("enabled") or not c.get("topic"):
            return False
        payload = {"topic": c["topic"], "title": msg["title"], "message": msg["body"], "tags": ["package"]}
        if msg.get("url"):
            payload["click"] = msg["url"]
        requests.post(c.get("server", "https://ntfy.sh").rstrip("/"), json=payload, timeout=15).raise_for_status()

    def _bark(self, msg: dict) -> None:
        c = self.cfg.get("bark") or {}
        if not c.get("enabled") or not c.get("key"):
            return False
        payload = {"device_key": c["key"], "title": msg["title"], "body": msg["body"], "group": "openbox"}
        if msg.get("url"):
            payload["url"] = msg["url"]
        requests.post(c.get("server", "https://api.day.app").rstrip("/") + "/push", json=payload, timeout=15).raise_for_status()

    def _telegram(self, msg: dict) -> None:
        c = self.cfg.get("telegram") or {}
        if not c.get("enabled") or not c.get("bot_token") or not c.get("chat_id"):
            return False
        text = f"{msg['title']}\n{msg['body']}" + (f"\n{msg['url']}" if msg.get("url") else "")
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{c['bot_token']}/sendMessage",
                json={"chat_id": c["chat_id"], "text": text, "disable_web_page_preview": True},
                timeout=15,
            )
        except requests.RequestException as e:  # the URL contains the bot token; don't log it
            raise RuntimeError(f"Telegram 连接失败：{type(e).__name__}") from None
        if r.status_code >= 400:
            raise RuntimeError(f"Telegram HTTP {r.status_code}: {r.text[:120]}")
