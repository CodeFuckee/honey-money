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
  tests/              离线 pytest 用例（API 骨架 + 爬虫 + 任务调度）
frontend/           Vue3 + Vite 前端
  src/App.vue         服务仪表盘（Carbon 风格）：四状态（加载骨架/空态插画/
                      错误横幅/降级占位）+ 数据·任务·回测·教学页 +
                      数据源子页（新浪/东财来源筛选变体）；「任务」页管理
                      每日爬虫任务（启停/手动触发/实时进度/历史）；
                      数据页点行进个股详情（ECharts K线近250根 + 全字段
                      明细分页 + 新浪/东财来源切换，返回保留列表状态）
  nginx.conf          生产：静态托管 + /api 反代后端
deploy/             （预留：将来放 NAS 专属编排变体）
tests/e2e/          Playwright E2E（自拉后端 uvicorn + 前端 vite preview）
docker-compose.yml  五服务编排（frontend/backend/postgres/redis/proxy_pool）
.gitlab-ci.yml      main 流水线：backend-test（pytest 全绿才可部署）
.env.example        环境变量模板（复制为 .env，改密码/端口）
docs/               调研文档（A 股日线数据源实测等）
```

## 本地开发

```bash
# 后端（仓库根）
.venv/Scripts/python.exe -m pip install -r backend/requirements.txt
.venv/Scripts/python.exe -m uvicorn app.main:app --port 8000 --reload --app-dir backend
# 验证：curl http://127.0.0.1:8000/healthz

# 前端（另开终端）
cd frontend && npm install && npm run dev    # 默认 5173，/api 已反代到 8000

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
min=max 锁死不提速**（出口 IP 曾被封）；新浪源 1 QPS 起步 AIMD 自适应（上限 5）。
可用 `--initial-qps/--max-qps/--min-qps/--jitter` 覆盖。

```bash
# NAS 部署后，在 backend 容器内运行（编排已注入 DATABASE_URL）
docker exec honey-money-backend-1 python -m app.crawler --limit 20   # 试跑前 20 只（新浪源）
docker exec honey-money-backend-1 python -m app.crawler              # 全量/增量（新浪源，默认）
docker exec honey-money-backend-1 python -m app.crawler --source eastmoney  # 东财源（解封后校验用）

# 常用参数
#   --source sina|eastmoney  默认 sina（直连可用，含换手率/流通股本；东财源见下）
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
2. 回测引擎与策略（原 backtrader 体系迁入 backend，暴露回测 API）；
3. 前端页面：双均线教学页移植为 Vue 组件 + 回测结果展示；
4. 实盘信号与调仓单（原 live 体系）。
