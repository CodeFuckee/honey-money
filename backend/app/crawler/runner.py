"""爬取编排：并发抓取 + 单协程写库的流水线，内置防封措施。

- 抓取：asyncio worker 池（Semaphore 限并发）；每只股票失败只记入汇总，
  绝不中断整体；
- 写库：独立写入协程消费结果队列，psycopg 同步调用放 to_thread 避免阻塞
  事件循环；连接只被写入协程使用，无并发竞争；
- 断点：incremental 模式取库内该源每股最新交易日 +1 作为 beg（按源隔离），
  full 模式一律 beg=0 全量重爬；
- 防封：① batch_size/batch_pause 分批批间休眠；② 连续失败达到
  abort_after_consecutive_failures 即熔停——停止派发新任务、优雅落库退出，
  绝不在带病状态继续冲击数据源。
"""

import asyncio
import datetime as dt
import time
from dataclasses import dataclass, field

from .eastmoney import EastmoneyClient
from .store import Store


@dataclass
class CrawlSummary:
    mode: str
    total_stocks: int
    ok_stocks: int
    failed_stocks: int
    bars_upserted: int
    elapsed_sec: float
    source: str = "eastmoney"
    aborted: bool = False
    failures: list[str] = field(default_factory=list)


class CrawlRunner:
    def __init__(self, *, client: EastmoneyClient, store: Store, conn,
                 concurrency: int = 8, progress=None,
                 sleep=asyncio.sleep, clock=time.monotonic):
        """progress: 可选回调 progress(done, total)，每完成一股触发。
        批次/熔停等单次运行策略放在 run() 参数里。"""
        self._client = client
        self._store = store
        self._conn = conn
        self._concurrency = concurrency
        self._progress = progress
        self._sleep = sleep
        self._clock = clock

    @staticmethod
    def _beg_for(max_date: dt.date | None) -> str:
        """断点日期 → 数据源 beg 参数；无断点拉全史。"""
        if max_date is None:
            return "0"
        return (max_date + dt.timedelta(days=1)).strftime("%Y%m%d")

    async def run(self, stocks: list[dict], *, mode: str = "incremental",
                  limit: int | None = None, source: str = "eastmoney",
                  batch_size: int | None = None, batch_pause: float = 0.0,
                  abort_after_consecutive_failures: int | None = None,
                  on_batch=None) -> CrawlSummary:
        """batch_size/batch_pause：每爬 N 只歇 M 秒（防封节奏）；
        abort_after_consecutive_failures：连续 N 只失败熔停，保存进度退出。
        on_batch：每个批次边界回调（__main__ 里传 conn.commit —— 批级提交，
        之前全run单事务导致：库内进度长期不可见 + 锁持有到结束 + 崩溃全丢）。"""
        if limit is not None:
            stocks = stocks[:limit]
        max_dates = (self._store.max_trade_dates(self._conn, source)
                     if mode == "incremental" else {})

        summary = CrawlSummary(mode=mode, total_stocks=len(stocks),
                               ok_stocks=0, failed_stocks=0, bars_upserted=0,
                               elapsed_sec=0.0, source=source)
        run_id = self._store.start_run(self._conn, mode=mode, source=source)
        started = time.perf_counter()

        results: asyncio.Queue = asyncio.Queue(maxsize=self._concurrency * 2)
        write_task = asyncio.create_task(self._db_writer(results, summary, source))

        state = {"idx": 0, "done": 0, "consec_fail": 0,
                 "abort": False, "pause_until": 0.0}
        sem = asyncio.Semaphore(self._concurrency)

        async def worker() -> None:
            while True:
                if state["abort"]:
                    return
                i = state["idx"]
                if i >= len(stocks):        # 先查队列空，再休眠：结束时不多睡
                    return
                now = self._clock()
                if now < state["pause_until"]:      # 批间休眠（防封节奏）
                    await self._sleep(state["pause_until"] - now)
                state["idx"] = i + 1
                stock = stocks[i]

                async with sem:
                    beg = self._beg_for(max_dates.get(stock["code"]))
                    try:
                        bars = await self._client.fetch_daily_bars(
                            stock["market"], stock["code"], beg=beg)
                    except Exception as exc:   # 单股失败隔离：记录后继续
                        summary.failed_stocks += 1
                        summary.failures.append(stock["code"])
                        state["consec_fail"] += 1
                        print(f"[fail] {stock['code']} {stock['name']}: {exc}")
                        if (abort_after_consecutive_failures is not None
                                and state["consec_fail"] >= abort_after_consecutive_failures):
                            state["abort"] = True
                            print(f"[abort] 连续 {state['consec_fail']} 只失败，"
                                  f"熔停保护：停止派发，保存进度退出")
                    else:
                        state["consec_fail"] = 0
                        await results.put((stock["code"], bars.rows))

                state["done"] += 1
                if self._progress:
                    self._progress(state["done"], summary.total_stocks)
                if batch_size and state["done"] % batch_size == 0:
                    if on_batch:
                        on_batch(state["done"])      # 批级提交：进度可见/锁释放/可断点
                    if (state["done"] < summary.total_stocks
                            and not state["abort"]):
                        state["pause_until"] = self._clock() + batch_pause

        workers = [asyncio.create_task(worker()) for _ in range(
            min(self._concurrency, max(1, len(stocks))))]
        await asyncio.gather(*workers)
        await results.put(None)            # 写入协程收哨兵
        await write_task

        summary.elapsed_sec = round(time.perf_counter() - started, 1)
        summary.aborted = state["abort"]
        self._store.finish_run(
            self._conn, run_id,
            total_stocks=summary.total_stocks, ok_stocks=summary.ok_stocks,
            failed_stocks=summary.failed_stocks,
            bars_upserted=summary.bars_upserted, failures=summary.failures,
            aborted=summary.aborted,
        )
        return summary

    async def _db_writer(self, results: asyncio.Queue, summary: CrawlSummary,
                         source: str) -> None:
        """顺序消费爬取结果并落库（to_thread 隔离同步 psycopg 调用）。"""
        while True:
            item = await results.get()
            if item is None:
                return
            code, rows = item
            n = await asyncio.to_thread(
                self._store.upsert_daily_bars, self._conn, code, rows, source)
            summary.ok_stocks += 1
            summary.bars_upserted += n
