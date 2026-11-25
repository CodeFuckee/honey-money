"""任务运行态持久化：task_runtime 单表承载队列/阶段/进度/停止指令。

为什么落 PG：任务执行搬进独立 worker 容器后（backend 只留 API，见
app/worker.py），「任务页 3s 轮询的运行态」与「手动触发/取消/停止指令」
都必须跨进程——PG 是两个容器唯一的共享介质（先例：任务定义 crawl_task、
运行历史 crawl_run 早已以 PG 为事实源）。并发面很窄：worker 是唯一的
出队/执行态写者，API 只写指令行（入队/取消/停止）——push 用条件 upsert
保证「queued/running 拒绝」原子成立，无需锁。

表 task_runtime，每任务一行（首次入队惰性创建；执行结束保留 idle 行——
last_result 是任务页 last_error 的唯一出口，执行体异常发生在 start_run
之前时 crawl_run 无记录）。cancel 只回 idle 不删行，原因同上。

风格同 task_store/crawler.store：无状态方法收已打开连接，事务边界由
调用方控制；jsonb 经 psycopg.types.json.Json 适配。
"""

import datetime as dt

import psycopg
from psycopg.types.json import Json
from zoneinfo import ZoneInfo

SH_TZ = ZoneInfo("Asia/Shanghai")

_DDL = """
CREATE TABLE IF NOT EXISTS task_runtime (
  task_id        BIGINT PRIMARY KEY,
  phase          VARCHAR(16) NOT NULL DEFAULT 'idle',
  trigger_kind   VARCHAR(16),
  enqueued_at    TIMESTAMPTZ,
  progress       JSONB,
  last_result    JSONB,
  stop_requested BOOLEAN NOT NULL DEFAULT false,
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

# 入队（原子）：无行则建；有行且 idle 才允许重新排队（queued/running 拒绝
# ——WHERE 不满足时 DO UPDATE 静默跳过，RETURNING 无行 → rowcount 0）
_PUSH = """
INSERT INTO task_runtime (task_id, phase, trigger_kind, enqueued_at, updated_at)
VALUES (%s, 'queued', %s, now(), now())
ON CONFLICT (task_id) DO UPDATE
  SET phase = 'queued', trigger_kind = EXCLUDED.trigger_kind,
      enqueued_at = now(), updated_at = now()
  WHERE task_runtime.phase = 'idle'
RETURNING task_id
"""

_POP_NEXT = """
SELECT task_id, trigger_kind FROM task_runtime
WHERE phase = 'queued'
ORDER BY enqueued_at NULLS LAST, task_id
LIMIT 1
"""

_MARK_RUNNING = """
UPDATE task_runtime SET phase = 'running', stop_requested = false,
       progress = NULL, updated_at = now()
WHERE task_id = %s
"""

_CANCEL = """
UPDATE task_runtime SET phase = 'idle', trigger_kind = NULL,
       enqueued_at = NULL, updated_at = now()
WHERE task_id = %s AND phase = 'queued'
RETURNING task_id
"""

_STOP = """
UPDATE task_runtime SET stop_requested = true, updated_at = now()
WHERE task_id = %s AND phase = 'running'
RETURNING task_id
"""

_SET_PROGRESS = """
UPDATE task_runtime SET progress = %s, updated_at = now()
WHERE task_id = %s
"""

_FINISH = """
UPDATE task_runtime SET phase = 'idle', trigger_kind = NULL,
       enqueued_at = NULL, progress = NULL, last_result = %s,
       stop_requested = false, updated_at = now()
WHERE task_id = %s
"""

_MARK_IDLE = """
UPDATE task_runtime SET phase = 'idle', trigger_kind = NULL,
       enqueued_at = NULL, progress = NULL, updated_at = now()
WHERE task_id = %s
"""

_MARK_RUNNING_IDLE = """
UPDATE task_runtime SET phase = 'idle', trigger_kind = NULL,
       enqueued_at = NULL, progress = NULL, stop_requested = false,
       updated_at = now()
WHERE phase = 'running'
"""

_SNAPSHOT = """
SELECT task_id, phase, progress, enqueued_at FROM task_runtime
ORDER BY enqueued_at NULLS LAST, task_id
"""


class TaskRuntime:
    """task_runtime 行操作（worker 调度与 API 控制共用；可整体换假件）。

    写方法均收已打开连接；带 RETURNING 的操作以 rowcount 表达成败。
    """

    def __init__(self, dsn: str, connect=None):
        self._dsn = dsn
        self._connect = connect or psycopg.connect

    # ---- schema ----

    def ensure_schema(self, conn) -> None:
        with conn.cursor() as cur:
            cur.execute(_DDL)

    # ---- 查询（读不落事务边界） ----

    def phase(self, conn, task_id: int) -> str:
        with conn.cursor() as cur:
            cur.execute("SELECT phase FROM task_runtime WHERE task_id = %s",
                        (task_id,))
            row = cur.fetchone()
        return row[0] if row else "idle"

    def snapshot(self, conn) -> dict:
        """运行态快照：phases/progress/queue/running_task_id（server_time 由
        调用方补——控制面时钟在各自进程）。"""
        with conn.cursor() as cur:
            cur.execute(_SNAPSHOT)
            rows = cur.fetchall()
        phases, progress, queue, running = {}, {}, [], None
        for tid, phase, prog, _enq in rows:
            phases[tid] = phase
            if prog is not None:
                progress[tid] = prog
            if phase == "queued":
                queue.append(tid)          # 已按 enqueued_at 排序
            elif phase == "running" and running is None:
                running = tid
        return {"phases": phases, "progress": progress, "queue": queue,
                "running_task_id": running}

    def last_result(self, conn, task_id: int) -> dict | None:
        with conn.cursor() as cur:
            cur.execute("SELECT last_result FROM task_runtime "
                        "WHERE task_id = %s", (task_id,))
            row = cur.fetchone()
        return row[0] if row else None

    # ---- 队列/指令（写） ----

    def push(self, conn, task_id: int, trigger: str) -> bool:
        """入队；已在排队/运行中拒绝（False）——manual 与 scan 同一条路。"""
        with conn.cursor() as cur:
            cur.execute(_PUSH, (task_id, trigger))
            return cur.rowcount > 0

    def pop_next(self, conn) -> dict | None:
        """取最旧排队行并置 running（单 worker 消费，无并发竞争）。"""
        with conn.cursor() as cur:
            cur.execute(_POP_NEXT)
            row = cur.fetchone()
            if row is None:
                return None
            cur.execute(_MARK_RUNNING, (row[0],))
        return {"task_id": row[0], "trigger": row[1]}

    def cancel_queued(self, conn, task_id: int) -> bool:
        """取消排队（回 idle 保留 last_result）；不在排队中返回 False。"""
        with conn.cursor() as cur:
            cur.execute(_CANCEL, (task_id,))
            return cur.rowcount > 0

    def request_stop(self, conn, task_id: int) -> bool:
        """请求协作停止（仅运行中可置；worker 轮询消费，finish 时复位）。"""
        with conn.cursor() as cur:
            cur.execute(_STOP, (task_id,))
            return cur.rowcount > 0

    def is_stop_requested(self, conn, task_id: int) -> bool:
        with conn.cursor() as cur:
            cur.execute("SELECT stop_requested FROM task_runtime "
                        "WHERE task_id = %s", (task_id,))
            row = cur.fetchone()
        return bool(row and row[0])

    def set_progress(self, conn, task_id: int, done: int, total: int) -> None:
        with conn.cursor() as cur:
            cur.execute(_SET_PROGRESS, (Json({"done": done, "total": total}),
                                        task_id))

    def finish(self, conn, task_id: int, result: dict) -> None:
        """执行收尾：回 idle、记录结果摘要、清进度与停止标志。"""
        with conn.cursor() as cur:
            cur.execute(_FINISH, (Json(result), task_id))

    def mark_idle(self, conn, task_id: int) -> None:
        """不记结果的收尾（取消/任务已被删）：只清队列痕迹。"""
        with conn.cursor() as cur:
            cur.execute(_MARK_IDLE, (task_id,))

    def mark_running_idle(self, conn) -> int:
        """启动兜底：上一代 worker 被杀遗留的 running 行回 idle（排队行
        保留——队列跨重启存活是 PG 化的收益之一）。返回标记行数。"""
        with conn.cursor() as cur:
            cur.execute(_MARK_RUNNING_IDLE)
            return cur.rowcount

    def delete(self, conn, task_id: int) -> None:
        """任务删除时同步清运行态行（防 snapshot 里残留幽灵 phase）。"""
        with conn.cursor() as cur:
            cur.execute("DELETE FROM task_runtime WHERE task_id = %s",
                        (task_id,))


class PgTaskControl:
    """API 侧任务控制面：写指令行 + 读运行态（backend 进程内使用）。

    接口即协议（request_run/cancel_queued/stop_running/runtime_state/
    last_error/delete）——TaskScheduler 也实现同款方法（单进程开发模式
    SCHEDULER_ENABLED=1 时两者等价），测试注入内存假件即可。
    """

    def __init__(self, dsn: str, connect=None, runtime: TaskRuntime | None = None,
                 clock=None):
        self._dsn = dsn
        self._connect = connect or psycopg.connect
        self._runtime = runtime or TaskRuntime(dsn, connect=connect)
        self._clock = clock or (lambda: dt.datetime.now(SH_TZ))

    def runtime_state(self) -> dict:
        with self._connect(self._dsn) as conn:
            state = self._runtime.snapshot(conn)
        state["server_time"] = self._clock().isoformat()
        return state

    def request_run(self, task_id: int) -> bool:
        """手动触发（不管 enabled）；排队/运行中拒绝。"""
        with self._connect(self._dsn) as conn:
            return self._runtime.push(conn, task_id, "manual")

    def cancel_queued(self, task_id: int) -> bool:
        with self._connect(self._dsn) as conn:
            return self._runtime.cancel_queued(conn, task_id)

    def stop_running(self, task_id: int) -> bool:
        """停止运行中任务：置 PG 停止标志，worker 轮询（≤5s）翻译成
        stop_event——协作式，runner 停止派发、优雅落库。"""
        with self._connect(self._dsn) as conn:
            return self._runtime.request_stop(conn, task_id)

    def last_error(self, task_id: int) -> str | None:
        """上次执行体异常原因（PG 持久，重启不失；正常/未跑过 None）。"""
        with self._connect(self._dsn) as conn:
            result = self._runtime.last_result(conn, task_id)
        return (result or {}).get("detail") if result and result.get("error") \
            else None

    def delete(self, task_id: int) -> None:
        with self._connect(self._dsn) as conn:
            self._runtime.delete(conn, task_id)


def write_progress(dsn: str, task_id: int, done: int, total: int) -> None:
    """子进程友好的进度落库（2026-11-25 十轮）：回测引擎在 spawn 池内回调
    基金级进度——闭包不可 pickle，模块级函数 + 基元参数（调度器经
    functools.partial 绑定 dsn/task_id 下传）才能跨进程；与
    PgTaskControl.set_progress 同一 UPDATE，自带连接，失败仅打印绝不影响
    计算。"""
    try:
        with psycopg.connect(dsn, connect_timeout=10) as conn:
            TaskRuntime(dsn).set_progress(conn, task_id, done, total)
    except Exception as exc:      # noqa: BLE001 —— 进度失败绝不影响计算
        print(f"[backtest] 进度落库失败（忽略）: {exc}")
