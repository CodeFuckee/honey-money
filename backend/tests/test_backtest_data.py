"""回测数据加载层测试：无效价清洗（离线，纯函数 + 引擎联动）。

背景：新浪源曾吐 2024-11-06 三只科创板 open=0 的脏行，vectorbt 撮合对
「非 NaN 但非正」的价格直接抛 ValueError——买入持有全量回测这三只恒
失败。清洗策略：<=0 置 NaN 走引擎既有的缺失容忍路径（停牌日语义），
**不做 ffill**（曾试过 ffill，把交易日不齐的真缺失也填成假价格，被
load_history 既有测试拦下）；volume=0（停牌）不清洗。
"""

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.backtest.data import sanitize_prices  # noqa: E402
from app.backtest.runner import run_strategy  # noqa: E402


def _bars(open_vals, close_vals):
    idx = pd.bdate_range("2026-01-01", periods=len(close_vals))
    return {
        "open": pd.Series(open_vals, index=idx).to_frame("688173"),
        "close": pd.Series(close_vals, index=idx).to_frame("688173"),
        "high": (pd.Series(close_vals, index=idx) + 0.2).to_frame("688173"),
        "low": (pd.Series(close_vals, index=idx) - 0.2).to_frame("688173"),
        "volume": pd.Series([1e6] * len(close_vals), index=idx).to_frame("688173"),
    }


class TestSanitizePrices:
    def test_zero_open_becomes_nan(self):
        bars = _bars([10.0, 10.1, 0.0, 10.3], [10.05, 10.15, 10.25, 10.35])
        out = sanitize_prices(bars)
        col = out["open"]["688173"]
        assert col.iloc[:2].tolist() == pytest.approx([10.0, 10.1])
        assert pd.isna(col.iloc[2])
        assert col.iloc[3] == pytest.approx(10.3)

    def test_leading_zero_becomes_nan(self):
        bars = _bars([0.0, 10.1, 10.2], [10.05, 10.15, 10.25])
        out = sanitize_prices(bars)
        assert pd.isna(out["open"]["688173"].iloc[0])

    def test_volume_zero_preserved(self):
        """volume=0 是停牌语义，不在价格清洗之列。"""
        bars = _bars([10.0, 10.1], [10.05, 10.15])
        bars["volume"] = pd.DataFrame({"688173": [0.0, 1e6]},
                                      index=bars["volume"].index)
        out = sanitize_prices(bars)
        assert out["volume"]["688173"].tolist() == [0.0, 1e6]

    def test_true_missing_stays_nan(self):
        """交易日不齐的真缺失（停牌日）语义不变——曾用 ffill 把它填成
        假价格，被 load_history 既有测试拦下后改为只清非正价。"""
        bars = _bars([10.0, 10.1, 10.2], [10.05, 10.15, None])
        out = sanitize_prices(bars)
        assert pd.isna(out["close"]["688173"].iloc[-1])


class TestEngineToleratesZeroPrice:
    """回归：含 0 开盘价的 bars 经清洗后买入持有可跑通（曾直接
    ValueError: order.price must be finite and greater than 0）。"""

    def test_buyhold_with_dirty_open_runs(self):
        bars = _bars([10.0] * 2 + [0.0] + [10.0] * 27,
                     [10.0 + 0.1 * i for i in range(30)])
        cleaned = sanitize_prices(bars)
        result = run_strategy(cleaned, strategy="buy_hold", params={},
                              exec_mode="next_open", commission=0.0,
                              sell_tax=0.0)
        assert result.get("message") in (None, "")
        assert result["metrics"]["total_return"] is not None
        assert result["metrics"]["total_return"] > 0

    def test_raw_zero_open_still_blows_engine(self):
        """未清洗的 0 价引擎仍会炸——防回归锚点：防线在数据层，不掩盖。"""
        bars = _bars([10.0] * 2 + [0.0] + [10.0] * 7,
                     [10.0 + 0.1 * i for i in range(10)])
        with pytest.raises(ValueError):
            run_strategy(bars, strategy="buy_hold", params={},
                         exec_mode="next_open", commission=0.0, sell_tax=0.0)
