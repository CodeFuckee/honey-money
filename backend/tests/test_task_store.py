"""任务存储层测试：用假 psycopg 连接验证 SQL 与参数（离线，无真实 PG）。

覆盖：crawl_task 表 DDL、预置任务幂等播种、任务 CRUD、crawl_run 关联查询
（task_id/trigger 列、ran_today 只认计划触发、最近运行列表）。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.task_store import TaskStore  # noqa: E402


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.executed: list[str] = []
        self.execute_calls: list[tuple] = []
        self.executemany_calls: list[tuple[str, list]] = []
        self.fetchone_rows: list = []
        self.fetchall_rows: list = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        self.executed.append(query)
        self.execute_calls.append((query, params))

    def executemany(self, query, params_seq):
        self.executemany_calls.append((query, list(params_seq)))

    def fetchone(self):
        return self.fetchone_rows[0] if self.fetchone_rows else None

    def fetchall(self):
        return self.fetchall_rows


class FakeConn:
    def __init__(self):
        self.commits = 0
        self.cursor_obj = FakeCursor(self)

    def cursor(self):
        return self.cursor_obj

    def commit(self):
        self.commits += 1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def conn():
    return FakeConn()


# list_tasks 的返回行形状（id, name, source, mode, schedule_time, enabled）
TASK_ROW = (1, "新浪每日增量", "sina", "incremental", "18:00", False)


class TestSchema:
    def test_init_schema_creates_crawl_task(self, conn):
        TaskStore().init_schema(conn)
        ddl = "\n".join(conn.cursor_obj.executed)
        assert "CREATE TABLE IF NOT EXISTS crawl_task" in ddl
        # crawl_run 的 task_id/trigger_kind 关联列由 crawler.store 负责补建
        assert "ALTER TABLE crawl_run" not in ddl

    def test_init_schema_seeds_default_task(self, conn):
        """预置任务：新浪每日增量 18:00 默认禁用；幂等（WHERE NOT EXISTS 播种）。"""
        TaskStore().init_schema(conn)
        seed = [c for c in conn.cursor_obj.execute_calls
                if "INSERT INTO crawl_task" in c[0]]
        assert seed, "未播种预置任务"
        sql, params = seed[0]
        assert "WHERE NOT EXISTS" in sql, "播种必须幂等（WHERE NOT EXISTS 防重）"
        assert params == ("新浪每日增量", "sina", "incremental", "18:00", False)


class TestCrud:
    def test_list_tasks_parses_rows(self, conn):
        conn.cursor_obj.fetchall_rows = [TASK_ROW]
        tasks = TaskStore().list_tasks(conn)
        assert tasks == [{"id": 1, "name": "新浪每日增量", "source": "sina",
                          "mode": "incremental", "schedule_time": "18:00",
                          "enabled": False}]

    def test_get_task_found_and_missing(self, conn):
        conn.cursor_obj.fetchone_rows = [TASK_ROW]
        task = TaskStore().get_task(conn, 1)
        assert task["id"] == 1
        assert "WHERE id = %s" in conn.cursor_obj.executed[-1]
        conn.cursor_obj.fetchone_rows = []
        assert TaskStore().get_task(conn, 99) is None

    def test_create_task_inserts(self, conn):
        conn.cursor_obj.fetchone_rows = [TASK_ROW]
        task = TaskStore().create_task(conn, source="sina", mode="incremental",
                                       schedule_time="18:00", name=None)
        assert task["id"] == 1
        sql, params = conn.cursor_obj.execute_calls[-1]
        assert "INSERT INTO crawl_task" in sql
        # name 为空时用「{源中文名}·{模式}」兜底
        assert params[0] == "新浪·增量"

    def test_create_task_custom_name(self, conn):
        conn.cursor_obj.fetchone_rows = [(2, "补数", "eastmoney", "full", "03:00", False)]
        TaskStore().create_task(conn, source="eastmoney", mode="full",
                                schedule_time="03:00", name="补数")
        _, params = conn.cursor_obj.execute_calls[-1]
        assert params[0] == "补数"

    def test_update_task_partial_fields_only(self, conn):
        """部分更新：只 SET 传入的字段，其余不动。"""
        conn.cursor_obj.fetchone_rows = [TASK_ROW]
        TaskStore().update_task(conn, 1, enabled=True)
        sql, params = conn.cursor_obj.execute_calls[-1]
        assert "SET enabled = %s" in sql
        assert params == (True, 1)
        assert "source" not in sql.split("SET")[1].split("WHERE")[0]

    def test_update_task_multi_fields(self, conn):
        conn.cursor_obj.fetchone_rows = [TASK_ROW]
        TaskStore().update_task(conn, 1, source="eastmoney", mode="full",
                                schedule_time="03:00", enabled=True)
        sql, params = conn.cursor_obj.execute_calls[-1]
        set_clause = sql.split("SET")[1].split("WHERE")[0]
        for col in ("source", "mode", "schedule_time", "enabled"):
            assert col in set_clause
        assert params == ("eastmoney", "full", "03:00", True, 1)

    def test_update_task_no_fields_raises(self, conn):
        with pytest.raises(ValueError):
            TaskStore().update_task(conn, 1)

    def test_delete_task(self, conn):
        conn.cursor_obj.rowcount = 1
        assert TaskStore().delete_task(conn, 1) is True
        assert "DELETE FROM crawl_task" in conn.cursor_obj.executed[-1]
        conn.cursor_obj.rowcount = 0
        assert TaskStore().delete_task(conn, 99) is False


class TestCrawlRunLink:
    def test_ran_today_queries_planned_triggers_only(self, conn):
        """「今天已跑」只认计划触发（schedule/catch_up）——手动触发不抑制当晚调度。"""
        conn.cursor_obj.fetchone_rows = [(1,)]
        assert TaskStore().ran_today(conn, 5, "2026-10-09") is True
        sql, params = conn.cursor_obj.execute_calls[-1]
        assert "crawl_run" in sql and "task_id = %s" in sql
        assert "'schedule'" in sql and "'catch_up'" in sql
        assert "manual" not in sql, "手动触发不得计入「今日已计划执行」"

    def test_ran_today_none_when_no_row(self, conn):
        assert TaskStore().ran_today(conn, 5, "2026-10-09") is False

    def test_recent_runs_shape(self, conn):
        conn.cursor_obj.fetchall_rows = [(
            "2026-10-09T18:00:00+08:00", "2026-10-09T18:40:00+08:00",
            "incremental", "sina", 5560, 2, 11120, False, "schedule",
        )]
        runs = TaskStore().recent_runs(conn, task_id=1, limit=20)
        assert len(runs) == 1
        r = runs[0]
        assert r["ok_stocks"] == 5560 and r["trigger"] == "schedule"
        assert "ORDER BY id DESC" in conn.cursor_obj.executed[-1]
        assert "LIMIT" in conn.cursor_obj.executed[-1]
