# Changelog

遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，语义化版本。
（注：本仓库 2026-09-28 重建，v0.1.0 之前的本地历史未迁移。）

## [Unreleased]

### Added
- 东财 A 股历史日线爬虫（backend/app/crawler/，CLI：`python -m app.crawler`）：
  - 沪深 A 股全量清单（clist 分页，实测 5561 只）+ 全史日线（push2his
    单请求全史，fqt=0 不复权）；
  - 按域名分通道动态限速：滑动窗口成功率 AIMD（升/降速）+ 连续失败熔断
    冷却（数字前缀子域归并同通道）；
  - 代理池经纪：jhao104/proxy_pool `/get/` 取代理、失败 `/delete/` 降分、
    成功复用、空池/池挂自动回退直连；`--proxy-mode direct|pool` 可切换；
  - PostgreSQL 落库：stock_basic / daily_bar（(code,trade_date) 主键幂等
    覆盖更新）/ crawl_run 运行汇总；断点续爬（库内每股最新日期 +1 续拉）；
  - 失败隔离：传输层失败换代理重试，单股最终失败仅记录不中断整体；
    退出码区分成败便于计划任务告警；
  - 测试：限速器/代理经纪/东财客户端/存储/编排/CLI 共 57 个离线用例
    （backend/tests/test_crawler_*.py，MockTransport/假时钟全离线）。

### 决策记录（实测驱动）
- 东财 WAF 实测断连免费代理的机房 IP 段（19 个免费代理 0 成功），家宽直连
  低频可靠但单 IP 频控严格（短时密集请求触发分钟级封禁）→ 生产建议
  `--proxy-mode direct --initial-qps 1`；代理池保留给未来其他数据源。
- 传输层异常分类：ProxyError/ConnectError 归代理问题（换代理重试，不计通道
  降速）；RemoteProtocolError/Timeout 归目标层瞬时故障（换代理 + 降速）。

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
