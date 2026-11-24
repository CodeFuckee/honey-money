"""NAS 部署守门的运行中任务查询（CI 侧 stdin 喂进 backend 容器执行）。

被 .gitlab-ci.yml 的 busy_query 调用：docker exec -i <backend 容器> python -
< 本文件。容器内有 psycopg 与 DATABASE_URL env，输出一行两列
「crawl_busy full_busy」（1/0），CI 据此决定重建哪些服务（见文件头注释）：
- crawl_busy=1（task_runtime 有排队/运行行，或 crawl_run 有未收尾行）
  → 延后重建 worker 与 backend（任务在 worker 进程里；backend 延后是
  守门上线初的迁移期安全——旧 backend 可能仍是调度宿主）
- full_busy=1（backtest_full_run 有 running/canceling 行）→ 延后重建
  backend（教学页全量回测仍在 backend 进程）

容错：表不存在（守门上线初旧容器还没有 task_runtime）或 PG 抖动 → 该项
视为 0（守门是尽力保护而非部署前置条件；误闲由 worker 启动清理兜底）。
本地自测：DATABASE_URL=postgresql://… python scripts/nas/busy_check.py
"""

import os

import psycopg


def q(sql: str) -> bool:
    try:
        with psycopg.connect(os.environ["DATABASE_URL"], connect_timeout=5) as c:
            return c.execute(sql).fetchone() is not None
    except Exception:
        return False


crawl = q("SELECT 1 WHERE EXISTS (SELECT 1 FROM task_runtime "
          "WHERE phase IN ('queued','running'))") \
        or q("SELECT 1 FROM crawl_run WHERE finished_at IS NULL LIMIT 1")
full = q("SELECT 1 FROM backtest_full_run "
         "WHERE status IN ('running','canceling') LIMIT 1")
print(int(crawl), int(full))
