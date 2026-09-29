"""东财客户端测试：kline 行解析、清单分页、代理失败换代理重试、目标站失败计通道。

MockTransport 模拟东财响应；RateLimiter 用间谍实现记录 report 调用。
"""

import asyncio
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.crawler.eastmoney import (  # noqa: E402
    EastmoneyClient, EastmoneyError, parse_kline_row, secid,
)

KLINE_URL_HOST = "push2his.eastmoney.com"


def run(coro):
    return asyncio.run(coro)


class SpyRate:
    """记录 acquire/report 的假限速器（acquire 零等待）。"""

    def __init__(self):
        self.reports: list[tuple[str, bool]] = []
        self.acquires: list[str] = []

    @staticmethod
    def channel_key(url: str) -> str:
        host = url.split("//", 1)[1].split("/", 1)[0]
        parts = host.split(".")
        if parts[0].isdigit():
            parts = parts[1:]
        return ".".join(parts)

    async def acquire(self, key):
        self.acquires.append(key)
        return 0.0

    def report(self, key, ok):
        self.reports.append((key, ok))


class FakeBroker:
    def __init__(self):
        self.acquired = 0
        self.failures: list = []

    async def acquire(self):
        self.acquired += 1
        return None  # 直连（测试不关心代理地址本身）

    async def report_success(self, proxy):
        pass

    async def report_failure(self, proxy):
        self.failures.append(proxy)


def make_client(handler, *, rate=None, broker=None, max_proxy_retries=2):
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return EastmoneyClient(
        rate=rate or SpyRate(), proxy_broker=broker or FakeBroker(),
        http=http, max_proxy_retries=max_proxy_retries,
    )


class TestSecid:
    def test_sh(self):
        assert secid(1, "600000") == "1.600000"

    def test_sz(self):
        assert secid(0, "000001") == "0.000001"


class TestParseKlineRow:
    ROW = "2026-09-29,9.15,9.18,9.25,9.09,805097,739740988.00,1.75,0.22,0.02,0.24"

    def test_ok_row(self):
        row = parse_kline_row(self.ROW)
        assert row[0] == "2026-09-29"
        assert row[1] == 9.15   # open
        assert row[2] == 9.18   # close
        assert row[3] == 9.25   # high
        assert row[4] == 9.09   # low
        assert row[5] == 805097  # volume(手)
        assert row[6] == 739740988.00  # amount
        assert row[10] == 0.24  # turnover
        assert len(row) == 11

    def test_missing_fields_raises(self):
        with pytest.raises(ValueError):
            parse_kline_row("2026-09-29,9.15,9.18")

    def test_non_numeric_raises(self):
        bad = self.ROW.replace(",805097,", ",not-a-number,")
        with pytest.raises(ValueError):
            parse_kline_row(bad)

    def test_empty_line_raises(self):
        with pytest.raises(ValueError):
            parse_kline_row("")


class TestFetchDailyBars:
    def kline_body(self, klines):
        return {"rc": 0, "data": {"code": "600000", "name": "浦发银行", "dktotal": len(klines),
                                  "klines": klines}}

    def test_ok(self):
        rate = SpyRate()
        client = make_client(
            lambda req: httpx.Response(200, json=self.kline_body([TestParseKlineRow.ROW])),
            rate=rate,
        )
        bars = run(client.fetch_daily_bars(1, "600000"))
        assert bars.name == "浦发银行"
        assert bars.dktotal == 1
        assert bars.rows[0][0] == "2026-09-29"
        # 目标站成功 → 通道记成功
        assert (KLINE_URL_HOST, True) in rate.reports

    def test_api_error_reports_channel_failure(self):
        """rc!=0 / data 为空 → 记通道失败（目标站拒绝，要降速），抛 EastmoneyError。"""
        rate = SpyRate()
        client = make_client(
            lambda req: httpx.Response(200, json={"rc": -1, "data": None}), rate=rate,
        )
        with pytest.raises(EastmoneyError):
            run(client.fetch_daily_bars(1, "600000"))
        assert (KLINE_URL_HOST, False) in rate.reports

    def test_empty_klines_is_ok(self):
        """beg 晚于最后交易日：klines 为空是正常业务结果（增量无新数据），不算失败。"""
        client = make_client(lambda req: httpx.Response(200, json=self.kline_body([])))
        bars = run(client.fetch_daily_bars(1, "600000", beg="20991231"))
        assert bars.rows == []

    def test_bad_json_raises(self):
        rate = SpyRate()
        client = make_client(lambda req: httpx.Response(200, text="<html>blocked</html>"), rate=rate)
        with pytest.raises(EastmoneyError):
            run(client.fetch_daily_bars(1, "600000"))
        assert (KLINE_URL_HOST, False) in rate.reports


class TestProxyRetry:
    def test_proxy_error_switches_proxy_and_retries(self):
        """代理连不上 → 换代理重试（不记通道失败），重试成功即返回。"""
        rate = SpyRate()
        broker = FakeBroker()
        calls = {"n": 0}

        def handler(req):
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.ProxyError("proxy dead")
            return httpx.Response(200, json={"rc": 0, "data": {"code": "600000", "name": "x",
                                                               "dktotal": 0, "klines": []}})

        client = make_client(handler, rate=rate, broker=broker)
        bars = run(client.fetch_daily_bars(1, "600000"))
        assert bars.rows == []
        assert calls["n"] == 2                    # 重试了一次
        assert broker.acquired == 2               # 取了新代理
        assert broker.failures == [None]          # 旧代理被上报降分
        assert (KLINE_URL_HOST, False) not in rate.reports  # 代理问题不算目标站失败

    def test_proxy_retries_exhausted_raises(self):
        rate = SpyRate()
        broker = FakeBroker()

        def handler(req):
            raise httpx.ProxyError("always dead")

        client = make_client(handler, rate=rate, broker=broker, max_proxy_retries=2)
        with pytest.raises(EastmoneyError):
            run(client.fetch_daily_bars(1, "600000"))
        assert broker.acquired == 3               # 首次 + 2 次重试
        assert rate.reports == []                 # 从未到达目标站，不评价通道

    def test_server_disconnect_retries_and_reports(self):
        """对端直接断连（真实环境实测会出现）：换代理重试 + 记通道失败。"""
        rate = SpyRate()
        broker = FakeBroker()
        calls = {"n": 0}

        def handler(req):
            calls["n"] += 1
            if calls["n"] == 1:
                raise httpx.RemoteProtocolError("Server disconnected")
            return httpx.Response(200, json={"rc": 0, "data": {"code": "600000",
                                                               "name": "x", "dktotal": 0,
                                                               "klines": []}})

        client = make_client(handler, rate=rate, broker=broker)
        bars = run(client.fetch_daily_bars(1, "600000"))
        assert bars.rows == []
        assert calls["n"] == 2
        assert (KLINE_URL_HOST, False) in rate.reports   # 断连计入通道降速
        assert broker.failures == [None]                  # 换代理重试


class TestProxyMode:
    def test_direct_mode_skips_broker(self):
        """direct 模式完全绕过代理池（东财断连机房 IP，家宽直连才是有效路径）。"""
        rate = SpyRate()
        broker = FakeBroker()

        def handler(req):
            return httpx.Response(200, json={"rc": 0, "data": {"code": "600000",
                                                               "name": "x", "dktotal": 0,
                                                               "klines": []}})

        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = EastmoneyClient(rate=rate, proxy_broker=broker, http=http,
                                 proxy_mode="direct")
        run(client.fetch_daily_bars(1, "600000"))
        assert broker.acquired == 0


class TestFetchStockList:
    def test_pagination_until_empty(self):
        """clist 按 pn 翻页，diff 为空即止；只翻两次。"""
        rate = SpyRate()
        pages = [
            {"total": 3, "diff": [{"f12": "600000", "f13": 1, "f14": "浦发银行"},
                                   {"f12": "000001", "f13": 0, "f14": "平安银行"}]},
            {"total": 3, "diff": [{"f12": "300750", "f13": 0, "f14": "宁德时代"}]},
            {"total": 3, "diff": []},
        ]

        def handler(req):
            pn = int(req.url.params["pn"])
            return httpx.Response(200, json={"rc": 0, "data": pages[min(pn - 1, 2)]})

        client = make_client(handler, rate=rate)
        stocks = run(client.fetch_stock_list())
        assert stocks == [
            {"code": "600000", "market": 1, "name": "浦发银行"},
            {"code": "000001", "market": 0, "name": "平安银行"},
            {"code": "300750", "market": 0, "name": "宁德时代"},
        ]
