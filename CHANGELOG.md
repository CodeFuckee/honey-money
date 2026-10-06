# Changelog

遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，语义化版本。
（注：本仓库 2026-09-28 重建，v0.1.0 之前的本地历史未迁移。）

## [Unreleased]

### Changed
- **前端按 macOS 风格 Penpot 原型重写**（App.vue 单文件，数据层逻辑不变）：
  macOS 窗口壳（渐变壁纸/红绿灯标题栏/半透明侧栏导航：仪表盘·数据·回测·教学）+
  毛玻璃卡片 + 四状态完备（加载骨架 / 空态微场景插画：显示器+爬虫机器人 SVG /
  错误横幅：矢量警告三角 / 降级虚线占位）；数据·回测·教学为 Placeholder v0.2+ 占位页。
- E2E 新增 macOS 外壳与导航切换断言（红绿灯/侧栏四项/占位页标记）。

### Added
- **新浪数据源**（默认源，`--source sina`）：akshare 新浪日线（kjs 全史，含
  换手率/流通股本）+ 自研清单分页；字段口径统一（volume 手、换手率百分数、
  振幅/涨跌幅基于前收现算）；清单完整性防护（空页重试 + 官方总数校验，
  截断即报错绝不静默漏爬）。
- **数据来源标注**：daily_bar 主键升级 (code, trade_date, source)，多源数据
  并存不覆盖，可直接 SQL 对比；crawl_run/crawl_stats/前端卡片均带来源。
- **防封四件套**（默认开启）：限速间隔随机抖动 ±30%、分批批间休眠
  （300 只/30s）、连续 15 只失败自动熔停保存退出、按源隔离断点续爬。
- 仪表盘新增「爬虫数据」卡片（K线总数/覆盖进度/待爬/来源分布/最近运行）
  + 后端 `GET /api/crawl/stats`；表未建显示"爬虫未运行"，PG 不可达优雅降级。
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
