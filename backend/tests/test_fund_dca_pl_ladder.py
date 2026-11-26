"""盈亏阶梯定投（pl_ladder，2026-11-24 八轮用户定案）测试。

口径定案（2026-11-24 四问澄清）：
- 浮盈/浮亏均用持仓口径：pr = 持仓市值/已投入 - 1（与止盈触发线同口径）
  ——与浮盈阶梯的本质差异：后者的亏损半边用价格回撤（净值较历史最高）；
- 浮亏 ≥10% 投 1.5×、≥20% 投 2×（≤ 阈值取最深档）；浮盈 ≥10% 投 0.5×、
  ≥20% 投 0×（≥ 取最高浮盈档，0× = 当期完全停投——无份额无现金流）；
- 其余（-10% < pr < 10%）与浮盈无定义（invested=0：新轮首买/停投后期）
  投 1×；无回撤分量（浮亏半边不看价格回撤）；
- 第 6 种定投方式接入 DCA_METHODS，止盈状态机/费用/XIRR 与既有方式共用；
  0× 停投期止盈检查照常运行（浮盈 ≥20% 恰是 tp=0.20 的触发区，停投避免
  止盈前追高）。

手算注意：持仓浮亏按均价摊薄，与价格回撤不等——多期低位买入后浮亏比
价格跌幅浅（如 2 期 0.85 后净值再跌到 0.85，浮亏仅 -6% 而非 -15%）。
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


def _pl_nav():
    """盈亏阶梯主序列（4 个自然月，tp=99 禁触发下手算全分支）：
    1 月平（首买 invested=0 浮盈无定义 → 1×）、2 月跌 15%（浮亏 15% →
    1.5×）、3 月深跌至 0.60（持仓浮亏被摊薄到 33.6% → ≥20% → 2×）、
    4 月回到 1.0（浮盈 35.5% → ≥20% → 0× 停投，当期无现金流）。

    月度买入日：1/1（a=1.0）、2/1（a=0.85）、3/1（a=0.60）、4/1（a=1.0）。
    """
    dates = pd.DatetimeIndex(pd.date_range("2024-01-01", periods=120, freq="D"))
    adj = np.ones(120)
    adj[31:60] = 0.85      # 2 月：浮亏 15%
    adj[60:91] = 0.60      # 3 月：浮亏 33.6%（低位加码摊薄后）
    adj[91:] = 1.0         # 4 月：浮盈 35.5% → 停投
    return dates, adj


def _round_restart_nav():
    """止盈重启序列（tp=0.20 生效）：1 月平 1×；2 月跳涨 25%（浮盈 25%
    → 0× 停投，同日浮盈 ≥20% 触发止盈清算——停投期止盈检查照常运行）；
    3 月回到 1.0（新轮 invested=0 浮盈无定义 → 1×）；4 月跌 15%（新轮
    浮亏 15% → 1.5×）。

    月度买入日：1/1（a=1.0）、2/1（a=1.25）、3/1（a=1.0）、4/1（a=0.85）。
    """
    dates = pd.DatetimeIndex(pd.date_range("2024-01-01", periods=120, freq="D"))
    adj = np.ones(120)
    adj[31:60] = 1.25      # 2 月：浮盈 +25% → 停投 + 同日止盈
    adj[60:91] = 1.0       # 3 月：新轮首买 1×
    adj[91:] = 0.85        # 4 月：新轮浮亏 15% → 1.5×
    return dates, adj


class TestSimulatePlLadder:
    def test_methods_pin_values(self):
        """定投方式口径锚：八档（十九轮 pl_ladder_deep、二十三轮
        pl_ladder_stop 盈亏阶梯即停追加，
        见 test_fund_dca_pl_ladder_deep.py / test_fund_dca_pl_ladder_stop.py）。"""
        from app.dca_backtest import DCA_METHODS
        assert DCA_METHODS == ("amount", "share", "dd_ladder",
                               "dd_ladder_deep", "profit_dd_ladder",
                               "pl_ladder", "pl_ladder_deep",
                               "pl_ladder_stop")

    def test_pl_constants_pinned(self):
        """档位契约：浮亏 (-10% → 1.5×, -20% → 2×)、浮盈 (10% → 0.5×,
        20% → 0×)；浮盈半边进 PROFIT_LADDERS（复用现有浮盈档分派）、
        浮亏半边独立 PL_LOSS_LADDERS；无回撤分量（DD_LADDERS 无此键）。"""
        from app.dca_backtest import (DD_LADDERS, PL_LOSS_LADDER,
                                      PL_LOSS_LADDER_DEEP, PL_LOSS_LADDERS,
                                      PL_PROFIT_LADDER, PROFIT_LADDERS)
        assert PL_LOSS_LADDER == ((-0.10, 1.5), (-0.20, 2.0))
        assert PL_PROFIT_LADDER == ((0.10, 0.5), (0.20, 0.0))
        assert PL_LOSS_LADDERS == {"pl_ladder": PL_LOSS_LADDER,
                                   "pl_ladder_deep": PL_LOSS_LADDER_DEEP,
                                   "pl_ladder_stop": PL_LOSS_LADDER}
        assert PROFIT_LADDERS["pl_ladder"] == PL_PROFIT_LADDER
        assert "pl_ladder" not in DD_LADDERS   # 盈亏阶梯无回撤半边

    def test_pl_flat_nav_equals_amount(self):
        """恒定净值：无费时浮盈恒 0、含费外扣浮盈恒 -1.5%（>-10% 均未触发）
        → pl_ladder ≡ amount 逐字段一致（gross+fee 双口径零回归锚）。"""
        from app import dca_backtest as mod
        dates = pd.DatetimeIndex(pd.date_range("2024-01-01", periods=400,
                                               freq="D"))
        adj = np.ones(400)
        for fee in (None, mod.FeeSchedule(0.015, mod.REDEEM_EQUITY)):
            amt = mod.simulate(dates, adj, fee, take_profit=0.20)
            pl = mod.simulate(dates, adj, fee, take_profit=0.20,
                              method="pl_ladder")
            assert pl == amt

    def test_pl_hand_computed(self):
        """手算（_pl_nav 4 期全分支）：1× / 1.5× / 2× / 0×（停投无现金流）
        → 投入 1000+1500+2000 = 4500；期末份额
        (1000+1500/0.85+2000/0.6) × 1.0；停投月仍计月（月份已访问）。"""
        from app import dca_backtest as mod
        dates, adj = _pl_nav()
        r = mod.simulate(dates, adj, take_profit=99.0, method="pl_ladder")
        assert r["rounds"] == 0
        spend = [-a for _, a in r["flows"] if a < 0]
        assert spend == pytest.approx([1000.0, 1500.0, 2000.0])
        assert r["invested"] == pytest.approx(4500.0)
        shares = 1000 + 1500 / 0.85 + 2000 / 0.60
        assert r["final_value"] == pytest.approx(shares * 1.0)
        assert r["profit_rate"] == pytest.approx(shares / 4500.0 - 1.0)
        assert r["months"] == 4

    @pytest.mark.parametrize("d, expected",
                             [(0.91, 1000.0),   # 浮亏 9% 未达 → 1×
                              (0.89, 1500.0),   # 浮亏 11% → 1.5×
                              (0.85, 1500.0),   # 浮亏 15% 仍 1.5×
                              (0.79, 2000.0),   # 浮亏 21% → 2×（取最深档）
                              (0.70, 2000.0)])  # 浮亏 30% 仍 2×
    def test_loss_tier_boundaries(self, d, expected):
        """浮亏档区间锚（pr ≤ 阈值取最深匹配档）：不取恰 -10%/-20% 净值
        ——浮点表示方向不定，档位语义用区间值锚定（与回撤/浮盈档同规）。"""
        from app import dca_backtest as mod
        dates = pd.DatetimeIndex(pd.date_range("2024-01-01", periods=60,
                                               freq="D"))
        adj = np.ones(60)
        adj[31:] = d          # 2/1 起下跌，首期后持仓浮亏 = d - 1
        res = mod.simulate(dates, adj, take_profit=99.0, method="pl_ladder")
        spend = [-a for _, a in res["flows"] if a < 0]
        assert spend == pytest.approx([1000.0, expected]), (d, spend)

    @pytest.mark.parametrize("r, expected",
                             [(1.08, 1000.0),   # 浮盈 8% 未触发 → 1×
                              (1.12, 500.0),    # 浮盈 12% → 0.5×
                              (1.18, 500.0),    # 浮盈 18% 仍 0.5×
                              (1.22, 0.0),      # 浮盈 22% → 0× 停投
                              (1.50, 0.0)])     # 浮盈 50% → 0× 停投
    def test_profit_tier_boundaries(self, r, expected):
        """浮盈档区间锚（pr ≥ 阈值取最高档）；0× 期完全无买入现金流。"""
        from app import dca_backtest as mod
        dates = pd.DatetimeIndex(pd.date_range("2024-01-01", periods=60,
                                               freq="D"))
        adj = np.ones(60)
        adj[31:] = r          # 2/1 起上涨，浮盈 = r - 1
        res = mod.simulate(dates, adj, take_profit=99.0, method="pl_ladder")
        spend = [-a for _, a in res["flows"] if a < 0]
        assert spend == pytest.approx([1000.0, expected] if expected
                                      else [1000.0]), (r, spend)
        assert res["invested"] == pytest.approx(1000.0 + expected)

    def test_loss_half_keys_on_position_not_price_dd(self):
        """亏损半边口径锚（与回撤加码/浮盈阶梯的本质差异）：3 月价格回撤
        -34% 已达 dd_ladder 的 ≤-20% → 2× 档与 profit_dd_ladder 深撤
        ≤-30% → 2× 档，但持仓浮亏被 2 月低位加码摊薄到 -19.3%（仅 ≥10%
        档）→ pl_ladder 只投 1.5×——档位看持仓盈亏而非价格回撤。
        （profit_dd_ladder 的 2 月是深撤 -25% → 1.5×，与 pl 的浮亏
        -25% → 2× 亦不同。）"""
        from app import dca_backtest as mod
        dates = pd.DatetimeIndex(pd.date_range("2024-01-01", periods=90,
                                               freq="D"))
        adj = np.ones(90)
        adj[31:60] = 0.75      # 2 月：浮亏 25% → 2×（deep 撤档只给 1.5×）
        adj[60:] = 0.66        # 3 月：价格回撤 -34%、持仓浮亏 -19.3% → 1.5×
        pl = mod.simulate(dates, adj, take_profit=99.0, method="pl_ladder")
        spend = [-a for _, a in pl["flows"] if a < 0]
        assert spend == pytest.approx([1000.0, 2000.0, 1500.0])
        # 同序列对照：dd_ladder（价格回撤口径）3 月给 2×、
        # profit_dd_ladder（浮盈 miss 后落深撤档）3 月给 2×
        dd = mod.simulate(dates, adj, take_profit=99.0, method="dd_ladder")
        assert [-a for _, a in dd["flows"] if a < 0] == \
            pytest.approx([1000.0, 2000.0, 2000.0])
        pdd = mod.simulate(dates, adj, take_profit=99.0,
                           method="profit_dd_ladder")
        assert [-a for _, a in pdd["flows"] if a < 0] == \
            pytest.approx([1000.0, 1500.0, 2000.0])

    def test_round_restart_skip_then_tp_then_new_round(self):
        """止盈重启全链（tp=0.20 生效）：2 月浮盈 25% → 0× 停投（当期无
        买入现金流）且同日止盈清算（停投期止盈检查照常运行，轮次投入
        =1000）；3 月新轮首买 invested=0 浮盈无定义 → 1×；4 月新轮浮亏
        15% → 1.5×。spend=[1000, 1000, 1500]、rounds=1。"""
        from app import dca_backtest as mod
        dates, adj = _round_restart_nav()
        r = mod.simulate(dates, adj, take_profit=0.20, method="pl_ladder")
        assert r["rounds"] == 1
        spend = [-a for _, a in r["flows"] if a < 0]
        assert spend == pytest.approx([1000.0, 1000.0, 1500.0])
        # 2 月停投后同日触发止盈：本轮投入 = 1000（唯一一期买入）
        assert r["round_events"][0][5] == pytest.approx(1000.0)

    def test_pl_fee_external(self):
        """外扣费（与等额/回撤档同式）：付出现金 = 1000×倍数，费率在内
        折份额——到手份额 = 现金/(1+费率)/净值，现金流记全额。"""
        from app import dca_backtest as mod
        dates, adj = _pl_nav()
        fee = mod.FeeSchedule(0.015, ())
        r = mod.simulate(dates, adj, fee, take_profit=99.0, method="pl_ladder")
        spend = [-a for _, a in r["flows"] if a < 0]
        assert spend == pytest.approx([1000.0, 1500.0, 2000.0])
        assert r["invested"] == pytest.approx(4500.0)
        shares = (1000 + 1500 / 0.85 + 2000 / 0.60) / 1.015
        assert r["final_value"] == pytest.approx(shares * 1.0)

    def test_pl_tp_round_invested_varies(self):
        """止盈轮次记可变投入（状态机与盈亏倍数共存）：1× / 1.5× / 1.5×
        三期共 4000，4 月跳涨浮盈 +210% → 0× 停投并触发止盈 → 本轮投入
        4000；同序列 dd_ladder（无浮盈停投）4 月仍按 1× 买入 1000 后止盈
        → 本轮投入 5500——浮盈 ≥20% 停投减投的差异面。"""
        from app import dca_backtest as mod
        dates = pd.DatetimeIndex(pd.date_range("2024-01-01", periods=120,
                                               freq="D"))
        adj = np.ones(120)
        adj[31:60] = 0.85      # 2 月：浮亏 15% → 1.5×
        adj[60:91] = 0.75      # 3 月：摊薄后浮亏 -17.1% → 1.5×（深撤档 2×）
        adj[91:] = 2.6         # 4 月：浮盈 +210% → 0× 停投 + 同日止盈
        r = mod.simulate(dates, adj, take_profit=0.20, method="pl_ladder")
        assert r["rounds"] == 1
        assert r["round_events"][0][5] == pytest.approx(4000.0)  # 1000+1500+1500
        dd = mod.simulate(dates, adj, take_profit=0.20, method="dd_ladder")
        assert dd["round_events"][0][5] == pytest.approx(
            5500.0)            # 1000+1500+2000+1000（4 月 1× 买入后止盈）

    def test_pl_rows_carry_method(self):
        from app import dca_backtest as mod
        nav, basic, scale_map = _inputs()
        rows, rounds = mod.run_backtest_tp(nav, basic, scale_map,
                                           method="pl_ladder")
        assert rows and all(r["method"] == "pl_ladder" for r in rows)
        assert all(t[1] == "pl_ladder" for t in rounds)


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


class TestPlLadderRoutes:
    @pytest.mark.parametrize("path", ["/api/funds/dca/tp",
                                      "/api/funds/dca/scatter"])
    def test_method_whitelist_accepts_pl_ladder(self, client, monkeypatch,
                                                path):
        """method=pl_ladder 经 DCA_METHODS 白名单放行（API 与引擎同一事实
        源，无需改 dca_api——此测试钉住新方式不回落 amount）。"""
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
        resp = client.get(path, params={"method": "pl_ladder"})
        assert resp.json()["method"] == "pl_ladder"
        assert any("pl_ladder" in (p or ()) for p in conn.params)


class TestPlLadderEngineVersion:
    def test_pl_constants_in_version_material(self):
        """盈亏阶梯常量纳入引擎版本串（改档位不 bump base 也自动全量——
        防脏数据不变量；见 engine_version docstring「新增口径常量须同步
        补进」）。"""
        from app import dca_backtest as mod
        old = mod.engine_version()
        saved = mod.PL_LOSS_LADDER
        try:
            mod.PL_LOSS_LADDER = ((-0.10, 1.5), (-0.20, 2.0), (-0.30, 3.0))
            assert mod.engine_version() != old
        finally:
            mod.PL_LOSS_LADDER = saved
        assert mod.engine_version() == old
