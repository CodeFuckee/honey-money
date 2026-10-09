"""任务管理 API 测试：CRUD/触发/取消/停止/状态查询，全离线（假连接+假调度器）。

约定：查询接口始终 200（PG 不可达在 body.status 表达，同 /api/infra 降级风格）；
写操作用标准状态码（404 不存在 / 409 状态冲突 / 422 参数非法）。
"""

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.main import create_app  # noqa: E402


TASK_ROW = {"id": 1, "name": "新浪每日增量", "source": "sina",
            "mode": "incremental", "schedule_time": "18:00", "enabled": True}


class FakeScheduler:
    """调度器假件：只维护运行态内存与操作调用记录。"""

    def __init__(self, phases=None):
        self.phases = dict(phases or {})
        self.calls: list[tuple] = []

    def runtime_state(self):
        return {"phases": dict(self.phases), "progress": {},
                "queue": [tid for tid, p in self.phases.items() if p == "queued"],
                "running_task_id": next((tid for tid, p in self.phases.items()
                                         if p == "running"), None),
                "server_time": "2026-10-09T12:00:00+08:00"}

    def request_run(self, task_id):
        self.calls.append(("run", task_id))
        return self.phases.get(task_id) not in ("queued", "running")

    def cancel_queued(self, task_id):
        self.calls.append(("cancel", task_id))
        return self.phases.get(task_id) == "queued"

    def stop_running(self, task_id):
        self.calls.append(("stop", task_id))
        return self.phases.get(task_id) == "running"


class FakeTaskConn:
    """连接假件：游标可配置 fetchone/fetchall 返回。"""

    def __init__(self, store):
        self.store = store

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        self.last_query = query
        self.fetchone_row = None
        self.fetchall_rows = []

    def fetchone(self):
        return self.fetchone_row

    def fetchall(self):
        return self.fetchall_rows

    def commit(self):
        pass


class FakeTaskStore:
    def __init__(self, tasks=()):
        self.tasks = list(tasks)
        self.ran: set[tuple[int, str]] = set()
        self.saved: list = []

    def list_tasks(self, conn):
        return [dict(t) for t in self.tasks]

    def get_task(self, conn, task_id):
        for t in self.tasks:
            if t["id"] == task_id:
                return dict(t)
        return None

    def create_task(self, conn, *, source, mode, schedule_time, name=None):
        task = {"id": len(self.tasks) + 1, "name": name or f"新建{source}",
                "source": source, "mode": mode,
                "schedule_time": schedule_time, "enabled": False}
        self.tasks.append(task)
        return dict(task)

    def update_task(self, conn, task_id, **fields):
        task = self.get_task(conn, task_id)
        if task is None:
            raise LookupError(task_id)
        task.update({k: v for k, v in fields.items() if v is not None})
        return task

    def delete_task(self, conn, task_id):
        before = len(self.tasks)
        self.tasks = [t for t in self.tasks if t["id"] != task_id]
        return len(self.tasks) < before

    def ran_today(self, conn, task_id, day):
        return (task_id, day) in self.ran

    def recent_runs(self, conn, task_id, limit=20):
        return []


def make_client(*, tasks=(), phases=None, ran=(), connect=None):
    """构建注入假件的测试 app：返回 (client, store, scheduler)。"""
    store = FakeTaskStore(tasks)
    store.ran.update(ran)
    sched = FakeScheduler(phases=phases)
    app = create_app(scheduler=sched, task_store=store,
                     connect=connect or (lambda *a, **k: FakeTaskConn(store)))
    return TestClient(app), store, sched


class TestGetTasks:
    def test_lists_tasks_with_runtime_merge(self):
        client, store, sched = make_client(tasks=[TASK_ROW], phases={1: "queued"})
        resp = client.get("/api/tasks")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "up"
        assert len(body["tasks"]) == 1
        t = body["tasks"][0]
        assert t["phase"] == "queued"
        assert t["schedule_time"] == "18:00"
        assert t["next_run_at"], "启用任务应能算出下次执行时刻"
        assert "server_time" in body

    def test_empty_task_list_ok(self):
        client, _, _ = make_client()
        body = client.get("/api/tasks").json()
        assert body["status"] == "up"
        assert body["tasks"] == []

    def test_pg_down_degrades_to_200(self):
        """PG 不可达：仍 200，status=down（不允许 5xx）。"""

        def boom(*a, **k):
            raise RuntimeError("pg down")

        client, _, _ = make_client(connect=boom)
        resp = client.get("/api/tasks")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "down"
        assert body["tasks"] == []

    def test_disabled_task_next_run_none(self):
        row = {**TASK_ROW, "enabled": False}
        client, _, _ = make_client(tasks=[row])
        t = client.get("/api/tasks").json()["tasks"][0]
        assert t["next_run_at"] is None


class TestCreateTask:
    def test_create_ok(self):
        client, _, _ = make_client()
        resp = client.post("/api/tasks", json={
            "source": "sina", "mode": "incremental", "schedule_time": "07:30"})
        assert resp.status_code == 200
        assert resp.json()["schedule_time"] == "07:30"

    def test_create_bad_source_rejected(self):
        client, _, _ = make_client()
        assert client.post("/api/tasks", json={
            "source": "tushare", "mode": "incremental",
            "schedule_time": "07:30"}).status_code == 422

    def test_create_bad_mode_rejected(self):
        client, _, _ = make_client()
        assert client.post("/api/tasks", json={
            "source": "sina", "mode": "once", "schedule_time": "07:30"}).status_code == 422

    def test_create_bad_time_rejected(self):
        client, _, _ = make_client()
        for bad in ("25:00", "12:60", "18点", "1800", "", "7:30"):
            resp = client.post("/api/tasks", json={
                "source": "sina", "mode": "incremental", "schedule_time": bad})
            assert resp.status_code == 422, f"{bad!r} 应被拒绝"


class TestPatchTask:
    def test_patch_enabled(self):
        client, _, _ = make_client(tasks=[TASK_ROW])
        resp = client.patch("/api/tasks/1", json={"enabled": True})
        assert resp.status_code == 200

    def test_patch_missing_404(self):
        client, _, _ = make_client()
        assert client.patch("/api/tasks/99", json={"enabled": True}).status_code == 404

    def test_patch_no_fields_400(self):
        client, _, _ = make_client(tasks=[TASK_ROW])
        assert client.patch("/api/tasks/1", json={}).status_code == 400


class TestDeleteTask:
    def test_delete_ok(self):
        client, _, _ = make_client(tasks=[TASK_ROW])
        assert client.delete("/api/tasks/1").status_code == 200

    def test_delete_missing_404(self):
        client, _, _ = make_client()
        assert client.delete("/api/tasks/99").status_code == 404

    def test_delete_running_conflict_409(self):
        client, _, _ = make_client(tasks=[TASK_ROW], phases={1: "running"})
        resp = client.delete("/api/tasks/1")
        assert resp.status_code == 409
        assert "停止" in resp.json()["detail"]

    def test_delete_queued_cancels_first(self):
        """删除排队中的任务：先移出队列再删。"""
        client, _, sched = make_client(tasks=[TASK_ROW], phases={1: "queued"})
        assert client.delete("/api/tasks/1").status_code == 200
        assert ("cancel", 1) in sched.calls


class TestRunTask:
    def test_run_ok(self):
        client, _, sched = make_client(tasks=[TASK_ROW])
        resp = client.post("/api/tasks/1/run")
        assert resp.status_code == 200
        assert ("run", 1) in sched.calls

    def test_run_missing_404(self):
        client, _, _ = make_client()
        assert client.post("/api/tasks/99/run").status_code == 404

    def test_run_while_running_conflict_409(self):
        client, _, _ = make_client(tasks=[TASK_ROW], phases={1: "running"})
        assert client.post("/api/tasks/1/run").status_code == 409

    def test_run_while_queued_conflict_409(self):
        client, _, _ = make_client(tasks=[TASK_ROW], phases={1: "queued"})
        assert client.post("/api/tasks/1/run").status_code == 409


class TestCancelStop:
    def test_cancel_ok(self):
        client, _, _ = make_client(tasks=[TASK_ROW], phases={1: "queued"})
        assert client.post("/api/tasks/1/cancel").status_code == 200

    def test_cancel_not_queued_409(self):
        client, _, _ = make_client(tasks=[TASK_ROW])
        assert client.post("/api/tasks/1/cancel").status_code == 409

    def test_cancel_missing_404(self):
        client, _, _ = make_client()
        assert client.post("/api/tasks/99/cancel").status_code == 404

    def test_stop_ok(self):
        client, _, _ = make_client(tasks=[TASK_ROW], phases={1: "running"})
        assert client.post("/api/tasks/1/stop").status_code == 200

    def test_stop_not_running_409(self):
        client, _, _ = make_client(tasks=[TASK_ROW])
        assert client.post("/api/tasks/1/stop").status_code == 409

    def test_stop_missing_404(self):
        client, _, _ = make_client()
        assert client.post("/api/tasks/99/stop").status_code == 404


class TestHistory:
    def test_task_history_endpoint(self):
        client, _, _ = make_client(tasks=[TASK_ROW])
        resp = client.get("/api/tasks/1/runs")
        assert resp.status_code == 200
        assert resp.json()["runs"] == []
