"""盈亏阶梯即停定投（pl_ladder_stop，2026-11-26 二十三轮用户定案）测试。

口径定案（用户原话：浮亏≥10% 投 1.5 倍、≥20% 投 2 倍、浮盈>0% 投 0）：
- 加码半边与盈亏阶梯（pl_ladder）完全同表（PL_LOSS_LADDER）：浮亏（持仓
  口径 pr = 持仓市值/已投入 - 1，与止盈触发线同口径）≥10% 投 1.5×、≥20%
  投 2×（≤ 阈值取最深档）；
- 减码半边换即停表（PL_PROFIT_LADDER_STOP = ((0.0, 0.0),)）：只要当轮
  浮盈 ≥0（含恰打平——浮盈档沿家族 pr ≥ 阈值 匹配口径，与「>0%」的浮点
  差异生产不可观测）就整期停投——pl_ladder 是浮盈 ≥10% 才减半、≥20% 才
  停投，本方式回本即收手、只在亏损期持续投入；
- 其余（-10% < pr < 0）与浮盈无定义（invested=0：新轮首买/停投后期）投
  1×；无回撤分量（DD_LADDERS 无此键——pl_ladder 系同款）；0× 停投期
  止盈检查照常运行（浮盈达止盈线同日清算）；
- 第 8 种定投方式接入 DCA_METHODS，止盈状态机/费用/XIRR 与既有方式共用。

手算注意（同 pl_ladder 系）：持仓浮亏按均价摊薄，恒不深于价格回撤。
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


def _stop_nav():
    """盈亏阶梯即停主序列（4 个自然月，tp=99 禁触发下手算全分支）：
    1 月平（首买 invested=0 浮盈无定义 → 1×）、2 月跌 15%（浮亏 15% →
    1.5×）、3 月深跌至 0.50（持仓浮亏被摊薄到 44.7% → ≥20% → 2×）、
    4 月回升到 0.70（浮盈 +5.2%，落在 (0, 10%) 区间 → ≥0 即停投，当期
    无现金流）。

    月度买入日：1/1（a=1.0）、2/1（a=0.85）、3/1（a=0.50）、4/1（a=0.70）。
    """
    dates = pd.DatetimeIndex(pd.date_range("2024-01-01", periods=120, freq="D"))
    adj = np.ones(120)
    adj[31:60] = 0.85      # 2 月：浮亏 15%（10% 档触发）
    adj[60:91] = 0.50      # 3 月：摊薄后浮亏 44.7% → 2×
    adj[91:] = 0.70        # 4 月：浮盈 +5.2% → ≥0 停投
    return dates, adj


def _stop_no_tp_nav():
    """停投不止盈序列（tp=0.20 生效但浮盈不达线）：1 月平 1×；2 月涨 5%
    （浮盈 +5% → ≥0 停投，但 5% < 20% 不触发止盈——停投 ≠ 清仓，头寸
    原样保留）；3 月回落 0.95（浮亏 -5% 浅亏 → 复投 1×）；4 月跌至 0.70
    （摊薄后浮亏 -28.2% → ≥20% → 2×）。

    月度买入日：1/1（a=1.0）、2/1（a=1.05）、3/1（a=0.95）、4/1（a=0.70）。
    """
    dates = pd.DatetimeIndex(pd.date_range("2024-01-01", periods=120, freq="D"))
    adj = np.ones(120)
    adj[31:60] = 1.05      # 2 月：浮盈 +5% → 停投、不止盈
    adj[60:91] = 0.95      # 3 月：浮亏 -5% 浅亏 → 1×
    adj[91:] = 0.70        # 4 月：摊薄后浮亏 -28.2% → 2×
    return dates, adj


def _round_restart_nav():
    """止盈重启序列（tp=0.20 生效）：1 月平 1×；2 月跳涨 25%（浮盈 +25%
    → ≥0 停投，同日浮盈 ≥20% 触发止盈清算——停投期止盈检查照常运行）；
    3 月回到 1.0（新轮 invested=0 浮盈无定义 → 1×）；4 月跌 35%（新轮
    浮亏 35% → ≥20% → 2×）。

    月度买入日：1/1（a=1.0）、2/1（a=1.25）、3/1（a=1.0）、4/1（a=0.65）。
    """
    dates = pd.DatetimeIndex(pd.date_range("2024-01-01", periods=120, freq="D"))
    adj = np.ones(120)
    adj[31:60] = 1.25      # 2 月：浮盈 +25% → 停投 + 同日止盈
    adj[60:91] = 1.0       # 3 月：新轮首买 1×
    adj[91:] = 0.65        # 4 月：新轮浮亏 35% → 2×
    return dates, adj


class TestSimulatePlLadderStop:
    def test_methods_pin_values(self):
        """定投方式口径锚：八档（二十三轮 pl_ladder_stop 盈亏阶梯即停追加，
        见本文件）。"""
        from app.dca_backtest import DCA_METHODS
        assert DCA_METHODS == ("amount", "share", "dd_ladder",
                               "dd_ladder_deep", "profit_dd_ladder",
                               "pl_ladder", "pl_ladder_deep",
                               "pl_ladder_stop")

    def test_pl_stop_constants_pinned(self):
        """档位契约：加码半边与 pl_ladder 完全同表（PL_LOSS_LADDER 进
        PL_LOSS_LADDERS）、减码半边独立即停表 ((0.0, 0.0),) 进
        PROFIT_LADDERS；无回撤分量（DD_LADDERS 无此键）。"""
        from app.dca_backtest import (DD_LADDERS, PL_LOSS_LADDER,
                                      PL_LOSS_LADDERS, PL_PROFIT_LADDER_STOP,
                                      PROFIT_LADDERS)
        assert PL_PROFIT_LADDER_STOP == ((0.0, 0.0),)
        assert PL_LOSS_LADDERS == {"pl_ladder": PL_LOSS_LADDER,
                                   "pl_ladder_deep": ((-0.20, 1.5),
                                                      (-0.30, 2.0)),
                                   "pl_ladder_stop": PL_LOSS_LADDER}
        assert PROFIT_LADDERS["pl_ladder_stop"] == PL_PROFIT_LADDER_STOP
        assert "pl_ladder_stop" not in DD_LADDERS   # 即停盈亏阶梯无回撤半边

    def test_pl_stop_flat_nav_stops_after_first_buy(self):
        """恒定净值（gross 口径）：首期 1× 后持仓浮盈恒为恰 0（pr = 0 ≥ 0
        → 即停表命中）→ 仅首期投入 1000、此后整期停投——恰打平也停投的
        口径锚（≥ 匹配边界钉值）。含费外扣口径浮盈恒 -1.48%（<0 浅亏）
        → 每期 1× ≡ amount（与 gross 相反的边界面，两者都钉住）。"""
        from app import dca_backtest as mod
        dates = pd.DatetimeIndex(pd.date_range("2024-01-01", periods=400,
                                               freq="D"))
        adj = np.ones(400)
        r = mod.simulate(dates, adj, take_profit=0.20,
                         method="pl_ladder_stop")
        spend = [-a for _, a in r["flows"] if a < 0]
        assert spend == [1000.0]
        assert r["invested"] == pytest.approx(1000.0)
        assert r["rounds"] == 0
        assert r["final_value"] == pytest.approx(1000.0)
        # 含费：pr ≈ -1.48% 落浅亏区 → 每期 1×，与 amount 逐字段一致
        fee = mod.FeeSchedule(0.015, mod.REDEEM_EQUITY)
        amt = mod.simulate(dates, adj, fee, take_profit=0.20)
        stop = mod.simulate(dates, adj, fee, take_profit=0.20,
                            method="pl_ladder_stop")
        assert stop == amt

    def test_pl_stop_hand_computed(self):
        """手算（_stop_nav 4 期全分支）：1× / 1.5× / 2× / 0×（停投无现金
        流）→ 投入 1000+1500+2000 = 4500；期末份额
        (1000+1500/0.85+2000/0.5) × 0.70；4 月浮盈 +5.2% 落 (0,10%) 即停。
        同序列 pl_ladder（减码半边 10%/20% 档）：4 月 +5.2% 未触发任何档
        → 1× 再投 1000 → 投入 5500——减码半边差异面可分辨。"""
        from app import dca_backtest as mod
        dates, adj = _stop_nav()
        r = mod.simulate(dates, adj, take_profit=99.0,
                         method="pl_ladder_stop")
        assert r["rounds"] == 0
        spend = [-a for _, a in r["flows"] if a < 0]
        assert spend == pytest.approx([1000.0, 1500.0, 2000.0])
        assert r["invested"] == pytest.approx(4500.0)
        shares = 1000 + 1500 / 0.85 + 2000 / 0.50
        assert r["final_value"] == pytest.approx(shares * 0.70)
        assert r["profit_rate"] == pytest.approx(shares * 0.70 / 4500.0 - 1.0)
        assert r["months"] == 4
        pl = mod.simulate(dates, adj, take_profit=99.0, method="pl_ladder")
        pl_spend = [-a for _, a in pl["flows"] if a < 0]
        assert pl_spend == pytest.approx([1000.0, 1500.0, 2000.0, 1000.0])
        assert pl["invested"] == pytest.approx(5500.0)

    def test_loss_half_identical_to_pl_ladder(self):
        """加码半边同表锚：纯亏损序列（1.0→0.85→0.70→0.60，逐期浮亏
        -15%/-22.6%/-25% 恒不转盈）下 pl_ladder_stop 与 pl_ladder 输出
        逐字段一致（gross+fee 双口径）——两方式只在浮盈半边分岔。"""
        from app import dca_backtest as mod
        dates = pd.DatetimeIndex(pd.date_range("2024-01-01", periods=120,
                                               freq="D"))
        adj = np.ones(120)
        adj[31:60] = 0.85
        adj[60:91] = 0.70
        adj[91:] = 0.60
        for fee in (None, mod.FeeSchedule(0.015, mod.REDEEM_EQUITY)):
            stop = mod.simulate(dates, adj, fee, take_profit=99.0,
                                method="pl_ladder_stop")
            pl = mod.simulate(dates, adj, fee, take_profit=99.0,
                              method="pl_ladder")
            assert stop == pl
        spend = [-a for _, a in stop["flows"] if a < 0]
        assert spend == pytest.approx([1000.0, 1500.0, 2000.0, 2000.0])
        assert stop["invested"] == pytest.approx(6500.0)

    @pytest.mark.parametrize("d, expected",
                             [(0.91, 1000.0),   # 浮亏 9% 未达 10% → 1×
                              (0.89, 1500.0),   # 浮亏 11% → 1.5×
                              (0.81, 1500.0),   # 浮亏 19% 仍 1.5×
                              (0.79, 2000.0),   # 浮亏 21% → 2×（取最深档）
                              (0.55, 2000.0)])  # 浮亏 45% 仍 2×
    def test_loss_tier_boundaries(self, d, expected):
        """浮亏档区间锚（pr ≤ 阈值取最深匹配档，与 pl_ladder 同表）：
        不取恰 -10%/-20% 净值——浮点表示方向不定，档位语义用区间值锚定。"""
        from app import dca_backtest as mod
        dates = pd.DatetimeIndex(pd.date_range("2024-01-01", periods=60,
                                               freq="D"))
        adj = np.ones(60)
        adj[31:] = d          # 2/1 起下跌，首期后持仓浮亏 = d - 1
        res = mod.simulate(dates, adj, take_profit=99.0,
                           method="pl_ladder_stop")
        spend = [-a for _, a in res["flows"] if a < 0]
        assert spend == pytest.approx([1000.0, expected]), (d, spend)

    @pytest.mark.parametrize("r, expected",
                             [(0.99, 1000.0),   # 浮亏 1% 浅亏 → 1×
                              (1.02, 0.0),      # 浮盈 2% → ≥0 停投
                              (1.08, 0.0),      # 浮盈 8% → 停投（pl_ladder 1×）
                              (1.30, 0.0),      # 浮盈 30% → 停投
                              (2.00, 0.0)])     # 浮盈 100% → 停投
    def test_profit_tier_boundaries(self, r, expected):
        """浮盈档区间锚（(0.0, 0.0) 单档：pr ≥ 0 即命中停投）：1.02 停投
        是与 pl_ladder（浮盈 <10% 落 1×）的分界面；0× 期完全无买入现金流。"""
        from app import dca_backtest as mod
        dates = pd.DatetimeIndex(pd.date_range("2024-01-01", periods=60,
                                               freq="D"))
        adj = np.ones(60)
        adj[31:] = r          # 2/1 起上涨，浮盈 = r - 1
        res = mod.simulate(dates, adj, take_profit=99.0,
                           method="pl_ladder_stop")
        spend = [-a for _, a in res["flows"] if a < 0]
        assert spend == pytest.approx([1000.0, expected] if expected
                                      else [1000.0]), (r, spend)
        assert res["invested"] == pytest.approx(1000.0 + expected)

    def test_stop_without_tp_keeps_position(self):
        """停投 ≠ 清仓（tp=0.20 生效但浮盈不达线）：2 月浮盈 +5% → 停投
        且不触发止盈（5% < 20%，头寸原样保留）；3 月浮亏 -5% 浅亏 → 复投
        1×；4 月摊薄浮亏 -28.2% → 2×。spend=[1000,1000,2000]、rounds=0
        ——只在亏损期投入、盈利期空仓等待，回落即复投。"""
        from app import dca_backtest as mod
        dates, adj = _stop_no_tp_nav()
        r = mod.simulate(dates, adj, take_profit=0.20,
                         method="pl_ladder_stop")
        assert r["rounds"] == 0
        spend = [-a for _, a in r["flows"] if a < 0]
        assert spend == pytest.approx([1000.0, 1000.0, 2000.0])
        assert r["invested"] == pytest.approx(4000.0)
        assert r["months"] == 4

    def test_round_restart_skip_then_tp_then_new_round(self):
        """止盈重启全链（tp=0.20 生效）：2 月浮盈 25% → ≥0 停投（当期无
        买入现金流）且同日止盈清算（停投期止盈检查照常运行，轮次投入
        =1000）；3 月新轮首买 invested=0 浮盈无定义 → 1×；4 月新轮浮亏
        35% → ≥20% → 2×。spend=[1000, 1000, 2000]、rounds=1。"""
        from app import dca_backtest as mod
        dates, adj = _round_restart_nav()
        r = mod.simulate(dates, adj, take_profit=0.20,
                         method="pl_ladder_stop")
        assert r["rounds"] == 1
        spend = [-a for _, a in r["flows"] if a < 0]
        assert spend == pytest.approx([1000.0, 1000.0, 2000.0])
        # 2 月停投后同日触发止盈：本轮投入 = 1000（唯一一期买入）
        assert r["round_events"][0][5] == pytest.approx(1000.0)

    def test_pl_stop_fee_external(self):
        """外扣费（与等额/回撤档同式）：付出现金 = 1000×倍数，费率在内
        折份额——到手份额 = 现金/(1+费率)/净值，现金流记全额。"""
        from app import dca_backtest as mod
        dates, adj = _stop_nav()
        fee = mod.FeeSchedule(0.015, ())
        r = mod.simulate(dates, adj, fee, take_profit=99.0,
                         method="pl_ladder_stop")
        spend = [-a for _, a in r["flows"] if a < 0]
        assert spend == pytest.approx([1000.0, 1500.0, 2000.0])
        assert r["invested"] == pytest.approx(4500.0)
        shares = (1000 + 1500 / 0.85 + 2000 / 0.50) / 1.015
        assert r["final_value"] == pytest.approx(shares * 0.70)

    def test_pl_stop_tp_round_invested_varies(self):
        """止盈轮次记可变投入（状态机与即停倍数共存）：2 月 -25% → 2×、
        3 月摊薄 -20.6% → 2×，三期共 5000；4 月跳涨浮盈 +250.7% → ≥0
        停投并同日止盈 → 本轮投入 5000；同序列 dd_ladder_deep（价格回撤
        口径、无浮盈停投：4 月 dd=0 → 1× 买入后止盈）→ 5500——浮盈>0%
        停投减投的差异面。"""
        from app import dca_backtest as mod
        dates = pd.DatetimeIndex(pd.date_range("2024-01-01", periods=120,
                                               freq="D"))
        adj = np.ones(120)
        adj[31:60] = 0.75      # 2 月：浮亏 25% → 2×
        adj[60:91] = 0.65      # 3 月：摊薄后浮亏 -20.6% → 2×
        adj[91:] = 2.6         # 4 月：浮盈 +250.7% → 停投 + 同日止盈
        r = mod.simulate(dates, adj, take_profit=0.20,
                         method="pl_ladder_stop")
        assert r["rounds"] == 1
        assert r["round_events"][0][5] == pytest.approx(5000.0)  # 1000+2000+2000
        dd = mod.simulate(dates, adj, take_profit=0.20,
                          method="dd_ladder_deep")
        assert dd["round_events"][0][5] == pytest.approx(
            5500.0)            # 1000+1500+2000+1000（4 月 1× 买入后止盈）

    def test_pl_stop_rows_carry_method(self):
        from app import dca_backtest as mod
        nav, basic, scale_map = _inputs()
        rows, rounds = mod.run_backtest_tp(nav, basic, scale_map,
                                           method="pl_ladder_stop")
        assert rows and all(r["method"] == "pl_ladder_stop" for r in rows)
        assert all(t[1] == "pl_ladder_stop" for t in rounds)


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


class TestPlLadderStopRoutes:
    @pytest.mark.parametrize("path", ["/api/funds/dca/tp",
                                      "/api/funds/dca/scatter"])
    def test_method_whitelist_accepts_pl_ladder_stop(self, client,
                                                     monkeypatch, path):
        """method=pl_ladder_stop 经 DCA_METHODS 白名单放行（API 与引擎同一
        事实源，无需改 dca_api——此测试钉住新方式不回落 amount）。"""
        import app.dca_api as dca_mod

        class _FakeConn:
            def __init__(self):
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
                return (None, None, None)

            def fetchall(self):
                return []

        conn = _FakeConn()
        monkeypatch.setattr(dca_mod.psycopg, "connect",
                            lambda *a, **k: conn)
        resp = client.get(path, params={"method": "pl_ladder_stop"})
        assert resp.json()["method"] == "pl_ladder_stop"
        assert any("pl_ladder_stop" in (p or ()) for p in conn.params)


class TestPlLadderStopEngineVersion:
    def test_pl_stop_constants_in_version_material(self):
        """盈亏阶梯即停常量纳入引擎版本串（改档位不 bump base 也自动全量
        ——防脏数据不变量；见 engine_version docstring「新增口径常量须
        同步补进」）。"""
        from app import dca_backtest as mod
        old = mod.engine_version()
        saved = mod.PL_PROFIT_LADDER_STOP
        try:
            mod.PL_PROFIT_LADDER_STOP = ((0.0, 0.0), (0.05, 0.0))
            assert mod.engine_version() != old
        finally:
            mod.PL_PROFIT_LADDER_STOP = saved
        assert mod.engine_version() == old
