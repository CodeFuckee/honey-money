"""数据浏览接口测试：全离线（TestClient 下 PG 必然不可达 → 降级路径）+
SQL 行为用 sqlite 不现实，此处验证路由契约与降级形状；SQL 正确性由
真实数据冒烟（NAS 实机）覆盖。"""

import datetime as dt
import sys
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

        def fake_bars(dsn, code, *, limit=250, offset=0, source=""):
            captured.update(code=code, source=source, limit=limit, offset=offset)
            return {"status": "up", "code": code, "count": 0, "bars": []}

        monkeypatch.setattr(main_mod, "fetch_stock_bars", fake_bars)
        resp = client.get("/api/stocks/600519/bars",
                          params={"source": "sina", "limit": 50, "offset": 50})
        assert resp.status_code == 200
        assert captured == {"code": "600519", "source": "sina",
                            "limit": 50, "offset": 50}

    def test_endpoint_source_default_empty(self, client, monkeypatch):
        """不传 source：端点透传空串（不过滤，兼容旧行为）。"""
        import app.main as main_mod
        captured = {}

        def fake_bars(dsn, code, *, limit=250, offset=0, source=""):
            captured["source"] = source
            return {"status": "up", "code": code, "count": 0, "bars": []}

        monkeypatch.setattr(main_mod, "fetch_stock_bars", fake_bars)
        client.get("/api/stocks/600519/bars")
        assert captured["source"] == ""
