"""爬取编排测试：断点续爬、失败隔离、并发限制，全用假 client/store。"""

import asyncio
import datetime as dt
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.crawler.eastmoney import EastmoneyError  # noqa: E402
from app.crawler.runner import CrawlRunner  # noqa: E402

ROW = ("2026-09-29", 9.15, 9.18, 9.25, 9.09, 805097, 739740988.0, 1.75, 0.22, 0.02, 0.24)


class FakeBars:
    def __init__(self, code, rows):
        self.code = code
        self.name = "fake"
        self.dktotal = len(rows)
        self.rows = rows


class FakeClient:
    def __init__(self, fail_codes=()):
        self.fail_codes = set(fail_codes)
        self.calls: list[dict] = []

    async def fetch_daily_bars(self, market, code, *, beg="0", **kw):
        self.calls.append({"market": market, "code": code, "beg": beg})
        await asyncio.sleep(0)
        if code in self.fail_codes:
            raise EastmoneyError(f"mock fail {code}")
        return FakeBars(code, [ROW] if beg != "20991231" else [])

    async def fetch_stock_list(self):
        return STOCKS


class FakeStore:
    def __init__(self, max_dates=None):
        self.max_dates = max_dates or {}
        self.upserted: list[tuple[str, int]] = []
        self.sources: list[str] = []

    def max_trade_dates(self, conn, source=None):
        return dict(self.max_dates)

    def upsert_daily_bars(self, conn, code, rows, source=None):
        self.upserted.append((code, len(rows)))
        self.sources.append(source)
        return len(rows)

    def start_run(self, conn, mode, source=None, task_id=None, trigger="manual"):
        self.run_source = source
        self.run_task_id = task_id
        self.run_trigger = trigger
        return 1

    def finish_run(self, conn, run_id, **stats):
        self.last_stats = stats


class FakeConn:
    pass


STOCKS = [
    {"code": "600000", "market": 1, "name": "浦发银行"},
    {"code": "000001", "market": 0, "name": "平安银行"},
    {"code": "300750", "market": 0, "name": "宁德时代"},
]


def make_runner(client=None, store=None):
    return CrawlRunner(
        client=client or FakeClient(), store=store or FakeStore(),
        conn=FakeConn(), concurrency=2,
    )


def run(coro):
    return asyncio.run(coro)


class TestHappyPath:
    def test_all_ok(self):
        client, store = FakeClient(), FakeStore()
        summary = run(make_runner(client, store).run(STOCKS))
        assert summary.total_stocks == 3
        assert summary.ok_stocks == 3
        assert summary.failed_stocks == 0
        assert summary.bars_upserted == 3
        assert sorted(code for code, _ in store.upserted) == ["000001", "300750", "600000"]

    def test_finish_run_recorded(self):
        store = FakeStore()
        run(make_runner(store=store).run(STOCKS))
        assert store.last_stats["ok_stocks"] == 3


class TestFailureIsolation:
    def test_failed_stock_does_not_abort_run(self):
        client, store = FakeClient(fail_codes={"000001"}), FakeStore()
        summary = run(make_runner(client, store).run(STOCKS))
        assert summary.ok_stocks == 2
        assert summary.failed_stocks == 1
        assert "000001" in summary.failures
        # 失败股不落库，其余两股正常
        assert sorted(code for code, _ in store.upserted) == ["300750", "600000"]

    def test_unexpected_error_also_isolated(self):
        class ExplodingClient(FakeClient):
            async def fetch_daily_bars(self, market, code, *, beg="0", **kw):
                if code == "300750":
                    raise RuntimeError("boom")  # 非 EastmoneyError 的意外异常
                return await super().fetch_daily_bars(market, code, beg=beg)

        summary = run(make_runner(ExplodingClient()).run(STOCKS))
        assert summary.failed_stocks == 1
        assert "300750" in summary.failures


class TestIncremental:
    def test_new_stock_fetches_full_history(self):
        client, store = FakeClient(), FakeStore(max_dates={})
        run(make_runner(client, store).run(STOCKS))
        assert all(c["beg"] == "0" for c in client.calls)

    def test_existing_stock_fetches_from_next_day(self):
        """断点续爬：库内最新 2026-09-29 → 只拉 20260930 起。"""
        client = FakeClient()
        store = FakeStore(max_dates={
            "600000": dt.date(2026, 9, 29),
            "000001": dt.date(2026, 9, 29),
            "300750": dt.date(2026, 9, 29),
        })
        run(make_runner(client, store).run(STOCKS))
        assert all(c["beg"] == "20260930" for c in client.calls)

    def test_full_mode_ignores_checkpoint(self):
        client = FakeClient()
        store = FakeStore(max_dates={"600000": dt.date(2026, 9, 29)})
        run(make_runner(client, store).run(STOCKS[:1], mode="full"))
        assert client.calls[0]["beg"] == "0"


class TestLimit:
    def test_limit_restricts_stock_count(self):
        client, store = FakeClient(), FakeStore()
        summary = run(make_runner(client, store).run(STOCKS, limit=1))
        assert summary.total_stocks == 1
        assert len(client.calls) == 1

    def test_limit_zero_processes_nothing(self):
        summary = run(make_runner().run(STOCKS, limit=0))
        assert summary.ok_stocks == 0
        assert summary.total_stocks == 0


class TestCli:
    def test_help_exits_zero(self):
        """python -m app.crawler --help 可用（参数说明包含关键开关）。"""
        backend = str(Path(__file__).resolve().parents[1])
        proc = subprocess.run(
            [sys.executable, "-m", "app.crawler", "--help"],
            cwd=backend, capture_output=True, text=True, timeout=30,
        )
        assert proc.returncode == 0
        for flag in ("--mode", "--limit", "--only", "--concurrency", "--source"):
            assert flag in proc.stdout


class TestAntiBan:
    """防封措施：分批批间休眠、连续失败熔停。"""

    def test_batch_pause_between_batches(self):
        """每 batch_size 只休 batch_pause 秒（注入假 sleep 记录）。"""
        sleeps: list[float] = []

        async def fake_sleep(sec):
            sleeps.append(sec)

        runner = CrawlRunner(client=FakeClient(), store=FakeStore(), conn=FakeConn(),
                             concurrency=1, sleep=fake_sleep)
        run(runner.run(STOCKS, batch_size=1, batch_pause=30))
        # 3 只分 3 批，批间歇 2 次（真实时钟下 ≈30s，取近似）
        pauses = [s for s in sleeps if 29.0 <= s <= 30.5]
        assert len(pauses) == 2

    def test_no_pause_without_batches(self):
        sleeps: list[float] = []

        async def fake_sleep(sec):
            sleeps.append(sec)

        runner = CrawlRunner(client=FakeClient(), store=FakeStore(), conn=FakeConn(),
                             concurrency=1, sleep=fake_sleep)
        run(runner.run(STOCKS, batch_pause=30))   # 未开分批
        assert sleeps == []

    def test_aborts_after_consecutive_failures(self):
        """连续 N 只失败即熔停：不再派发剩余任务，优雅保存退出。"""
        client = FakeClient(fail_codes={s["code"] for s in STOCKS})
        store = FakeStore()
        runner = CrawlRunner(client=client, store=store, conn=FakeConn(),
                             concurrency=1)
        summary = run(runner.run(STOCKS, abort_after_consecutive_failures=2))
        assert summary.aborted is True
        assert summary.failed_stocks == 2      # 第 2 只失败后停止，第 3 只未派发
        assert summary.ok_stocks == 0

    def test_success_resets_abort_counter(self):
        """失败计数被成功打断：夹着成功的零星失败不触发熔停。"""
        client = FakeClient(fail_codes={"000001"})   # 第 2 只失败
        runner = CrawlRunner(client=client, store=FakeStore(), conn=FakeConn(),
                             concurrency=1)
        summary = run(runner.run(STOCKS, abort_after_consecutive_failures=2))
        assert summary.aborted is False
        assert summary.ok_stocks == 2 and summary.failed_stocks == 1


class TestSource:
    def test_source_passed_to_store_and_run(self):
        """来源标注贯穿：store 收到 source，run 汇总记录 source。"""
        client, store = FakeClient(), FakeStore()
        summary = run(CrawlRunner(client=client, store=store, conn=FakeConn())
                      .run(STOCKS, source="sina"))
        assert summary.source == "sina"
        assert store.sources == ["sina"] * 3
        assert store.run_source == "sina"


class TestBatchCommit:
    """批级提交钩子：每个 batch_size 边界必须回调（含最后一个边界），
    库内进度可见/锁及时释放——全 run 单事务的教训修复。"""

    def test_on_batch_called_at_boundaries(self):
        calls = []
        runner = make_runner()
        summary = run(runner.run(STOCKS, batch_size=1, on_batch=calls.append))
        assert calls == [1, 2, 3], f"批边界回调异常: {calls}"   # STOCKS 3 只
        assert summary.ok_stocks == 3

    def test_on_batch_none_when_no_batch(self):
        calls = []
        runner = make_runner()
        run(runner.run(STOCKS, batch_size=None, on_batch=calls.append))
        assert calls == []


class TestStopEvent:
    """外部停止信号（任务管理页「停止」按钮）：停止派发、优雅落库、不算失败。"""

    def test_stopped_before_start_runs_nothing(self):
        stop = asyncio.Event()
        stop.set()
        client, store = FakeClient(), FakeStore()
        summary = run(make_runner(client, store).run(STOCKS, stop_event=stop))
        assert client.calls == []
        assert summary.ok_stocks == 0
        assert summary.stopped is True
        assert summary.aborted is False, "用户主动停止 ≠ 熔停失败"

    def test_stop_midway_finishes_inflight_and_stops_dispatch(self):
        """第 1 只完成后置位：在跑的不打断，后续不再派发。"""
        stop = asyncio.Event()
        client, store = FakeClient(), FakeStore()

        async def fetch_then_stop(market, code, *, beg="0", **kw):
            result = await client.fetch_daily_bars(market, code, beg=beg)
            if len(client.calls) >= 1:
                stop.set()
            return result

        runner = CrawlRunner(client=FakeClientProxy(client, fetch_then_stop),
                             store=store, conn=FakeConn(), concurrency=1)
        summary = run(runner.run(STOCKS, stop_event=stop))
        assert summary.stopped is True
        assert summary.ok_stocks == 1          # 只有第 1 只完成
        assert len(client.calls) == 1          # 第 2、3 只未派发

    def test_no_stop_event_runs_to_completion(self):
        summary = run(make_runner().run(STOCKS, stop_event=None))
        assert summary.ok_stocks == 3
        assert summary.stopped is False

    def test_task_id_and_trigger_passthrough(self):
        """调度器联动：run 记录关联 task_id 与触发方式（crawl_run.task_id/trigger）。"""
        store = FakeStore()
        run(make_runner(store=store).run(STOCKS, task_id=5, trigger="catch_up"))
        assert store.run_task_id == 5
        assert store.run_trigger == "catch_up"

    def test_default_trigger_is_manual(self):
        """CLI 直跑不传参：trigger=manual（ran_today 不认 manual，不影响当晚调度）。"""
        store = FakeStore()
        run(make_runner(store=store).run(STOCKS))
        assert store.run_trigger == "manual"


class FakeClientProxy:
    """包装 client，替换 fetch_daily_bars 实现（其余方法透传）。"""

    def __init__(self, inner, fetch):
        self._inner = inner
        self._fetch = fetch

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def fetch_daily_bars(self, market, code, *, beg="0", **kw):
        return await self._fetch(market, code, beg=beg, **kw)
