"""爬虫任务持久化：crawl_task 表 CRUD + crawl_run 关联查询。

无状态设计（同 crawler/store.py）：所有方法接收已打开的 psycopg 连接，
事务边界由调用方控制，便于离线假连接测试。

表：
- crawl_task  任务定义（名称/源/模式/每日执行时刻/启用）——调度器的唯一
  事实来源，重启后据此恢复调度；
- crawl_run   复用爬虫运行表；其 task_id/trigger_kind 关联列由
  crawler.store 的 init_schema 负责补建（CLI 与调度器共用该表）。

预置任务：init_schema 在空表时播种「新浪每日增量 18:00」默认禁用——
开个开关即可用，同时不会被动的默默开始爬。

「今日已计划执行」（ran_today）只认 schedule/catch_up 触发——手动触发
是显式意图，不抑制当晚的计划调度；增量幂等，多跑一次只是空转。
"""

import datetime as dt

import psycopg

SOURCE_NAMES = {"sina": "新浪", "eastmoney": "东财"}
MODE_NAMES = {"incremental": "增量", "full": "全量"}

_DDL_TASK = """
CREATE TABLE IF NOT EXISTS crawl_task (
  id            BIGSERIAL PRIMARY KEY,
  name          VARCHAR(64) NOT NULL,
  source        VARCHAR(16) NOT NULL,
  mode          VARCHAR(16) NOT NULL,
  schedule_time TIME NOT NULL,
  enabled       BOOLEAN NOT NULL DEFAULT false,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

# 预置任务：WHERE NOT EXISTS 保证幂等（重复 init 不重播、用户任务不受影响）
_SEED = """
INSERT INTO crawl_task (name, source, mode, schedule_time, enabled)
SELECT %s, %s, %s, %s::time, %s
WHERE NOT EXISTS (SELECT 1 FROM crawl_task)
"""

_TASK_COLS = ("id, name, source, mode, "
              "to_char(schedule_time, 'FMHH24:MI') AS schedule_time, enabled")

_RAN_TODAY = """
SELECT 1 FROM crawl_run
WHERE task_id = %s AND trigger_kind IN ('schedule', 'catch_up')
  AND started_at::date = %s::date
LIMIT 1
"""

_RECENT_RUNS = """
SELECT started_at, finished_at, mode, source, ok_stocks, failed_stocks,
       bars_upserted, aborted, trigger_kind
FROM crawl_run WHERE task_id = %s
ORDER BY id DESC LIMIT %s
"""


def _row_to_task(row) -> dict:
    return {"id": row[0], "name": row[1], "source": row[2], "mode": row[3],
            "schedule_time": row[4], "enabled": bool(row[5])}


class TaskStore:
    def init_schema(self, conn) -> None:
        with conn.cursor() as cur:
            cur.execute(_DDL_TASK)
            cur.execute(_SEED, ("新浪每日增量", "sina", "incremental", "18:00", False))
            # 预置手动任务（按名字幂等）：退市名单变化慢，季度手动触发一次即可
            cur.execute(
                "INSERT INTO crawl_task (name, source, mode, schedule_time, enabled)"
                " SELECT '退市股历史补爬', 'delisted', 'full', NULL::time, FALSE"
                " WHERE NOT EXISTS (SELECT 1 FROM crawl_task"
                "                    WHERE name = '退市股历史补爬')")
    def list_tasks(self, conn) -> list[dict]:
        with conn.cursor() as cur:
            cur.execute(f"SELECT {_TASK_COLS} FROM crawl_task ORDER BY id")
            return [_row_to_task(r) for r in cur.fetchall()]

    def get_task(self, conn, task_id: int) -> dict | None:
        with conn.cursor() as cur:
            cur.execute(f"SELECT {_TASK_COLS} FROM crawl_task WHERE id = %s",
                        (task_id,))
            row = cur.fetchone()
        return _row_to_task(row) if row else None

    def create_task(self, conn, *, source: str, mode: str, schedule_time: str,
                    name: str | None = None) -> dict:
        if not name:
            name = f"{SOURCE_NAMES.get(source, source)}·{MODE_NAMES.get(mode, mode)}"
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO crawl_task (name, source, mode, schedule_time, enabled) "
                "VALUES (%s, %s, %s, %s::time, false) RETURNING id",
                (name, source, mode, schedule_time))
            task_id = cur.fetchone()[0]
        return {"id": task_id, "name": name, "source": source, "mode": mode,
                "schedule_time": schedule_time, "enabled": False}

    def update_task(self, conn, task_id: int, *, source: str | None = None,
                    mode: str | None = None, schedule_time: str | None = None,
                    enabled: bool | None = None) -> dict:
        """部分更新：只 SET 传入的字段；无可更新字段直接拒绝。"""
        fields = {"source": source, "mode": mode,
                  "schedule_time": schedule_time, "enabled": enabled}
        sets = {k: v for k, v in fields.items() if v is not None}
        if not sets:
            raise ValueError("无字段可更新")
        cols = {k: f"{k} = %s{'::time' if k == 'schedule_time' else ''}"
                for k in sets}
        with conn.cursor() as cur:
            cur.execute(f"UPDATE crawl_task SET {', '.join(cols.values())} "
                        f"WHERE id = %s", (*sets.values(), task_id))
        return {"id": task_id, **sets}

    def delete_task(self, conn, task_id: int) -> bool:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM crawl_task WHERE id = %s", (task_id,))
            return cur.rowcount > 0

    def ran_today(self, conn, task_id: int, day: str) -> bool:
        """当天是否已执行过计划内运行（schedule/catch_up；manual 不算）。"""
        with conn.cursor() as cur:
            cur.execute(_RAN_TODAY, (task_id, day))
            return cur.fetchone() is not None

    def recent_runs(self, conn, task_id: int, limit: int = 20) -> list[dict]:
        """该任务最近运行记录（新→旧），供任务页历史列表。"""

        def iso(v):
            return v.isoformat() if isinstance(v, (dt.datetime, dt.date)) else v

        with conn.cursor() as cur:
            cur.execute(_RECENT_RUNS, (task_id, limit))
            return [{
                "started_at": iso(r[0]), "finished_at": iso(r[1]),
                "mode": r[2], "source": r[3], "ok_stocks": r[4],
                "failed_stocks": r[5], "bars_upserted": r[6],
                "aborted": bool(r[7]), "trigger": r[8],
            } for r in cur.fetchall()]
