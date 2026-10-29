"""数据浏览接口测试：全离线（TestClient 下 PG 必然不可达 → 降级路径）+
SQL 行为用 sqlite 不现实，此处验证路由契约与降级形状；SQL 正确性由
真实数据冒烟（NAS 实机）覆盖。"""

import datetime as dt
import sys

import pandas as pd
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from app.main import create_app  # noqa: E402


@pytest.fixture
def client():
    return TestClient(create_app())


class TestStocksRoutes:
    def test_list_down_degrades_gracefully(self, client, monkeypatch):
        """PG 不可达：仍 200，status=down 且带空 stocks（不允许 5xx）。"""
        monkeypatch.setattr(
            "app.config.DATABASE_URL", "postgresql://nobody:x@127.0.0.1:59999/none")
        resp = client.get("/api/stocks")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "down"
        assert body["stocks"] == [] and body["total"] == 0

    def test_list_param_clamping(self, client):
        """limit/offset 越界值被钳制（limit≤200、offset≥0），不抛错。"""
        resp = client.get("/api/stocks", params={"limit": 99999, "offset": -5})
        assert resp.status_code == 200
        body = resp.json()
        assert body["limit"] == 200
        assert body["offset"] == 0

    def test_bars_down_degrades_gracefully(self, client, monkeypatch):
        monkeypatch.setattr(
            "app.config.DATABASE_URL", "postgresql://nobody:x@127.0.0.1:59999/none")
        resp = client.get("/api/stocks/600519/bars")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "down" and body["code"] == "600519"
        assert body["bars"] == []


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.executed: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        self.executed.append((query, params))

    def fetchall(self):
        return self.conn.fetchall_rows

    def fetchone(self):
        return None


class FakeConn:
    """捕获 SQL 与参数，返回可配置行（单股日K 13 列）。"""

    def __init__(self, rows=None):
        self.fetchall_rows = rows or []
        self.cursor_obj = FakeCursor(self)

    def cursor(self):
        return self.cursor_obj

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


BAR_ROW = (dt.date(2026, 10, 9), 9.15, 9.18, 9.25, 9.09, 805097, 739740988.0,
           1.75, 0.22, 0.02, 0.24, "sina", 3.33e10)


class TestBarsSourceFilter:
    """详情页来源过滤：bars 接口按源取数（同一天不再混返两源）。"""

    def _fetch(self, **kw):
        import app.browse as browse_mod
        conn = FakeConn([BAR_ROW])
        monkey_kw = dict(conn=conn)

        def fake_connect(*a, **k):
            return conn

        orig_connect = browse_mod.psycopg.connect
        browse_mod.psycopg.connect = fake_connect
        try:
            return browse_mod.fetch_stock_bars(
                "postgresql://fake", "600519", **kw), conn
        finally:
            browse_mod.psycopg.connect = orig_connect

    def test_source_filter_appended(self):
        """source 非空：SQL 追加 source 过滤，参数顺序正确。"""
        body, conn = self._fetch(source="sina")
        sql, params = conn.cursor_obj.executed[0]
        assert "source = %s" in sql
        assert params == ("600519", "sina", 250, 0)
        assert body["bars"][0]["outstanding_share"] == 3.33e10
        assert body["bars"][0]["source"] == "sina"

    def test_no_source_keeps_legacy_no_filter(self):
        """source 缺省（空）：不过滤，兼容旧行为，参数不含 source。
        回归：无 source 填充曾是裸 `true`（拼出 `WHERE code = %s true`
        语法错误，FakeConn 不校验语法故单测长期假阳性）。"""
        body, conn = self._fetch()
        sql, params = conn.cursor_obj.executed[0]
        assert "source = %s" not in sql
        assert params == ("600519", 250, 0)
        assert "= %s true" not in sql.replace("AND true", "")  # 无裸 true 碎片
        assert "AND true" in sql                               # 合法占位

    def test_rows_include_outstanding_share(self):
        """SELECT 补 outstanding_share（新浪专有），行 dict 含该键。"""
        body, _ = self._fetch(limit=1)
        assert body["count"] == 1
        assert set(body["bars"][0].keys()) >= {
            "date", "open", "close", "high", "low", "volume", "amount",
            "amplitude", "pct_chg", "change", "turnover", "source",
            "outstanding_share"}

    def test_source_with_pagination_params(self):
        """source + offset 组合（详情表明细分页）：参数序正确。"""
        _, conn = self._fetch(source="eastmoney", limit=50, offset=50)
        _, params = conn.cursor_obj.executed[0]
        assert params == ("600519", "eastmoney", 50, 50)


class TestBarsEndpointSource:
    def test_endpoint_passes_source(self, client, monkeypatch):
        """API 端点把 source query 参数透传给查询层。"""
        import app.main as main_mod
        captured = {}

        def fake_bars(dsn, code, *, limit=250, offset=0, source="", adjust=""):
            captured.update(code=code, source=source, limit=limit, offset=offset,
                            adjust=adjust)
            return {"status": "up", "code": code, "count": 0, "bars": []}

        monkeypatch.setattr(main_mod, "fetch_stock_bars", fake_bars)
        resp = client.get("/api/stocks/600519/bars",
                          params={"source": "sina", "limit": 50, "offset": 50})
        assert resp.status_code == 200
        assert captured["code"] == "600519"
        assert captured["source"] == "sina"
        assert captured["adjust"] == ""          # 未传 adjust：透传空串（不过滤口径）
        assert captured["limit"] == 50 and captured["offset"] == 50

    def test_endpoint_source_default_empty(self, client, monkeypatch):
        """不传 source：端点透传空串（不过滤，兼容旧行为）。"""
        import app.main as main_mod
        captured = {}

        def fake_bars(dsn, code, *, limit=250, offset=0, source="", adjust=""):
            captured["source"] = source
            return {"status": "up", "code": code, "count": 0, "bars": []}

        monkeypatch.setattr(main_mod, "fetch_stock_bars", fake_bars)
        client.get("/api/stocks/600519/bars")
        assert captured["source"] == ""


class QueueConn:
    """按 execute 调用序弹出预置结果的假连接（fetch_stock_list 三段式：
    清单分页 fetchall → 当页聚合 fetchall → count fetchone）。"""

    def __init__(self, fetchall_queue=(), fetchone_queue=()):
        self.fetchall_queue = list(fetchall_queue)
        self.fetchone_queue = list(fetchone_queue)
        self.executed = []

    def cursor(self):
        conn = self

        class _Cur:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def execute(self, query, params=None):
                conn.executed.append((query, params))

            def fetchall(self):
                return conn.fetchall_queue.pop(0)

            def fetchone(self):
                return conn.fetchone_queue.pop(0)

        return _Cur()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestStockListDelisted:
    """数据页列表透出退市标记：stock_basic.delisted → stocks[].delisted。"""

    def _fetch(self, monkeypatch, list_rows, page_ids=None, **kw):
        import app.browse as browse_mod
        page_ids = page_ids if page_ids is not None else [
            (r[0], r[1]) for r in list_rows]
        conn = QueueConn(
            fetchall_queue=[page_ids, list_rows],
            fetchone_queue=[(len(list_rows),)])
        monkeypatch.setattr(browse_mod.psycopg, "connect",
                            lambda *a, **k: conn)
        return browse_mod.fetch_stock_list("postgresql://fake", **kw), conn

    def test_list_sql_selects_delisted(self, monkeypatch):
        """SELECT 必须带 b.delisted（漏列则前端恒显示在市——回归锚点）。"""
        body, conn = self._fetch(monkeypatch, [
            ("600519", "贵州茅台", False, 1500.0, 1.2, dt.date(2026, 10, 9),
             250, dt.date(2001, 8, 27), "sina")])
        list_sql = conn.executed[1][0]
        assert "b.delisted" in list_sql
        assert conn.executed[1][1] == (["600519"],)

    def test_delisted_flag_propagates(self, monkeypatch):
        """True/False 两行：各自 stocks[].delisted 正确，顺序保持 code 序。"""
        body, _ = self._fetch(monkeypatch, [
            ("400001", "退市示例", True, None, None, None,
             5000, dt.date(1995, 1, 3), "sina"),
            ("600519", "贵州茅台", False, 1500.0, 1.2, dt.date(2026, 10, 9),
             250, dt.date(2001, 8, 27), "sina"),
        ])
        assert body["status"] == "up"
        by_code = {s["code"]: s for s in body["stocks"]}
        assert [s["code"] for s in body["stocks"]] == ["400001", "600519"]
        assert by_code["400001"]["delisted"] is True
        assert by_code["600519"]["delisted"] is False

    def test_delisted_row_with_null_summary(self, monkeypatch):
        """退市股行情摘要常为空（close/pct_chg/最新交易日 NULL）：不炸、四舍五入跳过。"""
        body, _ = self._fetch(monkeypatch, [
            ("400001", "退市示例", True, None, None, None,
             5000, dt.date(1995, 1, 3), "sina")])
        s = body["stocks"][0]
        assert s["delisted"] is True
        assert s["close"] is None and s["pct_chg"] is None
        assert s["last_date"] is None
        assert s["first_date"] == "1995-01-03"


class TestStockListDelistedFilter:
    """数据页退市过滤：delisted="" 全部 / "false" 仅在市 / "true" 仅退市。"""

    ROW = ("600519", "贵州茅台", False, 1500.0, 1.2, dt.date(2026, 10, 9),
           250, dt.date(2001, 8, 27), "sina")

    def _fetch(self, monkeypatch, **kw):
        conn = QueueConn(
            fetchall_queue=[[("600519", "贵州茅台")], [self.ROW]],
            fetchone_queue=[(1,)])
        import app.browse as browse_mod
        monkeypatch.setattr(browse_mod.psycopg, "connect",
                            lambda *a, **k: conn)
        body = browse_mod.fetch_stock_list("postgresql://fake", **kw)
        ids_sql, count_sql = conn.executed[0][0], conn.executed[2][0]
        return body, ids_sql, count_sql

    def test_empty_filter_has_no_clause(self, monkeypatch):
        """缺省（空）：不过滤，SQL 无退市谓词（兼容旧行为）。"""
        _, ids_sql, count_sql = self._fetch(monkeypatch, delisted="")
        assert "stock_basic.delisted =" not in ids_sql
        assert "stock_basic.delisted =" not in count_sql

    def test_live_filter_appends_false(self, monkeypatch):
        """delisted="false"：清单与 count 两段 SQL 都加退市谓词。

        谓词必须用全表名 stock_basic.delisted——这两段模板的表无别名，
        曾写 b.delisted 报 42P01 被 _safe 吞成 down（单测断言 SQL 文本
        不执行、属假阳性盲区，由 NAS 实机冒烟暴露）。"""
        _, ids_sql, count_sql = self._fetch(monkeypatch, delisted="false")
        assert "stock_basic.delisted = FALSE" in ids_sql
        assert "stock_basic.delisted = FALSE" in count_sql
        assert "b.delisted" not in ids_sql and "b.delisted" not in count_sql

    def test_delisted_filter_appends_true(self, monkeypatch):
        """delisted="true"：两段 SQL 都加退市谓词。"""
        _, ids_sql, count_sql = self._fetch(monkeypatch, delisted="true")
        assert "stock_basic.delisted = TRUE" in ids_sql
        assert "stock_basic.delisted = TRUE" in count_sql

    def test_garbage_value_treated_as_all(self, monkeypatch):
        """非法值（"yes"/"1"/None 等）：按不过滤处理，不抛错。"""
        for bad in ("yes", "1", "TRUE ", None, "live"):
            _, ids_sql, count_sql = self._fetch(monkeypatch, delisted=bad)
            assert "stock_basic.delisted =" not in ids_sql, bad
            assert "stock_basic.delisted =" not in count_sql, bad

    def test_endpoint_passes_delisted(self, client, monkeypatch):
        """API 端点把 delisted query 参数透传给查询层。"""
        import app.main as main_mod
        captured = {}

        def fake_list(dsn, **kw):
            captured.update(kw)
            return {"status": "up", "total": 0, "limit": 50,
                    "offset": 0, "stocks": []}

        monkeypatch.setattr(main_mod, "fetch_stock_list", fake_list)
        resp = client.get("/api/stocks", params={"delisted": "true"})
        assert resp.status_code == 200
        assert captured["delisted"] == "true"

    def test_endpoint_delisted_default_empty(self, client, monkeypatch):
        """不传 delisted：端点透传空串（不过滤，兼容旧行为）。"""
        import app.main as main_mod
        captured = {}

        def fake_list(dsn, **kw):
            captured.update(kw)
            return {"status": "up", "total": 0, "limit": 50,
                    "offset": 0, "stocks": []}

        monkeypatch.setattr(main_mod, "fetch_stock_list", fake_list)
        client.get("/api/stocks")
        assert captured["delisted"] == ""


class TestStocksBasic:
    """教学页代码下拉的数据源：全清单 code+name（轻量，一次性拉取）。"""

    def test_basic_lists_code_and_name(self, client, monkeypatch):
        import app.main as main_mod

        def fake_basic(dsn):
            return {"status": "up", "total": 2,
                    "stocks": [{"code": "600519", "name": "贵州茅台"},
                               {"code": "000001", "name": "平安银行"}]}

        monkeypatch.setattr(main_mod, "fetch_stock_basic", fake_basic)
        resp = client.get("/api/stocks/basic")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "up"
        assert body["stocks"] == [{"code": "600519", "name": "贵州茅台"},
                                  {"code": "000001", "name": "平安银行"}]

    def test_basic_down_degrades(self, client, monkeypatch):
        import app.main as main_mod

        def boom(*a, **k):
            raise RuntimeError("pg down")

        monkeypatch.setattr(main_mod, "fetch_stock_basic", boom)
        resp = client.get("/api/stocks/basic")
        assert resp.status_code == 200
        assert resp.json()["status"] == "down"
        assert resp.json()["stocks"] == []


class TestBarsAdjust:
    """前复权（教学/详情页复权切换）：因子懒加载 + OHLC 换算 + 衍生字段重算。"""

    ROWS = [
        (dt.date(2026, 1, 2), 100.0, 101.0, 102.0, 98.0,
         1e6, 1e8, 4.0, 1.0, 1.0, 0.5, "sina", 1e10),
        (dt.date(2026, 1, 5), 51.0, 51.5, 52.0, 50.0,
         1e6, 5e7, 4.0, 2.0, 1.0, 0.5, "sina", 1e10),
    ]   # SQL 列序 date,open,close,high,low,vol,amount,amp,pct,chg,turnover,src,osha
        # 第二日除权：close ÷2（假 -50%）

    def _fetch(self, monkeypatch, adjust="qfq", factor_series=None, fail=False):
        import app.browse as browse_mod

        conn = FakeConn(rows=list(self.ROWS))
        monkeypatch.setattr(browse_mod.psycopg, "connect",
                            lambda *a, **k: conn)
        if fail:
            def boom(*a, **k):
                raise RuntimeError("sina down")
            monkeypatch.setattr(browse_mod, "ensure_factors", boom)
        else:
            monkeypatch.setattr(browse_mod, "ensure_factors",
                                lambda *a, **k: {"600519": "ok"})
        monkeypatch.setattr(browse_mod, "load_factors", lambda conn_, code:
                            pd.Series([1.0, 2.0], index=pd.to_datetime(
                                ["2026-01-02", "2026-01-05"])) if not fail
                            else pd.Series(dtype=float))
        return browse_mod.fetch_stock_bars(
            "postgresql://fake", "600519", adjust=adjust)

    def test_qfq_recomputes_derived_fields(self, monkeypatch):
        import pandas as pd

        body = self._fetch(monkeypatch)
        assert body["adjust_applied"] is True
        bars = body["bars"]                    # 倒序输出（最新在上）
        assert bars[0]["close"] == pytest.approx(51.5 * 2)   # 51.5×2=103
        # 除权日假 -50% 修正为真实涨跌：(103-101)/101 ≈ +1.98%
        assert bars[0]["pct_chg"] == pytest.approx(
            (103.0 - 101.0) / 101.0 * 100, abs=0.05)
        assert bars[0]["change"] == pytest.approx(2.0)
        assert bars[1]["close"] == pytest.approx(101.0)      # 基准日因子=1 原值

    def test_qfq_failure_degrades(self, monkeypatch):
        body = self._fetch(monkeypatch, fail=True)
        assert body["adjust_applied"] is False
        assert body["bars"][0]["close"] == pytest.approx(51.5)  # 原值

    def test_no_adjust_keeps_legacy(self, monkeypatch):
        body = self._fetch(monkeypatch, adjust="")
        assert body["adjust_applied"] is False
        assert body["bars"][0]["close"] == pytest.approx(51.5)  # 倒序：最新在上


# ---------------------------------------------------------- 基金列表/净值 ----

# 基金列表第二段查询返回行（code, name, fund_type, unit_nav, pct_chg,
# nav_date, navs, first_date）
FUND_PAGE_ROW = ("000001", "华夏成长混合", "混合型-灵活",
                 1.046012, 2.054, dt.date(2026, 10, 23),
                 6029, dt.date(2001, 12, 18))
# 基金净值明细行（nav_date, unit_nav, accum_nav, pct_chg, unit_money, source）
FUND_NAV_NEW = (dt.date(2026, 10, 23), 1.046, 3.619, 2.05, "0.018", "fund_em")
FUND_NAV_OLD = (dt.date(2026, 10, 22), 1.025, 3.598, -3.67, None, "fund_em")


def _with_fake_conn(monkeypatch, rows):
    """monkeypatch browse.psycopg.connect → 返回 (被调用时透传的 FakeConn)。"""
    import app.browse as browse_mod
    conn = FakeConn(rows)
    monkeypatch.setattr(browse_mod.psycopg, "connect", lambda *a, **k: conn)
    return conn


class TestFundList:
    """基金列表三段式查询（对齐股票列表：fund_basic 小表分页 + 当页聚合
    fund_nav + count——用 QueueConn 按调用序弹出结果）。"""

    def _fetch(self, monkeypatch, **kw):
        import app.browse as browse_mod
        conn = QueueConn(
            fetchall_queue=[[(FUND_PAGE_ROW[0], FUND_PAGE_ROW[1])],
                            [FUND_PAGE_ROW]],
            fetchone_queue=[(1,)])
        monkeypatch.setattr(browse_mod.psycopg, "connect",
                            lambda *a, **k: conn)
        return browse_mod.fetch_fund_list("postgresql://fake", **kw), conn

    def test_two_phase_queries_shape_and_rounding(self, monkeypatch):
        body, conn = self._fetch(monkeypatch, q="华夏")
        ids_sql, ids_params = conn.executed[0]
        page_sql, page_params = conn.executed[1]
        cnt_sql, cnt_params = conn.executed[2]
        assert "FROM fund_basic" in ids_sql and "ILIKE" in ids_sql
        assert ids_params == ("华夏", "%华夏%", "%华夏%", 50, 0)
        assert "fund_nav" in page_sql and "LATERAL" in page_sql
        assert page_params == (["000001"],)       # 只聚合当页代码
        assert "fund_basic" in cnt_sql and "ILIKE" in cnt_sql
        assert cnt_params == ("华夏", "%华夏%", "%华夏%")
        row = body["funds"][0]
        assert row["code"] == "000001"
        assert row["fund_type"] == "混合型-灵活"
        assert row["unit_nav"] == 1.046           # round 4 位
        assert row["pct_chg"] == 2.05             # round 2 位
        assert row["navs"] == 6029
        assert row["last_date"] == "2026-10-23"
        assert row["first_date"] == "2001-12-18"

    def test_default_no_search_params(self, monkeypatch):
        _, conn = self._fetch(monkeypatch)
        ids_params = conn.executed[0][1]
        assert ids_params == ("", "%%", "%%", 50, 0)


class TestFundNavs:
    def test_sql_shape_and_ascending_output(self, monkeypatch):
        """SQL 倒序取页、输出升序（对齐 bars 约定：图升序、表自行倒序）。"""
        conn = _with_fake_conn(monkeypatch, [FUND_NAV_NEW, FUND_NAV_OLD])
        import app.browse as browse_mod
        body = browse_mod.fetch_fund_navs("postgresql://fake", "000001",
                                          limit=50, offset=0)
        sql, params = conn.cursor_obj.executed[0]
        assert "FROM fund_nav" in sql and "source" in sql
        assert params == ("000001", 50, 0)
        assert [n["date"] for n in body["navs"]] == ["2026-10-22", "2026-10-23"]
        nav = body["navs"][-1]
        assert nav["unit_nav"] == 1.046 and nav["accum_nav"] == 3.619
        assert nav["pct_chg"] == 2.05 and nav["unit_money"] == "0.018"
        assert nav["source"] == "fund_em"
        assert body["count"] == 2 and body["code"] == "000001"

    def test_empty_navs(self, monkeypatch):
        _with_fake_conn(monkeypatch, [])
        import app.browse as browse_mod
        body = browse_mod.fetch_fund_navs("postgresql://fake", "000001")
        assert body["status"] == "up" and body["navs"] == []


class TestFundsRoutes:
    def test_list_down_degrades_gracefully(self, client, monkeypatch):
        """PG 不可达：仍 200，status=down 空列表（同 /api/stocks 降级约定）。"""
        monkeypatch.setattr(
            "app.config.DATABASE_URL", "postgresql://nobody:x@127.0.0.1:59999/none")
        resp = client.get("/api/funds")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "down" and body["funds"] == [] and body["total"] == 0

    def test_list_param_clamping(self, client):
        resp = client.get("/api/funds", params={"limit": 99999, "offset": -5})
        assert resp.status_code == 200
        body = resp.json()
        assert body["limit"] == 200 and body["offset"] == 0

    def test_navs_down_degrades_gracefully(self, client, monkeypatch):
        monkeypatch.setattr(
            "app.config.DATABASE_URL", "postgresql://nobody:x@127.0.0.1:59999/none")
        resp = client.get("/api/funds/000001/navs")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "down" and body["code"] == "000001"
        assert body["navs"] == []


class TestFundListDelistedFilter:
    """基金列表退市过滤：对齐股票页 delisted 三态（'' 全部/false 在市/true 退市）。"""

    ROW = ("000001", "华夏成长混合", "混合型-灵活", 1.046, 2.05,
           dt.date(2026, 10, 23), 6029, dt.date(2001, 12, 18))

    def _fetch(self, monkeypatch, delisted):
        import app.browse as browse_mod
        conn = QueueConn(
            fetchall_queue=[[(self.ROW[0], self.ROW[1])], [self.ROW]],
            fetchone_queue=[(1,)])
        monkeypatch.setattr(browse_mod.psycopg, "connect",
                            lambda *a, **k: conn)
        return browse_mod.fetch_fund_list("postgresql://fake",
                                          delisted=delisted), conn

    def test_clause_appended_when_true(self, monkeypatch):
        body, conn = self._fetch(monkeypatch, "true")
        ids_sql = conn.executed[0][0]
        cnt_sql = conn.executed[2][0]
        assert "fund_basic.delisted = TRUE" in ids_sql
        assert "fund_basic.delisted = TRUE" in cnt_sql
        assert body["status"] == "up"

    def test_clause_false_and_default(self, monkeypatch):
        _, conn = self._fetch(monkeypatch, "false")
        assert "fund_basic.delisted = FALSE" in conn.executed[0][0]
        _, conn2 = self._fetch(monkeypatch, "")
        assert "delisted = " not in conn2.executed[0][0]

    def test_invalid_value_treated_as_all(self, monkeypatch):
        _, conn = self._fetch(monkeypatch, "yes")
        assert "delisted = " not in conn.executed[0][0]


class TestFundBackendFilter:
    """基金列表后端收费份额过滤：默认隐藏（名称含"后端"），show_backend=1 显示。"""

    def test_backend_hidden_by_default(self, monkeypatch):
        import app.browse as browse_mod
        conn = QueueConn(
            fetchall_queue=[[(TestFundListDelistedFilter.ROW[0],
                              TestFundListDelistedFilter.ROW[1])],
                            [TestFundListDelistedFilter.ROW]],
            fetchone_queue=[(1,)])
        monkeypatch.setattr(browse_mod.psycopg, "connect",
                            lambda *a, **k: conn)
        body = browse_mod.fetch_fund_list("postgresql://fake")
        ids_sql = conn.executed[0][0]
        cnt_sql = conn.executed[2][0]
        assert "position('后端' in fund_basic.name) = 0" in ids_sql
        assert "position('后端' in fund_basic.name) = 0" in cnt_sql
        assert body["status"] == "up"

    def test_show_backend_removes_clause(self, monkeypatch):
        import app.browse as browse_mod
        conn = QueueConn(
            fetchall_queue=[[(TestFundListDelistedFilter.ROW[0],
                              TestFundListDelistedFilter.ROW[1])],
                            [TestFundListDelistedFilter.ROW]],
            fetchone_queue=[(1,)])
        monkeypatch.setattr(browse_mod.psycopg, "connect",
                            lambda *a, **k: conn)
        browse_mod.fetch_fund_list("postgresql://fake", show_backend="1")
        assert "position('后端'" not in conn.executed[0][0]
        assert "position('后端'" not in conn.executed[2][0]
        assert "NOT LIKE" not in conn.executed[0][0]  # 内联片段禁裸 %（psycopg3 格式化坑）

    def test_endpoint_passes_show_backend(self, client, monkeypatch):
        import app.main as main_mod
        captured = {}

        def fake_funds(dsn, *, q="", limit=50, offset=0, delisted="",
                       show_backend="", show_money=""):
            captured["show_backend"] = show_backend
            return {"status": "up", "total": 0, "limit": limit,
                    "offset": offset, "funds": []}

        monkeypatch.setattr(main_mod, "fetch_fund_list", fake_funds)
        client.get("/api/funds", params={"show_backend": "1"})
        assert captured["show_backend"] == "1"
        client.get("/api/funds")
        assert captured["show_backend"] == ""


class TestFundMoneyFilter:
    """基金列表货币基金过滤：默认隐藏（fund_type 含"货币"），show_money=1 显示。"""

    def _conn(self, monkeypatch):
        import app.browse as browse_mod
        conn = QueueConn(
            fetchall_queue=[[(TestFundListDelistedFilter.ROW[0],
                              TestFundListDelistedFilter.ROW[1])],
                            [TestFundListDelistedFilter.ROW]],
            fetchone_queue=[(1,)])
        monkeypatch.setattr(browse_mod.psycopg, "connect",
                            lambda *a, **k: conn)
        return conn

    def test_money_hidden_by_default(self, monkeypatch):
        import app.browse as browse_mod
        conn = self._conn(monkeypatch)
        body = browse_mod.fetch_fund_list("postgresql://fake")
        clause = "COALESCE(position('货币' in fund_basic.fund_type), 0) = 0"
        assert clause in conn.executed[0][0]        # NULL 类型不受影响
        assert clause in conn.executed[2][0]
        assert body["status"] == "up"

    def test_show_money_removes_clause(self, monkeypatch):
        import app.browse as browse_mod
        conn = self._conn(monkeypatch)
        browse_mod.fetch_fund_list("postgresql://fake", show_money="1")
        assert "position('货币'" not in conn.executed[0][0]
        assert "position('货币'" not in conn.executed[2][0]

    def test_endpoint_passes_show_money(self, client, monkeypatch):
        import app.main as main_mod
        captured = {}

        def fake_funds(dsn, *, q="", limit=50, offset=0, delisted="",
                       show_backend="", show_money=""):
            captured["show_money"] = show_money
            return {"status": "up", "total": 0, "limit": limit,
                    "offset": offset, "funds": []}

        monkeypatch.setattr(main_mod, "fetch_fund_list", fake_funds)
        client.get("/api/funds", params={"show_money": "1"})
        assert captured["show_money"] == "1"
        client.get("/api/funds")
        assert captured["show_money"] == ""
