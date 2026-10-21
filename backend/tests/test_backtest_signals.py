"""策略信号层测试：6 个经典策略的信号生成，全用合成数据手算精确值（纯 pandas）。

教学契约：信号 = 当日收盘判定（True/False 序列）；撮合层（engine）负责
按 exec_mode 把信号映射到当日收盘价或次日开盘价成交——信号层绝不前视。
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.backtest.signals import (  # noqa: E402
    bollinger_signals, donchian_signals, grid_target,
    ma_cross_signals, momentum_weights)


def close_series(values, start="2026-01-01"):
    idx = pd.bdate_range(start, periods=len(values))
    return pd.Series(values, index=idx, name="600519")


class TestMaCross:
    def test_golden_cross_and_death_cross(self):
        """先跌后涨再跌：MA3 上穿 MA6 → 金叉；下穿 → 死叉。"""
        # 手工构造：MA3 与 MA6 在中段金叉、尾段死叉
        vals = [10, 9.8, 9.6, 9.4, 9.2, 9.0,          # 下跌段
                9.2, 9.6, 10.2, 11.0, 11.8, 12.6,      # 上涨段（金叉出现）
                12.0, 11.0, 10.0, 9.0, 8.0, 7.0]       # 下跌段（死叉出现）
        close = close_series(vals)
        entries, exits = ma_cross_signals(close, fast=3, slow=6)
        assert entries.sum() >= 1 and exits.sum() >= 1
        # 金叉日：MA3 从下方穿越 MA6 的当天，信号为 True 且只发生在穿越日
        ma3, ma6 = close.rolling(3).mean(), close.rolling(6).mean()
        cross_up = (ma3 > ma6) & ~(ma3 > ma6).shift(1, fill_value=False)
        cross_dn = (ma3 < ma6) & ~(ma3 < ma6).shift(1, fill_value=False)
        pd.testing.assert_series_equal(entries, cross_up.fillna(False))
        pd.testing.assert_series_equal(exits, cross_dn.fillna(False))

    def test_no_signal_before_slow_window(self):
        """慢窗口未满前不得有任何信号（无前视）。"""
        close = close_series([10, 10.1, 10.2, 10.1, 10.3])
        entries, exits = ma_cross_signals(close, fast=2, slow=4)
        assert not entries.iloc[:3].any() and not exits.iloc[:3].any()


class TestDonchian:
    def test_breakout_entry_and_exit(self):
        """构造前 5 日高点 10.0 → 第 6 日收 10.5 突破买入；
        后续跌破 3 日低点 → 卖出。"""
        highs = [9.5, 9.8, 9.6, 9.9, 10.0]
        lows = [9.0, 9.2, 9.1, 9.3, 9.4]
        closes = [9.2, 9.5, 9.3, 9.6, 9.8]
        # 第 6 日大阳线突破前高
        highs.append(10.6); lows.append(10.0); closes.append(10.5)
        # 随后三根下跌并跌破 3 日低点
        for h, l, c in [(10.4, 10.1, 10.2), (10.0, 9.6, 9.7), (9.6, 9.0, 9.1)]:
            highs.append(h); lows.append(l); closes.append(c)
        idx = pd.bdate_range("2026-01-01", periods=len(closes))
        high, low, close = (pd.Series(v, index=idx) for v in (highs, lows, closes))

        entries, exits = donchian_signals(high, low, close, entry_n=5, exit_n=3)
        assert bool(entries.iloc[5]) is True          # 第 6 日突破 5 日高
        assert bool(exits.iloc[-1]) is True           # 末日跌破 3 日低
        assert not entries.iloc[:5].any()             # 窗口不满不入场

    def test_prior_window_excludes_current_bar(self):
        """通道用「不含当日」的前 N 日高低——当日新高不算自我突破。"""
        highs = [10.0] * 6
        lows = [9.0] * 6
        closes = [9.5] * 6
        idx = pd.bdate_range("2026-01-01", periods=6)
        entries, _ = donchian_signals(pd.Series(highs, index=idx),
                                      pd.Series(lows, index=idx),
                                      pd.Series(closes, index=idx),
                                      entry_n=5, exit_n=3)
        assert not entries.any()                      # 一直贴着前高 10.0，未突破


class TestBollinger:
    def test_lower_band_touch_enters_upper_exit(self):
        """跌至均线下 k 倍标准差 → 买入；回升至均线 → 卖出。"""
        vals = [10] * 10 + [9.0, 8.5, 9.0, 10.0, 10.0, 10.0]
        close = close_series(vals)
        entries, exits = bollinger_signals(close, window=10, alpha=2.0)
        ma = close.rolling(10).mean()
        sd = close.rolling(10).std()
        touched = close < ma - 2.0 * sd
        assert bool(entries[touched].idxmax() == entries.idxmax()) is True
        assert entries.any()
        # 均值回归后回到均线上方 → 卖出信号出现在买入之后
        assert exits.any()
        assert exits.idxmax() > entries.idxmin()

    def test_flat_series_no_signals(self):
        """恒定价格序列 std=0、触及下轨判定必须为 False（除零防护）。"""
        close = close_series([10.0] * 15)
        entries, exits = bollinger_signals(close, window=10, alpha=2.0)
        assert not entries.any()


class TestGrid:
    def test_levels_trigger_buys_then_sells(self):
        """基准 10 元、步长 5%：跌破 9.5 买 1 档、9.0 买 2 档；反弹回 10+ 卖出。
        目标仓位序列 = 档位数 × 每档权重。"""
        close = close_series([10.0, 9.8, 9.4, 8.9, 9.6, 10.2, 10.1])
        target = grid_target(close, step_pct=5.0, levels=4)
        assert isinstance(target, pd.Series)
        assert target.iloc[0] == 0.0                  # 基准日空仓
        # 9.4（-6%）→ 1 档；8.9（-11%）→ 2 档；10.2（+2%）→ 0 档
        assert target.iloc[2] == pytest.approx(1 / 4)
        assert target.iloc[3] == pytest.approx(2 / 4)
        assert target.iloc[5] == 0.0

    def test_upside_beyond_base_stays_flat(self):
        """涨过基准不追高（网格只低买高卖到基准）。"""
        close = close_series([10.0, 11.0, 12.0])
        target = grid_target(close, step_pct=5.0, levels=4)
        assert (target == 0).all()


class TestMomentum:
    def test_top_k_selection_and_rebalance_dates(self):
        """3 标的：月末调仓，买过去 N 日涨幅前 2 名、等权。"""
        idx = pd.bdate_range("2026-01-01", periods=40)
        a = np.linspace(10, 12, 40)                   # 涨 20% 最强
        b = np.linspace(10, 11, 40)                   # 涨 10%
        c = np.linspace(10, 9, 40)                    # 跌 10% 最弱
        close = pd.DataFrame({"000001": a, "000002": b, "000003": c}, index=idx)
        weights = momentum_weights(close, window=20, top_k=2)
        # 仅月末调仓行有权重（其他行为 NaN = 不调仓）
        rebalance_rows = weights.dropna(how="all")
        assert 1 <= len(rebalance_rows) <= len(idx[20:])
        last = rebalance_rows.iloc[-1].dropna()
        assert set(last.index) == {"000001", "000002"}   # 弱者被排除
        assert last.sum() == pytest.approx(1.0)          # 等权归一
        assert last["000001"] == last["000002"] == pytest.approx(0.5)

    def test_window_shorter_than_history_required(self):
        close = pd.DataFrame({"000001": np.linspace(10, 11, 5)})
        weights = momentum_weights(close, window=20, top_k=1)
        assert weights.dropna(how="all").empty        # 窗口不满 → 无调仓


class TestBuyHold:
    def test_buy_hold_is_full_position_from_start(self):
        """买入持有基准：首日即满仓、持有到期末（对照基准）。"""
        from app.backtest.signals import buy_hold_target
        close = close_series([10.0, 10.5, 11.0])
        target = buy_hold_target(close)
        assert (target == 1.0).all()


class TestMomentumRebalanceFreq:
    """四期：动量轮动调仓频率可配（month 缺省 / week）。"""

    def _close(self, days=60):
        idx = pd.bdate_range("2026-01-01", periods=days)
        return pd.DataFrame({"000001": np.linspace(10, 12, days),
                             "000002": np.linspace(10, 11, days),
                             "000003": np.linspace(10, 9, days)}, index=idx)

    def test_default_is_monthly(self):
        """缺省 rebalance：行为与旧版（月末调仓）一致。"""
        close = self._close()
        default = momentum_weights(close, window=20, top_k=2)
        explicit = momentum_weights(close, window=20, top_k=2,
                                    rebalance="month")
        pd.testing.assert_frame_equal(default, explicit)

    def test_weekly_rebalances_more_often_than_monthly(self):
        """60 个交易日（约 3 个月）：周度调仓行数严格多于月度。"""
        close = self._close()
        n_month = momentum_weights(close, window=20, top_k=2
                                   ).dropna(how="all").shape[0]
        n_week = momentum_weights(close, window=20, top_k=2,
                                  rebalance="week").dropna(how="all").shape[0]
        assert n_month >= 1 and n_week > n_month >= 2

    def test_weekly_picks_strongest_still_excludes_loser(self):
        """周度调仓同样只买强者：末次调仓行排除最弱标的。"""
        close = self._close()
        w = momentum_weights(close, window=20, top_k=2, rebalance="week")
        last = w.dropna(how="all").iloc[-1].dropna()
        assert set(last.index) == {"000001", "000002"}
        assert last.sum() == pytest.approx(1.0)

    def test_unknown_freq_falls_back_to_monthly(self):
        """非法频率值：按月度处理（不抛错——对 UI/MCP 乱输入鲁棒）。"""
        close = self._close()
        pd.testing.assert_frame_equal(
            momentum_weights(close, window=20, top_k=2, rebalance="day"),
            momentum_weights(close, window=20, top_k=2, rebalance="month"))
