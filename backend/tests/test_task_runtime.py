"""task_runtime 存储层测试：假 psycopg 连接验证 SQL 形状与参数（离线无 PG）。

重点保护三个跨进程语义（任务执行独立 worker 后 API/worker 全靠这张表）：
- push 原子拒绝（ON CONFLICT ... WHERE phase='idle'——queued/running 不被覆盖）
- pop_next 消费置 running（单 worker 语义）
- mark_running_idle 只动 running 行（排队行保留=队列跨 worker 重启存活）
"""

import sys
from datetime import datetime as _dt
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.task_runtime import PgTaskControl, SH_TZ, TaskRuntime  # noqa: E402


class _Cur:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.conn.executed.append((sql, params))

    def fetchone(self):
        return self.conn.fetchone_row

    def fetchall(self):
        return self.conn.fetchall_rows

    @property
    def rowcount(self):
        return self.conn.rowcount


class FakeConn:
    def __init__(self, *, fetchone=None, fetchall=None, rowcount=1):
        self.fetchone_row = fetchone
        self.fetchall_rows = fetchall or []
        self.rowcount = rowcount
        self.executed = []                      # [(sql, params)]
        self.commits = 0

    def cursor(self):
        return _Cur(self)

    def commit(self):
        self.commits += 1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def make_runtime(conn):
    return TaskRuntime("postgresql://fake", connect=lambda *a, **k: conn)


def last_sql(conn):
    return conn.executed[-1][0]


class TestSchema:
    def test_ensure_schema_creates_table(self):
        conn = FakeConn()
        make_runtime(conn).ensure_schema(conn)
        assert "CREATE TABLE IF NOT EXISTS task_runtime" in conn.executed[0][0]


class TestPush:
    def test_push_executes_conditional_upsert(self):
        """入队走条件 upsert：WHERE phase='idle' 保证 queued/running 拒绝
        是 SQL 原子的（API 入队与 worker 状态写不共享进程，靠不了内存判断）。"""
        conn = FakeConn(rowcount=1)
        assert make_runtime(conn).push(conn, 1, "manual") is True
        sql, params = conn.executed[0]
        assert "ON CONFLICT (task_id) DO UPDATE" in sql
        assert "WHERE task_runtime.phase = 'idle'" in sql
        assert params == (1, "manual")

    def test_push_rejected_reports_false(self):
        """rowcount=0（WHERE 未命中：已在排队/运行中）→ False（409 依据）。"""
        conn = FakeConn(rowcount=0)
        assert make_runtime(conn).push(conn, 1, "manual") is False

    def test_trigger_kind_column_not_reserved_word(self):
        """列名用 trigger_kind（crawl_run 先例）——trigger 是 PG 保留字，
        裸列名会语法错。"""
        conn = FakeConn(rowcount=1)
        make_runtime(conn).push(conn, 1, "schedule")
        assert "trigger_kind" in last_sql(conn)


class TestPopNext:
    def test_empty_queue_returns_none(self):
        conn = FakeConn(fetchone=None)
        assert make_runtime(conn).pop_next(conn) is None
        assert len(conn.executed) == 1, "空队列不应发 UPDATE"

    def test_pop_marks_running(self):
        conn = FakeConn(fetchone=(7, "catch_up"))
        entry = make_runtime(conn).pop_next(conn)
        assert entry == {"task_id": 7, "trigger": "catch_up"}
        sql, params = conn.executed[1]
        assert "phase = 'running'" in sql
        assert params == (7,)
        assert "stop_requested = false" in sql, "重入运行必须复位停止标志"


class TestCancelAndStop:
    def test_cancel_queued_targets_queued_only(self):
        conn = FakeConn(rowcount=1)
        assert make_runtime(conn).cancel_queued(conn, 3) is True
        sql, params = conn.executed[0]
        assert "phase = 'queued'" in sql
        assert params == (3,)

    def test_cancel_not_queued_reports_false(self):
        conn = FakeConn(rowcount=0)
        assert make_runtime(conn).cancel_queued(conn, 3) is False

    def test_request_stop_targets_running_only(self):
        conn = FakeConn(rowcount=1)
        assert make_runtime(conn).request_stop(conn, 5) is True
        sql, params = conn.executed[0]
        assert "stop_requested = true" in sql
        assert "phase = 'running'" in sql

    def test_is_stop_requested_missing_row_is_false(self):
        conn = FakeConn(fetchone=None)
        assert make_runtime(conn).is_stop_requested(conn, 5) is False


class TestProgressAndFinish:
    def test_set_progress_jsonb(self):
        conn = FakeConn(rowcount=1)
        make_runtime(conn).set_progress(conn, 2, 1, 3)
        sql, params = conn.executed[0]
        assert "progress = %s" in sql
        payload = params[0]
        assert getattr(payload, "obj", payload) == {"done": 1, "total": 3}
        assert params[1] == 2

    def test_finish_resets_and_records_result(self):
        """收尾：回 idle、记 last_result、清进度/停止标志——last_error 的
        唯一出口（执行体异常发生在 start_run 前时 crawl_run 无记录）。"""
        conn = FakeConn(rowcount=1)
        result = {"trigger": "manual", "error": True, "detail": "boom"}
        make_runtime(conn).finish(conn, 4, result)
        sql, params = conn.executed[0]
        assert "phase = 'idle'" in sql
        assert "last_result = %s" in sql
        assert "stop_requested = false" in sql
        assert getattr(params[0], "obj", params[0]) == result

    def test_mark_idle_keeps_last_result(self):
        """取消/任务被删：只清队列痕迹，不动 last_result（历史异常仍可查）。"""
        conn = FakeConn(rowcount=1)
        make_runtime(conn).mark_idle(conn, 4)
        sql, _ = conn.executed[0]
        assert "phase = 'idle'" in sql
        assert "last_result" not in sql


class TestStartupCleanup:
    def test_mark_running_idle_keeps_queue(self):
        """启动兜底只把 running 回 idle——排队行必须保留（队列跨 worker
        重启存活是 PG 化的收益；误清排队=手动触发悄悄丢失）。"""
        conn = FakeConn(rowcount=1)
        make_runtime(conn).mark_running_idle(conn)
        sql, _ = conn.executed[0]
        assert "WHERE phase = 'running'" in sql
        assert "'queued'" not in sql

    def test_delete_removes_row(self):
        conn = FakeConn(rowcount=1)
        make_runtime(conn).delete(conn, 9)
        sql, params = conn.executed[0]
        assert "DELETE FROM task_runtime" in sql
        assert params == (9,)


class TestSnapshot:
    def test_snapshot_shape_and_order(self):
        """快照映射：phases 全行（含 idle）、progress 仅非空、queue 按
        enqueued_at 序（SQL 排序，假件行按序给）、running 取首个。"""
        rows = [(1, "running", {"done": 1, "total": 2}, None),
                (2, "queued", None, "t1"),
                (3, "idle", None, None),
                (4, "queued", None, "t2")]
        conn = FakeConn(fetchall=rows)
        state = make_runtime(conn).snapshot(conn)
        assert state["phases"] == {1: "running", 2: "queued",
                                   3: "idle", 4: "queued"}
        assert state["progress"] == {1: {"done": 1, "total": 2}}
        assert state["queue"] == [2, 4]
        assert state["running_task_id"] == 1

    def test_snapshot_empty(self):
        conn = FakeConn(fetchall=[])
        state = make_runtime(conn).snapshot(conn)
        assert state == {"phases": {}, "progress": {}, "queue": [],
                         "running_task_id": None}

    def test_last_result(self):
        conn = FakeConn(fetchone=({"error": False, "ok": 3},))
        assert make_runtime(conn).last_result(conn, 1) == {"error": False,
                                                           "ok": 3}


class TestPgTaskControl:
    """API 控制面：指令行写到 runtime、快照补 server_time、last_error 规则。"""

    def make_control(self, conn, clock):
        rt = TaskRuntime("postgresql://fake", connect=lambda *a, **k: conn)
        return PgTaskControl("postgresql://fake",
                             connect=lambda *a, **k: conn,
                             runtime=rt, clock=clock)

    def test_runtime_state_adds_server_time(self):
        conn = FakeConn(fetchall=[])
        fixed = _dt(2026, 10, 9, 12, 0, tzinfo=SH_TZ)
        ctrl = self.make_control(conn, clock=lambda: fixed)
        state = ctrl.runtime_state()
        assert state["server_time"] == "2026-10-09T12:00:00+08:00"

    def test_request_run_pushes_manual(self):
        conn = FakeConn(rowcount=1)
        ctrl = self.make_control(conn, clock=lambda: None)
        assert ctrl.request_run(1) is True
        _, params = conn.executed[0]
        assert params == (1, "manual")

    def test_last_error_only_when_error(self):
        """error=True 透出 detail；正常结束/未跑过 None（不误报）。"""
        ok_conn = FakeConn(fetchone=({"error": False, "ok": 3},))
        assert self.make_control(ok_conn, lambda: None).last_error(1) is None
        err_conn = FakeConn(fetchone=({"error": True,
                                       "detail": "UndefinedTable: x"},))
        assert "UndefinedTable" in self.make_control(
            err_conn, lambda: None).last_error(1)
        none_conn = FakeConn(fetchone=(None,))
        assert self.make_control(none_conn, lambda: None).last_error(1) is None

    def test_delete_passthrough(self):
        conn = FakeConn(rowcount=1)
        self.make_control(conn, lambda: None).delete(9)
        assert "DELETE FROM task_runtime" in last_sql(conn)
