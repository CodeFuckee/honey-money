"""熊市断缴实验测试（bear_pause 引擎 + fund_bear_pause_* 三表存储 +
/api/funds/dca/pause-experiment 与 /api/funds/dca/bear-pause/{code}）。

引擎口径与 scratch/bear_pause_experiment.py（2026-11-19 全市场 9,576 只
实验）逐字段一致：两个校验锚（A ≡ simulate_multi；永不入熊 B/C/D ≡ A）
转正为单测；断缴语义（囤现金/恢复 flush/加倍）用手造净值曲线断言。
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import create_app  # noqa: E402


@pytest.fixture
def client():
    return TestClient(create_app())


DEAD_DSN = "postgresql://nobody:x@127.0.0.1:59999/none"


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


def _nav(growth, start="2015-01-05"):
    """手造净值：bdays 日历 + 增长率链乘复权净值。"""
    dates = pd.DatetimeIndex(pd.bdate_range(start, periods=len(growth)))
    return dates, np.cumprod(1.0 + np.asarray(growth, dtype=float))


def _scenario_nav():
    """一年半场景：先涨后深跌（触发熊市态）→ 横盘 → 恢复（越过 -10%）。"""
    g = ([0.003] * 120 + [-0.005] * 100 + [0.0] * 60 + [0.004] * 120)
    return _nav(g)


class TestBearPauseEngine:
    def test_anchor_a_matches_engine(self):
        """锚①：A 组与 dca_backtest.simulate_multi(tp=0.20, gross) 逐字段一致。"""
        from app.bear_pause import run_fund
        from app.dca_backtest import simulate_multi

        dates, adj = _scenario_nav()
        res = simulate_multi(dates, adj, None, [0.20])[0.20]
        row = run_fund(dates, adj)
        assert row["tp_a_rounds"] == res["rounds"]
        assert row["tp_a_out"] == pytest.approx(res["invested"], rel=1e-9)
        assert row["tp_a_wallet"] == pytest.approx(res["final_value"],
                                                   rel=1e-9)
        assert (row["tp_a_xirr"] is None) == (res["xirr"] is None)
        if res["xirr"] is not None:
            assert row["tp_a_xirr"] == pytest.approx(res["xirr"], abs=1e-6)

    def test_anchor_no_pause_arms_equal_a(self):
        """锚②：入线阈值 1.01（永不入熊）时 B/C/D 必须退化为 A。"""
        from app.bear_pause import bear_state, simulate_arm

        dates, adj = _scenario_nav()
        n = len(adj)
        mo = dates.to_numpy().astype("datetime64[M]").astype(np.int64)
        newm = np.empty(n, dtype=bool)
        newm[0] = True
        newm[1:] = mo[1:] != mo[:-1]
        a = simulate_arm(np.zeros(n, dtype=bool), set(), newm, adj, dates,
                         0.20, "A")
        bear2, trans2, ep2 = bear_state(np.full(n, -1.0), 1.01, 0.0)
        assert ep2 == 0 and not bear2.any()
        for arm in ("B", "C", "D"):
            r = simulate_arm(bear2, set(trans2), newm, adj, dates, 0.20, arm)
            assert r["rounds"] == a["rounds"]
            assert r["out"] == pytest.approx(a["out"], rel=1e-9)
            assert r["wallet"] == pytest.approx(a["wallet"], rel=1e-9)
            assert (r["xirr"] is None) == (a["xirr"] is None)
            if a["xirr"] is not None:
                assert r["xirr"] == pytest.approx(a["xirr"], abs=1e-6)

    def test_pause_semantics(self):
        """断缴语义：B 熊市月不买+囤现金、C 恢复日全量 flush、D 熊市月投 2×。"""
        from app.bear_pause import bear_state, simulate_arm

        dates, adj = _scenario_nav()
        n = len(adj)
        cummax = np.maximum.accumulate(adj)
        dd = adj / cummax - 1.0
        bear, trans, episodes = bear_state(dd, 0.20, 0.10)
        assert episodes == 1 and trans           # 一进一出（有恢复转出日）
        mo = dates.to_numpy().astype("datetime64[M]").astype(np.int64)
        newm = np.empty(n, dtype=bool)
        newm[0] = True
        newm[1:] = mo[1:] != mo[:-1]
        pause_starts = int(np.sum(newm & bear))   # 熊市态中的月投日数
        assert pause_starts > 0
        base = dict(newm=newm, adj=adj, dates=dates, tp=0.20)
        a = simulate_arm(np.zeros(n, dtype=bool), set(), **base, mode="A")
        b = simulate_arm(bear, set(trans), **base, mode="B")
        c = simulate_arm(bear, set(trans), **base, mode="C")
        d = simulate_arm(bear, set(trans), **base, mode="D")
        assert b["pause_months"] == pause_starts
        assert b["out"] == pytest.approx(a["out"])            # 出钱包同额
        assert b["idle_cash"] == pytest.approx(pause_starts * 1000.0)
        assert c["pause_months"] == pause_starts
        assert c["flushed"] == pytest.approx(pause_starts * 1000.0)
        assert c["idle_cash"] == 0.0
        assert d["out"] == pytest.approx(a["out"] + pause_starts * 1000.0)

    def test_run_fund_flat_keys(self):
        """run_fund 产出扁平 dict（键与 fund_bear_pause_fund 列一致）。"""
        from app.bear_pause import run_fund

        dates, adj = _scenario_nav()
        row = run_fund(dates, adj)
        expect = {"span_days", "months", "ever_paused", "episodes",
                  "pause_months", "dd_end", "dd_min"}
        for p in ("tp", "nt"):
            for a in ("a", "b", "c", "d"):
                expect |= {f"{p}_{a}_{k}" for k in
                           ("wallet", "xirr", "out", "rounds")}
        assert expect | {"code", "in_universe"} \
            == set(row) | {"code", "in_universe"}
        assert row["ever_paused"] is True and row["episodes"] == 1
        assert row["nt_a_rounds"] == 0 and row["tp_a_rounds"] > 0

    def test_aggregate_market_shape(self):
        """聚合行 = 2 口径 × 非空子样本（空跳过）；分层按 dd_end 归桶；
        D 钱包差按多投入折算（同额可比）。"""
        from app.bear_pause import aggregate_market, run_fund

        dates, adj = _scenario_nav()
        funds = [
            run_fund(dates, adj),                        # 断缴后恢复 → near_high
            run_fund(*_nav([0.001] * 300)),              # 单边上涨 → near_high
            run_fund(*_nav([0.002] * 100 + [-0.006] * 80 + [0.0] * 60)),
            run_fund(*_nav([0.003] * 120 + [-0.005] * 100 + [0.004] * 90)),
        ]
        for i, f in enumerate(funds):
            f["code"] = f"X{i + 1}"
        rows = aggregate_market(funds)
        assert [(r["mode"], r["subset"]) for r in rows] == [
            (m, s) for m in ("tp", "nt")
            for s in ("all", "paused_only", "near_high", "mid", "stuck")]
        by = {(r["mode"], r["subset"]): r for r in rows}
        assert by[("tp", "all")]["n"] == 4
        assert by[("tp", "paused_only")]["n"] == 3   # 单边上涨那条没断缴过
        assert by[("tp", "near_high")]["n"] == 2
        assert by[("tp", "mid")]["n"] == 1
        assert by[("tp", "stuck")]["n"] == 1
        # D 同额折算：单基金子样本的中位 = wallet_d - (out_d - out_a) - wallet_a
        single = aggregate_market([funds[0]])[0]
        f0 = funds[0]
        manual = (f0["tp_d_wallet"] - (f0["tp_d_out"] - f0["tp_a_out"])
                  - f0["tp_a_wallet"])
        assert single["d_wallet_delta_med"] == pytest.approx(manual, abs=1e-6)


class TestBearPauseStore:
    def test_bear_pause_ddl(self):
        from app.dca_store import ensure_bear_pause_schema
        conn = FakeConn()
        ensure_bear_pause_schema(conn)
        sql = conn.cursor_obj.executed[0][0]
        assert "CREATE TABLE IF NOT EXISTS fund_bear_pause_fund" in sql
        assert "CREATE TABLE IF NOT EXISTS fund_bear_pause_market" in sql
        assert "CREATE TABLE IF NOT EXISTS fund_bear_pause_meta" in sql
        assert "PRIMARY KEY (subset, mode)" in sql

    def test_import_bear_pause_replaces_all(self):
        """三表整表替换（重跑导入后旧数据不得残留）+ meta upsert。"""
        from app.dca_store import (BEAR_PAUSE_FUND_COLS,
                                   BEAR_PAUSE_MARKET_COLS, import_bear_pause)
        assert len(BEAR_PAUSE_FUND_COLS) == 41
        assert len(BEAR_PAUSE_MARKET_COLS) == 26
        fund_row = tuple([None] * 8 + [True]
                         + [1000.0] * 24 + [0] * 8)
        market_row = tuple([None] * 25)
        conn = FakeConn()
        nf, nm = import_bear_pause(conn, [fund_row], [market_row],
                                   params={"k": "v"})
        assert (nf, nm) == (1, 1)
        sqls = [sql for sql, _ in conn.cursor_obj.executed]
        assert sqls[0] == "DELETE FROM fund_bear_pause_fund"
        assert sqls[1] == "DELETE FROM fund_bear_pause_market"
        assert "INSERT INTO fund_bear_pause_meta" in sqls[2]
        assert len(conn.cursor_obj.batch) == 2
        sql_f, batch_f = conn.cursor_obj.batch[0]
        sql_m, batch_m = conn.cursor_obj.batch[1]
        assert "INSERT INTO fund_bear_pause_fund" in sql_f
        assert batch_f[0] == fund_row
        assert len(sql_f.split(",")) >= len(BEAR_PAUSE_FUND_COLS)
        assert "INSERT INTO fund_bear_pause_market" in sql_m
        assert batch_m[0] == market_row


def _patch_connect(monkeypatch, module, conn):
    def fake_connect(*a, **k):
        return conn
    monkeypatch.setattr(module.psycopg, "connect", fake_connect)


def _market_row(subset="all", mode="tp"):
    return (subset, mode, 9020, 72000.0, 80705.0, 0.094, 0.067, 0.131,
            0.018, 0.838, -6959.0, -12544.0, -1126.0, 0.819,
            0.069, 0.707, 68.0, -9213.0, 1304.0, 0.472,
            0.103, 0.339, 6354.0, 1541.0, 10002.0, 0.0)


def _fund_row(code="510300"):
    return tuple([code, 5290, 248, True, 6, 118, -0.209, -0.48, True]
                 + [1000.0 * (i + 1) for i in range(24)]
                 + list(range(8)) * 1)


class TestBearPauseRoutes:
    def test_pause_experiment_down_degrades(self, client, monkeypatch):
        monkeypatch.setattr("app.config.DATABASE_URL", DEAD_DSN)
        resp = client.get("/api/funds/dca/pause-experiment")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "down" and body["market"] == {}
        assert body["cases"] == [] and body["meta"] == {}

    def test_pause_experiment_shape(self, client, monkeypatch):
        """meta + market（mode→subset 两层分拣）+ 案例基金三段组装。"""
        import app.dca_api as dca_mod

        class SeqConn:
            i = 0

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def cursor(self):
                return self

            def execute(self, q, p=None):
                self.i += 1

            def fetchone(self):
                return (9020, {"case_codes": ["510300"]}) \
                    if self.i == 1 else None

            def fetchall(self):
                return [_market_row()] if self.i == 2 else [_fund_row()]

        conn = SeqConn()
        _patch_connect(monkeypatch, dca_mod, conn)
        body = client.get("/api/funds/dca/pause-experiment").json()
        assert body["status"] == "up"
        assert body["meta"]["fund_count"] == 9020
        assert body["meta"]["params"]["case_codes"] == ["510300"]
        blk = body["market"]["tp"]["all"]
        assert blk["n"] == 9020 and blk["xirr_a_med"] == 0.094
        assert blk["b_wallet_delta_med"] == -6959.0      # 金额两位口径
        assert blk["d_xirr_lower_pct"] == 0.339
        assert "mode" not in blk and "subset" not in blk  # 分拣键已弹出
        assert body["cases"][0]["code"] == "510300"
        assert body["cases"][0]["pause_months"] == 118

    def test_bear_pause_fund_hit_and_miss(self, client, monkeypatch):
        import app.dca_api as dca_mod

        class OneShotConn:
            def __init__(self, one):
                self.one = one

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def cursor(self):
                return self

            def execute(self, q, p=None):
                self.q = q

            def fetchone(self):
                return self.one

        _patch_connect(monkeypatch, dca_mod, OneShotConn(_fund_row()))
        body = client.get("/api/funds/dca/bear-pause/510300").json()
        assert body["status"] == "up"
        f = body["fund"]
        assert f["code"] == "510300" and f["ever_paused"] is True
        assert f["tp_a_wallet"] == 1000.0 and f["tp_a_xirr"] == 2000.0
        assert f["nt_d_rounds"] == 7

        _patch_connect(monkeypatch, dca_mod, OneShotConn(None))
        body = client.get("/api/funds/dca/bear-pause/000000").json()
        assert body == {"status": "up", "fund": None}

    def test_bear_pause_fund_down_degrades(self, client, monkeypatch):
        monkeypatch.setattr("app.config.DATABASE_URL", DEAD_DSN)
        resp = client.get("/api/funds/dca/bear-pause/510300")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "down" and body["fund"] is None
