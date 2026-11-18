# 用户采集端协议（Micro Center）

> 草案 v0.1 · 2026-11-17 · 对应决策 [#6](decisions.md#6-micro-center-数据用户在自己的-ip-上采集汇总后所有用户共享)

Micro Center 的 open box 数据由用户的采集端（浏览器扩展或 Docker 采集器）在用户自己的网络上抓取。服务器负责分派任务、解析页面、合并成共享存档。采集端不做解析，只负责“按任务取页面、原样上传”。

## 流程

1. **注册采集端：** 用户在门户的“贡献数据”页同意说明，选择门店（默认是 ZIP 半径内的店），拿到一个采集端 token（可随时吊销）。
2. **领任务：** `POST /api/collector/lease`。服务器按“最久没更新”的顺序，从该采集端选的门店里挑一家，返回这家店的第 1 页任务和一个 `crawl_id`。
3. **取页面：** 采集端按任务里的 URL 请求 microcenter.com，不带登录状态，遵守限速。
4. **上传：** `POST /api/collector/pages`，上传 gzip 后的原始 HTML 和 HTTP 状态。服务器解析后回复：还有下一页就给出下一页任务，否则这家店的本轮采集结束。
5. **出错：** 返回 403、验证页或连续网络错误时，采集端上报 `blocked` 并停止 24 小时。服务器把这家店交给别的采集端。

## 接口

### `POST /api/collector/lease`

请求头 `Authorization: Bearer <collector token>`。

```json
{"client": "extension/0.1.0", "max_pages": 12}
```

响应（无任务时返回 `204`，`retry_after` 秒后再来）：

```json
{
  "crawl_id": "c_01HX…",
  "store": {"id": "085", "name": "Rockville"},
  "task": {"page": 1, "url": "https://www.microcenter.com/search/search_results.aspx?fq=Valuable%20Links%3AOpen%20Box&storeid=085&rpp=96&page=1&myStore=true"},
  "min_interval_sec": 10,
  "lease_expires_in": 900
}
```

### `POST /api/collector/pages`

```json
{
  "crawl_id": "c_01HX…",
  "page": 1,
  "fetched_at": "2026-11-24T14:03:11Z",
  "http_status": 200,
  "final_url": "https://www.microcenter.com/search/search_results.aspx?…",
  "html_gzip_b64": "H4sIAAAA…"
}
```

响应：

```json
{"accepted": true, "parsed_items": 96, "next": {"page": 2, "url": "…"}}
```

`next` 为 `null` 表示本轮结束。`accepted: false` 时带 `reason`（`lease_expired`、`unparseable`、`store_mismatch`）。

### `POST /api/collector/blocked`

```json
{"crawl_id": "c_01HX…", "http_status": 403, "detail": "challenge page"}
```

## 服务器端规则

- **任务分派：** 同一家店同一时间只租给一个采集端。目标刷新间隔 30 分钟，用户少时放宽。同一采集端连续两轮不分到同一家店（有别人可用时）。
- **限速：** 服务器按 `min_interval_sec` 和每小时页数上限拒绝过快的上传（`429`）。
- **解析：** 用 openbox-monitor 已验证的解析器（`data-category` 作为分类、`storeid` 带进商品链接），记录解析器版本；页面原文保留 7 天。
- **完整性：** 一个 `crawl_id` 的全部页面都在 30 分钟内收齐，才算完整批次。只有完整批次能把商品判为下架。
- **可信度：**
  - 页面里的门店 ID 必须和任务一致，商品数和页码要连续。
  - 异常价格（折扣超过 60%，或比上次低一半以上）先标“待确认”，等另一个采集端或下一轮确认后才进提醒。
  - 每个采集端有信任分：解析失败、数据与他人冲突会扣分，低分的采集端停发任务。
- **隐私：** 不记录采集端的 IP 以外的设备信息；IP 只用于限速和滥用排查，保留 7 天。

## 浏览器扩展（Manifest V3）

- 权限：`host_permissions: ["https://www.microcenter.com/*", "<portal origin>/*"]`、`alarms`、`storage`。不申请其他站点权限。
- 后台：`chrome.alarms` 每分钟唤醒一次；有任务就按间隔逐页 `fetch(url, {credentials: 'omit'})`，上传，直到本轮结束或达到每小时上限。
- 设置页：粘贴 token、显示选中的门店、今日请求数、暂停按钮、最近一次错误。
- 停止条件：`403`、页面里出现验证挑战、连续 3 次网络错误。停止后 24 小时内不再请求。
- 发布：Chrome Web Store 需要一次性注册费和审核（时长不定），先提交；审核期间提供“加载已解压的扩展”安装说明。

## Docker / Python 采集器

openbox-monitor 加一个 `collect` 模式：只跑 lease → fetch → upload 循环，复用现有的 HTTP 客户端，但关掉指纹轮换（固定一种，被拒即停）。供有 NAS 或常开电脑的用户，以及作为扩展审核期间的备用。
