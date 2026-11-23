"""回撤梯度加码定投（dd_ladder，2026-11-23 四轮）测试。

口径定案（延续熊市加倍实验证据链）：
- 每月买入日按当日回撤分档加倍：回撤 = adj/历史最高（含当日）- 1，因果无前视；
- 档位 DD_LADDER =（回撤 ≤ -10% → 1.5×，回撤 ≤ -20% → 2×），其余 1×
  —— ≤-20% 投 2× 正是 2026-11-19 熊市加倍实验 D 组（全市场 68% 基金更优）
  的单点验证，中间档 -10% → 1.5× 为自然梯度；
- 基础金额/外扣费/止盈状态机/XIRR 与等额共用（每期现金 = 1000×倍数）。
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


def _ladder_nav():
    """4 个自然月：1 月平（dd=0）、2 月跌 12%（1.5×）、3 月跌 25%（2×）、
    4 月回到 1.0（较峰值 -0% → 1×）。

    月度买入日：1/1（a=1.0）、2/1（a=0.88）、3/1（a=0.75）、4/1（a=1.0）。
    """
    dates = pd.DatetimeIndex(pd.date_range("2024-01-01", periods=120, freq="D"))
    adj = np.ones(120)
    adj[31:59] = 0.88      # 2 月：较前高 -12%
    adj[60:90] = 0.75      # 3 月：较前高 -25%
    return dates, adj


class TestSimulateDdLadder:
    def test_methods_pin_values(self):
        """定投方式口径锚：三档（四轮追加 dd_ladder）。"""
        from app.dca_backtest import DCA_METHODS
        assert DCA_METHODS == ("amount", "share", "dd_ladder")

    def test_ladder_constant_pinned(self):
        """档位契约：-10% → 1.5×、-20% → 2×（防悄悄改坏）。"""
        from app.dca_backtest import DD_LADDER
        assert DD_LADDER == ((-0.10, 1.5), (-0.20, 2.0))

    def test_flat_nav_equals_amount(self):
        """恒定净值：无回撤恒 1× → dd_ladder ≡ amount 逐字段一致。"""
        from app import dca_backtest as mod
        dates = pd.DatetimeIndex(pd.date_range("2024-01-01", periods=400,
                                               freq="D"))
        adj = np.ones(400)
        for fee in (None, mod.FeeSchedule(0.015, mod.REDEEM_EQUITY)):
            amt = mod.simulate(dates, adj, fee, take_profit=0.20)
            lad = mod.simulate(dates, adj, fee, take_profit=0.20,
                               method="dd_ladder")
            assert lad == amt

    def test_rising_nav_equals_amount(self):
        """持续上行：同样无回撤（每日新高 dd=0）→ ≡ amount。"""
        from app import dca_backtest as mod
        dates = pd.DatetimeIndex(pd.date_range("2024-01-01", periods=400,
                                               freq="D"))
        adj = np.cumprod(1.0 + np.full(400, 0.002))
        amt = mod.simulate(dates, adj, take_profit=0.20)
        lad = mod.simulate(dates, adj, take_profit=0.20, method="dd_ladder")
        assert lad == amt

    def test_ladder_hand_computed(self):
        """手算：4 期买入 1000 / 1500 / 2000 / 1000 → 投入 5500。
        期末 a=1.0、份额 = 1000+1500/0.88+2000/0.75+1000 ≈ 5952.95。"""
        from app import dca_backtest as mod
        dates, adj = _ladder_nav()
        r = mod.simulate(dates, adj, take_profit=99.0, method="dd_ladder")
        assert r["rounds"] == 0
        assert r["invested"] == pytest.approx(5500.0)
        spend = [-a for _, a in r["flows"] if a < 0]
        assert spend == pytest.approx([1000.0, 1500.0, 2000.0, 1000.0])
        shares = 1000 + 1500 / 0.88 + 2000 / 0.75 + 1000
        assert r["final_value"] == pytest.approx(shares * 1.0)
        assert r["profit_rate"] == pytest.approx(shares * 1.0 / 5500.0 - 1.0)

    def test_ladder_fee_external(self):
        """外扣费（与等额同式）：付出现金 = 1000×倍数，费率在内折份额——
        到手份额 = 现金/(1+费率)/净值，现金流记全额。"""
        from app import dca_backtest as mod
        dates, adj = _ladder_nav()
        fee = mod.FeeSchedule(0.015, ())
        r = mod.simulate(dates, adj, fee, take_profit=99.0, method="dd_ladder")
        spend = [-a for _, a in r["flows"] if a < 0]
        assert spend == pytest.approx([1000.0, 1500.0, 2000.0, 1000.0])
        assert r["invested"] == pytest.approx(5500.0)
        shares = (1000 + 1500 / 0.88 + 2000 / 0.75 + 1000) / 1.015
        assert r["final_value"] == pytest.approx(shares * 1.0)

    def test_ladder_recover_to_new_high_is_base(self):
        """回到新高（4 月 a=1.0 = 历史最高）→ dd=0 → 1×（含当日的峰值口径）。"""
        from app import dca_backtest as mod
        dates, adj = _ladder_nav()
        r = mod.simulate(dates, adj, take_profit=99.0, method="dd_ladder")
        spend = [-a for _, a in r["flows"] if a < 0]
        assert spend[3] == pytest.approx(1000.0)

    def test_ladder_tp_round_invested_varies(self):
        """止盈轮次记可变投入（状态机与倍数共存）：1 月 1×、2/3 月回撤
        -15% 各 1.5×、4 月回到新高 1× 后触发 → 本轮投入 5000。"""
        from app import dca_backtest as mod
        dates = pd.DatetimeIndex(pd.date_range("2024-01-01", periods=120,
                                               freq="D"))
        adj = np.ones(120)
        adj[31:91] = 0.85      # 2~3 月回撤 -15%（至 3/31）
        adj[91:] = 2.0         # 4/1 新高：当日买入后触发止盈
        r = mod.simulate(dates, adj, take_profit=0.20, method="dd_ladder")
        assert r["rounds"] == 1
        assert r["round_events"][0][5] == pytest.approx(5000.0)  # 1000+1500+1500+1000


class TestRunBacktestDdLadder:
    def test_rows_carry_method(self):
        from app import dca_backtest as mod
        nav, basic, scale_map = _inputs()
        rows, rounds = mod.run_backtest_tp(nav, basic, scale_map,
                                           method="dd_ladder")
        assert rows and all(r["method"] == "dd_ladder" for r in rows)
        assert all(t[1] == "dd_ladder" for t in rounds)

    def test_full_backtest_covers_three_methods(self, monkeypatch):
        from app import dca_backtest as mod
        nav, basic, scale_map = _inputs()

        class _Ctx:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(mod, "_load_codes",
                            lambda conn: sorted(nav["code"].unique()))
        monkeypatch.setattr(mod, "_load_basic",
                            lambda conn, codes: basic.loc[codes])
        monkeypatch.setattr(mod, "_load_scale", lambda conn: scale_map)
        monkeypatch.setattr(mod, "_load_nav",
                            lambda conn, codes:
                            nav[nav["code"].isin(codes)].reset_index(drop=True))
        monkeypatch.setattr(mod.psycopg, "connect",
                            lambda *a, **k: _Ctx())
        old_rows, _or, tp_rows, _tr, _t, _r, stats = \
            mod.run_full_backtest_tp("postgresql://fake")
        methods = {r["method"] for r in tp_rows}
        assert methods == set(mod.DCA_METHODS)
        assert stats["methods"] == list(mod.DCA_METHODS)
        # 旧口径行仍由 amount 派生
        assert len(old_rows) == 2


def _inputs():
    dates = pd.date_range("2024-01-01", periods=420, freq="D")
    navs = []
    a = np.cumprod(1.0 + np.full(420, 0.0025))
    for i, d in enumerate(dates):
        navs.append(("000001", d, round(float(a[i]), 6),
                     round(float(a[i]), 6), 0.25))
        navs.append(("000002", d, 1.0, 1.0, 0.0))
    nav = pd.DataFrame(navs, columns=["code", "nav_date", "unit_nav",
                                      "accum_nav", "pct_chg"])
    basic = pd.DataFrame(
        {"name": {"000001": "成长先锋混合A", "000002": "稳如磐石混合A"},
         "fund_type": {"000001": "混合型-偏股", "000002": "混合型-偏股"},
         "delisted": {"000001": False, "000002": False}})
    scale_map = {"000001": {"scale_yi": 12.5, "scale_date": "2026-09-30"},
                 "000002": {"scale_yi": 3.2, "scale_date": "2026-09-30"}}
    return nav, basic, scale_map


class TestDdLadderRoutes:
    def test_method_whitelist_accepts_dd_ladder(self, client, monkeypatch):
        import app.dca_api as dca_mod
        conn = _FakeConnOne((None, None, None))
        _patch_connect(monkeypatch, dca_mod, conn)
        resp = client.get("/api/funds/dca/tp",
                          params={"method": "dd_ladder"})
        assert resp.json()["method"] == "dd_ladder"
        assert any("dd_ladder" in (p or ()) for p in conn.params)

    def test_scatter_accepts_dd_ladder(self, client, monkeypatch):
        import app.dca_api as dca_mod
        conn = _FakeConnOne((None,), rows=[])
        _patch_connect(monkeypatch, dca_mod, conn)
        resp = client.get("/api/funds/dca/scatter",
                          params={"method": "dd_ladder"})
        assert resp.json()["method"] == "dd_ladder"
        main = [s for s in conn.executed if s.startswith("SELECT code,")][-1]
        assert "FROM fund_dca_tp_result" in main


class _FakeConnOne:
    def __init__(self, one, rows=None):
        self.one = one
        self.rows = rows or []
        self.executed: list = []
        self.params: list = []

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        self.executed.append(query)
        self.params.append(params)

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.rows


def _patch_connect(monkeypatch, module, conn):
    monkeypatch.setattr(module.psycopg, "connect",
                        lambda *a, **k: conn)
