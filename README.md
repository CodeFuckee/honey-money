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
  tests/              6 个离线 pytest 用例
frontend/           Vue3 + Vite 前端
  src/App.vue         服务状态仪表盘（三卡：后端 / PostgreSQL / 代理池）
  nginx.conf          生产：静态托管 + /api 反代后端
deploy/             （预留：将来放 NAS 专属编排变体）
tests/e2e/          Playwright E2E（自拉后端 uvicorn + 前端 vite preview）
docker-compose.yml  五服务编排（frontend/backend/postgres/redis/proxy_pool）
.gitlab-ci.yml      main 流水线：backend-test（pytest 全绿才可部署）
.env.example        环境变量模板（复制为 .env，改密码/端口）
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

1. 数据层：AKShare 拉取 + PostgreSQL 存储（替代原 SQLite 方案，SQLAlchemy），
   拉数经代理池（根治东财 IP 限流）；
2. 回测引擎与策略（原 backtrader 体系迁入 backend，暴露回测 API）；
3. 前端页面：双均线教学页移植为 Vue 组件 + 回测结果展示；
4. 实盘信号与调仓单（原 live 体系）。
