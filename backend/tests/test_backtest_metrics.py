"""绩效指标与撮合成本测试：合成净值手算精确值；成本模型按 A 股费率结构。

指标契约（metrics.py，全部自算不依赖回测库）：
- total_return：期末/期初 - 1
- annual_return：按自然日年化（(1+total)^(365/天数) - 1）
- max_drawdown：净值峰谷最大回撤（正数百分比）
- sharpe：日收益均值/日收益标准差 × sqrt(252)（无风险利率 0）
- win_rate / profit_loss_ratio：按平仓交易统计
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.backtest.metrics import (  # noqa: E402
    max_drawdown, perf_metrics, trade_stats)


def eq(values, start="2026-01-01"):
    idx = pd.bdate_range(start, periods=len(values))
    return pd.Series(values, index=idx)


class TestMaxDrawdown:
    def test_known_drawdown(self):
        """100 → 120 → 60 → 90：峰谷回撤 (120-60)/120 = 50%。"""
        dd = max_drawdown(eq([1.0, 1.2, 0.6, 0.9]))
        assert dd == pytest.approx(0.5)

    def test_monotonic_up_no_drawdown(self):
        assert max_drawdown(eq([1.0, 1.1, 1.2])) == 0.0


class TestPerfMetrics:
    def test_one_year_known_case(self):
        """整一年净值 1.0 → 1.26：年化 26%；日收益恒定 → 夏普可手算。"""
        idx = pd.bdate_range("2026-01-01", periods=252)   # 一年交易日
        net = pd.Series(np.linspace(1.0, 1.26, 252), index=idx)
        m = perf_metrics(net)
        assert m["total_return"] == pytest.approx(0.26, abs=1e-6)
        assert m["annual_return"] == pytest.approx(0.26, abs=0.02)
        assert m["max_drawdown"] == pytest.approx(0.0)     # 线性上涨无回撤
        assert m["sharpe"] > 1                             # 稳定上涨夏普为正
        assert m["vol_days"] == (idx[-1] - idx[0]).days    # 自然日跨度

    def test_zero_volatility_sharpe_is_zero_not_error(self):
        m = perf_metrics(eq([1.0, 1.0, 1.0]))
        assert m["sharpe"] == 0.0                          # 除零防护
        assert m["total_return"] == 0.0


class TestTradeStats:
    def test_win_rate_and_pl_ratio(self):
        """交易收益序列：+10%、-5%、+20%、-10% → 胜率 50%，盈亏比 15/7.5=2。"""
        s = trade_stats([0.10, -0.05, 0.20, -0.10])
        assert s["trades"] == 4
        assert s["win_rate"] == pytest.approx(0.5)
        assert s["profit_loss_ratio"] == pytest.approx(0.15 / 0.075)

    def test_no_trades_zeroes(self):
        s = trade_stats([])
        assert s["trades"] == 0 and s["win_rate"] == 0.0 and s["profit_loss_ratio"] == 0.0

    def test_all_wins_pl_ratio_infinite_guard(self):
        s = trade_stats([0.1, 0.2])
        assert s["win_rate"] == 1.0
        assert s["profit_loss_ratio"] == 0.0               # 无亏损交易时不给 inf


class TestCostModel:
    def test_round_trip_commission_and_sell_tax(self):
        """A 股费率结构：买入佣金；卖出佣金 + 印花税（无最低保护时按比例）。"""
        from app.backtest.metrics import round_trip_cost
        cost = round_trip_cost(value=10_000, commission=2.5e-4, sell_tax=5e-4,
                               min_commission=0.0)
        buy = 10_000 * 2.5e-4                               # 2.5
        sell_amount = 10_000 - buy
        sell = sell_amount * (2.5e-4 + 5e-4)
        assert cost == pytest.approx(2.5 + sell_amount * 7.5e-4, abs=1e-6)

    def test_commission_floor(self):
        """最低佣金 5 元（行业惯例）——小额交易双边按 5 元起收。"""
        from app.backtest.metrics import round_trip_cost
        cost = round_trip_cost(value=1_000, commission=2.5e-4, sell_tax=5e-4,
                               min_commission=5.0)
        assert cost == pytest.approx(5 + 5 + (1000 - 5) * 5e-4, abs=1e-6)
