"""新浪源客户端测试：清单分页/北交所过滤、日线映射/单位转换/衍生字段现算。

日线取数经 akshare（kjs 全史接口），测试注入假 fetcher 返回构造 DataFrame；
清单为自研 httpx 分页（MockTransport）。
"""

import asyncio
import datetime as dt
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.crawler.sina import SinaClient, df_to_rows  # noqa: E402


def run(coro):
    return asyncio.run(coro)


class SpyRate:
    def __init__(self):
        self.reports = []
        self.acquires = []

    @staticmethod
    def channel_key(url: str) -> str:
        return url.split("//", 1)[1].split("/", 1)[0]

    async def acquire(self, key):
        self.acquires.append(key)
        return 0.0

    def report(self, key, ok):
        self.reports.append((key, ok))


def make_client(handler=None, *, daily=None):
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler or (lambda req: httpx.Response(200, json=[]))))
    return SinaClient(rate=SpyRate(), http=http, daily_fetcher=daily), http


def make_handler(pages, total=5570):
    """按 URL 分发：getHQNodeStockCount → 总数；getHQNodeData → 分页数据。"""
    def handler(req):
        if "getHQNodeStockCount" in req.url.path:
            return httpx.Response(200, json=total)
        page = int(req.url.params["page"])
        return httpx.Response(200, json=pages[min(page - 1, len(pages) - 1)])
    return handler


class TestFetchStockList:
    def test_pagination_and_prefix_filter(self):
        """分页直到空页；sh/sz 转 market，北交所(bj)过滤掉；总数校验通过。"""
        handler = make_handler([
            [{"symbol": "sh600000", "code": "600000", "name": "浦发银行"},
             {"symbol": "sz000001", "code": "000001", "name": "平安银行"},
             {"symbol": "bj920000", "code": "920000", "name": "安徽凤凰"}],
            [{"symbol": "sz300750", "code": "300750", "name": "宁德时代"}],
            [],
        ], total=3)   # 官方总数=3（含 bj），过滤后 3 只 ≥ 3*0.85 校验通过

        client, _ = make_client(handler)
        stocks = run(client.fetch_stock_list())
        assert stocks == [
            {"code": "600000", "market": 1, "name": "浦发银行"},
            {"code": "000001", "market": 0, "name": "平安银行"},
            {"code": "300750", "market": 0, "name": "宁德时代"},
        ]   # bj920000 被过滤

    def test_with_referer_header(self):
        """新浪接口需要 Referer（实测不带可能被拒）。"""
        seen = {}

        def handler(req):
            seen["referer"] = req.headers.get("referer")
            if "getHQNodeStockCount" in req.url.path:
                return httpx.Response(200, json=3)
            return httpx.Response(200, json=[
                {"symbol": "sh600000", "code": "600000", "name": "浦发银行"}])

        client, _ = make_client(handler)
        run(client.fetch_stock_list())
        assert "sina.com.cn" in (seen["referer"] or "")

    def test_truncated_list_raises(self):
        """空页截断（软限流）且远低于官方总数 → 报错而非静默漏爬。"""
        handler = make_handler([
            [{"symbol": "sh600000", "code": "600000", "name": "浦发银行"}],
            [],   # 第 2 页空，重试 2 次仍空 → 放弃
        ], total=5570)

        client, _ = make_client(handler)
        with pytest.raises(Exception, match="清单不完整"):
            run(client.fetch_stock_list())

    def test_empty_page_retries_same_page(self):
        """空页重试同页：第 2 页首次空、重试成功 → 正常继续分页。"""
        calls: list[int] = []

        def handler(req):
            if "getHQNodeStockCount" in req.url.path:
                return httpx.Response(200, json=2)
            page = int(req.url.params["page"])
            calls.append(page)
            if page >= 3:
                return httpx.Response(200, json=[])   # 第 3 页起结束
            if page == 2 and calls.count(2) == 1:
                return httpx.Response(200, json=[])   # 首次空（软限流）
            return httpx.Response(200, json=[
                {"symbol": "sh600000", "code": "600000", "name": "浦发银行"}]
                if page == 1 else
                [{"symbol": "sz000001", "code": "000001", "name": "平安银行"}])

        client, _ = make_client(handler)
        stocks = run(client.fetch_stock_list())
        assert len(stocks) == 2
        assert calls.count(2) >= 2   # 第 2 页被重试过


class FakeDF(dict):
    """极简 DataFrame 替身：支持列访问与按行迭代。"""

    def __init__(self, columns, rows):
        super().__init__({c: [r[i] for r in rows] for i, c in enumerate(columns)})

    def __len__(self):
        return len(next(iter(self.values())))

    @property
    def empty(self):
        return len(self) == 0


def daily_df():
    return FakeDF(
        ["date", "open", "high", "low", "close", "volume", "amount",
         "outstanding_share", "turnover"],
        [
            [dt.date(2026, 9, 28), 9.03, 9.18, 9.00, 9.16, 118485300, 1.08e9, 3.33e10, 0.00356],
            [dt.date(2026, 9, 29), 9.15, 9.25, 9.09, 9.18, 80509739, 7.4e8, 3.33e10, 0.00242],
        ],
    )


class TestDfToRows:
    def test_mapping_and_units(self):
        """列映射、volume 股→手、衍生字段现算（基于前收）。"""
        rows = df_to_rows(daily_df())
        assert len(rows) == 2
        r1, r2 = rows
        assert r2[0] == "2026-09-29"
        assert r2[1] == 9.15 and r2[2] == 9.18      # open, close
        assert r2[5] == 805097                       # volume: 80509739 股 → 手
        assert r2[10] == pytest.approx(0.242, abs=0.01)   # turnover：小数×100 统一百分数口径
        assert r2[11] == pytest.approx(3.33e10)              # outstanding_share
        # pct_chg = (9.18 - 9.16) / 9.16 ≈ 0.218%（与东财口径一致）
        assert r2[8] == pytest.approx(0.2183, abs=0.02)
        # amplitude = (9.25 - 9.09) / 9.16
        assert r2[7] == pytest.approx(1.747, abs=0.05)
        # change = 9.18 - 9.16
        assert r2[9] == pytest.approx(0.02, abs=1e-6)
        # 首根无前收：衍生字段为 None
        assert r1[7] is None and r1[8] is None and r1[9] is None

    def test_beg_filters_old_rows(self):
        """增量场景：beg 之前的行丢弃。"""
        rows = df_to_rows(daily_df(), beg="20260929")
        assert len(rows) == 1 and rows[0][0] == "2026-09-29"

    def test_empty_df(self):
        assert df_to_rows(FakeDF(["date"], [])) == []


class TestFetchDailyBars:
    def test_ok(self):
        client, _ = make_client(daily=lambda symbol, **kw: daily_df())
        bars = run(client.fetch_daily_bars(1, "600000"))
        assert bars.code == "600000"
        assert bars.source == "sina"
        assert len(bars.rows) == 2

    def test_symbol_prefix_by_market(self):
        seen = {}

        def fetcher(symbol, **kw):
            seen["symbol"] = symbol
            return daily_df()

        client, _ = make_client(daily=fetcher)
        run(client.fetch_daily_bars(0, "000001"))    # 深市
        assert seen["symbol"] == "sz000001"

    def test_incremental_beg_passed(self):
        seen = {}

        def fetcher(symbol, *, start_date=None, end_date=None):
            seen["start_date"] = start_date
            return daily_df()

        client, _ = make_client(daily=fetcher)
        run(client.fetch_daily_bars(1, "600000", beg="20260930"))
        assert seen["start_date"] == "20260930"

    def test_fetcher_error_raises_sina_error(self):
        def boom(symbol, **kw):
            raise RuntimeError("sina down")

        client, _ = make_client(daily=boom)
        from app.crawler.sina import SinaError
        with pytest.raises(SinaError):
            run(client.fetch_daily_bars(1, "600000"))

    def test_rate_channel_used(self):
        """每次日线取数前必须经过限速通道（新浪域名独立限速）。"""
        client, _ = make_client(daily=lambda symbol, **kw: daily_df())
        run(client.fetch_daily_bars(1, "600000"))
        rate = client._rate
        assert any("sina" in k for k in rate.acquires)
