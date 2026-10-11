"""回测数据读取与 API 契约测试（离线：假连接 + monkeypatch 引擎）。

engine 的 vectorbt 撮合正确性由 test_backtest_engine.py 用合成数据覆盖。
"""

import sys
from pathlib import Path

import pandas as pd

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import create_app  # noqa: E402


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
        return self.conn.rows


class FakeConn:
    def __init__(self, rows):
        self.rows = rows
        self.cursor_obj = FakeCursor(self)

    def cursor(self):
        return self.cursor_obj

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestDataLayer:
    def test_load_history_shapes_and_sql(self, monkeypatch):
        """load_history：升序取数 + IN 子句 + 透视成 OHLC 宽表（列=代码）。"""
        import datetime as dt

        from app.backtest import data as bdata

        rows = [
            (dt.date(2026, 1, 2), "600519", 9.0, 9.6, 8.9, 9.5, 1_000),
            (dt.date(2026, 1, 2), "000001", 11.0, 11.3, 10.9, 11.2, 2_000),
            (dt.date(2026, 1, 5), "600519", 9.5, 9.9, 9.4, 9.8, 1_100),
        ]
        conn = FakeConn(rows)
        monkeypatch.setattr(bdata.psycopg, "connect", lambda *a, **k: conn)

        out = bdata.load_history("postgresql://fake", ["600519", "000001"],
                                 start="2026-01-01")
        sql, params = conn.cursor_obj.executed[0]
        assert "ORDER BY trade_date" in sql
        assert "2026-01-01" in str(params)
        assert {"600519", "000001"} <= set(out["close"].columns)
        assert out["close"].loc["2026-01-02", "600519"] == 9.5
        assert out["close"].loc["2026-01-05", "600519"] == 9.8
        # 000001 缺 01-05 → 透视后 NaN（各标的长度不齐由引擎容忍）
        import pandas as pd
        assert pd.isna(out["close"].loc["2026-01-05", "000001"])

    def test_pg_down_degrades(self, monkeypatch):
        from app.backtest import data as bdata

        def boom(*a, **k):
            raise RuntimeError("pg down")

        monkeypatch.setattr(bdata.psycopg, "connect", boom)
        with pytest.raises(RuntimeError):
            bdata.load_history("postgresql://fake", ["600519"], start="2026-01-01")


class TestBacktestApi:
    def _client(self, monkeypatch, history=None, result=None):
        from app import backtest_api as bapi

        hist = history or {"close": None, "open": None, "high": None, "low": None,
                           "volume": None}
        # backtest_api 以 from-import 绑定依赖——须 patch 模块属性；
        # API 测试只测契约（校验/降级/形状），runner 内部由引擎测试覆盖
        monkeypatch.setattr(bapi, "load_history", lambda *a, **k: hist)
        monkeypatch.setattr(bapi, "run_strategy", lambda *a, **k: result or {
            "metrics": {"total_return": 0.1},
            "equity": [], "drawdown": [], "trades": [], "gap_dates": []})
        app = create_app()
        return TestClient(app)

    def test_validates_strategy(self, monkeypatch):
        client = self._client(monkeypatch)
        resp = client.post("/api/backtest", json={
            "strategy": "no-such", "codes": ["600519"], "range": "3y"})
        assert resp.status_code == 422

    def test_validates_empty_codes(self, monkeypatch):
        client = self._client(monkeypatch)
        resp = client.post("/api/backtest", json={
            "strategy": "ma_cross", "codes": [], "range": "3y"})
        assert resp.status_code == 422

    def test_validates_momentum_needs_pool(self, monkeypatch):
        """轮动策略少于 2 只标的 → 400（横截面无意义）。"""
        client = self._client(monkeypatch)
        resp = client.post("/api/backtest", json={
            "strategy": "momentum", "codes": ["600519"], "range": "3y"})
        assert resp.status_code == 400

    def test_ok_response_shape(self, monkeypatch):
        result = {"metrics": {"total_return": 0.26, "annual_return": 0.26,
                              "max_drawdown": 0.1, "sharpe": 1.5,
                              "win_rate": 0.5, "profit_loss_ratio": 2.0,
                              "trades": 4},
                  "equity": [["2026-01-02", 1.0, 1.0]],
                  "drawdown": [["2026-01-02", 0.0]],
                  "trades": [{"entry_date": "2026-01-02", "exit_date": "2026-01-05",
                              "entry_price": 9.0, "exit_price": 9.5,
                              "pnl_pct": 0.05, "direction": "long"}],
                  "gap_dates": []}
        client = self._client(monkeypatch, result=result)
        resp = client.post("/api/backtest", json={
            "strategy": "ma_cross", "codes": ["600519"], "range": "1y",
            "params": {"fast": 5, "slow": 20},
            "exec_mode": "next_open", "commission": 2.5e-4, "sell_tax": 5e-4})
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "up"
        assert body["metrics"]["total_return"] == pytest.approx(0.26)
        assert body["equity"] and body["trades"]

    def test_pg_down_degrades_to_200(self, monkeypatch):
        """PG 不可达：200 + status=down（同 /api/infra 降级约定）。"""
        from app import backtest_api as bapi

        def boom(*a, **k):
            raise RuntimeError("pg down")

        monkeypatch.setattr(bapi, "load_history", boom)
        client = TestClient(create_app())
        resp = client.post("/api/backtest", json={
            "strategy": "ma_cross", "codes": ["600519"], "range": "3y"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "down"

    def test_empty_history_message(self, monkeypatch):
        """区间内无数据 → status=up + message（空态由前端渲染）。"""
        empty = {"close": None, "open": None, "high": None, "low": None,
                 "volume": None}
        client = self._client(monkeypatch, history=empty, result={
            "metrics": {}, "equity": [], "drawdown": [], "trades": [],
            "gap_dates": [], "message": "区间内无行情数据"})
        resp = client.post("/api/backtest", json={
            "strategy": "donchian", "codes": ["600519"], "range": "1y"})
        body = resp.json()
        assert body["status"] == "up"
        assert "无行情数据" in body.get("message", "")


class TestBacktestAdjust:
    def _hist(self):
        idx = pd.to_datetime(["2026-01-01", "2026-01-02"])
        close = pd.DataFrame({"600519": [10.0, 10.5]}, index=idx)
        hist = {"close": close}
        hist.update({k: close.copy() for k in ("open", "high", "low")})
        hist["volume"] = close * 1e6
        return hist

    def test_adjust_applied_true(self, monkeypatch):
        from app import backtest_api as bapi
        monkeypatch.setattr(bapi, "load_history", lambda *a, **k: self._hist())
        # 接缝：复权懒加载已提取为 adjust.ensure_and_apply（单股/全量共用）
        factors = pd.Series([3.0], index=pd.to_datetime(["2026-01-02"]))
        monkeypatch.setattr(
            bapi, "ensure_and_apply",
            lambda bars, codes, dsn: (bars, {"600519": factors}))

        captured = {}
        result_run = {"metrics": {"total_return": 0.26}, "equity": [], "drawdown": [],
                      "trades": [], "gap_dates": []}

        def fake_run(bars, **kw):
            captured["factors"] = kw.get("factors")
            return result_run

        monkeypatch.setattr(bapi, "run_strategy", fake_run)
        client = TestClient(create_app())
        resp = client.post("/api/backtest", json={
            "strategy": "ma_cross", "codes": ["600519"], "range": "1y"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["adjust_applied"] is True
        assert captured["factors"] is not None

    def test_adjust_missing_degrades(self, monkeypatch):
        from app import backtest_api as bapi
        seen = {}

        def fake_run(bars, **kw):
            seen["first_close"] = bars["close"].iloc[0, 0]
            return {"metrics": {"total_return": 0.0}, "equity": [], "drawdown": [],
                    "trades": [], "gap_dates": []}

        monkeypatch.setattr(bapi, "load_history", lambda *a, **k: self._hist())
        monkeypatch.setattr(
            bapi, "ensure_and_apply",
            lambda bars, codes, dsn: (bars, None))   # 因子不可用降级不复权
        monkeypatch.setattr(bapi, "run_strategy", fake_run)
        client = TestClient(create_app())
        resp = client.post("/api/backtest", json={
            "strategy": "ma_cross", "codes": ["600519"], "range": "1y"})
        body = resp.json()
        assert body["adjust_applied"] is False
        assert seen["first_close"] == 10.0
