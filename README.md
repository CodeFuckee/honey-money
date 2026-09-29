# honey-money

A 股量化研究与交易系统（工程化骨架阶段）。目标形态：Vue 前端 + FastAPI 后端 +
PostgreSQL + 代理池，Docker Compose 一键部署（本地 / 群晖 NAS），GitLab CI 门禁。

> 当前版本 **0.1.0 是工程骨架**：五个服务的编排、健康检查与 CI 已就绪；
> 业务代码（AKShare 数据层 / Backtrader 回测 / 策略 / 教学页）在后续版本迁入。

## 仓库结构

```
backend/            Python 后端（FastAPI）
  app/main.py         应用工厂：/healthz 存活探针、/api/infra 基础设施探测
  app/infra.py        PostgreSQL / 代理池 连通性检查（快速失败、优雅降级）
  app/crawler/        东财日线爬虫：rate(按域名动态限速) / proxies(代理池经纪) /
                      eastmoney(接口客户端) / store(存储) / runner(编排) / __main__(CLI)
  tests/              63 个离线 pytest 用例（API 骨架 6 + 爬虫 57）
frontend/           Vue3 + Vite 前端
  src/App.vue         服务状态仪表盘（四卡：后端 / PostgreSQL / 代理池 / 爬虫数据）
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

- **只有 frontend 对外暴露端口**（默认 28080；DSM 自用 5000/5001 已避开），
  backend/redis/proxy_pool 仅 compose 内网互通；
- **PostgreSQL 不由本编排创建**：连接 NAS 上既有 PG 容器（宿主 5433，库/用户
  honey_money），backend 经 host.docker.internal 访问；数据持久化由该容器自理，
  本编排仅 `redis_data` 一个 named volume；
- **CPU 架构**：编排按 x86_64 常见镜像编写；ARM 机型上 postgres/redis/node 官方
  镜像均有 arm64 版，但 `jhao104/proxy_pool` 无官方 ARM 镜像，需在 NAS 上
  `docker build` 其源码仓库后替换 image 名；
- 代理池后台默认无对外端口（内部经 backend 探活）；如需查看其 Web UI，
  在 compose 中给 proxy_pool 加 ports 映射（默认端口 5010）。

## 日线数据爬虫（python -m app.crawler）

爬东方财富全市场 A 股历史日线（不复权）入 PostgreSQL，断点续爬、动态限速、可走代理池。

```bash
# NAS 部署后，在 backend 容器内运行（编排已注入 DATABASE_URL / PROXY_POOL_URL）
docker exec honey-money-backend-1 python -m app.crawler --limit 20   # 试跑前 20 只
docker exec honey-money-backend-1 python -m app.crawler              # 全量/增量
docker exec honey-money-backend-1 python -m app.crawler --only 600000,000001

# 常用参数
#   --mode incremental|full   默认 incremental：按库内断点续爬；full 全量重爬
#   --proxy-mode pool|direct  默认 pool（空池自动回退直连）；direct 全直连
#   --initial-qps / --max-qps 每通道限速上下界（AIMD 动态调整，默认 2/20）
#   --concurrency N           并发抓取协程数（默认 8）
#   --limit N / --only CODES  试跑 / 指定股票
# 退出码：0 全部成功；1 有失败（失败清单在 crawl_run.detail 与输出中）
```

行为要点：

- **断点续爬**：每只股票按库内最新交易日 +1 续拉，中断重跑自动接续；
  `(code, trade_date)` 主键幂等覆盖，当日数据盘后重拉会刷新旧值；
- **动态限速**：按目标域名分通道（`91.push2his` 等数字子域归并同通道），
  滑动窗口成功率驱动 AIMD 升降速，连续 8 次失败熔断 60s；
- **失败分类**：代理连不上（ProxyError/ConnectError）只换代理不计降速；
  对端断连/超时（RemoteProtocolError/Timeout）换代理并计入通道降速；
- **实测建议**（见 docs/ashare-daily-data-sources.md）：东财 WAF 断连免费代理的
  机房 IP 段，家宽/办公网直连低频可靠但单 IP 频控严格——NAS 生产建议
  `--proxy-mode direct --initial-qps 1`；代理池留给未来新浪/腾讯等多源轮换。

数据表：`stock_basic`（代码/市场/名称快照）、`daily_bar`（日线，约 5500 股 ×
全史 ≈ 2500 万行）、`crawl_run`（每次运行汇总与失败清单）。全量首爬约 5600 个
请求（1 股 1 请求拿全史），1 QPS 下约 1.5 小时；此后每日增量数分钟。

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

1. 数据层：✅ 东财日线爬虫已落地（app/crawler，见上节）；待做：复权因子、
   财务/基本面数据、每日增量定时任务（NAS 计划任务或调度服务）；
2. 回测引擎与策略（原 backtrader 体系迁入 backend，暴露回测 API）；
3. 前端页面：双均线教学页移植为 Vue 组件 + 回测结果展示；
4. 实盘信号与调仓单（原 live 体系）。
