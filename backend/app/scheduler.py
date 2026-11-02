"""爬虫任务调度器：backend 进程内自治调度，替代外部计划任务。

职责边界：
- 调度：每 poll_interval 秒扫描 crawl_task（上海时区 HH:MM，不辨交易日——
  非交易日照常执行，增量断点机制保证空转无害）；启动时对已过点未跑的任务
  补跑一次（catch_up）；任务定义与「今日已计划执行」均以 PG 为准，重启恢复；
- 串行：全局单 worker 队列，同一时刻只跑一个任务（新浪防封限速按全局通道
  设计，并行=加倍请求压力）；后到排队；
- 互斥：执行前取 PostgreSQL advisory lock（会话级、专用连接）——防住手动
  CLI 与调度任务并发写库、未来多 worker 重复调度；取不到则等待重试；
- 执行：委托注入的 executor 协程（生产=build_crawl_executor：拉清单 +
  CrawlRunner，防封参数内置安全默认值），调度器不感知爬虫细节，可全假件测试；
- 停止：stop_event 协作式停止（runner 停止派发、优雅落库，不算失败）；
- 状态：运行态（phase/progress/队列）只在内存——随进程消失，历史在 crawl_run。

scan_once / drain_once 单步公开方法：测试直接驱动，不跑真实循环；
start() 拉起的后台循环只是反复调用它们。
"""

import asyncio
import datetime as dt
from zoneinfo import ZoneInfo

import psycopg

from .crawler.eastmoney import EastmoneyClient
from .crawler.fund_store import FundStore
from .crawler.proxies import ProxyBroker
from .crawler.rate import RateLimiter
from .crawler.runner import CrawlRunner
from .crawler.sina import SinaClient
from .crawler.store import Store
from .task_store import TaskStore

SH_TZ = ZoneInfo("Asia/Shanghai")

# 各源防封默认值（与 CLI __main__.SOURCE_DEFAULTS 一致，任务不可配置——
# 限速/抖动/分批/熔停配错是有实际封 IP 事故史的，杜绝危险组合）
_SOURCE_DEFAULTS = {
    "sina": {"initial_qps": 1.0, "min_qps": 0.2, "max_qps": 5.0},
    "eastmoney": {"initial_qps": 1 / 30, "min_qps": 1 / 30, "max_qps": 1 / 30},
    "delisted": {"initial_qps": 1.0, "min_qps": 0.2, "max_qps": 2.0},
    # 基金接口与 push2his 不同子系统、无被封史：1 QPS 起步 AIMD 自适应，
    # 失败降速托底 0.1、成功最多回到 2，熔停/批休全套照旧
    "fund": {"initial_qps": 1.0, "min_qps": 0.1, "max_qps": 2.0},
}
_BATCH_SIZE, _BATCH_PAUSE, _ABORT_AFTER, _CONCURRENCY = 300, 30.0, 15, 4


def next_run_at(task: dict, now: dt.datetime, *, ran_today: bool) -> dt.datetime | None:
    """任务下次执行时刻（上海时区 aware）；禁用任务 None。

    已过今日计划点或今日已计划执行 → 明天同一时刻；否则今晚。
    """
    if not task["enabled"] or not task["schedule_time"]:
        return None                      # 禁用 / 纯手动任务不参与排程
    hh, mm = task["schedule_time"].split(":")
    due = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
    if due > now and not ran_today:
        return due
    return due + dt.timedelta(days=1)


class PgAdvisoryLock:
    """会话级 advisory lock：专用连接上取/还，防多进程并发爬取。"""

    KEY = "honey-money-crawler"

    def __init__(self, dsn: str):
        self._dsn = dsn
        self._conn = None

    def _connect(self):
        return psycopg.connect(self._dsn, connect_timeout=5)

    async def acquire(self) -> bool:
        if self._conn is None:
            self._conn = await asyncio.to_thread(self._connect)
        cur = await asyncio.to_thread(
            self._conn.execute,
            "SELECT pg_try_advisory_lock(hashtext(%s))", (self.KEY,))
        row = await asyncio.to_thread(cur.fetchone)
        return bool(row and row[0])

    async def release(self) -> None:
        if self._conn is None:
            return
        try:
            await asyncio.to_thread(
                self._conn.execute,
                "SELECT pg_advisory_unlock(hashtext(%s))", (self.KEY,))
        finally:
            conn, self._conn = self._conn, None
            try:
                await asyncio.to_thread(conn.close)
            except Exception:
                pass


async def _run_backtest_job(dsn: str, task: dict, progress,
                            stop_event: asyncio.Event) -> dict:
    """定投回测日更：子进程全量重算（~20-30 分钟，纯 CPU 不占 API 进程）
    → 事务直写 fund_dca_result/fund_dca_rounds → crawl_run 记录。

    前置只做净值新鲜度告警不阻断（跑最新可得数据，爬取失败次日自愈）；
    stop_event 无法中断子进程，仅在各阶段间生效；kill/重启丢弃当日运行
    （导入为末尾单事务，半途无脏数据），次日计划照常。
    """
    import concurrent.futures
    import multiprocessing

    from . import dca_backtest, dca_store
    from .crawler.store import Store

    store = Store()
    loop = asyncio.get_running_loop()
    with psycopg.connect(dsn) as conn:
        latest = conn.execute(
            "SELECT max(nav_date)::date FROM fund_nav").fetchone()[0]
        stale = latest is None or (dt.datetime.now(SH_TZ).date() - latest).days > 1
        if stale:
            print(f"[backtest] 净值最新 {latest}（≥2 天未更新），按现有数据继续")
        run_id = store.start_run(conn, mode=task["mode"], source="backtest",
                                 task_id=task["id"],
                                 trigger=task.get("_trigger", "manual"))
        conn.commit()
    if stop_event.is_set():
        with psycopg.connect(dsn) as conn:
            store.finish_run(conn, run_id, total_stocks=0, ok_stocks=0,
                             failed_stocks=0, bars_upserted=0, failures=[],
                             aborted=True)
            conn.commit()
        return {"total": 0, "ok": 0, "failed": 0, "bars": 0, "failures": [],
                "aborted": True, "stopped": True, "error": False}

    def _job():
        return dca_backtest.run_full_backtest(dsn)

    progress(0, 1)
    ctx = multiprocessing.get_context("spawn")
    try:
        with concurrent.futures.ProcessPoolExecutor(max_workers=1,
                                                    mp_context=ctx) as pool:
            result_rows, rounds_rows, stats = await loop.run_in_executor(pool, _job)
    except Exception as exc:
        with psycopg.connect(dsn) as conn:
            store.finish_run(conn, run_id, total_stocks=0, ok_stocks=0,
                             failed_stocks=0, bars_upserted=0,
                             failures=[f"{type(exc).__name__}: {str(exc)[:180]}"],
                             aborted=True)
            conn.commit()
        raise
    if stop_event.is_set():
        # 算完但等停：丢弃结果（不导入），下次计划照常全量重算
        with psycopg.connect(dsn) as conn:
            store.finish_run(conn, run_id, total_stocks=stats["fund_count"],
                             ok_stocks=0, failed_stocks=0,
                             bars_upserted=stats["round_count"], failures=[],
                             aborted=True)
            conn.commit()
        return {"total": stats["fund_count"], "ok": 0, "failed": 0,
                "bars": stats["round_count"], "failures": [],
                "aborted": True, "stopped": True, "error": False}

    progress(1, 1)
    params = {"amount": 1000.0, "take_profit": 0.2, "frequency": "monthly",
              "window_end": stats.get("window_end"), "min_age_years": None,
              "scale_min_yi": None, "source": "scheduler/backtest"}
    with psycopg.connect(dsn) as conn:
        dca_store.import_rows(conn, result_rows, params=params)
        dca_store.import_rounds(conn, rounds_rows)
        store.finish_run(conn, run_id, total_stocks=stats["fund_count"],
                         ok_stocks=stats["fund_count"], failed_stocks=0,
                         bars_upserted=stats["round_count"], failures=[],
                         aborted=False)
        conn.commit()
    print(f"[backtest] 日更完成：{stats['fund_count']:,} 只 / "
          f"轮次 {stats['round_count']:,} 行，窗口截至 {stats.get('window_end')}")
    return {"total": stats["fund_count"], "ok": stats["fund_count"],
            "failed": 0, "bars": stats["round_count"], "failures": [],
            "aborted": False, "stopped": False, "error": False}


def build_crawl_executor(dsn: str, proxy_pool_url: str):
    """生产执行体：拉清单 → CrawlRunner 增量/全量爬取，返回结果摘要。

    trigger 经 task['_trigger'] 注入（schedule/catch_up/manual），透传
    crawl_run 作「今日已计划执行」判定依据。
    """

    async def execute(task: dict, *, progress, stop_event: asyncio.Event) -> dict:
        if task["source"] == "backtest":
            return await _run_backtest_job(dsn, task, progress, stop_event)
        defaults = _SOURCE_DEFAULTS[task["source"]]
        rate = RateLimiter(initial_qps=defaults["initial_qps"],
                           min_qps=defaults["min_qps"], max_qps=defaults["max_qps"],
                           jitter=0.3)
        store = (FundStore() if task["source"] == "fund" else Store())
        if task["source"] == "sina":
            direct = httpx_async_client(timeout=30.0)
            client = SinaClient(rate=rate, http=direct)
        elif task["source"] == "delisted":
            from .crawler.delisted import DelistedClient
            direct = httpx_async_client(timeout=30.0)
            client = DelistedClient(rate=rate, http=direct)
            with psycopg.connect(dsn) as conn:  # 历史 delisted 源迁回 sina（幂等）
                n = store.migrate_delisted_source(conn)
                conn.commit()
                if n:
                    print(f"[migrate] {n} 行 source delisted → sina")
        elif task["source"] == "fund":
            from .crawler.fund import FundClient
            # 免 Cookie（带 Referer 即可），直连 + AIMD 自适应限速
            direct = httpx_async_client(timeout=30.0)
            client = FundClient(rate=rate, http=direct)
        else:
            from . import em_cookie
            cookie = em_cookie.load_cookie()
            if not cookie:
                raise RuntimeError(
                    "东财源需要浏览器 Cookie：请在网页端「数据源 → 东方财富」"
                    "的 Cookie 卡粘贴保存后重试")
            verdict = em_cookie.verify_cookie(cookie)
            if not verdict["valid"]:
                raise RuntimeError(
                    f"东财 Cookie 验证失败：{verdict['reason']}"
                    "——请更换新 Cookie 后重试")
            print(f"[cookie] 东财 Cookie 验证通过（{verdict.get('checked_at')}）")
            # 家宽/办公网直连 + drip 节奏是东财唯一存活路径（免费代理全断，
            # pool 模式实测 ProxyError 循环——与 CLI/爬虫历史结论一致）
            direct = httpx_async_client(timeout=15.0)
            pool = httpx_async_client(base_url=proxy_pool_url, timeout=5.0)
            client = EastmoneyClient(rate=rate, proxy_broker=ProxyBroker(http=pool),
                                     http=direct, proxy_mode="direct",
                                     cookie=cookie)
        try:
            if task["source"] == "fund" and task["mode"] == "incremental":
                # 快照增量：当日净值列表（t=1）一次拉全市场最新已披露净值，
                # 约 250 页/5 分钟（对比 full 逐只全史 8 小时）。快照不含
                # 货币列表，退市标记不在增量刷新（full 负责）
                from .crawler.fund import crawl_nav_snapshot
                with psycopg.connect(dsn) as conn:
                    summary = await crawl_nav_snapshot(
                        client, store, conn, mode="incremental",
                        source="fund", task_id=task["id"],
                        trigger=task.get("_trigger", "manual"),
                        progress=progress,
                        on_batch=lambda _done: conn.commit())
                return {"total": summary["total"], "ok": summary["ok"],
                        "failed": summary["failed"], "bars": summary["bars"],
                        "failures": summary["failures"][:50],
                        "aborted": False, "stopped": summary["stopped"],
                        "error": False}
            stocks = await client.fetch_stock_list()
            with psycopg.connect(dsn) as conn:
                if task["source"] == "fund":
                    store.upsert_fund_basic(conn, stocks)
                else:
                    store.upsert_stock_basic(conn, stocks)
                conn.commit()
                runner = CrawlRunner(
                    client=client, store=store, conn=conn,
                    concurrency=_CONCURRENCY, progress=progress,
                    upsert=(store.upsert_fund_navs
                            if task["source"] == "fund" else None))
                summary = await runner.run(
                    stocks, mode=task["mode"], source=task["source"],
                    batch_size=_BATCH_SIZE, batch_pause=_BATCH_PAUSE,
                    abort_after_consecutive_failures=_ABORT_AFTER,
                    stop_event=stop_event, task_id=task["id"],
                    trigger=task.get("_trigger", "manual"),
                    on_batch=lambda done: conn.commit())
        finally:
            if hasattr(client, "aclose"):
                await client.aclose()
        return {"total": summary.total_stocks, "ok": summary.ok_stocks,
                "failed": summary.failed_stocks, "bars": summary.bars_upserted,
                "failures": summary.failures[:50], "aborted": summary.aborted,
                "stopped": summary.stopped, "error": False}

    return execute


def httpx_async_client(**kw):
    """延迟导入 httpx（便于无网络依赖的纯调度测试装配）。"""
    import httpx
    return httpx.AsyncClient(**kw)


class TaskScheduler:
    def __init__(self, *, dsn: str, poll_interval: float = 30.0,
                 clock=None, sleeper=None, executor=None, lock=None,
                 connect=None, task_store: TaskStore | None = None):
        self._dsn = dsn
        self._poll_interval = poll_interval
        self._clock = clock or (lambda: dt.datetime.now(SH_TZ))
        self._sleeper = sleeper or asyncio.sleep
        self._executor = executor
        self._lock = lock
        self._connect = connect or psycopg.connect
        self._tasks = task_store or TaskStore()
        self._queue: list[dict] = []          # {task_id, trigger, cancelled}
        self._phases: dict[int, str] = {}     # task_id → queued | running | idle
        self._progress: dict[int, dict] = {}
        self._stop_events: dict[int, asyncio.Event] = {}
        self._running_task_id: int | None = None
        self.last_results: dict[int, dict] = {}
        self._loop_task: asyncio.Task | None = None

    # ---- 后台循环 ----

    async def start(self) -> None:
        if self._loop_task is None:
            self._loop_task = asyncio.create_task(self._run_forever())

    async def stop(self) -> None:
        if self._loop_task is not None:
            self._loop_task.cancel()
            self._loop_task = None

    async def _run_forever(self) -> None:
        first = True
        while True:
            try:
                await self.scan_once(startup=first)
                while self._queue:            # 串行清空队列
                    await self.drain_once()
            except Exception as exc:          # PG 短暂不可达等：记日志下轮再来
                print(f"[scheduler] 扫描异常（下轮重试）: {exc}")
            first = False
            await self._sleeper(self._poll_interval)

    # ---- 扫描与执行（单步，可测）----

    async def scan_once(self, *, startup: bool = False) -> None:
        """扫描任务表：到期（且今日未计划执行）的启用任务入队。"""
        now = self._clock()
        with self._connect(self._dsn) as conn:
            tasks = await asyncio.to_thread(self._tasks.list_tasks, conn)
            for task in tasks:
                if not task["enabled"]:
                    continue
                if not task["schedule_time"]:
                    continue          # 纯手动任务（无调度时刻）不参与排程
                if self._phases.get(task["id"]) in ("queued", "running"):
                    continue
                if await asyncio.to_thread(self._tasks.ran_today, conn, task["id"],
                                           now.date().isoformat()):
                    continue
                hh, mm = task["schedule_time"].split(":")
                due = now.replace(hour=int(hh), minute=int(mm),
                                  second=0, microsecond=0)
                if now >= due:
                    self._enqueue(task["id"],
                                  trigger="catch_up" if startup else "schedule")

    async def drain_once(self) -> bool:
        """执行队列中下一个有效任务（一次一个，全局串行）。无任务返回 False。"""
        while self._queue:
            entry = self._queue.pop(0)
            if entry["cancelled"]:
                continue
            task = await asyncio.to_thread(self._load_task, entry["task_id"])
            if task is None:                  # 排队期间被删除：跳过
                self._phases.pop(entry["task_id"], None)
                continue
            await self._execute(entry, task)
            return True
        return False

    def _load_task(self, task_id: int) -> dict | None:
        with self._connect(self._dsn) as conn:
            return self._tasks.get_task(conn, task_id)

    async def _execute(self, entry: dict, task: dict) -> None:
        task_id = task["id"]
        trigger = entry["trigger"]
        stop_event = asyncio.Event()
        self._stop_events[task_id] = stop_event
        self._phases[task_id] = "running"
        self._running_task_id = task_id
        exec_task = {**task, "_trigger": trigger}

        def progress(done: int, total: int) -> None:
            self._progress[task_id] = {"done": done, "total": total}

        result: dict = {"trigger": trigger, "error": False, "stopped": False,
                        "total": 0, "ok": 0, "failed": 0, "bars": 0,
                        "failures": [], "aborted": False}
        locked = False
        started = dt.datetime.now(SH_TZ)
        print(f"[task] #{task_id} {task['name']} 开始执行（{trigger}，"
              f"{task['source']}/{task['mode']}）")
        try:
            while not (locked := await self._lock.acquire()):
                if stop_event.is_set():       # 等锁期间被停止
                    break
                await self._sleeper(self._poll_interval)
            if locked and not stop_event.is_set():
                result.update(await self._executor(
                    exec_task, progress=progress, stop_event=stop_event))
            result["stopped"] = stop_event.is_set()
        except Exception as exc:              # 执行体异常：记录，不拖垮调度循环
            result["error"] = True
            result["detail"] = f"{type(exc).__name__}: {str(exc)[:200]}"
            print(f"[task] #{task_id} 执行异常: {result['detail']}")
        finally:
            if locked:
                await self._lock.release()
            result["started_at"] = started.isoformat()
            result["finished_at"] = dt.datetime.now(SH_TZ).isoformat()
            self.last_results[task_id] = result
            self._progress.pop(task_id, None)
            self._stop_events.pop(task_id, None)
            self._running_task_id = None
            self._phases[task_id] = "idle"
            print(f"[task] #{task_id} 结束：{'已停止' if result['stopped'] else ''}"
                  f"{'异常' if result['error'] else ''} "
                  f"成 {result['ok']} / 败 {result['failed']}")

    # ---- 管理操作（同步，纯内存/快速 DB 读）----

    def _enqueue(self, task_id: int, *, trigger: str) -> None:
        self._queue.append({"task_id": task_id, "trigger": trigger,
                            "cancelled": False})
        self._phases[task_id] = "queued"

    def request_run(self, task_id: int) -> bool:
        """手动触发（不管 enabled）；排队/运行中拒绝。"""
        if self._phases.get(task_id) in ("queued", "running"):
            return False
        self._enqueue(task_id, trigger="manual")
        return True

    def cancel_queued(self, task_id: int) -> bool:
        if self._phases.get(task_id) != "queued":
            return False
        for entry in self._queue:
            if entry["task_id"] == task_id:
                entry["cancelled"] = True
        self._phases.pop(task_id, None)
        return True

    def stop_running(self, task_id: int) -> bool:
        """停止运行中任务：置位 stop_event，runner 停止派发、优雅落库。"""
        if self._phases.get(task_id) != "running":
            return False
        self._stop_events[task_id].set()
        return True

    def runtime_state(self) -> dict:
        return {
            "phases": dict(self._phases),
            "progress": {k: dict(v) for k, v in self._progress.items()},
            "queue": [e["task_id"] for e in self._queue if not e["cancelled"]],
            "running_task_id": self._running_task_id,
            "server_time": self._clock().isoformat(),
        }
