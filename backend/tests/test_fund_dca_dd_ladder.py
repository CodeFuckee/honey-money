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


def _profit_nav():
    """浮盈阶梯主序列（4 个自然月，tp=99 禁触发下手算全分支）：
    1 月平（首买 invested=0 → 回撤档 dd=0 → 1×）、2 月涨 25%（浮盈 25%
    → 0.25×）、3 月小回（浮盈 15.2% → 0.5×）、4 月回落至峰值 -28%
    （浮盈 -16.9% 转负 → 落回撤档 -28% → 1.5×）。

    月度买入日：1/1（a=1.0）、2/1（a=1.25）、3/1（a=1.20）、4/1（a=0.90）。
    """
    dates = pd.DatetimeIndex(pd.date_range("2024-01-01", periods=120, freq="D"))
    adj = np.ones(120)
    adj[31:60] = 1.25      # 2 月：新高（dd=0），浮盈 +25%
    adj[60:91] = 1.20      # 3 月：较峰值 -4%，浮盈 +15.2%
    adj[91:] = 0.90        # 4 月：较峰值 -28%，浮盈 -16.9%
    return dates, adj


def _precedence_nav():
    """优先级序列：2 月新高 2.0（浮盈 100% → 0.25×）、3 月回落 1.5
    ——较峰值 -25% 触发回撤档 1.5×，但浮盈仍有 35% ≥20%：浮盈减码优先
    → 仍 0.25×（回撤优先的错误实现会取 1.5×）。

    月度买入日：1/1（a=1.0）、2/1（a=2.0）、3/1（a=1.5）。
    """
    dates = pd.DatetimeIndex(pd.date_range("2024-01-01", periods=90, freq="D"))
    adj = np.ones(90)
    adj[31:60] = 2.0       # 2 月：新高 2.0
    adj[60:] = 1.5         # 3 月：较峰值 -25%、浮盈 +35%
    return dates, adj


def _round_restart_nav():
    """止盈重启序列（tp=0.20 生效）：1 月平 1×；2 月跳涨 2.0（0.25× 买入
    后同日触发止盈清算，invested 归零）；3 月 1.5（较峰值 -25%、新轮
    invested=0 浮盈无定义）→ 首买落回撤档 1.5×。

    月度买入日：1/1（a=1.0）、2/1（a=2.0）、3/1（a=1.5）。
    """
    dates = pd.DatetimeIndex(pd.date_range("2024-01-01", periods=120, freq="D"))
    adj = np.ones(120)
    adj[31:60] = 2.0       # 2 月：新高 2.0，2/1 买入后立即止盈
    adj[60:] = 1.5         # 3 月起：较峰值 -25%
    return dates, adj


class TestSimulateDdLadder:
    def test_methods_pin_values(self):
        """定投方式口径锚：六档（四轮 dd_ladder、六轮 dd_ladder_deep、
        七轮 profit_dd_ladder 浮盈阶梯、八轮 pl_ladder 盈亏阶梯，见
        test_fund_dca_pl_ladder.py）。"""
        from app.dca_backtest import DCA_METHODS
        assert DCA_METHODS == ("amount", "share", "dd_ladder",
                               "dd_ladder_deep", "profit_dd_ladder",
                               "pl_ladder")

    def test_ladder_constant_pinned(self):
        """档位契约：-10% → 1.5×、-20% → 2×（防悄悄改坏）。"""
        from app.dca_backtest import DD_LADDER
        assert DD_LADDER == ((-0.10, 1.5), (-0.20, 2.0))

    def test_ladder_deep_constant_pinned(self):
        """深梯度档位契约：-20% → 1.5×、-30% → 2×（2026-11-23 用户定案）；
        profit_dd_ladder 的回撤半边复用深梯度表（七轮）。"""
        from app.dca_backtest import DD_LADDER_DEEP, DD_LADDERS
        assert DD_LADDER_DEEP == ((-0.20, 1.5), (-0.30, 2.0))
        assert DD_LADDERS == {"dd_ladder": ((-0.10, 1.5), (-0.20, 2.0)),
                              "dd_ladder_deep": ((-0.20, 1.5), (-0.30, 2.0)),
                              "profit_dd_ladder": ((-0.20, 1.5), (-0.30, 2.0))}

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


class TestSimulateDdLadderDeep:
    """深回撤加码（dd_ladder_deep）：档位 (-20% → 1.5×, -30% → 2×)，
    分派/状态机与 dd_ladder 共用，仅档位表不同（2026-11-23 用户定案）。"""

    def test_deep_flat_nav_equals_amount(self):
        """恒定净值：无回撤恒 1× → dd_ladder_deep ≡ amount 逐字段一致。"""
        from app import dca_backtest as mod
        dates = pd.DatetimeIndex(pd.date_range("2024-01-01", periods=400,
                                               freq="D"))
        adj = np.ones(400)
        for fee in (None, mod.FeeSchedule(0.015, mod.REDEEM_EQUITY)):
            amt = mod.simulate(dates, adj, fee, take_profit=0.20)
            deep = mod.simulate(dates, adj, fee, take_profit=0.20,
                                method="dd_ladder_deep")
            assert deep == amt

    def test_deep_hand_computed(self):
        """手算（同 _ladder_nav 4 期）：deep 梯度下 2 月 -12% 未达 -20% 归 1×、
        3 月 -25% 命中 1.5× → 投入 1000+1000+1500+1000 = 4500（标准梯度同
        序列为 5500——浅跌不追投的差异面）。"""
        from app import dca_backtest as mod
        dates, adj = _ladder_nav()
        deep = mod.simulate(dates, adj, take_profit=99.0,
                            method="dd_ladder_deep")
        spend = [-a for _, a in deep["flows"] if a < 0]
        assert spend == pytest.approx([1000.0, 1000.0, 1500.0, 1000.0])
        assert deep["invested"] == pytest.approx(4500.0)
        shares = 1000 + 1000 / 0.88 + 1500 / 0.75 + 1000
        assert deep["final_value"] == pytest.approx(shares * 1.0)
        # 同序列标准梯度 5500：两种梯度在同一数据上可分辨
        std = mod.simulate(dates, adj, take_profit=99.0, method="dd_ladder")
        assert std["invested"] == pytest.approx(5500.0)

    def test_deep_threshold_boundaries(self):
        """档位区间（dd ≤ 阈值 取最深匹配档）：-19% → 1×、-21% → 1.5×、
        -29% → 1.5×、-31% → 2×。不取恰 -20%/-30% 净值——0.80/1-1 =
        -0.1999…（浮点表示方向不定），恰阈值行为属浮点序判定，真实
        净值恰等阈值概率为零，档位语义用区间值锚定。"""
        from app import dca_backtest as mod
        for drop, expected in ((0.81, 1000.0),   # -19% 未达深档
                               (0.79, 1500.0),   # -21% → 1.5×
                               (0.71, 1500.0),   # -29% 仍 1.5×
                               (0.69, 2000.0)):  # -31% → 2×
            dates = pd.DatetimeIndex(pd.date_range("2024-01-01", periods=60,
                                                   freq="D"))
            adj = np.ones(60)
            adj[31:] = drop       # 2/1 起回撤
            r = mod.simulate(dates, adj, take_profit=99.0,
                             method="dd_ladder_deep")
            spend = [-a for _, a in r["flows"] if a < 0]
            assert spend == pytest.approx([1000.0, expected]), (drop, spend)

    def test_deep_tp_round_invested_varies(self):
        """止盈轮次记可变投入：1 月 1×、2/3 月回撤 -25% 各 1.5×、4 月新高
        1× 后触发 → 本轮投入 5000（-15% 的浅跌在 deep 下归 1×，与标准
        梯度区分）。"""
        from app import dca_backtest as mod
        dates = pd.DatetimeIndex(pd.date_range("2024-01-01", periods=120,
                                               freq="D"))
        adj = np.ones(120)
        adj[31:91] = 0.75      # 2~3 月回撤 -25%（至 3/31）
        adj[91:] = 2.0         # 4/1 新高：当日买入后触发止盈
        r = mod.simulate(dates, adj, take_profit=0.20,
                         method="dd_ladder_deep")
        assert r["rounds"] == 1
        assert r["round_events"][0][5] == pytest.approx(5000.0)  # 1000+1500+1500+1000

    def test_deep_rows_carry_method(self):
        from app import dca_backtest as mod
        nav, basic, scale_map = _inputs()
        rows, rounds = mod.run_backtest_tp(nav, basic, scale_map,
                                           method="dd_ladder_deep")
        assert rows and all(r["method"] == "dd_ladder_deep" for r in rows)
        assert all(t[1] == "dd_ladder_deep" for t in rounds)


class TestSimulateProfitDdLadder:
    """浮盈阶梯（profit_dd_ladder，2026-11-24 七轮用户定案）：
    浮盈档优先——当轮浮盈 ≥10% 投 0.5×、≥20% 投 0.25×；未触发浮盈档时
    落深回撤加码档（≥20% → 1.5×、≥30% → 2×），余者 1×。浮盈 = 当轮
    持仓市值/已投入 - 1（与止盈触发线同口径）；新轮首买 invested=0 浮盈
    无定义 → 直接看回撤档；回撤 = adj/历史最高（含当日）- 1（纯净值口径，
    跨轮不重置，与 dd_ladder_deep 完全同源）。"""

    def test_profit_constants_pinned(self):
        """档位契约：浮盈 (10% → 0.5×, 20% → 0.25×) 与 PROFIT_LADDERS 映射
        （防悄悄改坏；方向是 ≥ 取最高浮盈档，与回撤 ≤ 取最深档方向相反）。
        八轮 pl_ladder 复用该分派、档位不同（见 test_fund_dca_pl_ladder）。"""
        from app.dca_backtest import PROFIT_LADDER, PROFIT_LADDERS
        assert PROFIT_LADDER == ((0.10, 0.5), (0.20, 0.25))
        assert PROFIT_LADDERS["profit_dd_ladder"] == \
            ((0.10, 0.5), (0.20, 0.25))
        assert PROFIT_LADDERS["pl_ladder"] == ((0.10, 0.5), (0.20, 0.0))

    def test_profit_flat_nav_equals_amount(self):
        """恒定净值：浮盈恒 0 未触发浮盈档、回撤恒 0 → profit_dd_ladder
        ≡ amount 逐字段一致（gross+fee 双口径）。"""
        from app import dca_backtest as mod
        dates = pd.DatetimeIndex(pd.date_range("2024-01-01", periods=400,
                                               freq="D"))
        adj = np.ones(400)
        for fee in (None, mod.FeeSchedule(0.015, mod.REDEEM_EQUITY)):
            amt = mod.simulate(dates, adj, fee, take_profit=0.20)
            pl = mod.simulate(dates, adj, fee, take_profit=0.20,
                              method="profit_dd_ladder")
            assert pl == amt

    def test_profit_hand_computed(self):
        """手算（_profit_nav 4 期全分支）：1× / 0.25× / 0.5× / 1.5×
        → 投入 1000+250+500+1500 = 3250；期末份额 3283.33×0.9 = 2955。
        同序列深回撤加码（无浮盈减码）投入 4500——两方式同数据可分辨。"""
        from app import dca_backtest as mod
        dates, adj = _profit_nav()
        r = mod.simulate(dates, adj, take_profit=99.0,
                         method="profit_dd_ladder")
        assert r["rounds"] == 0
        spend = [-a for _, a in r["flows"] if a < 0]
        assert spend == pytest.approx([1000.0, 250.0, 500.0, 1500.0])
        assert r["invested"] == pytest.approx(3250.0)
        shares = 1000 + 250 / 1.25 + 500 / 1.2 + 1500 / 0.9
        assert r["final_value"] == pytest.approx(shares * 0.9)
        assert r["profit_rate"] == pytest.approx(shares * 0.9 / 3250.0 - 1.0)
        deep = mod.simulate(dates, adj, take_profit=99.0,
                            method="dd_ladder_deep")
        assert deep["invested"] == pytest.approx(4500.0)

    def test_profit_precedence_over_dd(self):
        """优先级锚（用户定案：浮盈减码优先）：3/1 较峰值 -25% 触发回撤档
        1.5×，但浮盈 35% ≥20% → 仍 0.25×（回撤优先的错误实现取 1500）。"""
        from app import dca_backtest as mod
        dates, adj = _precedence_nav()
        r = mod.simulate(dates, adj, take_profit=99.0,
                         method="profit_dd_ladder")
        spend = [-a for _, a in r["flows"] if a < 0]
        assert spend == pytest.approx([1000.0, 250.0, 250.0])
        assert r["invested"] == pytest.approx(1500.0)

    @pytest.mark.parametrize("r, expected",
                             [(1.12, 500.0),    # 浮盈 12% → 0.5×
                              (1.18, 500.0),    # 浮盈 18% 仍 0.5×
                              (1.22, 250.0),    # 浮盈 22% → 0.25×（取最高档）
                              (1.08, 1000.0)])  # 浮盈 8% 未触发 → 回撤 0 → 1×
    def test_profit_tier_boundaries(self, r, expected):
        """浮盈档区间锚（pr ≥ 阈值取最高档）：不取恰 +10%/+20% 净值——
        浮点表示方向不定，档位语义用区间值锚定（与回撤档同规）。"""
        from app import dca_backtest as mod
        dates = pd.DatetimeIndex(pd.date_range("2024-01-01", periods=60,
                                               freq="D"))
        adj = np.ones(60)
        adj[31:] = r          # 2/1 起上涨，浮盈 = r - 1
        res = mod.simulate(dates, adj, take_profit=99.0,
                           method="profit_dd_ladder")
        spend = [-a for _, a in res["flows"] if a < 0]
        assert spend == pytest.approx([1000.0, expected]), (r, spend)

    @pytest.mark.parametrize("d, expected",
                             [(0.81, 1000.0),   # 浮盈 -19% → 回撤 -19% → 1×
                              (0.79, 1500.0),   # 回撤 -21% → 1.5×
                              (0.71, 1500.0),   # 回撤 -29% 仍 1.5×
                              (0.69, 2000.0)])  # 回撤 -31% → 2×
    def test_dd_fallthrough_boundaries(self, d, expected):
        """浮盈转负落回撤档的区间锚（invested>0、浮盈未触发 → 深梯度表）：
        与 dd_ladder_deep 阈值测试同数值，但走的是浮盈档 miss 后的回撤路径。"""
        from app import dca_backtest as mod
        dates = pd.DatetimeIndex(pd.date_range("2024-01-01", periods=60,
                                               freq="D"))
        adj = np.ones(60)
        adj[31:] = d          # 2/1 起回撤
        res = mod.simulate(dates, adj, take_profit=99.0,
                           method="profit_dd_ladder")
        spend = [-a for _, a in res["flows"] if a < 0]
        assert spend == pytest.approx([1000.0, expected]), (d, spend)

    def test_round_restart_first_buy_uses_dd(self):
        """新轮首买 invested=0 浮盈无定义 → 直接看回撤档：3/1 较峰值 -25%
        → 1.5×（既不除零、也不归 1×——错误实现两者必居其一）。4/1 浮盈 0
        未触发浮盈档 → 回撤档仍 1.5×。"""
        from app import dca_backtest as mod
        dates, adj = _round_restart_nav()
        r = mod.simulate(dates, adj, take_profit=0.20,
                         method="profit_dd_ladder")
        assert r["rounds"] == 1
        spend = [-a for _, a in r["flows"] if a < 0]
        assert spend == pytest.approx([1000.0, 250.0, 1500.0, 1500.0])
        # 2/1 买入（0.25×）后同日触发止盈：本轮投入 = 1000 + 250
        assert r["round_events"][0][5] == pytest.approx(1250.0)

    def test_profit_tp_round_invested_varies(self):
        """止盈轮次记可变投入（状态机与浮盈倍数共存）：浅跌 -15% 归 1×
        三期 1000、4 月跳涨浮盈 123% → 0.25× 投 250 → 本轮投入 3250
        （同序列 dd_ladder_deep 为 5000——浮盈减码的差异面）。"""
        from app import dca_backtest as mod
        dates = pd.DatetimeIndex(pd.date_range("2024-01-01", periods=120,
                                               freq="D"))
        adj = np.ones(120)
        adj[31:91] = 0.85      # 2~3 月浅跌 -15%（浮盈转负、回撤未达 -20% → 1×）
        adj[91:] = 2.0         # 4/1 新高：0.25× 买入后触发止盈
        r = mod.simulate(dates, adj, take_profit=0.20,
                         method="profit_dd_ladder")
        assert r["rounds"] == 1
        assert r["round_events"][0][5] == pytest.approx(3250.0)  # 1000×3+250

    def test_profit_fee_external(self):
        """外扣费（与等额/回撤档同式）：付出现金 = 1000×倍数，费率在内
        折份额——到手份额 = 现金/(1+费率)/净值，现金流记全额。"""
        from app import dca_backtest as mod
        dates, adj = _profit_nav()
        fee = mod.FeeSchedule(0.015, ())
        r = mod.simulate(dates, adj, fee, take_profit=99.0,
                         method="profit_dd_ladder")
        spend = [-a for _, a in r["flows"] if a < 0]
        assert spend == pytest.approx([1000.0, 250.0, 500.0, 1500.0])
        assert r["invested"] == pytest.approx(3250.0)
        shares = (1000 + 250 / 1.25 + 500 / 1.2 + 1500 / 0.9) / 1.015
        assert r["final_value"] == pytest.approx(shares * 0.9)

    def test_profit_rows_carry_method(self):
        from app import dca_backtest as mod
        nav, basic, scale_map = _inputs()
        rows, rounds = mod.run_backtest_tp(nav, basic, scale_map,
                                           method="profit_dd_ladder")
        assert rows and all(r["method"] == "profit_dd_ladder" for r in rows)
        assert all(t[1] == "profit_dd_ladder" for t in rounds)


class TestRunBacktestDdLadder:
    def test_rows_carry_method(self):
        from app import dca_backtest as mod
        nav, basic, scale_map = _inputs()
        rows, rounds = mod.run_backtest_tp(nav, basic, scale_map,
                                           method="dd_ladder")
        assert rows and all(r["method"] == "dd_ladder" for r in rows)
        assert all(t[1] == "dd_ladder" for t in rounds)

    def test_full_backtest_covers_five_methods(self, monkeypatch):
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
    @pytest.mark.parametrize("method", ["dd_ladder", "dd_ladder_deep",
                                        "profit_dd_ladder"])
    def test_method_whitelist_accepts_dd_ladder(self, client, monkeypatch,
                                                method):
        import app.dca_api as dca_mod
        conn = _FakeConnOne((None, None, None))
        _patch_connect(monkeypatch, dca_mod, conn)
        resp = client.get("/api/funds/dca/tp", params={"method": method})
        assert resp.json()["method"] == method
        assert any(method in (p or ()) for p in conn.params)

    @pytest.mark.parametrize("method", ["dd_ladder", "dd_ladder_deep",
                                        "profit_dd_ladder"])
    def test_scatter_accepts_dd_ladder(self, client, monkeypatch, method):
        import app.dca_api as dca_mod
        conn = _FakeConnOne((None,), rows=[])
        _patch_connect(monkeypatch, dca_mod, conn)
        resp = client.get("/api/funds/dca/scatter", params={"method": method})
        assert resp.json()["method"] == method
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
