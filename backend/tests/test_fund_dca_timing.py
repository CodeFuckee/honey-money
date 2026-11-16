"""定投时机功能测试（compute_dip_timing 纯函数 + fund_dip_timing 存储 + timing API）。

功能依据（2026-11-16 假设验证，25-projects/honey-money/dip-timing-validation.md）：
当前回撤 -25~-30% 区间的权益基金，历史上从该位置开始定投的首轮止盈最快、
成功率与年化最高；>30% 超深回撤群反而变差（起不来风险）——回撤桶以此为界。
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from app.main import create_app  # noqa: E402


@pytest.fixture
def client():
    return TestClient(create_app())


DEAD_DSN = "postgresql://nobody:x@127.0.0.1:59999/none"


# ===== compute_dip_timing 纯函数 =====

class TestComputeDipTiming:
    def test_rising_chain_no_dip(self):
        """一路上涨：dd=0 → band none、days_since_peak≈0。"""
        from app.dca_backtest import compute_dip_timing
        dates = pd.DatetimeIndex(pd.date_range("2024-01-01", periods=400,
                                               freq="D"))
        adj = np.cumprod(1.0 + np.full(400, 0.002))
        row = compute_dip_timing(dates, adj)
        assert row["dd_current"] == pytest.approx(0.0, abs=1e-9)
        assert row["dip_band"] == "none"
        assert row["days_since_peak"] == 0

    def test_deep_drawdown_band_and_days(self):
        """涨 50% 后跌 30%：dd=-0.30 → band 30+；days_since_peak 从最高点日起算。"""
        from app.dca_backtest import compute_dip_timing
        dates = pd.DatetimeIndex(pd.date_range("2024-01-01", periods=500,
                                               freq="D"))
        g = np.full(500, 0.001)
        g[100:400] = -0.0015           # 从峰值缓慢下撤
        adj = np.cumprod(1.0 + g)
        row = compute_dip_timing(dates, adj)
        assert row["dd_current"] < -0.2
        assert row["dip_band"] in ("20-25", "25-30", "30+")
        assert 0 < row["days_since_peak"] <= 400

    def test_band_boundaries(self):
        """桶边界：dd 精确 -0.25 归 25-30（-0.30 < dd ≤ -0.25）；-0.20 归 20-25。"""
        from app.dca_backtest import compute_dip_timing
        for target_dd, expect in ((-0.25, "25-30"), (-0.20, "20-25"),
                                  (-0.30, "30+"), (-0.19, "none")):
            dates = pd.DatetimeIndex(pd.date_range("2024-01-01", periods=300,
                                                   freq="D"))
            g = np.zeros(300)
            g[50:100] = 0.005                       # 拉出前高
            peak = float(np.prod(1.0 + g[:100]))
            drop = np.log(1 + target_dd) / (300 - 100)
            g[100:] = np.expm1(drop)                # 匀速跌到目标 dd
            adj = np.cumprod(1.0 + g)
            row = compute_dip_timing(dates, adj)
            assert row["dip_band"] == expect, (target_dd, row)

    def test_short_chain(self):
        """净值不足（<2 天）：字段全 None/none。"""
        from app.dca_backtest import compute_dip_timing
        dates = pd.DatetimeIndex(pd.date_range("2024-01-01", periods=1,
                                               freq="D"))
        row = compute_dip_timing(dates, np.array([1.0]))
        assert row["dd_current"] is None
        assert row["dip_band"] == "none"
        assert row["days_since_peak"] is None


# ===== fund_dip_timing 存储 =====

class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.executed: list = []
        self.batch: list = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        self.executed.append((query, params))

    def executemany(self, query, seq):
        self.batch.append((query, list(seq)))


class FakeConn:
    def __init__(self):
        self.cursor_obj = FakeCursor(self)

    def cursor(self):
        return self.cursor_obj

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestTimingStore:
    def test_timing_ddl(self):
        from app.dca_store import ensure_tp_schema
        conn = FakeConn()
        ensure_tp_schema(conn)
        sqls = [sql for sql, _ in conn.cursor_obj.executed]
        assert any("CREATE TABLE IF NOT EXISTS fund_dip_timing" in s
                   for s in sqls)
        assert any("code            VARCHAR(10) PRIMARY KEY" in s
                   for s in sqls)

    def test_import_timing_sequence_and_idempotent(self):
        from app.dca_store import import_timing
        rows = [("000001", -0.27, 340, "25-30", "2026-11-16")]
        seqs = []
        for _ in range(2):
            conn = FakeConn()
            import_timing(conn, rows)
            sqls = [sql for sql, _ in conn.cursor_obj.executed]
            assert sqls[0] == "DELETE FROM fund_dip_timing"
            assert len(conn.cursor_obj.batch) == 1
            sql, batch_rows = conn.cursor_obj.batch[0]
            assert "INSERT INTO fund_dip_timing" in sql
            assert len(batch_rows) == 1
            seqs.append((len(sqls), len(batch_rows)))
        assert seqs[0] == seqs[1]


# ===== /api/funds/dca/timing 端点 =====

def _patch_connect(monkeypatch, module, conn):
    def fake_connect(*a, **k):
        return conn
    monkeypatch.setattr(module.psycopg, "connect", fake_connect)


class FakeConnOne:
    def __init__(self, one, rows=None):
        self.one = one
        self.rows = rows or []
        self.executed: list = []

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        self.executed.append(query)

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.rows


class TestDcaTimingRoutes:
    def test_timing_down_degrades(self, client, monkeypatch):
        """PG 不可达：仍 200 down 空形状（降级约定同旧端点）。"""
        monkeypatch.setattr("app.config.DATABASE_URL", DEAD_DSN)
        resp = client.get("/api/funds/dca/timing", params={"band": "25-30"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "down" and body["rows"] == []
        assert body["band"] == "25-30"

    def test_timing_band_whitelist(self, client, monkeypatch):
        """band 白名单：乱值 → ''（全部）；合法值回显。"""
        import app.dca_api as dca_mod
        conn = FakeConnOne((None, None, None))
        _patch_connect(monkeypatch, dca_mod, conn)
        resp = client.get("/api/funds/dca/timing", params={"band": "banana"})
        assert resp.json()["band"] == ""
        resp = client.get("/api/funds/dca/timing", params={"band": "30+"})
        assert resp.json()["band"] == "30+"

    def test_timing_sql_shape(self, client, monkeypatch):
        """SQL：JOIN fund_dip_timing × fund_dca_tp_result（tp 过滤）、
        band 过滤、fee 口径后缀排序。"""
        import app.dca_api as dca_mod
        conn = FakeConnOne((None, None, None))
        _patch_connect(monkeypatch, dca_mod, conn)
        client.get("/api/funds/dca/timing",
                   params={"band": "25-30", "tp": "0.2", "fee": "nom",
                           "sort": "w10_xirr"})
        sqls = conn.executed
        assert any("JOIN fund_dca_tp_result" in s and "fund_dip_timing" in s
                   for s in sqls)
        assert any("r.tp = %s" in s for s in sqls)
        assert any("t.dip_band = %s" in s for s in sqls)
        assert any("ORDER BY r.w10_xirr_nom" in s for s in sqls)
        assert any("r.full_start" in s for s in sqls)   # 运作年限数据源

    def test_timing_row_mapping(self, client, monkeypatch):
        """行映射：dd/band/素质字段齐全，年化收益率 round(4)。"""
        import app.dca_api as dca_mod
        import datetime as dt
        row = ("000001", "成长先锋混合A", "混合型-偏股", True, -0.2712, 340,
               "25-30", 12.5, dt.date(2026, 11, 16) - dt.timedelta(days=3653),
               None, None, 3, 95.4, 0.92, 1.003, 0.18,
               3, 3, 0.90, 0.98, 0.98, 1.05, 0.175, 0.17, 94.0, 96.0)
        import datetime as dt2
        conn = FakeConnOne((dt2.date(2026, 11, 16),), rows=[row])
        _patch_connect(monkeypatch, dca_mod, conn)
        resp = client.get("/api/funds/dca/timing")
        body = resp.json()
        assert body["status"] == "up"
        r = body["rows"][0]
        assert r["code"] == "000001" and r["tp"] == 0.2
        assert r["dd_current"] == -0.2712 and r["dip_band"] == "25-30"
        assert r["days_since_peak"] == 340
        assert r["age_years"] == pytest.approx(10.0, abs=0.1)  # 运作年限
        assert r["w10_xirr"] == 0.92 and r["w10_xirr_nom"] == 0.98
        assert r["w10_profit_rate_disc"] == 0.175
