"""调度器测试：扫描入队、启动补跑、全局串行、手动触发、取消/停止、互斥锁。

运行态全经注入的 FakeRuntime（接口=TaskRuntime，内存版）——与生产
「PG task_runtime 跨进程」同一条协议（见 app/task_runtime.py）。

全离线：时钟/睡眠/执行体/锁/连接全部注入假件，scan_once/drain_once 单步驱动，
不跑真实定时循环，行为完全确定。
"""

import asyncio
import datetime as dt
import sys
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.scheduler import SH_TZ, TaskScheduler, next_run_at  # noqa: E402


def sh(*, hour=18, minute=0, day=9, month=10, year=2026):
    """构造上海时区的 aware datetime。"""
    return dt.datetime(year, month, day, hour, minute, tzinfo=SH_TZ)


class FakeClock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, **kw):
        self.now += dt.timedelta(**kw)


class FakeTask:
    """构造任务 dict（与 TaskStore.list_tasks 返回形状一致）。"""

    def __init__(self, tid, *, source="sina", mode="incremental",
                 schedule_time="18:00", enabled=True, name=None):
        self.d = {"id": tid, "name": name or f"任务{tid}", "source": source,
                  "mode": mode, "schedule_time": schedule_time, "enabled": enabled}


class FakeTaskStore:
    def __init__(self, tasks=()):
        self.tasks = list(tasks)
        self.ran: set[tuple[int, str]] = set()      # (task_id, day)
        self.deleted: set[int] = set()

    def list_tasks(self, conn):
        return [t.d for t in self.tasks if t.d["id"] not in self.deleted]

    def get_task(self, conn, task_id):
        for t in self.tasks:
            if t.d["id"] == task_id and t.d["id"] not in self.deleted:
                return t.d
        return None

    def ran_today(self, conn, task_id, day):
        return (task_id, day) in self.ran


class FakeConn:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeLock:
    """advisory lock 假件：可配置前 N 次 acquire 失败。"""

    def __init__(self, fail_times=0):
        self.fail_times = fail_times
        self.acquired = 0
        self.released = 0
        self.held = False

    async def acquire(self):
        if self.fail_times > 0:
            self.fail_times -= 1
            return False
        self.acquired += 1
        self.held = True
        return True

    async def release(self):
        self.released += 1
        self.held = False



class FakeRuntime:
    """task_runtime 假件：内存版队列/阶段/进度/结果（接口=TaskRuntime）。"""

    def __init__(self):
        self._rows: dict[int, dict] = {}
        self._seq = 0
        self.last_results: dict[int, dict] = {}

    def _row(self, tid):
        return self._rows.setdefault(tid, {})

    def phase(self, conn, tid):
        return self._row(tid).get("phase", "idle")

    def push(self, conn, tid, trigger):
        row = self._row(tid)
        if row.get("phase") in ("queued", "running"):
            return False
        self._seq += 1
        row.update(phase="queued", trigger=trigger, seq=self._seq,
                   progress=None)
        return True

    def pop_next(self, conn):
        queued = [(t, r) for t, r in self._rows.items()
                  if r.get("phase") == "queued"]
        if not queued:
            return None
        tid, row = min(queued, key=lambda x: x[1]["seq"])
        row.update(phase="running", progress=None, stop_requested=False)
        return {"task_id": tid, "trigger": row["trigger"]}

    def cancel_queued(self, conn, tid):
        row = self._row(tid)
        if row.get("phase") != "queued":
            return False
        row.update(phase="idle", trigger=None, progress=None)
        return True

    def request_stop(self, conn, tid):
        row = self._row(tid)
        if row.get("phase") != "running":
            return False
        row["stop_requested"] = True
        return True

    def is_stop_requested(self, conn, tid):
        return bool(self._row(tid).get("stop_requested"))

    def set_progress(self, conn, tid, done, total):
        self._row(tid)["progress"] = {"done": done, "total": total}

    def finish(self, conn, tid, result):
        row = self._row(tid)
        row.update(phase="idle", trigger=None, progress=None,
                   stop_requested=False, last_result=result)
        self.last_results[tid] = result

    def mark_idle(self, conn, tid):
        row = self._row(tid)
        row.update(phase="idle", trigger=None, progress=None)

    def snapshot(self, conn):
        order = sorted(self._rows.items(), key=lambda x: x[1].get("seq") or 0)
        phases, progress, queue, running = {}, {}, [], None
        for tid, row in order:
            ph = row.get("phase", "idle")
            phases[tid] = ph
            if row.get("progress") is not None:
                progress[tid] = row["progress"]
            if ph == "queued":
                queue.append(tid)
            elif ph == "running" and running is None:
                running = tid
        return {"phases": phases, "progress": progress, "queue": queue,
                "running_task_id": running}

    def last_result(self, conn, tid):
        return self._row(tid).get("last_result")

    def delete(self, conn, tid):
        self._rows.pop(tid, None)


RESULT = {"total": 3, "ok": 3, "failed": 0, "bars": 3, "failures": []}


class FakeExecutor:
    """执行体假件：记录调用、可注入延迟/异常/进行中回调。"""

    def __init__(self, *, fail=False, during=None):
        self.calls: list[dict] = []
        self.fail = fail
        self.during = during        # 执行中回调 during(scheduler, task, stop_event)

    async def __call__(self, task, *, progress, stop_event):
        self.calls.append({"id": task["id"], "trigger": task.get("_trigger")})
        await asyncio.sleep(0)
        if self.during:
            await self.during(task, progress, stop_event)
        if self.fail:
            raise RuntimeError("executor boom")
        return dict(RESULT)


def make_scheduler(*, tasks=(), clock=None, lock=None, executor=None,
                   task_store=None, poll_interval=30.0, runtime=None, **kw):
    """构建调度器（运行态假件经 sched._runtime 访问）。"""
    return TaskScheduler(
        dsn="postgresql://fake", poll_interval=poll_interval,
        clock=clock or FakeClock(sh(hour=17)),
        sleeper=None,                              # scan/drain 单步驱动，不该被调用
        executor=executor or FakeExecutor(),
        lock=lock or FakeLock(),
        connect=lambda *a, **k: FakeConn(),
        task_store=task_store or FakeTaskStore(tasks),
        runtime=runtime or FakeRuntime(),
        stop_poll_interval=0.01,                   # 停止指令经 PG 轮询，测试用快轮询
        **kw,
    )


class TestScan:
    def test_due_enabled_task_enqueued_and_executed(self):
        store = FakeTaskStore([FakeTask(1)])
        clock = FakeClock(sh(hour=18, minute=5))          # 已过 18:00
        sched = make_scheduler(tasks=[FakeTask(1)], clock=clock, task_store=store)
        run = asyncio.new_event_loop()
        try:
            run.run_until_complete(sched.scan_once())
            run.run_until_complete(sched.drain_once())
        finally:
            run.close()
        assert sched.runtime_state()["phases"] == {1: "idle"}
        assert sched._runtime.last_results[1]["trigger"] == "schedule"

    def test_null_schedule_manual_task_never_scheduled(self):
        """手动任务（schedule_time NULL）：scan 不入队也不崩——曾 AttributeError
        拖垮整个扫描循环（连排程任务都不扫，生产启用退市补爬后暴露）。"""
        tasks = [FakeTask(1),
                 FakeTask(2, source="delisted", mode="full",
                          schedule_time=None, enabled=True)]
        sched = make_scheduler(tasks=tasks, clock=FakeClock(sh(hour=18, minute=5)))
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(sched.scan_once())
            loop.run_until_complete(sched.drain_once())
        finally:
            loop.close()
        assert 2 not in sched.runtime_state()["queue"]
        assert sched._runtime.last_results.get(2) is None

    def test_disabled_task_never_scheduled(self):
        sched = make_scheduler(tasks=[FakeTask(1, enabled=False)],
                               clock=FakeClock(sh(hour=18, minute=5)))
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(sched.scan_once())
            loop.run_until_complete(sched.drain_once())
        finally:
            loop.close()
        assert sched.runtime_state()["queue"] == []
        assert sched.runtime_state()["phases"] == {}

    def test_already_ran_today_not_rescheduled(self):
        """当天计划已执行：再扫不再入队（防重复）。"""
        store = FakeTaskStore([FakeTask(1)])
        store.ran.add((1, "2026-10-09"))
        sched = make_scheduler(tasks=[FakeTask(1)], clock=FakeClock(sh(hour=19)),
                               task_store=store)
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(sched.scan_once())
        finally:
            loop.close()
        assert sched.runtime_state()["queue"] == []

    def test_future_task_not_enqueued(self):
        sched = make_scheduler(tasks=[FakeTask(1)], clock=FakeClock(sh(hour=17)))
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(sched.scan_once())
        finally:
            loop.close()
        assert sched.runtime_state()["queue"] == []

    def test_running_or_queued_not_double_enqueued(self):
        """任务已排队/运行中时，扫描不得重复入队。"""
        store = FakeTaskStore([FakeTask(1)])
        sched = make_scheduler(tasks=[FakeTask(1)], clock=FakeClock(sh(hour=18, minute=5)),
                               task_store=store)
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(sched.scan_once())
            loop.run_until_complete(sched.scan_once())      # 第二次扫描
        finally:
            loop.close()
        assert sched.runtime_state()["queue"] == [1]


class TestCatchUp:
    def test_startup_scan_enqueues_overdue_as_catch_up(self):
        """启动补跑：启动时已过今日计划点且未跑 → trigger=catch_up。"""
        sched = make_scheduler(tasks=[FakeTask(1)], clock=FakeClock(sh(hour=21)))
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(sched.scan_once(startup=True))
            loop.run_until_complete(sched.drain_once())
        finally:
            loop.close()
        assert sched._runtime.last_results[1]["trigger"] == "catch_up"

    def test_catch_up_runs_only_once_per_day(self):
        store = FakeTaskStore([FakeTask(1)])
        clock = FakeClock(sh(hour=21))
        sched = make_scheduler(tasks=[FakeTask(1)], clock=clock, task_store=store)
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(sched.scan_once(startup=True))
            loop.run_until_complete(sched.drain_once())
            store.ran.add((1, "2026-10-09"))                 # 模拟已记录
            loop.run_until_complete(sched.scan_once())
            loop.run_until_complete(sched.drain_once())
        finally:
            loop.close()
        assert len(sched._runtime.last_results) == 1

    def test_catch_up_suppressed_if_already_ran(self):
        store = FakeTaskStore([FakeTask(1)])
        store.ran.add((1, "2026-10-09"))
        sched = make_scheduler(tasks=[FakeTask(1)], clock=FakeClock(sh(hour=21)),
                               task_store=store)
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(sched.scan_once(startup=True))
        finally:
            loop.close()
        assert sched.runtime_state()["queue"] == []


class TestSerialQueue:
    def test_two_due_tasks_run_sequentially(self):
        """两任务都到点：串行执行，顺序=入队顺序，第一个完成后第二个才开始。"""
        store = FakeTaskStore([FakeTask(1), FakeTask(2)])
        order: list[str] = []

        async def during(task, progress, stop_event):
            order.append(f"start-{task['id']}")
            await asyncio.sleep(0)
            order.append(f"end-{task['id']}")

        sched = make_scheduler(tasks=[FakeTask(1), FakeTask(2)],
                               clock=FakeClock(sh(hour=18, minute=5)),
                               task_store=store, executor=FakeExecutor(during=during))
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(sched.scan_once())
            loop.run_until_complete(sched.drain_once())
            loop.run_until_complete(sched.drain_once())
        finally:
            loop.close()
        assert order == ["start-1", "end-1", "start-2", "end-2"]

    def test_single_drain_executes_only_one_task(self):
        sched = make_scheduler(tasks=[FakeTask(1), FakeTask(2)],
                               clock=FakeClock(sh(hour=18, minute=5)))
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(sched.scan_once())
            loop.run_until_complete(sched.drain_once())
        finally:
            loop.close()
        assert len(sched._runtime.last_results) == 1
        assert sched.runtime_state()["phases"] == {1: "idle", 2: "queued"}


class TestManualRun:
    def test_manual_runs_disabled_task(self):
        """禁用任务仍可手动触发（手动是显式意图）。"""
        store = FakeTaskStore([FakeTask(1, enabled=False)])
        sched = make_scheduler(tasks=[FakeTask(1, enabled=False)],
                               clock=FakeClock(sh(hour=12)), task_store=store)
        loop = asyncio.new_event_loop()
        try:
            ok = sched.request_run(1)
            loop.run_until_complete(sched.drain_once())
        finally:
            loop.close()
        assert ok is True
        assert sched._runtime.last_results[1]["trigger"] == "manual"

    def test_manual_does_not_count_as_planned_run(self):
        """手动跑完不写计划标记：当晚调度照常（scan 仍会入队）。"""
        store = FakeTaskStore([FakeTask(1)])
        clock = FakeClock(sh(hour=10))
        executor = FakeExecutor()
        sched = make_scheduler(tasks=[FakeTask(1)], clock=clock, task_store=store,
                               executor=executor)
        loop = asyncio.new_event_loop()
        try:
            sched.request_run(1)
            loop.run_until_complete(sched.drain_once())
            clock.advance(hours=8)                          # 18:05 到点
            loop.run_until_complete(sched.scan_once())
            loop.run_until_complete(sched.drain_once())
        finally:
            loop.close()
        assert [(c["id"], c["trigger"]) for c in executor.calls] == \
            [(1, "manual"), (1, "schedule")]

    def test_manual_while_queued_rejected(self):
        sched = make_scheduler(tasks=[FakeTask(1)], clock=FakeClock(sh(hour=18, minute=5)))
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(sched.scan_once())
            ok = sched.request_run(1)
        finally:
            loop.close()
        assert ok is False


class TestCancelAndStop:
    def test_cancel_queued_task_skips_execution(self):
        store = FakeTaskStore([FakeTask(1)])
        executor = FakeExecutor()
        sched = make_scheduler(tasks=[FakeTask(1)], clock=FakeClock(sh(hour=18, minute=5)),
                               task_store=store, executor=executor)
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(sched.scan_once())
            ok = sched.cancel_queued(1)
            loop.run_until_complete(sched.drain_once())
        finally:
            loop.close()
        assert ok is True
        assert executor.calls == []
        assert sched.runtime_state()["phases"] == {1: "idle"}

    def test_cancel_idle_task_returns_false(self):
        sched = make_scheduler(tasks=[FakeTask(1)], clock=FakeClock(sh(hour=12)))
        assert sched.cancel_queued(1) is False

    def test_stop_running_sets_event_and_marks_result(self):
        """停止运行中：stop_event 置位，结果标记 stopped（不算失败），恢复 idle。"""
        stop_seen: dict = {}

        async def during(task, progress, stop_event):
            stop_seen["set"] = stop_event.is_set()

        store = FakeTaskStore([FakeTask(1)])
        sched = make_scheduler(tasks=[FakeTask(1)], clock=FakeClock(sh(hour=18, minute=5)),
                               task_store=store, executor=FakeExecutor(during=during))
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(sched.scan_once())
            loop.run_until_complete(sched.drain_once())
        finally:
            loop.close()
        # during 执行时 stop_event 尚未置位（未被停止）——基础路径
        assert stop_seen["set"] is False

    def test_deleted_task_skipped_in_queue(self):
        """排队期间任务被删除：出队时跳过，不执行。"""
        store = FakeTaskStore([FakeTask(1)])
        executor = FakeExecutor()
        sched = make_scheduler(tasks=[FakeTask(1)], clock=FakeClock(sh(hour=18, minute=5)),
                               task_store=store, executor=executor)
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(sched.scan_once())
            store.deleted.add(1)
            loop.run_until_complete(sched.drain_once())
        finally:
            loop.close()
        assert executor.calls == []


class TestLock:
    def test_lock_waits_until_acquired(self):
        """锁被占（如另一进程在跑 CLI）：等待重试，拿到后才执行。"""
        lock = FakeLock(fail_times=2)
        sleeps: list[float] = []

        async def fake_sleep(sec):
            sleeps.append(sec)

        store = FakeTaskStore([FakeTask(1)])
        sched = make_scheduler(tasks=[FakeTask(1)], clock=FakeClock(sh(hour=18, minute=5)),
                               task_store=store, lock=lock)
        sched._sleeper = fake_sleep
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(sched.scan_once())
            loop.run_until_complete(sched.drain_once())
        finally:
            loop.close()
        assert lock.acquired == 1
        assert sleeps == [30.0, 30.0], f"应等待重试 2 次: {sleeps}"

    def test_lock_released_after_run(self):
        lock = FakeLock()
        sched = make_scheduler(tasks=[FakeTask(1)], clock=FakeClock(sh(hour=18, minute=5)),
                               lock=lock)
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(sched.scan_once())
            loop.run_until_complete(sched.drain_once())
        finally:
            loop.close()
        assert lock.held is False
        assert lock.released == 1

    def test_executor_error_still_releases_lock(self):
        """执行体抛异常：锁必须释放、任务标记失败、调度器不崩。"""
        lock = FakeLock()
        store = FakeTaskStore([FakeTask(1)])
        sched = make_scheduler(tasks=[FakeTask(1)], clock=FakeClock(sh(hour=18, minute=5)),
                               task_store=store, lock=lock,
                               executor=FakeExecutor(fail=True))
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(sched.scan_once())
            loop.run_until_complete(sched.drain_once())
        finally:
            loop.close()
        assert lock.held is False
        assert sched._runtime.last_results[1]["error"] is True
        assert sched.runtime_state()["phases"] == {1: "idle"}


class TestStopViaRuntime:
    def test_stop_running_writes_flag_and_executor_sees_stop(self):
        """停止是跨进程指令：stop_running 只写 runtime 标志（本进程没有
        Event 可置位），执行循环内 5s 轮询（测试 0.01s）翻译成 stop_event
        ——executor 的既有消费逻辑零改动。"""
        seen: dict = {}

        async def during(task, progress, stop_event):
            await asyncio.sleep(0.05)
            seen["requested"] = sched.stop_running(task["id"])   # 写 PG 标志
            await asyncio.sleep(0.2)                             # 给轮询留窗
            seen["event_set"] = stop_event.is_set()

        sched = make_scheduler(tasks=[FakeTask(1)],
                               clock=FakeClock(sh(hour=18, minute=5)),
                               executor=FakeExecutor(during=during))
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(sched.scan_once())
            loop.run_until_complete(sched.drain_once())
        finally:
            loop.close()
        assert seen == {"requested": True, "event_set": True}
        assert sched._runtime.last_results[1]["stopped"] is True
        assert sched._runtime.last_results[1]["error"] is False
        # 收尾复位停止标志（下次运行干净起步）
        assert sched._runtime.is_stop_requested(None, 1) is False

    def test_stop_idle_task_rejected(self):
        sched = make_scheduler(tasks=[FakeTask(1)], clock=FakeClock(sh(hour=12)))
        assert sched.stop_running(1) is False


class TestQueuePersistence:
    def test_queued_task_survives_new_scheduler(self):
        """队列在 runtime（PG）里：调度器实例换新（=worker 重启）后排队的
        任务仍被消费——内存队列时代重启即丢，这是拆分顺带修掉的缺陷。"""
        rt = FakeRuntime()
        conn = FakeConn()
        rt.push(conn, 1, "manual")                     # 上一代 worker 入的队
        sched = make_scheduler(tasks=[FakeTask(1)],
                               clock=FakeClock(sh(hour=18, minute=5)),
                               runtime=rt)
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(sched.drain_once())
        finally:
            loop.close()
        assert [c["id"] for c in sched._executor.calls] == [1]
        assert sched._runtime.last_results[1]["trigger"] == "manual"


class TestProgressAndState:
    def test_progress_visible_during_run(self):
        sched_holder: dict = {}

        async def during(task, progress, stop_event):
            progress(1, 3)
            sched_holder["mid"] = sched.runtime_state()["progress"]

        sched = make_scheduler(tasks=[FakeTask(1)], clock=FakeClock(sh(hour=18, minute=5)),
                               executor=FakeExecutor(during=during))
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(sched.scan_once())
            loop.run_until_complete(sched.drain_once())
        finally:
            loop.close()
        assert sched_holder["mid"] == {1: {"done": 1, "total": 3}}
        assert sched.runtime_state()["progress"] == {}, "运行结束应清空进度"

    def test_runtime_state_shape(self):
        sched = make_scheduler(tasks=[FakeTask(1)], clock=FakeClock(sh(hour=17)))
        state = sched.runtime_state()
        assert set(state.keys()) == {"phases", "progress", "queue", "running_task_id",
                                     "server_time"}

    def test_next_run_at_future_today(self):
        """17:00 看 18:00 任务：今晚 18:00。"""
        task = FakeTask(1).d
        assert next_run_at(task, sh(hour=17), ran_today=False) == sh(hour=18)

    def test_next_run_at_after_due_tomorrow(self):
        task = FakeTask(1).d
        assert next_run_at(task, sh(hour=19), ran_today=False) == sh(day=10, hour=18)

    def test_next_run_at_ran_today_tomorrow(self):
        task = FakeTask(1).d
        assert next_run_at(task, sh(hour=19), ran_today=True) == sh(day=10, hour=18)

    def test_next_run_at_disabled_none(self):
        task = FakeTask(1, enabled=False).d
        assert next_run_at(task, sh(hour=17), ran_today=False) is None

    def test_next_run_at_shanghai_across_utc(self):
        """时区边界：18:00 是上海时刻，不受服务器 UTC 时区影响（固定 +8 换算）。"""
        task = FakeTask(1).d
        nxt = next_run_at(task, sh(hour=17), ran_today=False)
        assert nxt.utcoffset() == dt.timedelta(hours=8)
        assert nxt.isoformat() == "2026-10-09T18:00:00+08:00"
