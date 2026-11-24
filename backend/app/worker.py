"""任务执行 worker 独立入口：python -m app.worker。

部署拓扑（2026-11-24）：backend 容器只跑 API（compose 设
SCHEDULER_ENABLED=0），本进程独占任务执行——流水线重建 backend 不再打断
运行中任务；worker 容器由 CI 空闲守门保护（task_runtime 有排队/运行行或
crawl_run 未收尾 → 延后重建，见 .gitlab-ci.yml deploy-nas，每小时定时
流水线在空闲后补部署）。

启动序：schema 兜底（app/startup.py 清单，与 backend 共用单一事实源）→
遗留运行清理（上一代 worker 被杀的 crawl_run 行标 aborted、task_runtime
running 行回 idle；排队行保留=队列跨重启存活）→ 调度循环常驻
（scan_once 到期判定 + catch_up 补跑，见 app/scheduler.py）。

SIGTERM/kill 无需优雅收尾：行未收尾正是下次启动清理的输入；merge 写入
单事务，进程死=回滚无半截数据（先例：backend 单进程模式 lifespan 同款
兜底，此处是唯一执行者后从 backend 挪来）。
"""

import asyncio

import psycopg

from . import config, startup
from .scheduler import PgAdvisoryLock, TaskScheduler, build_crawl_executor
from .task_store import TaskStore


async def _amain() -> None:
    connect = psycopg.connect
    await asyncio.to_thread(startup.run_schema_inits, connect, TaskStore())
    await asyncio.to_thread(startup.cleanup_interrupted_runs, connect)
    scheduler = TaskScheduler(
        dsn=config.DATABASE_URL,
        executor=build_crawl_executor(config.DATABASE_URL, config.PROXY_POOL_URL),
        lock=PgAdvisoryLock(config.DATABASE_URL))
    await scheduler.start()
    print(f"[worker] 任务执行 worker 已启动（调度常驻，poll="
          f"{scheduler._poll_interval:.0f}s；SIGTERM/kill 退出由下次启动清理兜底）")
    while True:
        await asyncio.sleep(3600)


def main() -> None:
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        print("[worker] 收到中断，退出（运行态行由下次启动清理兜底）")


if __name__ == "__main__":
    main()
