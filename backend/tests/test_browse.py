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
        """source 缺省（空）：不过滤，兼容旧行为，参数不含 source。"""
        body, conn = self._fetch()
        sql, params = conn.cursor_obj.executed[0]
        assert "source = %s" not in sql
        assert params == ("600519", 250, 0)

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
