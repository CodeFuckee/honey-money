"""风险/波动指标测试：合成净值序列手算锚定（全离线）。

覆盖：收益口径（相对而非绝对差分）、√252 年化、下行只取负收益、
窗口切片（恰好整窗的 >= 边界）、滚动波动率与历史分位、数据不足全 None、
risk_row 列序与 ROW_FIELDS 对齐。期望值一律用独立 numpy 表达式重算，
不复用被测实现。
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.risk_metrics import (ANNUALIZE, MIN_RETURNS, ROLL_WINDOW,  # noqa: E402
                              ROW_FIELDS, compute_risk_metrics, risk_row)


def _metrics(values, start="2024-01-01"):
    dates = pd.bdate_range(start, periods=len(values))
    return compute_risk_metrics(dates, np.asarray(values, dtype=float))


def _returns(adj):
    adj = np.asarray(adj, dtype=float)
    return adj[1:] / adj[:-1] - 1.0


def _expect(x):
    """实现按口径 round(4) 落库——期望值同样取整后精确比对（取整是契约）。"""
    return round(float(x), 4)


class TestBasicMetrics:
    """窗口四指标：公式锚定（vol/vol_down/mdd/sharpe 独立重算）。"""

    def test_alternating_series_matches_numpy_recompute(self):
        vals = np.tile([1.01, 0.99], 130)          # 260 点交替，全窗同分布
        m = _metrics(vals)
        rets = _returns(vals)
        assert m["vol_all"] == pytest.approx(
            _expect(np.std(rets, ddof=1) * ANNUALIZE), abs=1e-9)
        downs = rets[rets < 0]
        assert m["vol_down_all"] == pytest.approx(
            _expect(np.std(downs, ddof=1) * ANNUALIZE), abs=1e-9)
        peak = np.maximum.accumulate(vals)
        assert m["mdd_all"] == pytest.approx(
            _expect(((peak - vals) / peak).max()), abs=1e-9)
        assert m["sharpe_all"] == pytest.approx(
            _expect(rets.mean() / np.std(rets, ddof=1) * ANNUALIZE), abs=1e-9)

    def test_constant_nav_vol_zero_sharpe_none(self):
        """恒定净值（货基形态）：vol=0、mdd=0、sharpe 无定义 None、下行 None。"""
        m = _metrics(np.ones(300))
        assert m["vol_all"] == 0.0
        assert m["mdd_all"] == 0.0
        assert m["sharpe_all"] is None
        assert m["vol_down_all"] is None

    def test_up_only_downside_none_mdd_zero(self):
        """单边上涨：无负收益 → 下行波动率 None、最大回撤 0。"""
        m = _metrics(np.linspace(1.0, 1.6, 300))
        assert m["vol_down_all"] is None
        assert m["mdd_all"] == 0.0

    def test_down_only_downside_equals_total(self):
        """单边下跌：全部收益为负 → 下行波动率 = 总波动率。"""
        m = _metrics(np.linspace(1.5, 1.0, 300))
        rets = _returns(np.linspace(1.5, 1.0, 300))
        assert m["vol_down_all"] == pytest.approx(m["vol_all"], abs=1e-6)
        assert m["vol_down_all"] == pytest.approx(
            _expect(np.std(rets, ddof=1) * ANNUALIZE), abs=1e-9)

    def test_v_shape_mdd_is_depth(self):
        """V 型（1.0 → 0.8 → 1.0）：最大回撤 = 20%。"""
        half = 130
        vals = np.concatenate([np.linspace(1.0, 0.8, half),
                               np.linspace(0.8, 1.0, half)])
        m = _metrics(vals)
        assert m["mdd_all"] == pytest.approx(0.2, abs=1e-6)


class TestWindowSlicing:
    """窗口按收益个数取最近 N 个；样本不足整窗 → 该窗口全 None。"""

    def test_exact_window_boundary_inclusive(self):
        """251 点 = 恰好 250 个收益 → vol_1y 可算（>= 整窗边界，曾写成
        > 使恰好满窗误判不足——回归锚点）。"""
        vals = np.tile([1.01, 0.99], 130)[:251]
        m = _metrics(vals)
        rets = _returns(vals)
        assert m["vol_1y"] is not None
        assert m["vol_1y"] == pytest.approx(
            _expect(np.std(rets, ddof=1) * ANNUALIZE), abs=1e-9)
        assert m["vol_5y"] is None                 # 只有 250 个收益 < 1250
        assert m["vol_3y"] is None

    def test_short_of_window_is_none(self):
        vals = np.tile([1.01, 0.99], 125)          # 250 点 = 249 个收益
        m = _metrics(vals)
        assert m["vol_1y"] is None

    def test_window_is_most_recent_slice(self):
        """前段平静 + 后段狂野：vol_1y 取最近 250 个收益（≈狂野段）。"""
        calm = np.tile([1.001, 0.999], 600)[:1000]  # 1000 点平静
        wild = np.tile([1.05, 0.95], 130)[:251]     # 251 点狂野
        m = _metrics(np.concatenate([calm, wild]))
        wild_rets = _returns(wild)
        assert m["vol_1y"] == pytest.approx(
            _expect(np.std(wild_rets, ddof=1) * ANNUALIZE), abs=1e-9)
        assert m["vol_1y"] > m["vol_all"]           # 全史被平静段稀释
        assert m["mdd_1y"] == pytest.approx(_expect(0.10 / 1.05), abs=1e-9)
        assert m["mdd_all"] < 0.1                   # 全史峰基线在前段 1.001


class TestRollingVolPctile:
    """roll_vol = 末值 60 日滚动年化波动率；分位 = 末值在自身滚动序列的百分位。"""

    def test_below_roll_window_none(self):
        m = _metrics(np.tile([1.01, 0.99], 30)[:ROLL_WINDOW])   # 恰 60 点=59 收益
        assert m["roll_vol"] is None
        assert m["vol_pctile"] is None

    def test_last_window_is_max_pctile_one(self):
        """末段波动严格大于历史（幅度分离，边界窗口必严格更小）→ 分位 1.0。"""
        calm = np.tile([1.001, 0.999], 300)[:600]
        wild = np.tile([1.10, 0.90], 130)[:ROLL_WINDOW + 1]     # 末 60 收益狂野
        m = _metrics(np.concatenate([calm, wild]))
        assert m["vol_pctile"] == pytest.approx(1.0)
        rets = _returns(np.concatenate([calm, wild]))
        cur = rets[-ROLL_WINDOW:]
        assert m["roll_vol"] == pytest.approx(
            _expect(np.std(cur, ddof=1) * ANNUALIZE), abs=1e-9)

    def test_last_window_is_min_pctile_smallest(self):
        """末段最平静（前段狂野，幅度分离）：分位 = 末窗严格最小 → 1/滚动序列长。"""
        wild = np.tile([1.05, 0.95], 300)[:600]
        calm = np.tile([1.001, 0.999], 130)[:ROLL_WINDOW + 1]
        m = _metrics(np.concatenate([wild, calm]))
        assert m["vol_pctile"] < 0.01              # 只有末窗 ≤ 自身
        rets = _returns(np.concatenate([wild, calm]))
        assert m["roll_vol"] == pytest.approx(
            _expect(np.std(rets[-ROLL_WINDOW:], ddof=1) * ANNUALIZE), abs=1e-9)


class TestInsufficientData:
    """净值不足：窗口全 None，仅 nav_count/as_of 有值。"""

    def test_two_points_all_none(self):
        m = _metrics([1.0, 1.1])
        assert m["nav_count"] == 2
        assert m["as_of"] is not None
        for k in ROW_FIELDS:
            if k in ("as_of", "nav_count"):
                continue
            assert m[k] is None, k

    def test_below_min_returns_window_none(self):
        m = _metrics(np.linspace(1.0, 1.2, MIN_RETURNS))        # 19 个收益
        assert m["vol_all"] is None and m["sharpe_all"] is None


class TestRowShape:
    """risk_row：(code,) + ROW_FIELDS 逐位对齐（dca_store._RISK_COLS 锚点）。"""

    def test_row_order_follows_row_fields(self):
        vals = np.tile([1.01, 0.99], 130)[:251]
        m = _metrics(vals)
        row = risk_row("000001", m)
        assert len(row) == 1 + len(ROW_FIELDS) == 21
        assert row[0] == "000001"
        expected = ("000001",) + tuple(m[k] for k in ROW_FIELDS)
        assert row == expected

    def test_row_fields_cover_windows(self):
        for suf in ("1y", "3y", "5y", "all"):
            for metric in ("vol", "vol_down", "mdd", "sharpe"):
                assert f"{metric}_{suf}" in ROW_FIELDS
        assert set(ROW_FIELDS) >= {"roll_vol", "vol_pctile", "as_of", "nav_count"}
