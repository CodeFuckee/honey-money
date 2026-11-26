"""盈亏阶梯深档定投（pl_ladder_deep，2026-11-26 十九轮用户定案）测试。

口径定案（2026-11-25 两问澄清：浮亏按持仓口径 + 档位全按家族既有约定）：
- 减码半边与盈亏阶梯（pl_ladder）完全相同：浮盈 ≥10% 投 0.5×、≥20% 投
  0×（≥ 取最高浮盈档，0× = 当期完全停投——无份额无现金流）；
- 加码半边阈值加深：浮亏（持仓口径 pr = 持仓市值/已投入 - 1，与止盈
  触发线同口径）≥20% 投 1.5×、≥30% 投 2×（≤ 阈值取最深档）——比
  pl_ladder 的 10%/20% 更深，只在「账户真的亏了 20% 以上」才出手，
  加码次数更少、回撤期现金压力更小；
- 其余（-20% < pr < 10%）与浮盈无定义（invested=0：新轮首买/停投后期）
  投 1×；无回撤分量（DD_LADDERS 无此键——pl_ladder 同款）；
- 第 7 种定投方式接入 DCA_METHODS，止盈状态机/费用/XIRR 与既有方式共用。

手算注意（同 pl_ladder）：持仓浮亏按均价摊薄，恒不深于价格回撤。
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


def _deep_nav():
    """盈亏阶梯深档主序列（4 个自然月，tp=99 禁触发下手算全分支）：
    1 月平（首买 invested=0 浮盈无定义 → 1×）、2 月跌 25%（浮亏 25% →
    1.5×，深档 20% 线）、3 月深跌至 0.50（持仓浮亏被摊薄到 40% → ≥30%
    → 2×，深档 30% 线）、4 月回到 1.0（浮盈 55.6% → ≥20% → 0× 停投，
    当期无现金流）。

    月度买入日：1/1（a=1.0）、2/1（a=0.75）、3/1（a=0.50）、4/1（a=1.0）。
    """
    dates = pd.DatetimeIndex(pd.date_range("2024-01-01", periods=120, freq="D"))
    adj = np.ones(120)
    adj[31:60] = 0.75      # 2 月：浮亏 25%（10% 档不触发，20% 档触发）
    adj[60:91] = 0.50      # 3 月：摊薄后浮亏 40% → 2×
    adj[91:] = 1.0         # 4 月：浮盈 55.6% → 停投
    return dates, adj


def _round_restart_nav():
    """止盈重启序列（tp=0.20 生效）：1 月平 1×；2 月跳涨 25%（浮盈 25%
    → 0× 停投，同日浮盈 ≥20% 触发止盈清算——停投期止盈检查照常运行）；
    3 月回到 1.0（新轮 invested=0 浮盈无定义 → 1×）；4 月跌 35%（新轮
    浮亏 35% → ≥30% → 2×）。

    月度买入日：1/1（a=1.0）、2/1（a=1.25）、3/1（a=1.0）、4/1（a=0.65）。
    """
    dates = pd.DatetimeIndex(pd.date_range("2024-01-01", periods=120, freq="D"))
    adj = np.ones(120)
    adj[31:60] = 1.25      # 2 月：浮盈 +25% → 停投 + 同日止盈
    adj[60:91] = 1.0       # 3 月：新轮首买 1×
    adj[91:] = 0.65        # 4 月：新轮浮亏 35% → 2×
    return dates, adj


class TestSimulatePlLadderDeep:
    def test_methods_pin_values(self):
        """定投方式口径锚：七档（十九轮 pl_ladder_deep 盈亏阶梯深档追加，
        见本文件）。"""
        from app.dca_backtest import DCA_METHODS
        assert DCA_METHODS == ("amount", "share", "dd_ladder",
                               "dd_ladder_deep", "profit_dd_ladder",
                               "pl_ladder", "pl_ladder_deep")

    def test_pl_deep_constants_pinned(self):
        """档位契约：浮亏 (-20% → 1.5×, -30% → 2×)、浮盈半边复用
        PL_PROFIT_LADDER（10% → 0.5×, 20% → 0×）进 PROFIT_LADDERS、
        浮亏半边独立 PL_LOSS_LADDERS；无回撤分量（DD_LADDERS 无此键）。"""
        from app.dca_backtest import (DD_LADDERS, PL_LOSS_LADDER,
                                      PL_LOSS_LADDER_DEEP, PL_LOSS_LADDERS,
                                      PL_PROFIT_LADDER, PROFIT_LADDERS)
        assert PL_LOSS_LADDER_DEEP == ((-0.20, 1.5), (-0.30, 2.0))
        assert PL_LOSS_LADDERS == {"pl_ladder": PL_LOSS_LADDER,
                                   "pl_ladder_deep": PL_LOSS_LADDER_DEEP}
        assert PROFIT_LADDERS["pl_ladder_deep"] == PL_PROFIT_LADDER
        assert "pl_ladder_deep" not in DD_LADDERS   # 深档盈亏阶梯无回撤半边

    def test_pl_deep_flat_nav_equals_amount(self):
        """恒定净值：无费时浮盈恒 0、含费外扣浮盈恒 -1.5%（-20% < pr < 10%
        均未触发）→ pl_ladder_deep ≡ amount 逐字段一致（gross+fee 双口径
        零回归锚）。"""
        from app import dca_backtest as mod
        dates = pd.DatetimeIndex(pd.date_range("2024-01-01", periods=400,
                                               freq="D"))
        adj = np.ones(400)
        for fee in (None, mod.FeeSchedule(0.015, mod.REDEEM_EQUITY)):
            amt = mod.simulate(dates, adj, fee, take_profit=0.20)
            deep = mod.simulate(dates, adj, fee, take_profit=0.20,
                                method="pl_ladder_deep")
            assert deep == amt

    def test_pl_deep_hand_computed(self):
        """手算（_deep_nav 4 期全分支）：1× / 1.5× / 2× / 0×（停投无现金
        流）→ 投入 1000+1500+2000 = 4500；期末份额
        (1000+1500/0.75+2000/0.5) × 1.0；停投月仍计月（月份已访问）。
        同序列 pl_ladder（10%/20% 阈值）：2 月 -25% 已落 2× 档 →
        投入 [1000,2000,2000]=5000——深档与标准档可分辨。"""
        from app import dca_backtest as mod
        dates, adj = _deep_nav()
        r = mod.simulate(dates, adj, take_profit=99.0,
                         method="pl_ladder_deep")
        assert r["rounds"] == 0
        spend = [-a for _, a in r["flows"] if a < 0]
        assert spend == pytest.approx([1000.0, 1500.0, 2000.0])
        assert r["invested"] == pytest.approx(4500.0)
        shares = 1000 + 1500 / 0.75 + 2000 / 0.50
        assert r["final_value"] == pytest.approx(shares * 1.0)
        assert r["profit_rate"] == pytest.approx(shares / 4500.0 - 1.0)
        assert r["months"] == 4
        pl = mod.simulate(dates, adj, take_profit=99.0, method="pl_ladder")
        pl_spend = [-a for _, a in pl["flows"] if a < 0]
        assert pl_spend == pytest.approx([1000.0, 2000.0, 2000.0])
        assert pl["invested"] == pytest.approx(5000.0)

    @pytest.mark.parametrize("d, expected",
                             [(0.81, 1000.0),   # 浮亏 19% 未达 20% → 1×
                              (0.79, 1500.0),   # 浮亏 21% → 1.5×
                              (0.71, 1500.0),   # 浮亏 29% 仍 1.5×
                              (0.69, 2000.0),   # 浮亏 31% → 2×（取最深档）
                              (0.55, 2000.0)])  # 浮亏 45% 仍 2×
    def test_loss_tier_boundaries(self, d, expected):
        """浮亏档区间锚（pr ≤ 阈值取最深匹配档）：不取恰 -20%/-30% 净值
        ——浮点表示方向不定，档位语义用区间值锚定（与回撤/浮盈档同规）。"""
        from app import dca_backtest as mod
        dates = pd.DatetimeIndex(pd.date_range("2024-01-01", periods=60,
                                               freq="D"))
        adj = np.ones(60)
        adj[31:] = d          # 2/1 起下跌，首期后持仓浮亏 = d - 1
        res = mod.simulate(dates, adj, take_profit=99.0,
                           method="pl_ladder_deep")
        spend = [-a for _, a in res["flows"] if a < 0]
        assert spend == pytest.approx([1000.0, expected]), (d, spend)

    @pytest.mark.parametrize("r, expected",
                             [(1.08, 1000.0),   # 浮盈 8% 未触发 → 1×
                              (1.12, 500.0),    # 浮盈 12% → 0.5×
                              (1.18, 500.0),    # 浮盈 18% 仍 0.5×
                              (1.22, 0.0),      # 浮盈 22% → 0× 停投
                              (1.50, 0.0)])     # 浮盈 50% → 0× 停投
    def test_profit_tier_boundaries(self, r, expected):
        """浮盈档区间锚（pr ≥ 阈值取最高档；减码半边与 pl_ladder 同表）；
        0× 期完全无买入现金流。"""
        from app import dca_backtest as mod
        dates = pd.DatetimeIndex(pd.date_range("2024-01-01", periods=60,
                                               freq="D"))
        adj = np.ones(60)
        adj[31:] = r          # 2/1 起上涨，浮盈 = r - 1
        res = mod.simulate(dates, adj, take_profit=99.0,
                           method="pl_ladder_deep")
        spend = [-a for _, a in res["flows"] if a < 0]
        assert spend == pytest.approx([1000.0, expected] if expected
                                      else [1000.0]), (r, spend)
        assert res["invested"] == pytest.approx(1000.0 + expected)

    def test_loss_half_keys_on_position_not_price_dd(self):
        """亏损半边口径锚（持仓浮亏 ≠ 价格回撤）：3 月价格回撤 -34% 已达
        dd_ladder_deep 的 ≤-30% → 2× 档，但持仓浮亏被 2 月低位加码摊薄到
        -20.8%（仅 ≥20% 档）→ pl_ladder_deep 只投 1.5×——档位看持仓盈亏
        而非价格回撤。（pl_ladder 同序列 2 月 -25% 已落其 2× 档 → 2000，
        与深档的 1500 亦不同——阈值加深的差异面。）"""
        from app import dca_backtest as mod
        dates = pd.DatetimeIndex(pd.date_range("2024-01-01", periods=90,
                                               freq="D"))
        adj = np.ones(90)
        adj[31:60] = 0.75      # 2 月：浮亏 25% → 深档 1.5×
        adj[60:] = 0.66        # 3 月：价格回撤 -34%、持仓浮亏 -20.8% → 1.5×
        deep = mod.simulate(dates, adj, take_profit=99.0,
                            method="pl_ladder_deep")
        spend = [-a for _, a in deep["flows"] if a < 0]
        assert spend == pytest.approx([1000.0, 1500.0, 1500.0])
        # 同序列对照：pl_ladder（浅阈值）2 月 2×、3 月摊薄后 1.5×；
        # dd_ladder_deep（价格回撤口径）3 月给 2×
        pl = mod.simulate(dates, adj, take_profit=99.0, method="pl_ladder")
        assert [-a for _, a in pl["flows"] if a < 0] == \
            pytest.approx([1000.0, 2000.0, 1500.0])
        dd = mod.simulate(dates, adj, take_profit=99.0,
                          method="dd_ladder_deep")
        assert [-a for _, a in dd["flows"] if a < 0] == \
            pytest.approx([1000.0, 1500.0, 2000.0])

    def test_round_restart_skip_then_tp_then_new_round(self):
        """止盈重启全链（tp=0.20 生效）：2 月浮盈 25% → 0× 停投（当期无
        买入现金流）且同日止盈清算（停投期止盈检查照常运行，轮次投入
        =1000）；3 月新轮首买 invested=0 浮盈无定义 → 1×；4 月新轮浮亏
        35% → ≥30% → 2×。spend=[1000, 1000, 2000]、rounds=1。"""
        from app import dca_backtest as mod
        dates, adj = _round_restart_nav()
        r = mod.simulate(dates, adj, take_profit=0.20,
                         method="pl_ladder_deep")
        assert r["rounds"] == 1
        spend = [-a for _, a in r["flows"] if a < 0]
        assert spend == pytest.approx([1000.0, 1000.0, 2000.0])
        # 2 月停投后同日触发止盈：本轮投入 = 1000（唯一一期买入）
        assert r["round_events"][0][5] == pytest.approx(1000.0)

    def test_pl_deep_fee_external(self):
        """外扣费（与等额/回撤档同式）：付出现金 = 1000×倍数，费率在内
        折份额——到手份额 = 现金/(1+费率)/净值，现金流记全额。"""
        from app import dca_backtest as mod
        dates, adj = _deep_nav()
        fee = mod.FeeSchedule(0.015, ())
        r = mod.simulate(dates, adj, fee, take_profit=99.0,
                         method="pl_ladder_deep")
        spend = [-a for _, a in r["flows"] if a < 0]
        assert spend == pytest.approx([1000.0, 1500.0, 2000.0])
        assert r["invested"] == pytest.approx(4500.0)
        shares = (1000 + 1500 / 0.75 + 2000 / 0.50) / 1.015
        assert r["final_value"] == pytest.approx(shares * 1.0)

    def test_pl_deep_tp_round_invested_varies(self):
        """止盈轮次记可变投入（状态机与深档倍数共存）：1× / 1.5× / 1.5×
        三期共 4000，4 月跳涨浮盈 +245% → 0× 停投并触发止盈 → 本轮投入
        4000；同序列 dd_ladder_deep（无浮盈停投，4 月 dd=0 → 1× 买入后
        止盈）→ 本轮投入 5500——浮盈 ≥20% 停投减投的差异面。"""
        from app import dca_backtest as mod
        dates = pd.DatetimeIndex(pd.date_range("2024-01-01", periods=120,
                                               freq="D"))
        adj = np.ones(120)
        adj[31:60] = 0.75      # 2 月：浮亏 25% → 1.5×
        adj[60:91] = 0.65      # 3 月：摊薄后浮亏 -22% → 1.5×（深撤档 2×）
        adj[91:] = 2.6         # 4 月：浮盈 +245% → 0× 停投 + 同日止盈
        r = mod.simulate(dates, adj, take_profit=0.20,
                         method="pl_ladder_deep")
        assert r["rounds"] == 1
        assert r["round_events"][0][5] == pytest.approx(4000.0)  # 1000+1500+1500
        dd = mod.simulate(dates, adj, take_profit=0.20,
                          method="dd_ladder_deep")
        assert dd["round_events"][0][5] == pytest.approx(
            5500.0)            # 1000+1500+2000+1000（4 月 1× 买入后止盈）

    def test_pl_deep_rows_carry_method(self):
        from app import dca_backtest as mod
        nav, basic, scale_map = _inputs()
        rows, rounds = mod.run_backtest_tp(nav, basic, scale_map,
                                           method="pl_ladder_deep")
        assert rows and all(r["method"] == "pl_ladder_deep" for r in rows)
        assert all(t[1] == "pl_ladder_deep" for t in rounds)


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


class TestPlLadderDeepRoutes:
    @pytest.mark.parametrize("path", ["/api/funds/dca/tp",
                                      "/api/funds/dca/scatter"])
    def test_method_whitelist_accepts_pl_ladder_deep(self, client,
                                                     monkeypatch, path):
        """method=pl_ladder_deep 经 DCA_METHODS 白名单放行（API 与引擎同一
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
        resp = client.get(path, params={"method": "pl_ladder_deep"})
        assert resp.json()["method"] == "pl_ladder_deep"
        assert any("pl_ladder_deep" in (p or ()) for p in conn.params)


class TestPlLadderDeepEngineVersion:
    def test_pl_deep_constants_in_version_material(self):
        """盈亏阶梯深档常量纳入引擎版本串（改档位不 bump base 也自动全量
        ——防脏数据不变量；见 engine_version docstring「新增口径常量须
        同步补进」）。"""
        from app import dca_backtest as mod
        old = mod.engine_version()
        saved = mod.PL_LOSS_LADDER_DEEP
        try:
            mod.PL_LOSS_LADDER_DEEP = ((-0.20, 1.5), (-0.30, 2.0),
                                       (-0.40, 3.0))
            assert mod.engine_version() != old
        finally:
            mod.PL_LOSS_LADDER_DEEP = saved
        assert mod.engine_version() == old
