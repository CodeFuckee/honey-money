"""爬取编排：并发抓取 + 单协程写库的流水线。

- 抓取：asyncio.Semaphore 限并发；每只股票失败只记入汇总，绝不中断整体；
- 写库：独立写入协程消费结果队列，psycopg 同步调用放 to_thread 避免阻塞
  事件循环；连接只被写入协程使用，无并发竞争；
- 断点：incremental 模式取库内每股最新交易日 +1 作为 beg，已入库的自动
  只拉增量；full 模式一律 beg=0 全量重爬。
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
    failures: list[str] = field(default_factory=list)


class CrawlRunner:
    def __init__(self, *, client: EastmoneyClient, store: Store, conn,
                 concurrency: int = 8, progress=None):
        """progress: 可选回调 progress(done, total)，每完成一股触发。"""
        self._client = client
        self._store = store
        self._conn = conn
        self._concurrency = concurrency
        self._progress = progress

    @staticmethod
    def _beg_for(max_date: dt.date | None) -> str:
        """断点日期 → 东财 beg 参数；无断点拉全史。"""
        if max_date is None:
            return "0"
        return (max_date + dt.timedelta(days=1)).strftime("%Y%m%d")

    async def run(self, stocks: list[dict], *, mode: str = "incremental",
                  limit: int | None = None) -> CrawlSummary:
        if limit is not None:
            stocks = stocks[:limit]
        max_dates = (self._store.max_trade_dates(self._conn)
                     if mode == "incremental" else {})

        summary = CrawlSummary(mode=mode, total_stocks=len(stocks),
                               ok_stocks=0, failed_stocks=0, bars_upserted=0,
                               elapsed_sec=0.0)
        run_id = self._store.start_run(self._conn, mode=mode)
        started = time.perf_counter()
        done_counter = {"done": 0}

        results: asyncio.Queue = asyncio.Queue(maxsize=self._concurrency * 2)
        write_task = asyncio.create_task(self._db_writer(results, summary))

        sem = asyncio.Semaphore(self._concurrency)

        async def crawl_one(stock: dict) -> None:
            async with sem:
                beg = self._beg_for(max_dates.get(stock["code"]))
                try:
                    bars = await self._client.fetch_daily_bars(
                        stock["market"], stock["code"], beg=beg)
                except Exception as exc:   # 单股失败隔离：记录后继续
                    summary.failed_stocks += 1
                    summary.failures.append(stock["code"])
                    print(f"[fail] {stock['code']} {stock['name']}: {exc}")
                else:
                    await results.put((stock["code"], bars.rows))
                done_counter["done"] += 1
                if self._progress:
                    self._progress(done_counter["done"], summary.total_stocks)

        await asyncio.gather(*(crawl_one(s) for s in stocks))
        await results.put(None)            # 写入协程收哨兵
        await write_task

        summary.elapsed_sec = round(time.perf_counter() - started, 1)
        self._store.finish_run(
            self._conn, run_id,
            total_stocks=summary.total_stocks, ok_stocks=summary.ok_stocks,
            failed_stocks=summary.failed_stocks,
            bars_upserted=summary.bars_upserted, failures=summary.failures,
        )
        return summary

    async def _db_writer(self, results: asyncio.Queue, summary: CrawlSummary) -> None:
        """顺序消费爬取结果并落库（to_thread 隔离同步 psycopg 调用）。"""
        while True:
            item = await results.get()
            if item is None:
                return
            code, rows = item
            n = await asyncio.to_thread(
                self._store.upsert_daily_bars, self._conn, code, rows)
            summary.ok_stocks += 1
            summary.bars_upserted += n
