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

    def max_trade_dates(self, conn):
        return dict(self.max_dates)

    def upsert_daily_bars(self, conn, code, rows):
        self.upserted.append((code, len(rows)))
        return len(rows)

    def start_run(self, conn, mode):
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
        for flag in ("--mode", "--limit", "--only", "--concurrency"):
            assert flag in proc.stdout
