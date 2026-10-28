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
    def __init__(self, text, status_code=200):
        self.text = text
        self.status_code = status_code

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


class FakeRedirectHttp(FakeHttp):
    """所有请求返回 301（源上无数据文件的后端份额类代码）。"""

    async def get(self, url, params=None, headers=None, timeout=None):
        self.requests.append({"url": url, "headers": headers})
        return FakeResponse("", status_code=301)


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
        self.last_query = query
        self.executed.append(query)

    def executemany(self, query, params_seq):
        self.executemany_calls.append((query, list(params_seq)))

    def fetchone(self):
        # start_run 的 INSERT...RETURNING id：返回固定 id；其余查询（PK 检查
        # 等）返回 None 走"无行"分支
        if "RETURNING id" in self.last_query:
            return (1,)
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
        assert "fund_basic" in sql
        assert rows == [("000001", "华夏成长混合", "混合型-灵活", None)]

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


# ---------------------------------------------------- 在市集合与退市标记 ----

JJJZ_PAGE1 = ('var db={chars:["0","a"],datas:[["000001","华夏成长混合","HXCZHH"],'
              '["000003","中海可转债债券A","ZHKZZZQA"]],'
              'record:"2",pages:"1",curpage:"1"};')
# t=7 货币列表：传统货基只在此列表（在市并集的第二路，000009 误标教训）
JJJZ_PAGE7 = ('var db={datas:[["000009","易方达天天理财货币A"]],'
              'record:"1",pages:"1"};')


class FakeMultiHttp:
    """按调用序返回不同响应体（清单 → JJJZ 第1页 → JJJZ 第2页…）。

    params 像 httpx 一样编入 URL（?page=N），供翻页断言。"""

    def __init__(self, bodies):
        self.bodies = list(bodies)
        self.requests: list[dict] = []

    async def get(self, url, params=None, headers=None, timeout=None):
        from urllib.parse import urlencode
        if params:
            url = f"{url}?{urlencode(params)}"
        self.requests.append({"url": url, "headers": headers})
        return FakeResponse(self.bodies.pop(0))


class TestLiveCodesAndDelistedMark:
    def test_parse_jjjz_codes(self):
        assert fd.parse_jjjz_codes(JJJZ_PAGE1) == {"000001", "000003"}

    def test_fetch_stock_list_marks_delisted(self):
        """在集合=在市(False)；全集差集=退市(True)——退市股历史净值仍爬。"""
        http = FakeMultiHttp([FUND_LIST_JS, JJJZ_PAGE1, JJJZ_PAGE7])
        rate = FakeRate()
        client = fd.FundClient(rate=rate, http=http)
        funds = run(client.fetch_stock_list())
        by_code = {f["code"]: f["delisted"] for f in funds}
        # 在集合=在市；全集差集（005827）=清盘/退市——其历史净值仍照爬
        assert by_code == {"000001": False, "000003": False, "005827": True}
        # 在市=两列表并集：t=1 净值列表 + t=7 货币列表（货基只在后者）
        jjjz_urls = [r["url"] for r in http.requests
                     if "Fund_JJJZ_Data" in r["url"]]
        assert len(jjjz_urls) == 2
        assert "t=1" in jjjz_urls[0] and "t=7" in jjjz_urls[1]

    def test_jjjz_failure_falls_back_unmarked(self):
        """在市集合接口失败：跳过标记（全部不标退市），清单照常返回。"""
        http = FakeMultiHttp([FUND_LIST_JS, "<html>blocked</html>"])
        client = fd.FundClient(rate=FakeRate(), http=http)
        funds = run(client.fetch_stock_list())
        assert len(funds) == 3
        # 回退语义：不带 delisted 键 → 存储层 f.get()=None → COALESCE 保库内值
        assert all("delisted" not in f for f in funds)


class TestNoDataRedirect:
    def test_redirect_means_zero_rows_success(self):
        """301/404=源上无此基金数据（从未披露净值的份额代码）：0 行记成功
        跳过，不计入失败（与真实失败在运行汇总里保持可分）。"""
        http, rate = FakeRedirectHttp(PING_JS), FakeRate()
        client = fd.FundClient(rate=rate, http=http)
        bars = run(client.fetch_daily_bars(0, "000002"))
        assert bars.rows == [] and bars.dktotal == 0
        assert rate.reports[-1][1] is True          # 源已明确答复，非失败


class TestFundStoreDelisted:
    def test_init_schema_adds_delisted_column(self):
        """老库补列幂等：ALTER ADD COLUMN IF NOT EXISTS。"""
        conn = FakeConn()
        FundStore().init_schema(conn)
        ddl = "\n".join(conn.cursor_obj.executed)
        assert "ALTER TABLE fund_basic ADD COLUMN IF NOT EXISTS delisted" in ddl

    def test_upsert_fund_basic_keeps_delisted_when_none(self):
        """在市集合拉取失败传 None：COALESCE 保留库内已有标记，不误重置。"""
        conn = FakeConn()
        FundStore().upsert_fund_basic(conn, [
            {"code": "000001", "name": "华夏成长混合", "fund_type": "混合型",
             "delisted": True},
            {"code": "000003", "name": "中海可转债", "fund_type": "债券型"},
        ])
        (sql, rows), = conn.cursor_obj.executemany_calls
        assert "COALESCE" in sql
        assert rows[0] == ("000001", "华夏成长混合", "混合型", True)
        assert rows[1][3] is None


# ---------------------------------------------------- 净值快照增量模式 ----

# 快照页：今日槽(007805 已发布)/昨日槽(270007 未发布今日)/双空(QDII 滞后)
SNAP_PAGE1 = ('var db={chars:["0","a"],datas:['
              '["007805","华宝浮动净值货币","HBFHDJZHB","108.5867","110.5867",'
              '"108.5851","110.5851","0.0016","0.00","暂停申购","开放赎回","","1","0","1","","0","0.00%","","",""],'
              '["270007","广发大盘成长混合","GFDPCZHH","","","1.5654","1.7368","","",'
              '"开放申购","开放赎回","","1","0","1","","1","0.15%","","",""],'
              '["206006","鹏华全球中短债(QDII)人民币A","PHQZDZ","","","","","",'
              '"开放申购","开放赎回","","1","0","1","","0","0.00%","","",""]],'
              'record:"3",pages:"1",curpage:"1",'
              'showday:["2026-10-28","2026-10-27"]};')


class TestParseJjjzSnapshot:
    def test_slot_date_rule_and_laggy_skip(self):
        """今日槽有值→今日日期；空→昨日槽+昨日日期；双空（QDII 滞后）跳过——
        绝不落错误日期的行（数据完整性红线）。"""
        rows, pages, record = fd.parse_jjjz_snapshot(SNAP_PAGE1)
        assert pages == 1 and record == 3
        by_code = {r["code"]: r for r in rows}
        assert set(by_code) == {"007805", "270007"}      # 206006 双空被跳过
        assert by_code["007805"] == {
            "code": "007805", "name": "华宝浮动净值货币",
            "unit_nav": 108.5867, "accum_nav": 110.5867,
            "pct_chg": 0.0, "nav_date": "2026-10-28"}
        assert by_code["270007"]["nav_date"] == "2026-10-27"
        assert by_code["270007"]["unit_nav"] == 1.5654
        assert by_code["270007"]["pct_chg"] is None      # 增长率只随今日槽

    def test_garbage_raises(self):
        with pytest.raises(fd.FundError):
            fd.parse_jjjz_snapshot("<html>blocked</html>")


class TestIterNavSnapshot:
    def test_pagination_yields_per_page_rows(self):
        p1 = SNAP_PAGE1.replace('pages:"1"', 'pages:"2"')
        p2 = ('var db={datas:[["000001","华夏成长混合","HXCZHH","1.019",'
              '"3.592","1.046","3.619","-0.027","-2.58","开放申购","开放赎回"]],'
              'record:"4",pages:"2",curpage:"2",'
              'showday:["2026-10-28","2026-10-27"]};')
        http = FakeMultiHttp([p1, p2])
        client = fd.FundClient(rate=FakeRate(), http=http)

        async def collect():
            out = []
            async for batch in client.iter_nav_snapshot():
                out.append(batch)
            return out

        batches = run(collect())
        assert len(batches) == 2
        assert [b["page"] for b in batches] == [1, 2]
        assert batches[1]["rows"][0]["code"] == "000001"
        assert "page=1" in http.requests[0]["url"]
        assert "page=2" in http.requests[1]["url"]


class TestCrawlNavSnapshot:
    """增量编排：分页快照 → 基本信息保全型 upsert + 净值批量落库 + run 簿记。"""

    def _run(self, bodies):
        conn = FakeConn()
        store = FundStore()
        http = FakeMultiHttp(bodies)
        client = fd.FundClient(rate=FakeRate(), http=http)
        summary = run(fd.crawl_nav_snapshot(
            client, store, conn, mode="incremental", source="fund",
            task_id=9, trigger="manual"))
        return summary, store, conn

    def test_summary_basic_and_nav_upserts(self):
        p1 = SNAP_PAGE1.replace('pages:"1"', 'pages:"2"')
        p2 = ('var db={datas:[["000001","华夏成长混合","HXCZHH","1.019",'
              '"3.592","1.046","3.619","-0.027","-2.58","开放申购","开放赎回"]],'
              'record:"4",pages:"2",curpage:"2",'
              'showday:["2026-10-28","2026-10-27"]};')
        summary, store, conn = self._run([p1, p2])
        assert summary["total"] == 3 and summary["ok"] == 3
        assert summary["failed"] == 0 and summary["bars"] == 3
        # fund_basic：快照码保全型 upsert（fund_type 不给 → COALESCE 保旧）
        basic_calls = [c for c in conn.cursor_obj.executemany_calls
                       if "fund_basic" in c[0]]
        assert basic_calls, "应 upsert fund_basic"
        codes = {r[0] for call in basic_calls for r in call[1]}
        assert codes == {"007805", "270007", "000001"}
        assert all(r[3] is False for call in basic_calls for r in call[1])
        # fund_nav：批量行落库（code, date, unit, accum, pct, None, source）
        nav_calls = [c for c in conn.cursor_obj.executemany_calls
                     if "fund_nav" in c[0]]
        nav_rows = [r for call in nav_calls for r in call[1]]
        assert ("007805", "2026-10-28", 108.5867, 110.5867, 0.0, None,
                "fund_em") in nav_rows
        assert ("000001", "2026-10-28", 1.019, 3.592, -2.58, None,
                "fund_em") in nav_rows
        # run 簿记：INSERT RETURNING + finish 更新
        assert any("RETURNING id" in q for q in conn.cursor_obj.executed)
        assert any("UPDATE crawl_run" in q for q in conn.cursor_obj.executed)

    def test_failure_finishes_run_and_raises(self):
        summary_or_exc = None
        try:
            self._run([FUND_LIST_JS])          # 第一个响应不是快照 → 解析失败
        except fd.FundError:
            summary_or_exc = "raised"
        assert summary_or_exc == "raised"
