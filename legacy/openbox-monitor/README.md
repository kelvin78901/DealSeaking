# Open Box Monitor

监控你附近 Micro Center 门店的 open box：默认每家店的**全部** open box 一轮抓完存下
（电脑、显卡、显示器、存储……），页面按网站自己的分类 tag 筛；推送只推你选的分类 + 关注清单。
新上架、降价、重新上架都会推送到手机。Best Buy 线上 open box 可选。

> **关于 Micro Center 抓取**：Micro Center 会拦截脚本访问（普通请求、Chrome 指纹、无头浏览器都会 403）。
> 本工具用 curl_cffi 模拟浏览器指纹（默认先试 Safari）来绕过，这可能违反 Micro Center 的使用条款，
> IP 也可能被临时限制。请只做个人低频使用（默认 3 小时一轮、请求间隔 3 秒）。
> 不想抓的话在设置里关掉「自动抓取」，页面仍会列出各门店 open box 页面的链接。

## Docker 常驻

```bash
cd openbox-monitor
docker compose up -d --build     # 首次启动会生成 ./data/config.yaml
```

打开 http://localhost:8787/ → 右上角「设置」：

1. **Micro Center 门店**：填 ZIP 和半径，自动选范围内的门店（也可以手动勾选）；「自动抓取」默认开
2. **抓取范围**：「全部 open box」（默认），或只抓勾选的大类 / 存储细分
3. **推送哪些分类**：从已抓到的 tag 里勾（默认存储类）；关注清单关键词不受分类限制
4. 检查频率、静默时段、推送规则、推送渠道（ntfy / Bark / Telegram）
5. 「保存并立即检查」

也可以直接改 `./data/config.yaml`，下一轮自动生效。已保存的 key / token 不会回显到页面。

## 抓取是怎么工作的

- 「全部」：每家门店请求一次该店的 open box 列表（一页 96 条，自动翻页，最多 50 页）；
  每件商品的分类取自页面里的分类名，作为 tag 存下。选了具体大类/品类时只请求这些
- 存储类（SSD、硬盘、NAS、U 盘、存储卡）才计算容量和 $/TB，电脑里的“512GB SSD”不会被当成存储
- 改了抓取范围后，第一轮只记基线不推送
- 依次尝试 `impersonate` 里的浏览器指纹（默认 `safari, safari_ios, firefox, chrome, edge`），
  能用的那个记在 `data/fetch_state.json`，之后优先用
- 全部指纹都被拦：本轮停止，不再继续请求；页面状态显示「被拦截」。通常是 IP 被临时限制，
  调大检查间隔、过几个小时再看
- 某个门店抓取失败时，不会把它的商品误判为“已售出”
- 列表里的“店内新品 N”是该店新品库存，open box 本身通常只有 1–2 件，以商品页为准

## Best Buy

设置里勾选「抓取 Best Buy」，填搜索词或 Best Buy 链接（逗号分隔）。抓的是线上 open box，
每个商品按成色（Excellent / Good / Fair）各算一条，分类 tag 用 Best Buy 自己的（如 Laptop Computer）。

**限制**：只抓搜索结果页。商品详情页有反爬保护（直接断开连接），本工具不去绕。
搜索页每页 18 件，Best Buy 只给前 4 件带 open box 价格，所以页面上只会出现这部分：

- 排到前 4 过的商品，价格记住 7 天（`keep_price_days`），之后即使排到后面也照样显示；
  从搜索结果里消失才算售出
- 一个商品只是这一轮才排进前 4（之前就在），不会当成“新上架”推送
- 结果越少覆盖越全：用具体的分类代替宽泛的搜索词。在 Best Buy 网站上选好分类、勾上 Open-Box，
  把地址栏链接粘进设置里即可。例如 `ssd` 会被它归到 Computers & Tablets（大多是带 SSD 的电脑）

看某个搜索词或链接实际搜到什么：

```bash
docker compose run --rm openbox-monitor dump bestbuy "ssd"
```

## Docker 说明

- **命令**：`docker compose run --rm openbox-monitor status`、`... test-notify`
- **时区**：静默时段按 compose 里的 `TZ`（默认 `America/New_York`）计算
- **数据**：全在 `./data/`：`config.yaml`、`dashboard.html`、`data/openbox.db`、`data/monitor.log`
- **日志**：`docker compose logs -f`
- **健康检查**：`/healthz`；检查循环卡住时容器显示 unhealthy，`restart: unless-stopped` 负责拉起
- **更新**：`docker compose up -d --build`，数据不受影响
- **只给本机访问**：端口改成 `"127.0.0.1:8787:8787"`。页面不会通过 HTTP 暴露 `config.yaml` 和数据库，
  设置接口拒绝跨站请求
- **Windows / Mac**：Docker Desktop 设成登录时启动；电脑睡眠时容器也会暂停

## 不用 Docker

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp config.example.yaml config.yaml
python -m obm daemon          # 常驻：定时检查 + 页面 http://localhost:8787/
```

## 推送

最省事的是 **ntfy**：手机装 ntfy App，订阅一个只有你知道的 topic，在设置里填同一个 topic。
点通知直接打开商品页。第一轮只记录基线（发一条“已启动”），之后只推送变化。

## 常用命令

```bash
python -m obm run [--force] [--no-notify] [--open]   # 检查一轮
python -m obm daemon                                 # 常驻：定时检查 + 页面（Docker 跑的就是这个）
python -m obm status                                 # 每个门店最近一次结果
python -m obm dump {bestbuy,microcenter} [关键词]     # 保存原始页面到 data/debug，排查解析用
python -m obm test-notify                            # 测试推送
```

测试：`pip install pytest && python -m pytest tests`（页面测试另需 `pip install playwright && playwright install chromium`）
