"""爬虫任务管理 API：/api/tasks 查询（始终 200，down 在 body 表达，同 /api/infra
降级约定）+ 写操作（标准状态码：404 不存在 / 409 状态冲突 / 422 参数非法）。

页面能力 ↔ 接口：
- 任务列表+运行态+下次执行时刻+最近一次结果 → GET /api/tasks（前端 3s 轮询）
- 新建/编辑/删除               → POST / PATCH / DELETE
- 立即执行/取消排队/停止运行    → POST .../run | cancel | stop
- 单任务运行历史               → GET /api/tasks/{id}/runs
"""

import datetime as dt

import psycopg
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, field_validator

from . import config
from .scheduler import SH_TZ, TaskScheduler, next_run_at
from .task_store import TaskStore

VALID_SOURCES = ("sina", "eastmoney")
VALID_MODES = ("incremental", "full")


class TaskIn(BaseModel):
    source: str
    mode: str
    schedule_time: str
    name: str | None = None

    @field_validator("source")
    @classmethod
    def _source_ok(cls, v):
        if v not in VALID_SOURCES:
            raise ValueError(f"source 须为 {'/'.join(VALID_SOURCES)}")
        return v

    @field_validator("mode")
    @classmethod
    def _mode_ok(cls, v):
        if v not in VALID_MODES:
            raise ValueError(f"mode 须为 {'/'.join(VALID_MODES)}")
        return v

    @field_validator("schedule_time")
    @classmethod
    def _time_ok(cls, v):
        parts = v.split(":")
        if (len(parts) != 2 or not all(p.isdigit() and len(p) == 2
                                       for p in parts)
                or not (0 <= int(parts[0]) <= 23 and 0 <= int(parts[1]) <= 59)):
            raise ValueError("schedule_time 须为合法 HH:MM（24 小时制）")
        return v


class TaskPatch(BaseModel):
    source: str | None = None
    mode: str | None = None
    schedule_time: str | None = None
    enabled: bool | None = None

    @field_validator("source")
    @classmethod
    def _source_ok(cls, v):
        if v is not None and v not in VALID_SOURCES:
            raise ValueError(f"source 须为 {'/'.join(VALID_SOURCES)}")
        return v

    @field_validator("mode")
    @classmethod
    def _mode_ok(cls, v):
        if v is not None and v not in VALID_MODES:
            raise ValueError(f"mode 须为 {'/'.join(VALID_MODES)}")
        return v

    @field_validator("schedule_time")
    @classmethod
    def _time_ok(cls, v):
        if v is None:
            return v
        return TaskIn._time_ok(v)


def register_task_routes(app: FastAPI, *, scheduler: TaskScheduler,
                         task_store: TaskStore, connect) -> None:
    """挂载任务路由（依赖注入便于测试假件）。"""

    def _require(task_id: int) -> dict:
        try:
            with connect(config.DATABASE_URL) as conn:
                task = task_store.get_task(conn, task_id)
        except psycopg.Error:
            raise HTTPException(503, "数据库不可达")
        if task is None:
            raise HTTPException(404, f"任务 {task_id} 不存在")
        return task

    def _phase(task_id: int) -> str:
        return scheduler.runtime_state()["phases"].get(task_id, "idle")

    @app.get("/api/tasks")
    def list_tasks():
        state = scheduler.runtime_state()
        try:
            now = dt.datetime.fromisoformat(state["server_time"])
            with connect(config.DATABASE_URL) as conn:
                tasks = []
                for t in task_store.list_tasks(conn):
                    ran = task_store.ran_today(conn, t["id"],
                                               now.date().isoformat())
                    last = task_store.recent_runs(conn, t["id"], limit=1)
                    tasks.append({
                        **t,
                        "phase": state["phases"].get(t["id"], "idle"),
                        "next_run_at": (next_run_at(t, now, ran_today=ran)
                                        .isoformat() if t["enabled"] else None),
                        "progress": state["progress"].get(t["id"]),
                        "last_run": last[0] if last else None,
                    })
        except Exception as exc:              # PG 不可达：降级而非 5xx
            return {"status": "down", "detail": f"{type(exc).__name__}: "
                                                f"{str(exc)[:120]}",
                    "server_time": state["server_time"], "tasks": [],
                    "queue": [], "running": None}
        return {
            "status": "up", "server_time": state["server_time"], "tasks": tasks,
            "queue": state["queue"],
            "running": state["running_task_id"],
        }

    @app.post("/api/tasks")
    def create_task(body: TaskIn):
        try:
            with connect(config.DATABASE_URL) as conn:
                task = task_store.create_task(
                    conn, source=body.source, mode=body.mode,
                    schedule_time=body.schedule_time, name=body.name)
                conn.commit()
        except psycopg.Error as exc:
            raise HTTPException(503, f"数据库不可达: {exc}")
        return task

    @app.patch("/api/tasks/{task_id}")
    def patch_task(task_id: int, body: TaskPatch):
        _require(task_id)
        fields = {k: v for k, v in body.model_dump().items() if v is not None}
        if not fields:
            raise HTTPException(400, "无可更新字段")
        try:
            with connect(config.DATABASE_URL) as conn:
                task = task_store.update_task(conn, task_id, **fields)
                conn.commit()
        except psycopg.Error as exc:
            raise HTTPException(503, f"数据库不可达: {exc}")
        return task

    @app.delete("/api/tasks/{task_id}")
    def delete_task(task_id: int):
        _require(task_id)
        phase = _phase(task_id)
        if phase == "running":
            raise HTTPException(409, "任务运行中不可删除，请先停止运行")
        if phase == "queued":
            scheduler.cancel_queued(task_id)   # 移出队列再删
        try:
            with connect(config.DATABASE_URL) as conn:
                if not task_store.delete_task(conn, task_id):
                    raise HTTPException(404, f"任务 {task_id} 不存在")
                conn.commit()
        except psycopg.Error as exc:
            raise HTTPException(503, f"数据库不可达: {exc}")
        return {"deleted": task_id}

    @app.post("/api/tasks/{task_id}/run")
    def run_task(task_id: int):
        _require(task_id)
        if _phase(task_id) in ("queued", "running"):
            raise HTTPException(409, "任务已在排队/运行中")
        if not scheduler.request_run(task_id):
            raise HTTPException(409, "任务已在排队/运行中")
        return {"triggered": task_id, "queue_position": len(scheduler.runtime_state()["queue"])}

    @app.post("/api/tasks/{task_id}/cancel")
    def cancel_task(task_id: int):
        _require(task_id)
        if not scheduler.cancel_queued(task_id):
            raise HTTPException(409, "任务不在排队中")
        return {"cancelled": task_id}

    @app.post("/api/tasks/{task_id}/stop")
    def stop_task(task_id: int):
        _require(task_id)
        if not scheduler.stop_running(task_id):
            raise HTTPException(409, "任务未在运行")
        return {"stopping": task_id}

    @app.get("/api/tasks/{task_id}/runs")
    def task_runs(task_id: int, limit: int = 20):
        _require(task_id)
        try:
            with connect(config.DATABASE_URL) as conn:
                runs = task_store.recent_runs(conn, task_id, limit=min(limit, 100))
        except psycopg.Error as exc:
            raise HTTPException(503, f"数据库不可达: {exc}")
        return {"task_id": task_id, "runs": runs}


def now_sh() -> dt.datetime:
    """当前上海时刻（调度时刻基准，与容器时区无关）。"""
    return dt.datetime.now(SH_TZ)
