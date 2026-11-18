"""CLI: python -m obm <command> [-c config.yaml]"""
from __future__ import annotations

import argparse
import json
import logging
import os
import plistlib
import subprocess
import sys
import time
import webbrowser
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import load_config
from .dashboard import render_dashboard
from .http import Fetcher
from .notify import Notifier
from .runner import in_quiet_hours, run_once
from .store import Store

LABEL = "com.openbox-monitor"
PLIST = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"


def setup_logging(cfg, verbose: bool) -> None:
    cfg.log_path.parent.mkdir(parents=True, exist_ok=True)
    handlers = [RotatingFileHandler(cfg.log_path, maxBytes=2_000_000, backupCount=3, encoding="utf-8"),
                logging.StreamHandler(sys.stderr)]
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", handlers=handlers)


def cmd_run(cfg, a):
    stats = run_once(cfg, force=a.force, notify=not a.no_notify)
    print(json.dumps(stats, ensure_ascii=False, indent=2))
    if cfg["dashboard"]["open_after_run"] or a.open:
        webbrowser.open(cfg.dashboard_path.as_uri())


def cmd_watch(cfg, a):
    interval = int(cfg["interval_minutes"]) * 60
    print(f"每 {cfg['interval_minutes']} 分钟检查一次，Ctrl-C 退出。页面：{cfg.dashboard_path}")
    while True:
        try:
            if in_quiet_hours(cfg):
                logging.info("静默时段，跳过")
            else:
                run_once(cfg, notify=not a.no_notify)
        except Exception as e:
            logging.exception("run failed: %s", e)
        time.sleep(interval)


def cmd_dashboard(cfg, a):
    out = render_dashboard(cfg, Store(cfg.db_path))
    print(out)
    if a.open:
        webbrowser.open(out.as_uri())


def cmd_serve(cfg, a):
    """Serve the page on your LAN so you can open it from your phone."""
    from .server import serve_only

    serve_only(cfg, a.host, a.port)


def cmd_daemon(cfg, a):
    """Checks on a schedule + serves the page, in one process (what Docker runs)."""
    from .server import daemon

    daemon(a.config, a.host, a.port, notify=not a.no_notify)


def cmd_test_notify(cfg, a):
    Notifier(cfg).send({"title": "Open Box 监控 · 测试", "body": "推送渠道配置正常 ✅",
                        "url": "https://www.microcenter.com/site/products/open-box.aspx"})
    print("已发送，检查各渠道是否收到。")


def cmd_status(cfg, a):
    st = Store(cfg.db_path)
    for r in st.last_runs():
        t = time.strftime("%m-%d %H:%M", time.localtime(r["ts"]))
        print(f"{'OK ' if r['ok'] else 'ERR'} {t}  {r['scope']:<40} {r['n']:>4} 条  {r['error'] or ''}")
    live = [d for d in st.listings() if d["active"]]
    print(f"\n在架 {len(live)} 条。页面：{cfg.dashboard_path}")


def cmd_dump(cfg, a):
    """Save raw pages to data/debug/ so a parser can be written against the real HTML."""
    import re
    from urllib.parse import urlencode

    from .scrape_http import BlockedError, RotatingFetcher
    from .sources.microcenter import SEARCH, build_params, parse_page

    out = cfg.data_dir / "debug"
    out.mkdir(parents=True, exist_ok=True)

    def save(name, r):
        path = out / name
        path.write_text(r.text, encoding="utf-8")
        prices = len(re.findall(r"\$\s?\d[\d,]*\.\d{2}", r.text))
        print(f"  {name:<24} HTTP {r.status_code}  {len(r.text) // 1024:>5} KB  页面里的价格样式文本 {prices} 处")
        return path

    if a.site == "microcenter":
        stores = cfg["sources"]["microcenter"]["_stores"]
        if a.store:
            from .locations import find_store
            try:
                st = find_store(a.store)
            except ValueError as e:
                sys.exit(str(e))
            name, sid = st.name, st.id
        else:
            name, sid = stores[0]
        f = RotatingFetcher(cfg.data_dir, delay=2)
        try:
            if a.query and "/product/" in a.query:  # one product page, with the store selected
                from .sources.microcenter import product_url
                url = a.query if "storeid=" in a.query else product_url(a.query.split("#")[0], sid)
                r = f.get(url)
                save("mc_product.html", r)
                text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", r.text)))
                hits = [text[max(0, m.start() - 60): m.end() + 120] for m in re.finditer(r"(?i)open[ -]box", text)][:6]
                print(f"  {name}（storeid={sid}）商品页里 open box 相关文字：")
                for h in hits:
                    print("   …" + h.strip() + "…")
            else:
                r = f.get(SEARCH, build_params(None, sid, 1, False))
                save("mc_all_openbox.html", r)
                items, rng = parse_page(r.text, name, "其他", sid)
                tags = sorted({i.category for i in items})
                print(f"  {name} 全部 open box：解析到 {len(items)} 条，页面显示共 {rng[2] if rng else '?'} 条；分类 {len(tags)} 个：{', '.join(tags[:8])}…")
                odd = [i for i in items if i.discount_pct and i.discount_pct >= 40]
                for i in odd[:5]:
                    print(f"  折扣异常大：{i.name[:60]}  open box ${i.price_open_box} / 新品 ${i.price_new}（-{i.discount_pct}%）")
        except Exception as e:
            sys.exit(f"Micro Center 请求失败：{e}")
    else:
        from .runner import _one_profile
        from .sources.bestbuy import page_request, parse_search
        bb = cfg["sources"]["bestbuy"]
        f = RotatingFetcher(cfg.data_dir, delay=2, profiles=[_one_profile(bb.get("impersonate"))],
                            state_name="fetch_state_bestbuy.json")
        try:
            url, params = page_request(a.query, 1)
            r = f.get(url, params=params)
        except BlockedError as e:
            sys.exit(f"Best Buy 搜索页被拦：{e}")
        except Exception as e:
            sys.exit(f"Best Buy 搜索页请求失败：{e}")
        print(f"Best Buy（{f.backend}）「{a.query[:80]}」：")
        save("bb_search.html", r)
        res = parse_search(r.text)
        prods = res["products"]
        priced = [p for p in prods if p["name"] and (res["offers"].get(p["sku"]) or {}).get("offers")]
        print(f"  Best Buy 归到的分类：{res['category'] or '-'}；open box 共 {res['num_found']} 件")
        print(f"  本页 {len(prods)} 件，其中 {len(priced)} 件带 open box 价格（搜索页只给前几件标价）")
        print(f"  分类 tag：{', '.join(sorted({t for p in prods for t in p['tags'][:1]}))}")
        for p in priced:
            o = res["offers"][p["sku"]]
            print(f"    {p['name'][:60]}  新品 ${p['price_new']}  " + "  ".join(f"{x['condition']} ${x['price']}" for x in o["offers"]))
    f.close()
    print(f"\n文件在 {out}（Docker 里是 ./data/data/debug/），把整个文件夹压缩发给我。")


def cmd_install_launchd(cfg, a):
    if sys.platform != "darwin":
        sys.exit("launchd 只在 macOS 上可用；Linux 可以用 cron 或 `python -m obm watch`")
    cfg_path = Path(a.config).expanduser().resolve()
    logdir = cfg.log_path.parent
    logdir.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": LABEL,
        "ProgramArguments": [sys.executable, "-m", "obm", "run", "-c", str(cfg_path)],
        "WorkingDirectory": str(Path(__file__).resolve().parent.parent),
        "StartInterval": int(cfg["interval_minutes"]) * 60,
        "RunAtLoad": True,
        "StandardOutPath": str(logdir / "launchd.out.log"),
        "StandardErrorPath": str(logdir / "launchd.err.log"),
        "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"},
        "ProcessType": "Background",
    }
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    uid = os.getuid()
    subprocess.run(["launchctl", "bootout", f"gui/{uid}", str(PLIST)], capture_output=True)
    PLIST.write_bytes(plistlib.dumps(plist))
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(PLIST)], capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit(f"launchctl bootstrap 失败：{r.stderr.strip()}")
    print(f"已安装：每 {cfg['interval_minutes']} 分钟运行一次（静默时段自动跳过）。\n{PLIST}")
    print("改了 interval_minutes 之后重新运行这个命令即可生效。")


def cmd_uninstall_launchd(cfg, a):
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(PLIST)], capture_output=True)
    if PLIST.exists():
        PLIST.unlink()
    print("已卸载定时任务。")


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m obm", description="Micro Center / Best Buy open box 存储监控")
    p.add_argument("-c", "--config", default="config.yaml", help="配置文件路径（默认 ./config.yaml）")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("run", help="检查一轮")
    s.add_argument("--force", action="store_true", help="忽略静默时段")
    s.add_argument("--no-notify", action="store_true", help="不推送，只更新数据和页面")
    s.add_argument("--open", action="store_true", help="跑完打开页面")
    s.set_defaults(fn=cmd_run)

    s = sub.add_parser("watch", help="前台循环运行（按 interval_minutes）")
    s.add_argument("--no-notify", action="store_true")
    s.set_defaults(fn=cmd_watch)

    s = sub.add_parser("dashboard", help="只重新生成页面")
    s.add_argument("--open", action="store_true")
    s.set_defaults(fn=cmd_dashboard)

    s = sub.add_parser("serve", help="在局域网提供页面，手机也能看")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8787)
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("daemon", help="常驻：定时检查 + 页面服务（Docker 用这个）")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8787)
    s.add_argument("--no-notify", action="store_true")
    s.set_defaults(fn=cmd_daemon)

    sub.add_parser("status", help="查看每个门店/品类最近一次检查结果").set_defaults(fn=cmd_status)
    sub.add_parser("test-notify", help="发一条测试推送").set_defaults(fn=cmd_test_notify)


    s = sub.add_parser("dump", help="保存原始页面到 data/debug/，用于调试解析")
    s.add_argument("site", choices=["bestbuy", "microcenter"])
    s.add_argument("query", nargs="?", default="ssd", help="Best Buy 搜索词或从浏览器复制的 Best Buy 链接（默认 ssd）；Micro Center 可以给一个商品链接")
    s.add_argument("--store", help="Micro Center 门店名，例如 Fairfax（默认第一个监控的门店）")
    s.set_defaults(fn=cmd_dump)

    sub.add_parser("install-launchd", help="macOS：按配置的间隔安装定时任务").set_defaults(fn=cmd_install_launchd)
    sub.add_parser("uninstall-launchd", help="macOS：移除定时任务").set_defaults(fn=cmd_uninstall_launchd)

    a = p.parse_args(argv)
    cfg = load_config(a.config)
    setup_logging(cfg, a.verbose)
    a.fn(cfg, a)


if __name__ == "__main__":
    main()
