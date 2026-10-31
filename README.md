# honey-money

A 股量化研究与交易系统（工程化骨架阶段）。目标形态：Vue 前端 + FastAPI 后端 +
PostgreSQL + 代理池，Docker Compose 一键部署（本地 / 群晖 NAS），GitLab CI 门禁。

> 当前版本 **0.1.0 是工程骨架**：五个服务的编排、健康检查与 CI 已就绪；
> 业务代码（AKShare 数据层 / Backtrader 回测 / 策略 / 教学页）在后续版本迁入。

## 仓库结构

```
backend/            Python 后端（FastAPI）
  app/main.py         应用工厂：/healthz 存活探针、/api/infra 基础设施探测、
                      /api/tasks 爬虫任务管理
  app/infra.py        PostgreSQL / 代理池 连通性检查（快速失败、优雅降级）
  app/crawler/        日线爬虫：rate(按域名动态限速) / proxies(代理池经纪) /
                      eastmoney(接口客户端) / sina(新浪客户端) / store(存储) /
                      runner(编排) / __main__(CLI)
  app/task_store.py   爬虫任务持久化（crawl_task 表 CRUD + 预置任务播种）
  app/scheduler.py    进程内任务调度器（每日到点/启动补跑/全局串行/advisory lock 互斥）
  app/tasks_api.py    任务管理 API（CRUD/触发/取消/停止/历史）
  app/adjust.py       复权因子（懒加载入库；后/前复权合成；降级不复权）
  app/backtest/       vectorbt 回测引擎（信号/指标纯 pandas，六大经典策略）
  app/backtest_api.py 回测 API（策略/区间/撮合模式/成本，降级 200 约定）
  app/dca_store.py    定投止盈回测快照存储（fund_dca_result / fund_dca_meta 两表；
                      python -m app.dca_store <csv> 导入 scripts 离线回测产物，幂等）
  app/dca_api.py      /api/funds/dca 回测排行 API（搜索/在市过滤/排序白名单/
                      分页 + 在市全集分位统计 + 口径 meta，降级 200 约定）
  tests/              离线 pytest 用例（API 骨架 + 爬虫 + 任务调度）
frontend/           Vue3 + Vite 前端
  src/App.vue         服务仪表盘（Carbon 风格）：四状态（加载骨架/空态插画/
                      错误横幅/降级占位）+ 数据·任务·回测·教学页 +
                      数据源子页（新浪/东财来源筛选变体）+ 基金净值子页
                      （列表含类型/最新单位净值/日增长率/净值条数/区间，
                      点行进净值详情：单位+累计净值双线走势 + 分页明细）+
                      定投止盈回测子页（全市场公募基金「月定投+20% 止盈」
                      离线回测排行：7 张统计卡 + 服务端排序表格，搜索/
                      运作年限与规模输入过滤（>N 年 / >N 亿）/分页/排序
                      同步 URL + 止盈间隔列（≈N 月/次，点行弹窗看每轮
                      止盈日/间隔/持有天数/实际收益率，fund_dca_rounds
                      表 35k 轮全市场明细）；下方「年限 × 规模 × 年化」
                      可旋转三维散点
                      （原生 canvas 自绘引擎：拖拽旋转/滚轮缩放/悬停明细，
                      每基金一点）+ 年限段×规模段分箱统计矩阵与自动结论；
                      数据链路见 scripts/）；
                      「任务」页管理每日爬虫任务（启停/手动触发/实时进度/
                      历史/上次失败原因展示）；
                      数据页点行进个股详情（ECharts K线近250根 + 全字段
                      明细分页 + 新浪/东财来源切换，返回保留列表状态；
                      列表含「退市」列标已退市股，可按退市状态过滤
                      全部/仅在市/仅退市）；
                      教学页六大经典回测策略（vectorbt 引擎，撮合模式可切换
                      对比未来函数，净值对照买入持有）+ 单股/批量/全市场
                      三模式分段切换 + 🌐 全量回测（同策略全市场后台任务：
                      过滤面板 剔ST/最少K线数/含退市股/选股域——流通市值
                      区间+一手价上限（参考=区间首日收盘×流通股本，退市股
                      豁免市值维度）、实时进度可取消、
                      统计卡/Top20 叠加图/收益排行表分页排序/CSV 导出；
                      动量轮动在全市场为组合级（月末动量 top_k，单组合行
                      _PORTFOLIO，池护栏 max_universe 默认 1000））；
                      退市股历史补爬（--source delisted / 任务页手动任务，
                      两所退市名单 367 只 + 新浪 klc_kl 全史，修幸存者偏差）
  nginx.conf          生产：静态托管 + /api 反代后端
deploy/             （预留：将来放 NAS 专属编排变体）
tests/e2e/          Playwright E2E（自拉后端 uvicorn + 前端 vite preview）
docker-compose.yml  五服务编排（frontend/backend/postgres/redis/proxy_pool）
.gitlab-ci.yml      main 流水线：backend-test（pytest 全绿才可部署）
.env.example        环境变量模板（复制为 .env，改密码/端口）
docs/               调研文档（A 股日线数据源实测等）
scripts/            离线研究/数据管道脚本（不入 CI）：fund_scale_fetch*.py
                    （东财规模双通道 gmbd/pingzhongdata）/ fund_nav_backfill.py
                    （短历史净值回补）/ fund_full_sync_missing.py（fund_basic
                    全集差集同步——ETF 等）/ fund_dca_backtest.py（定投+止盈
                    全市场回测 → scripts/data/*.csv，gitignore，经 app.dca_store
                    导入后页面可见）
```

## MCP server（agent 接入回测与行情）

`backend/app/mcp_server.py`：独立 MCP server（官方 mcp SDK），让
ZCode / Claude Desktop 等 agent 直接调用本应用。8 个工具：

- **全市场回测**（后台任务，分钟~小时级）：`run_full_backtest`（提交）→
  `get_backtest_run`（轮询）→ `get_backtest_rows`（明细排行）+
  `list_backtest_runs` / `cancel_backtest_run`。单标的策略输出逐股排行；
  选股域过滤 `filters` 可配 `min_market_cap`/`max_market_cap`（流通市值
  元，默认 20亿~200亿）、`max_lot_cost`（一手成本上限元，默认 1000 即
  股价 ≤10 元；参考时点=区间首日收盘×流通股本，退市股无股本数据豁免
  市值维度）；momentum 为组合级（月末/周末动量 top_k 等权轮动，params 可配
  `window`/`top_k`/`rebalance`（month/week），结果为单组合行
  `_PORTFOLIO`，池默认截断至 K 线数最多的 1000 只，`filters.max_universe`
  可调）；
- **单股/批量回测**（同步秒级）：`run_backtest`（含 momentum 轮动，≥2 只）；
- **行情查询**：`list_stocks`（搜索，含退市过滤）、`get_stock_bars`
  （日K，前复权/来源可选）。

大体积曲线自动降采样 ≤120 点；错误一律转含状态码与下一步指引的可读文本。

接入方式二选一：

① **stdio（本机 agent，子进程方式）**——agent 的 MCP 配置加
（目标后端可用 `HONEY_MONEY_BACKEND_URL` 覆盖，缺省 `http://127.0.0.1:8000`）：

```json
{ "mcpServers": { "honey-money": {
    "command": "<仓库>/.venv/bin/python",
    "args": ["-m", "app.mcp_server"],
    "cwd": "<仓库>/backend" } } }
```

② **streamable HTTP（远程 agent 直连 NAS）**——compose 里的 `mcp` 服务
（宿主端口 `${MCP_PORT:-28081}`），agent 配置直接填 URL：

```json
{ "mcpServers": { "honey-money": { "url": "http://<NAS内网IP>:28081/mcp" } } }
```

典型用法：全市场用 `run_full_backtest(strategy="ma_cross",
params={"fast":5,"slow":20}, range="3y")` → 轮询 `get_backtest_run` 至
`status=done` → `get_backtest_rows` 取排行；指定股票直接
`run_backtest(strategy, codes)` 同步拿结果。HTTP 传输无鉴权，仅限内网
（与 backend 同安全级别），勿对公网映射 28081。

## 本地开发

```bash
# 后端（仓库根）
.venv/Scripts/python.exe -m pip install -r backend/requirements.txt
.venv/Scripts/python.exe -m uvicorn app.main:app --port 8000 --reload --app-dir backend
# 验证：curl http://127.0.0.1:8000/healthz

# 前端（另开终端）
cd frontend && npm install && npm run dev    # 默认 5173，/api 已反代到 8000

# 本地过目/预览（构建产物 + 反代生产后端，无需本地起后端；
# 绑 0.0.0.0，局域网设备可用 http://<本机IP>:4180 访问）
npm run preview:prod                         # 反代 http://nas.example.com:28080
# 自定义后端：BACKEND_URL=http://<host>:<port> npm run preview
# 反代本地后端：BACKEND_PORT=<port> npm run preview（缺省 8000）

# 测试（后端单测 + E2E，全部离线可跑）
.venv/Scripts/python.exe -m pytest
```

## Docker 部署（本地 / 群晖 NAS 通用）

```bash
cp .env.example .env     # 必改 POSTGRES_PASSWORD；端口按需
docker compose up -d --build
# 浏览器打开 http://<主机>:8080 —— 三张卡片应全绿（本地/NAS 需可拉镜像）
```

群晖（Container Manager）：项目 → 新建 → 路径选本仓库（或 git 克隆地址）→ 
使用现有 docker-compose.yml → 按 .env 配好变量后启动。要点：

- **编排仅 2 容器**（frontend + backend）：frontend 对外暴露 28080，
  backend 仅 compose 内网；
- **PostgreSQL 不由本编排创建**：连接 NAS 上既有 PG 容器（宿主 5433，库/用户
  honey_money），backend 经 host.docker.internal 访问；数据持久化由该容器自理，
  本编排仅 `redis_data` 一个 named volume；
- **代理池用 NAS 上的公共实例**（`proxy-pool:2.4.1-aclfix` + 专用 redis 8，
  宿主 5010，多项目共用）：backend 经 `PROXY_POOL_URL` 取代理与探活，
  本编排不再自带 proxy_pool/redis（切换前的官方镜像版本见 git 历史；
  官方镜像有 alpine 无 bash 的入口坑，公共池自建时已修复）。

## 日线数据爬虫（python -m app.crawler）

爬全市场 A 股历史日线（不复权）入 PostgreSQL，多数据源、断点续爬、动态限速、防封保护。

限速默认：**东财源 drip 模式——每 30 秒 1 请求 + ±30% 随机抖动（实际 21~39s），
min=max 锁死不提速**（出口 IP 曾被封）；新浪源 1 QPS 起步 AIMD 自适应（上限 5）；
基金源 1 QPS 起步 AIMD 自适应（0.1~2.0，基金接口与 push2his 不同子系统、无被封史）。
可用 `--initial-qps/--max-qps/--min-qps/--jitter` 覆盖。

```bash
# NAS 部署后，在 backend 容器内运行（编排已注入 DATABASE_URL）
docker exec honey-money-backend-1 python -m app.crawler --limit 20   # 试跑前 20 只（新浪源）
docker exec honey-money-backend-1 python -m app.crawler              # 全量/增量（新浪源，默认）
docker exec honey-money-backend-1 python -m app.crawler --source eastmoney  # 东财源（解封后校验用）
docker exec honey-money-backend-1 python -m app.crawler --source delisted   # 退市股历史补爬（367 只，新浪 klc_kl 全史；
                                                                            #  无成交额/换手率置 NULL；与主爬虫共用限速通道）
docker exec honey-money-backend-1 python -m app.crawler --source fund       # 基金历史净值全量（2.8 万只，约 8 小时；
                                                                            #  单请求全史 upsert 幂等，落 fund_nav 表）
docker exec honey-money-backend-1 python -m app.crawler --source fund --mode incremental  # 基金快照增量
                                                                            #  （当日净值列表一次拉全市场，约 5 分钟）

# 定投止盈回测：重跑回测并刷新「定投止盈回测」页数据（本机开发环境，非容器）
#   .venv/bin/python scripts/fund_dca_backtest.py                                 # 全市场回测 → scripts/data/fund_dca_backtest.csv
#   cd backend && ../.venv/bin/python -m app.dca_store ../scripts/data/fund_dca_backtest.csv   # 导入快照（幂等，秒级）

# 常用参数
#   --source sina|eastmoney|delisted|fund  默认 sina（delisted/fund 见上）
#   --mode incremental|full  默认 incremental：按库内断点续爬（按源隔离）；full 全量重爬
#   --initial-qps/--max-qps  每通道限速（默认按源：sina 1→5，AIMD 动态调整）
#   --jitter 0.3             请求间隔随机抖动 ±30%（防封，默认开）
#   --batch-size 300 --batch-pause 30   每 300 只歇 30s（防封，默认开）
#   --abort-after 15         连续 15 只失败自动熔停保存退出（默认开）
#   --limit N / --only CODES 试跑 / 指定股票
# 退出码：0 全部成功；1 有失败或熔停（计划任务可据此告警）
```

**数据源与多源对比**：K 线表主键 `(code, trade_date, source)`，不同来源并存不覆盖——
东财解封后重爬一遍即可直接 SQL 对比两源差异。字段口径已统一（volume 手、换手率
百分数；新浪源现算振幅/涨跌幅，流通股本为新浪专有字段）。仪表盘「爬虫数据」
卡片按来源展示 K 线分布。

**防封实测背景**（详见 docs/ashare-daily-data-sources.md）：东财 WAF 断连免费代理
的机房 IP 段且单 IP 频控严格（家宽出口曾被封 15h+）→ 新浪源直连 + 保守限速 +
抖动 + 分批 + 熔停是当前生产姿态；新浪清单分页有软限流截断现象，已内置空页
重试 + 官方总数校验（不完整即报错重跑，绝不静默漏爬）。

数据表：`stock_basic`（清单快照）、`daily_bar`（日线，全市场约 2500 万行/源）、
`crawl_run`（运行汇总与失败清单，`task_id`/`trigger_kind` 关联调度任务与触发方式）、
`crawl_task`（任务定义）。新浪源全量约 5200+ 请求，1 QPS 起步（AIMD
自动升速）约 1~2 小时；此后每日增量数分钟。

**基金历史净值（source=fund，2026-10 新增）**：爬全部公募基金（东财全集
28,169 只，含已清盘与 A/C 份额——全量爬取避免回测幸存者偏差）入
`fund_basic`（代码/名称/类型/退市标记）与 `fund_nav`（净值日，主键
`(code, nav_date, source)`：单位净值/累计净值/日增长率/分红原始串）。
两种模式：
- **full 全量**：天天基金 `pingzhongdata/{code}.js` 单请求全史逐只爬
  （约 8 小时；lsjz 分页接口单页上限 20 条不可用作爬虫通道）；预置手动
  任务「基金历史净值」默认禁用。清盘基金历史保留、照常入库（防幸存者
  偏差）；源上无数据的份额代码（301/404，如后端收费份额）记 0 行跳过。
- **incremental 增量**：Fund_JJJZ_Data 当日净值列表 243 页一次拉全市场
  最新已披露净值（实测 22,404 只/223 秒/2QPS），日期按槽位日历对齐解析
  （今日未发布自动落到昨日真实日期；QDII 滞后双空自动跳过不落错行）；
  预置任务「基金每日增量」每日 21:00 默认禁用，建议晚间净值发布后运行
  （白天跑当日行缺增长率，由全量任务补）。
退市标记口径：在市=当日列表（t=1 净值 ∪ t=7 货币）并集，与全集差集=
清盘/终止；数据页基金列表可按 全部/仅在市/仅退市 过滤。免 Cookie、带
Referer 即可；货币基金无单位净值走势按跳过处理。

## 每日数据更新调度（进程内自治，无需外部计划任务）

backend 启动即拉起任务调度循环（`app/scheduler.py`），到点自动增量爬取：

- **调度时刻**：北京时间（`Asia/Shanghai`，与容器时区无关）每日 HH:MM，
  不辨交易日——非交易日照常执行，增量断点机制保证空转无害；
- **错过补跑**：到点时恰在重启/关机 → 下次启动自动补跑一次（trigger=catch_up）；
- **全局串行**：同一时刻只跑一个任务，后到自动排队；执行前取 PostgreSQL
  advisory lock——手动 CLI 与调度任务并发写库也被互斥；
- **防封参数内置**：任务仅可配 数据源/模式/时刻，限速/抖动/分批/熔停用安全
  默认值不可配；预置「新浪每日增量 18:00」默认禁用，页面上开开关即用；
- **管理**：前端「任务」页（启停/手动触发/实时进度 3s 轮询/历史）或
  `/api/tasks` 接口；`SCHEDULER_ENABLED=0` 可整体关闭调度（CLI 手动跑不受影响）。

## CI/CD

推送 main 触发两阶段流水线：

1. `backend-test`（任意 runner）：python:3.12-slim 跑 backend/tests；
2. `deploy-nas`（仅 main，runner #18 "nas"，tags `linux,docker,nas,deploy`）：
   test 全绿后自动在群晖上 `docker compose up -d --build`——该 runner 映射了
   NAS 宿主 docker，容器直接落在 NAS 上持久运行。

前置：项目 CI/CD Variables 已配置 `POSTGRES_PASSWORD`（masked）。
部署后访问 `http://<NAS>:28080`（默认端口）。手动重部署也可以在 NAS 上
直接 `docker compose -p honey-money up -d --build`。

## 路线图（业务迁入顺序）

1. 数据层：✅ 日线爬虫（新浪/东财双源）与 ✅ 每日增量定时任务（进程内调度器
   + 「任务」页管理，见上节）已落地；待做：复权因子、财务/基本面数据；
2. 回测引擎与策略：✅ vectorbt 引擎 + 六大经典策略教学页 + ✅ 复权因子
   （回测后复权口径，除权失真消除；backtrader 已停维护，改选 vectorbt）；
   待做：参数寻优；
3. 前端页面：双均线教学页移植为 Vue 组件 + 回测结果展示；
4. 实盘信号与调仓单（原 live 体系）。
