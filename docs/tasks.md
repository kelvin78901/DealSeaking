# 任务清单

> 2026-11-17 · 对应 [plan-black-friday.md](plan-black-friday.md)。分两部分：Kelvin 自己要办的事，和可以直接派给 Claude Code 的任务。

## Kelvin 要做的事

这些要么需要你的账号、钱或身份，要么需要人来判断，Claude Code 替不了。

| 截止 | 事项 | 为什么必须是你 | 备注 |
| --- | --- | --- | --- |
| 11/17 | 仓库改成私有 | 仓库设置权限 | Settings → General → Danger Zone |
| 11/17 | 定产品名和域名，买域名 | 品牌决定 + 付款 | 放在 Cloudflare，方便后面配 DNS 和 Turnstile |
| 11/17 | 买 VPS（美国机房，2 核 4GB），给 Claude Code 一个部署用的 SSH key | 付款 + 服务器权限 | 系统选 Ubuntu 24.04 |
| 11/17 | 注册 Resend，验证发信子域名（如 `alerts.<域名>`），加 SPF / DKIM / DMARC 记录 | 账号 + DNS 权限 | DNS 生效和邮箱信誉都要时间，越早越好 |
| 11/17 | Cloudflare Turnstile 建站点，拿 site key / secret | 账号 | 免费 |
| 11/17 | 注册 Chrome Web Store 开发者账号（一次性注册费） | 付款 + 身份 | 审核时长不定，账号要提前开 |
| 11/17 | 在 Woot 论坛申请 API key；查 Best Buy key 审核状态 | 账号 | 拿到后放进服务器的 `.env`，不要发在聊天里 |
| 11/18 | 弄一个虚拟邮寄地址 | 付款 + 身份 | 邮件页脚法律要求 |
| 11/20 | 在自己电脑上用真实 Micro Center 试跑扩展和 Docker 采集器，把结果和问题反馈给 Claude Code | 需要你的住宅网络，开发环境不该直接去抓真实网站 | 先只开一家店，看日志 |
| 11/20 | 审核扩展的商店介绍和隐私问卷，提交 Chrome Web Store | 开发者账号 | Claude Code 会准备好文字、图标和截图 |
| 11/22 | 审核服务条款、隐私政策、联盟披露、采集同意页 | 法律责任在你；Claude Code 写的是草稿，不是法律意见 | 有条件就找律师看一眼 |
| 11/22 | 定首批品类、发件人名称、邮件里的品牌文案 | 产品决定 | 建议存储、显卡、笔记本 |
| 11/23 | 在服务器上填 `.env`（数据库密码、Resend、Turnstile、Woot / Best Buy key），把门户域名指到服务器 | 密钥只该经你的手 | Claude Code 给你一份 `.env.example` |
| 11/23 | 用 mail-tester 之类的工具测一封提醒邮件进不进收件箱 | 需要你的邮箱 | 不过关就先只发每日摘要 |
| 11/24 | 找 5–10 个朋友试用，其中至少 3 个装采集端，覆盖 Rockville / Fairfax / Parkville | 人脉 | |
| 11/25 | 申请联盟计划（Impact 上的 Walmart、Target，Best Buy 联盟等），在社区发上线帖 | 账号 + 身份；联盟申请要填可访问的网站 | 发帖前读社区规则 |
| 11/26–30 | 值守：看告警、投诉率、Micro Center 是否开始拦截，决定要不要暂停某个来源 | 需要人做判断 | |

## 派给 Claude Code 的任务

用法：每个任务在单独的分支或 worktree 里做，把下面整段贴给 Claude Code 即可。任务之间只通过 `CLAUDE.md` 约定的目录和 `docs/` 里的协议交互，所以同一波里的任务可以并行。

| 波次 | 任务 | 依赖 | 预计 |
| --- | --- | --- | --- |
| 第 1 波（11/17 起，可全部并行） | A1 应用骨架 · A2 迁移解析器与差异检测 · A3 规则引擎 · A4 Chrome 扩展 · A5 Feed / API 适配器 · A6 Python 采集器 | 无（A1 先合并最好，但不阻塞其他） | 各约半天到一天 |
| 第 2 波（A1、A2 合并后，约 11/19） | B1 采集服务端 · B2 入库流水线与调度 · B3 登录 · B4 通知与邮件 · B5 `/go` 跳转 | A1、A2；B4 还要 A3 | 各约一天 |
| 第 2.5 波（约 11/21） | B6 门户页面 | A1、A3、B3 | 一到两天 |
| 第 3 波（11/22–23） | C1 部署与运维 · C2 法务与上架文案草稿 · C3 端到端冒烟测试 | 前面全部；C1 要你给的服务器 | 各约半天 |

### A1 应用骨架

- **范围：** `dealseaking/api/`、`dealseaking/db/`、根目录的 `docker-compose.yml`、`Dockerfile`、`.env.example`、`.github/workflows/`、`tests/conftest.py`
- **要做：**
  - FastAPI 应用工厂，`GET /healthz` 返回数据库连通状态。
  - `pydantic-settings` 配置（前缀 `DS_`）。
  - SQLAlchemy 2 模型覆盖 `docs/schema.sql` 的全部表，Alembic 初始迁移。
  - `docker-compose.yml`：`db`（postgres:16）、`db-test`、`app`（uvicorn）、`worker`（APScheduler 进程，先空跑）、`caddy`。
  - GitHub Actions：ruff + pytest（用 Postgres service）。
- **完成标准：**
  - `docker compose up` 后 `/healthz` 返回 200。
  - `alembic upgrade head` 建出和 `schema.sql` 一致的 24 张表；`price_points` 是分区表，带一个当月分区的创建函数。
  - CI 通过。
- **不做：** 任何业务逻辑和页面。

### A2 迁移 Micro Center / Best Buy 解析器与差异检测

- **范围：** `dealseaking/sources/microcenter.py`、`dealseaking/sources/bestbuy_web.py`、`dealseaking/sources/stores_mc.json`、`dealseaking/pipeline/diff.py`、`dealseaking/pipeline/models.py`、`tests/sources/`、`tests/pipeline/`
- **输入：** `legacy/openbox-monitor/obm/`（`sources/microcenter.py`、`sources/bestbuy.py`、`models.py`、`store.py`、`locations.py`）和它的 `tests/`。
- **要做：**
  - 把解析逻辑改成纯函数：输入 HTML，输出 `Offer` dataclass 列表和分页信息。保留这些行为：`data-category` 作为分类；商品链接带 `storeid`；`ob=1`；“Add to Cart to see price”的商品跳过；仅存储类计算容量和每 TB 价格。
  - Micro Center 门店表转成 JSON（30 家店 + 坐标），加按 ZIP 求距离的函数（沿用 `zipcodes`）。
  - 差异检测改成纯函数：上次报价集合 + 本次报价集合 + 本次是否完整，输出事件列表（new / price_drop / price_up / restock / gone），规则同旧代码：不完整批次不判 gone，首次只记基线。
  - 迁移对应测试和 fixture。
- **完成标准：** 迁移后的测试全过；不 import `legacy`；不访问网络。
- **不做：** 数据库读写（第 2 波做）；抓取请求。

### A3 规则引擎

- **范围：** `dealseaking/rules/`、`tests/rules/`
- **要做：**
  - 用 `docs/rule.schema.json` 校验规则。
  - 把规则编译成判定函数：`any` 是 OR，同级键是 AND；`category` 匹配子节点；关键词大小写不敏感；`regex` 带超时。
  - 为每条规则生成倒排索引 token（`cat:`、`brand:`、`kw:`、`gtin:`、`mpn:`、`src:`），实现“事件 → 候选规则 → 命中”。
  - 投递辅助函数：静默时段（可跨午夜、按用户时区）、冷却判断、每日上限。
- **完成标准：** `docs/examples/rule-ssd.json` 在给定报价上命中 / 不命中的用例齐全；1 万条规则 × 1 千个事件的匹配在 2 秒内。
- **不做：** 数据库、发送。

### A4 Chrome 扩展采集端（MV3）

- **范围：** `extension/`
- **输入：** `docs/collector-protocol.md`。
- **要做：**
  - `manifest.json`：权限只有 `alarms`、`storage`，`host_permissions` 只有 microcenter.com 和门户域名（门户域名做成构建时变量）。
  - 后台 service worker 每分钟唤醒，执行“领任务 → 逐页 `fetch(url, {credentials: 'omit'})` → 上传 gzip 后的 HTML”，遵守 `min_interval_sec` 和每小时上限。
  - 遇到 403、页面里出现验证挑战、或连续 3 次网络错误时，上报 `blocked`，停 24 小时。
  - 设置页：粘贴 token、显示门店、今日请求数、暂停 / 继续、最近错误。
  - 一个本地 mock 服务器（Node）和测试（vitest）。
  - `npm run build` 打出可上传的 zip。
- **完成标准：** 对 mock 服务器能完整跑完一次 3 页的采集；收到 403 会停；两次请求间隔不小于设定值；测试通过。
- **不做：** 连真实 Micro Center（由 Kelvin 在 11/20 试跑）；商店上架。

### A5 Feed / API 适配器

- **范围：** `dealseaking/sources/dealnews.py`、`dealseaking/sources/woot.py`、`dealseaking/sources/bestbuy_api.py`、`tests/sources/`
- **要做：**
  - DealNews RSS：用 feedparser 解析，从标题里提取价格，保留原始推荐链接（条款要求），注明来源。
  - Woot API：`/feed/{Electronics,Computers}` 和 `getoffers`，key 从环境变量读，每天 1,000 次的预算计数。
  - Best Buy Products / Open Box API：每秒 5 次的限速器，返回的报价标记 `cache_ttl=72h`，open box 的 excellent / certified 映射到成色。
  - 全部输出 A2 定义的 `Offer`；如果 A2 还没合并，先在 `dealseaking/pipeline/models.py` 用同样字段定义，合并时以 A2 为准。
- **完成标准：** 每个适配器都有基于 fixture 的测试。DealNews 的 fixture 可以手动保存一份真实 RSS；Woot 和 Best Buy 没有 key，就按官方文档的示例响应做 fixture，并在代码里标注“未经真实接口验证”。
- **不做：** 调度、入库。

### A6 Python / Docker 采集器

- **范围：** `collector_cli/`（含 `Dockerfile`）
- **输入：** `docs/collector-protocol.md`；HTTP 部分参考 `legacy/openbox-monitor/obm/scrape_http.py`，但去掉指纹轮换：固定一种，被拒即停。
- **要做：**
  - `python -m collector_cli run --server URL --token T`：循环执行“领任务 → 取页 → 上传”，限速和停止规则同扩展。
  - 带一个 mock 服务器做测试。
- **完成标准：** 对 mock 服务器完成多页采集；遇 403 停止并上报；`docker build` 成功。
- **不做：** 连真实网站。

### B1 采集服务端

- **范围：** `dealseaking/collector/`、`dealseaking/api/collector.py`、`tests/collector/`
- **依赖：** A1、A2
- **要做：**
  - 按 `docs/collector-protocol.md` 实现 `lease`、`pages`、`blocked` 三个接口。
  - token 只存哈希；同一家店同时只租给一个采集端（部分唯一索引）；超过限速返回 429。
  - 服务器端用 A2 的解析器解析，记录 `parser_version`；原文存进 `raw_pages`。
  - 完整批次才能判下架。
  - 异常价格（折扣超过 60% 或比上次低一半以上）标 `needs_confirmation`，等另一个采集端或下一轮确认。
  - 信任分的加减规则。
  - 每天清理 7 天前的 `raw_pages`。
- **完成标准：** 集成测试里，两个模拟采集端轮流采同一家店，结果合并正确；伪造的异常低价不会直接产生可提醒的事件；吊销的 token 返回 401。

### B2 入库流水线与调度

- **范围：** `dealseaking/pipeline/`、`worker` 进程入口
- **依赖：** A1、A2（A5 合并后接入）
- **要做：**
  - 把 `Offer` 写进 `offers` / `price_points`，用 A2 的差异检测生成 `deal_events`。
  - 用 APScheduler 定时跑各 feed / API 来源，每个来源有自己的频率和配额。
  - Best Buy 数据过期清理；每月自动建 `price_points` 分区；按 `categories` 和 `category_map` 归类。
- **完成标准：** 用 fixture 驱动的来源跑两轮，产生正确的事件；第一次运行只记基线。

### B3 登录

- **范围：** `dealseaking/api/auth.py`、`dealseaking/web/` 下的登录页、`tests/auth/`
- **依赖：** A1
- **要做：**
  - 邮箱 6 位验证码 + 登录链接，令牌只存哈希、一次性、15 分钟过期；首次登录即完成邮箱验证。
  - 签名 cookie 会话；服务端校验 Cloudflare Turnstile；按 IP 和邮箱限速。
  - 用 `disposable-email-domains` 拦一次性邮箱；不管邮箱是否存在，响应一致。
  - 发信接口先用 B4 的接口，B4 没合并时用一个把邮件写日志的假实现。
- **完成标准：** 注册、登录、过期、重放、限速都有测试。

### B4 通知与邮件

- **范围：** `dealseaking/notify/`、`dealseaking/api/unsubscribe.py`、邮件模板
- **依赖：** A1、A3
- **要做：**
  - 用 A3 匹配事件 → 写 `notifications` 出箱，按 dedupe key 去重，应用冷却、静默时段、每日上限、抑制名单。
  - 即时提醒发送前复核链接：GET 请求，404 或页面里的 `availability` 显示缺货就不发。
  - 每日摘要定时任务，按用户时区。
  - Resend 客户端；HTML 加纯文本模板；邮件头带 `List-Unsubscribe` 和 `List-Unsubscribe-Post`；退订一键生效。
  - Resend webhook 收退信和投诉（校验签名），写进抑制名单。
  - 页脚有通信地址和联盟披露，都从配置读。
- **完成标准：** 用假的发信器测完整流程；同一 deal 24 小时内不重复；退订后不再发；退信后不再发。

### B5 `/go` 出站跳转

- **范围：** `dealseaking/api/go.py`
- **依赖：** A1
- **要做：** `GET /go/{offer_id}?via=web|email|push`：记录 `outbound_clicks`，按来源追加联盟参数（配置里每个来源一个模板，可以为空），然后 302 跳转；拒绝不在白名单里的目标域名。
- **完成标准：** 有测试；没配置联盟参数时跳转到原链接。

### B6 门户页面

- **范围：** `dealseaking/web/`
- **依赖：** A1、A3、B3
- **要做：**
  - 用 Jinja + htmx 做这些页面，视觉沿用 `legacy/openbox-monitor/obm/dashboard_template.html` 的风格和 tag 筛选：
    - deal feed（筛选、tag、排序、门店最后更新时间）
    - 规则列表和规则表单（表单生成规则 JSON，保存前预览过去 7 天会命中的 deal）
    - “贡献数据”页（同意说明、按 ZIP 选门店、发 / 吊销采集端 token、扩展和 Docker 的安装说明）
    - 设置（时区、静默时段、摘要时间、删除账户）
    - 条款、隐私、联盟披露三个占位页
  - 所有出站链接走 `/go`。
- **完成标准：** 新用户从注册到建好第一条规则不超过 3 分钟；手机宽度可用；Playwright 冒烟测试过。

### C1 部署与运维

- **依赖：** 前面全部；Kelvin 提供服务器和 SSH key
- **要做：**
  - 生产用的 compose 配置，Caddy 自动 HTTPS。
  - 每晚 `pg_dump` 备份到对象存储，并写一个恢复演练脚本。
  - 健康检查失败或错误率高时推送到 Kelvin 的 ntfy。
  - GitHub Actions 通过 SSH 部署。
  - `.env.example` 写全每个变量，并注明“由 Kelvin 在服务器上填写”。
- **完成标准：** 一条命令部署；做过一次恢复演练。

### C2 法务与上架文案草稿

- **要做：**
  - 服务条款、隐私政策、联盟披露、采集端同意页的草稿，写在 `docs/legal/`，要点见 `design.md` 的合规一节和决策 #6。
  - Chrome Web Store 的介绍、隐私问卷答案、图标和截图。
- **完成标准：** Kelvin 审核通过。草稿开头注明“草稿，非法律意见”。

### C3 端到端冒烟测试

- **要做：** 一个脚本跑完整流程：注册 → 建规则 → 模拟采集端上传一家店 → 产生事件 → 出箱里出现邮件 → 点 `/go` 有记录。
- **完成标准：** CI 和部署后都能跑；失败时退出码非 0。
