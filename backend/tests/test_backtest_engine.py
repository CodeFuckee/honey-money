"""vectorbt 撮合引擎测试：合成数据断言成交价/成本/基准对照（需 vectorbt）。

撮合契约：
- exec_mode="close"：信号当日收盘成交（教学：未来函数作弊模式）
- exec_mode="next_open"：信号次日开盘成交（正确模式）
- 成本：买入佣金；卖出佣金 + 印花税；最低佣金保护
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

vbt = pytest.importorskip("vectorbt")

from app.backtest.engine import run_backtest  # noqa: E402


def make_bars(n=10, start="2026-01-01", price=10.0):
    """上行序列：close 每日 +0.1，open 恒低开 0.05（两撮合模式有价差可断言）。"""
    idx = pd.bdate_range(start, periods=n)
    close = pd.Series([price + 0.1 * i for i in range(n)], index=idx)
    open_ = close - 0.05
    return {
        "close": close.to_frame("600519"),
        "open": open_.to_frame("600519"),
        "high": (close + 0.2).to_frame("600519"),
        "low": (close - 0.2).to_frame("600519"),
        "volume": pd.Series([1e6] * n, index=idx).to_frame("600519"),
    }


class TestExecModes:
    def test_next_open_executes_not_on_signal_day(self):
        """次日开盘模式：信号日不成交，次日开盘买入。"""
        bars = make_bars()
        result = run_backtest(
            bars, entries=bars["close"].notna(), exits=bars["close"].isna(),
            exec_mode="next_open", commission=0.0, sell_tax=0.0)
        assert result["equity"][0][1] == pytest.approx(1.0, abs=1e-6)  # 信号日仍是现金

    def test_close_mode_buys_same_day_close(self):
        """当日收盘模式（未来函数教学）：信号日收盘即成交。
        上行 + 低开序列：当日收盘买入比次日开盘便宜 → 作弊收益更高。"""
        bars = make_bars()
        entries = pd.Series([True] + [False] * 9, index=bars["close"].index)
        exits = pd.Series([False] * 7 + [True] + [False] * 2, index=bars["close"].index)
        r_close = run_backtest(bars, entries=entries, exits=exits, exec_mode="close",
                               commission=0.0, sell_tax=0.0)
        r_open = run_backtest(bars, entries=entries, exits=exits, exec_mode="next_open",
                              commission=0.0, sell_tax=0.0)
        m_close = r_close["metrics"]["total_return"]
        m_open = r_open["metrics"]["total_return"]
        assert m_close > m_open, f"收盘模式应跑赢（未来函数红利）: {m_close} vs {m_open}"

    def test_costs_reduce_returns(self):
        """费用越高净收益越低；印花税只收卖出。"""
        bars = make_bars()
        entries = pd.Series([True] + [False] * 9, index=bars["close"].index)
        exits = pd.Series([False] * 9 + [True], index=bars["close"].index)
        r0 = run_backtest(bars, entries=entries, exits=exits, exec_mode="next_open",
                          commission=0.0, sell_tax=0.0)
        r1 = run_backtest(bars, entries=entries, exits=exits, exec_mode="next_open",
                          commission=2.5e-4, sell_tax=5e-4)
        assert r1["metrics"]["total_return"] < r0["metrics"]["total_return"]
        assert r0["metrics"]["total_return"] - r1["metrics"]["total_return"] > 0


class TestOutputShape:
    def test_metrics_and_series_shapes(self):
        bars = make_bars(n=30)
        result = run_backtest(
            bars, entries=pd.Series([True] + [False] * 29, index=bars["close"].index),
            exits=pd.Series([False] * 29 + [True], index=bars["close"].index),
            exec_mode="next_open", commission=2.5e-4, sell_tax=5e-4)
        m = result["metrics"]
        for key in ("total_return", "annual_return", "max_drawdown", "sharpe",
                    "win_rate", "profit_loss_ratio", "trades"):
            assert key in m, f"缺指标 {key}"
        assert len(result["equity"]) == 30                     # 每日一行
        assert all(len(row) == 3 for row in result["equity"])  # (date, 策略, 基准)
        assert result["gap_dates"] == []                       # 平稳序列无跳空


class TestGapDetection:
    def test_overnight_gap_flagged(self):
        """除权假跳空检测：隔夜 -15%（超过跌停）→ 标记疑似除权日。"""
        bars = make_bars(n=6)
        # 第 3 天 open 人为砸出 -15% 跳空（前收 10.2 → open 8.6）
        bars["open"].iloc[3] = 8.6
        result = run_backtest(
            bars, entries=pd.Series([True] + [False] * 5, index=bars["close"].index),
            exits=pd.Series([False] * 5 + [True], index=bars["close"].index),
            exec_mode="next_open", commission=0.0, sell_tax=0.0)
        assert len(result["gap_dates"]) == 1


class TestTargetAndWeightsPaths:
    """回归（2026-10-10 用户报 买入持有 全 0）：target/weights 路径的无名
    Series 列名 0 被 reindex 整列丢弃 → 零成交。修复后三条输入路径都必须真实成交。
    此前的引擎测试只覆盖了 entries/exits 信号路径，是本次事故的测试盲区。"""

    def test_buy_hold_target_trades_and_tracks_benchmark(self):
        from app.backtest.signals import buy_hold_target
        bars = make_bars()
        target = buy_hold_target(bars["close"].iloc[:, 0])
        r = run_backtest(bars, target=target, exec_mode="next_open",
                         commission=0.0, sell_tax=0.0)
        assert r["metrics"]["trades"] >= 1
        assert r["metrics"]["total_return"] == pytest.approx(
            r["equity"][-1][2] - 1, abs=0.02)          # 与买入持有基准基本一致

    def test_grid_target_executes_orders(self):
        from app.backtest.signals import grid_target
        bars = make_bars(n=12)
        # 人造下跌+反弹：触发降档建仓与升档减仓
        close = bars["close"].iloc[:, 0]
        close.iloc[:6] = np.linspace(10, 8.6, 6)       # 下跌段
        close.iloc[6:] = np.linspace(9.4, 10.4, 6)     # 反弹段
        bars["low"] = (close - 0.2).to_frame("600519")
        target = grid_target(close, step_pct=5.0, levels=4)
        r = run_backtest(bars, target=target, exec_mode="next_open",
                         commission=0.0, sell_tax=0.0)
        assert r["metrics"]["trades"] >= 2, "网格应产生建仓与减仓多笔订单"
        assert r["metrics"]["total_return"] != 0.0

    def test_momentum_weights_execute_orders(self):
        idx = pd.bdate_range("2026-01-01", periods=45)
        a = np.linspace(10, 12, 45)                    # 强
        b = np.linspace(10, 10.5, 45)                  # 中
        c = np.linspace(10, 9, 45)                     # 弱
        close = pd.DataFrame({"AAA": a, "BBB": b, "CCC": c}, index=idx)
        bars = {"close": close, "open": close - 0.05,
                "high": close + 0.2, "low": close - 0.2,
                "volume": close * 1e6}
        from app.backtest.signals import momentum_weights
        weights = momentum_weights(close, window=20, top_k=2)
        r = run_backtest(bars, weights=weights, exec_mode="next_open",
                         commission=0.0, sell_tax=0.0)
        assert r["metrics"]["trades"] >= 1
        assert r["metrics"]["total_return"] != 0.0     # 强者组合应产生收益
