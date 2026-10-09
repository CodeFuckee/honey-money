"""环境配置：全部来自环境变量（12-factor），本地缺省值保证离线也能启动。"""

import os

# PostgreSQL 连接串。compose 部署时会注入服务内网地址；
# 本地开发不配置则健康检查报 down（服务本身仍可启动）。
DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://honey:honey@postgres:5432/honeymoney"
)

# 代理池（jhao104/proxy_pool）Web API 地址；/get 返回一个可用代理。
PROXY_POOL_URL = os.environ.get("PROXY_POOL_URL", "http://proxy_pool:5010")

# 进程内爬虫任务调度开关（默认开）：0/false 关闭自动调度（任务 CRUD 仍可用）。
SCHEDULER_ENABLED = os.environ.get("SCHEDULER_ENABLED", "1").strip().lower() \
    not in ("0", "false", "no")

SERVICE_VERSION = "0.1.0"
