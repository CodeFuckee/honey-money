# Changelog

遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，语义化版本。
（注：本仓库 2026-09-28 重建，v0.1.0 之前的本地历史未迁移。）

## [0.1.1] - 2026-09-28

### Changed
- PostgreSQL 改用 NAS 上既有容器（宿主 5433，库/用户 honey_money）：
  移除编排内 postgres 服务与 pg_data 卷，backend 经
  extra_hosts host.docker.internal:host-gateway 直连；DATABASE_URL 可用
  .env 覆盖。nas-diag 增加 /api/infra 端到端检查（PG+代理池状态）。

## [0.1.0] - 2026-09-28

### Added
- 工程化骨架（monorepo）：backend（FastAPI 最小 API：/healthz、/api/infra
  基础设施探测，PG/代理池不可达时优雅降级）+ frontend（Vue3+Vite 服务状态
  仪表盘，nginx 反代 /api）。
- docker-compose 五服务编排：frontend（唯一对外端口，默认 8080）/ backend /
  postgres:16（healthcheck + 持久卷）/ redis:7 / jhao104 代理池；.env.example。
- backend/.frontend 各自多阶段 Dockerfile 与 .dockerignore。
- GitLab CI：backend-test 流水线（pytest）。
- 测试：后端 6 个离线用例 + Playwright E2E 1 条（动态端口自拉双服务，
  覆盖页面渲染与优雅降级路径）。

### 决策记录
- PostgreSQL 完全替代 SQLite（storage 层后续用 SQLAlchemy 重写）。
- 单端口暴露策略：仅前端对外，其余服务内网互通。
- vite preview 在 Node 26/Windows 默认仅绑 IPv6 ::1，E2E 显式 --host 127.0.0.1。
