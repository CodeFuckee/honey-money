"""基金历史净值爬虫测试：清单/净值解析、客户端、存储、runner 写库注入点。

数据通道（2026-10-26 实测，知识库 25-projects/honey-money/fund-nav-data-sources.md）：
- 清单：fund.eastmoney.com/js/fundcode_search.js 一次返回全部基金代码；
- 净值：fund.eastmoney.com/pingzhongdata/{code}.js 单请求全史——lsjz 分页
  接口单页上限 20 条，全史要几百页/只，不可用作爬虫通道；
- x 时间戳为北京时间零点锚点（1008604800000 = 000001 首条净值 2001-12-18）。
"""

import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.crawler import fund as fd  # noqa: E402
from app.crawler.fund_store import FundStore  # noqa: E402
from app.crawler.runner import CrawlRunner  # noqa: E402

# ------------------------------------------------------------ 样本 ----

FUND_LIST_JS = (
    '\ufeffvar r = [["000001","HXCZHH","华夏成长混合","混合型-灵活","HXCZHH"],'
    '["000003","ZHKZZZQA","中海可转债债券A","债券型-混合二级","ZHKZZZQA"],'
    '["005827","YFHL","易方达蓝筹精选","混合型-偏股","YFHL"]];')

X1, X2 = 1008604800000, 1640966400000   # 2001-12-18、2022-01-01（上海时区）

PING_JS = f'''var fS_name = "华夏成长混合";
var Data_netWorthTrend = [{{"x":{X1},"y":1.0,"equityReturn":0,"unitMoney":""}},
{{"x":{X2},"y":1.046,"equityReturn":2.05,"unitMoney":"0.018"}}];
var Data_ACWorthTrend = [[{X1},1.0],[{X2},3.619]];
'''

PING_JS_NO_ACCUM = f'''var fS_name = "货基A";
var Data_netWorthTrend = [{{"x":{X1},"y":1.0,"equityReturn":"-","unitMoney":""}}];
var Data_ACWorthTrend = [];
'''

PING_JS_MONEY_FUND = '''var fS_name = "某货币基金";
var Data_netWorthTrend = [];
var Data_ACWorthTrend = [];
'''


# ------------------------------------------------------------ 清单解析 ----

class TestParseFundList:
    def test_official_shape(self):
        funds = fd.parse_fund_list(FUND_LIST_JS)
        assert funds[0] == {"code": "000001", "market": 0,
                            "name": "华夏成长混合", "fund_type": "混合型-灵活"}
        assert {f["code"] for f in funds} == {"000001", "000003", "005827"}

    def test_no_bom_or_decl_before_var(self):
        funds = fd.parse_fund_list(FUND_LIST_JS.lstrip("\ufeff"))
        assert len(funds) == 3

    def test_empty_text_raises(self):
        with pytest.raises(fd.FundError):
            fd.parse_fund_list("")

    def test_garbage_raises(self):
        with pytest.raises(fd.FundError):
            fd.parse_fund_list("<html>blocked</html>")


# ------------------------------------------------------------ 净值解析 ----

class TestParsePingzhongdata:
    def test_full_rows_aligned_with_accum(self):
        bars = fd.parse_pingzhongdata(PING_JS, "000001")
        assert bars.code == "000001" and bars.source == "fund_em"
        assert bars.name == "华夏成长混合"
        assert bars.dktotal == 2
        # 行：(nav_date, unit_nav, accum_nav, pct_chg, unit_money)
        assert bars.rows[0] == ("2001-12-18", 1.0, 1.0, 0.0, None)
        assert bars.rows[1] == ("2022-01-01", 1.046, 3.619, 2.05, "0.018")

    def test_equity_return_dash_becomes_none(self):
        bars = fd.parse_pingzhongdata(PING_JS_NO_ACCUM, "000001")
        assert bars.rows[0][3] is None            # "-" 涨跌幅缺省
        assert bars.rows[0][2] is None            # 累计净值序列为空 → None

    def test_money_fund_empty_trend_zero_rows(self):
        """货币基金无净值走势：0 行且不报错（记成功、按空处理）。"""
        bars = fd.parse_pingzhongdata(PING_JS_MONEY_FUND, "000001")
        assert bars.rows == []

    def test_missing_segment_raises(self):
        with pytest.raises(fd.FundError):
            fd.parse_pingzhongdata("var other = 1;", "000001")


# ------------------------------------------------------------ 客户端 ----

class FakeResponse:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


class FakeHttp:
    def __init__(self, text):
        self.text = text
        self.requests: list[dict] = []

    async def get(self, url, headers=None, timeout=None):
        self.requests.append({"url": url, "headers": headers})
        return FakeResponse(self.text)


class FakeHttpBoom(FakeHttp):
    async def get(self, url, headers=None, timeout=None):
        self.requests.append({"url": url, "headers": headers})
        raise RuntimeError("连接被拒")


class FakeRate:
    def __init__(self):
        self.acquired: list[str] = []
        self.reports: list[tuple] = []

    def channel_key(self, url):
        return url.split("//", 1)[-1].split("/", 1)[0]

    async def acquire(self, key):
        self.acquired.append(key)

    def report(self, key, ok):
        self.reports.append((key, ok))


def run(coro):
    return asyncio.run(coro)


class TestFundClient:
    def test_fetch_stock_list_parses_with_referer(self):
        http, rate = FakeHttp(FUND_LIST_JS), FakeRate()
        client = fd.FundClient(rate=rate, http=http)
        funds = run(client.fetch_stock_list())
        assert len(funds) == 3
        req = http.requests[0]
        assert "fundcode_search.js" in req["url"]
        assert "fund.eastmoney.com" in (req["headers"] or {}).get("Referer", "")

    def test_fetch_daily_bars_single_request_full_history(self):
        http, rate = FakeHttp(PING_JS), FakeRate()
        client = fd.FundClient(rate=rate, http=http)
        bars = run(client.fetch_daily_bars(0, "000001", beg="20260101"))
        assert any("pingzhongdata/000001.js" in r["url"] for r in http.requests)
        assert len(http.requests) == 1            # beg 忽略：单请求全史
        assert len(bars.rows) == 2
        assert bars.source == "fund_em"
        assert rate.reports[-1][1] is True        # 成功计入限速通道

    def test_garbage_body_raises_and_reports_false(self):
        http, rate = FakeHttp("<html>blocked</html>"), FakeRate()
        client = fd.FundClient(rate=rate, http=http)
        with pytest.raises(fd.FundError):
            run(client.fetch_daily_bars(0, "000001"))
        assert rate.reports[-1][1] is False       # 失败计入通道，驱动 AIMD 降速

    def test_transport_error_raises_and_reports_false(self):
        http, rate = FakeHttpBoom(PING_JS), FakeRate()
        client = fd.FundClient(rate=rate, http=http)
        with pytest.raises(fd.FundError):
            run(client.fetch_daily_bars(0, "000001"))
        assert rate.reports[-1][1] is False


# ------------------------------------------------------------ 存储 ----

class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.executed: list[str] = []
        self.executemany_calls: list[tuple[str, list]] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        self.executed.append(query)

    def executemany(self, query, params_seq):
        self.executemany_calls.append((query, list(params_seq)))

    def fetchone(self):
        return None

    def fetchall(self):
        return []


class FakeConn:
    def __init__(self):
        self.cursor_obj = FakeCursor(self)

    def cursor(self):
        return self.cursor_obj


class TestFundStore:
    def test_init_schema_creates_fund_tables(self):
        conn = FakeConn()
        FundStore().init_schema(conn)
        ddl = "\n".join(conn.cursor_obj.executed)
        assert "CREATE TABLE IF NOT EXISTS fund_basic" in ddl
        assert "CREATE TABLE IF NOT EXISTS fund_nav" in ddl
        assert "PRIMARY KEY (code, nav_date, source)" in ddl

    def test_upsert_fund_basic(self):
        conn = FakeConn()
        FundStore().upsert_fund_basic(conn, [
            {"code": "000001", "name": "华夏成长混合",
             "fund_type": "混合型-灵活"}])
        (sql, rows), = conn.cursor_obj.executemany_calls
        assert "fund_basic" in sql and rows == [("000001", "华夏成长混合",
                                                 "混合型-灵活")]

    def test_upsert_fund_navs_params_and_count(self):
        conn = FakeConn()
        rows = [("2022-01-01", 1.046, 3.619, 2.05, "0.018")]
        n = FundStore().upsert_fund_navs(conn, "000001", rows,
                                         source="fund_em")
        (sql, params), = conn.cursor_obj.executemany_calls
        assert "fund_nav" in sql
        assert params == [("000001", "2022-01-01", 1.046, 3.619, 2.05,
                           "0.018", "fund_em")]
        assert n == 1

    def test_upsert_fund_navs_empty_zero(self):
        assert FundStore().upsert_fund_navs(FakeConn(), "000001", []) == 0

    def test_max_trade_dates_reads_fund_nav_with_source(self):
        conn = FakeConn()
        FundStore().max_trade_dates(conn, "fund_em")
        sql = conn.cursor_obj.executed[0]
        assert "fund_nav" in sql and "source = %s" in sql


# ------------------------------------------------------ runner 注入点 ----

class RunnerBars:
    def __init__(self, code, rows):
        self.code, self.name = code, "x"
        self.dktotal = len(rows)
        self.rows = rows
        self.source = "fund_em"


class RunnerClient:
    def __init__(self, rows):
        self.rows = rows

    async def fetch_daily_bars(self, market, code, *, beg="0", **kw):
        return RunnerBars(code, self.rows)

    async def fetch_stock_list(self):
        return [{"code": "000001", "market": 0, "name": "x"}]


class RunnerStore:
    def __init__(self):
        self.daily_bar_calls: list = []

    def max_trade_dates(self, conn, source=None):
        return {}

    def upsert_daily_bars(self, conn, code, rows, source=None):
        self.daily_bar_calls.append(code)
        return len(rows)

    def start_run(self, conn, mode, source=None, task_id=None,
                  trigger="manual"):
        return 1

    def finish_run(self, conn, run_id, **stats):
        pass


NAV_ROWS = [("2022-01-01", 1.046, 3.619, 2.05, None)]


class TestRunnerUpsertInjection:
    def test_injected_upsert_receives_rows_not_daily_bar(self):
        """注入 upsert 后：行进注入函数，不再走 store.upsert_daily_bars。"""
        store, got = RunnerStore(), []

        def spy_upsert(conn, code, rows, source=None):
            got.append((code, rows, source))
            return len(rows)

        runner = CrawlRunner(client=RunnerClient(NAV_ROWS), store=store,
                             conn=object(), concurrency=1, upsert=spy_upsert)
        summary = run(runner.run([{"code": "000001", "market": 0,
                                   "name": "x"}], source="fund"))
        assert got == [("000001", NAV_ROWS, "fund_em")]
        assert store.daily_bar_calls == []        # 默认股票通道未被触碰
        assert summary.ok_stocks == 1
        assert summary.bars_upserted == 1

    def test_default_upsert_unchanged(self):
        """不注入时行为与现状一致（存量股票源回归守护）。"""
        store = RunnerStore()
        runner = CrawlRunner(client=RunnerClient([("d", 1.0)]), store=store,
                             conn=object(), concurrency=1)
        summary = run(runner.run([{"code": "600000", "market": 1,
                                   "name": "x"}]))
        assert store.daily_bar_calls == ["600000"]
        assert summary.bars_upserted == 1


# ---------------------------------------------------- 任务源与调度接入 ----

class TestFundSourceRegistration:
    def test_task_store_seeds_fund_preset_manual(self):
        from app.task_store import TaskStore
        conn = FakeConn()
        store = TaskStore()
        store.init_schema(conn)
        conn.cursor_obj.executed.clear()
        store.init_schema(conn)                   # 二次 init 不重播（幂等在 SQL 守卫）
        sqls = "\n".join(conn.cursor_obj.executed)
        assert "基金历史净值" in sqls
        assert "'fund'" in sqls                   # 预置任务 source=fund
        assert "WHERE name = '基金历史净值'" in sqls  # 按名字幂等守卫

    def test_scheduler_fund_rate_defaults(self):
        from app.scheduler import _SOURCE_DEFAULTS
        d = _SOURCE_DEFAULTS["fund"]
        assert 0 < d["min_qps"] <= d["initial_qps"] <= d["max_qps"]

    def test_cli_accepts_fund_source(self):
        from app.crawler.__main__ import parse_args, SOURCE_DEFAULTS
        args = parse_args(["--source", "fund"])
        assert args.source == "fund"
        assert "fund" in SOURCE_DEFAULTS
