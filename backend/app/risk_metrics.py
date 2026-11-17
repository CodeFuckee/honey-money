"""基金风险/波动指标：全复权口径自算（手算可验证），定投日更管线顺带落表。

指标口径（与 backtest/metrics.py 同源：年化系数 √252、无风险利率 0）：
- 输入 (dates, adj)：prep_window(g, None) 的复权净值链（build_growth 含
  分红再投兜底），adj 已按净值日升序；
- 收益序列 r = adj 日环比（n 个净值日 → n-1 个收益）；
- vol_N      年化波动率   = std(r, ddof=1) × √252；
- vol_down_N 下行年化波动率 = std(r[r<0], ddof=1) × √252（只统计下跌日）；
- mdd_N      窗口最大回撤  = max((峰-谷)/峰)，正小数（含窗口起点前一日作峰基线）；
- sharpe_N   夏普        = mean(r)/std(r) × √252，std=0（恒定净值）→ None；
- roll_vol   当前 60 个净值日滚动年化波动率（不足 60 个收益 → None）；
- vol_pctile roll_vol 在自身全史滚动波动率序列中的百分位（0~1，含自身）。
- 窗口按收益个数取最近 N 个（1y=250 / 3y=750 / 5y=1250 / all=全史），
  收益样本 <20 的窗口四指标全 None；
- 全部 round(4) 小数口径（0.185 = 18.5%），None = 数据不足/无法计算。

ROW_FIELDS 为落库元组字段序（dca_store._RISK_COLS 与之逐位对齐）。
"""

import numpy as np
from numpy.lib.stride_tricks import sliding_window_view

ANNUALIZE = float(np.sqrt(252))   # 年化系数（与 perf_metrics sharpe 同款）
ROLL_WINDOW = 60                  # 滚动波动率窗口（收益个数）
MIN_RETURNS = 20                  # 单窗口指标最少收益样本
WINDOWS = (("1y", 250), ("3y", 750), ("5y", 1250), ("all", None))

ROW_FIELDS = tuple(
    f"{metric}_{suf}"
    for suf, _ in WINDOWS
    for metric in ("vol", "vol_down", "mdd", "sharpe")
) + ("roll_vol", "vol_pctile", "as_of", "nav_count")


def _window_metrics(rets: np.ndarray, adj_win: np.ndarray) -> tuple:
    """单窗口 (vol, vol_down, mdd, sharpe)；样本不足/无法计算 → None。"""
    if rets.size < MIN_RETURNS:
        return None, None, None, None
    sd = float(np.std(rets, ddof=1))
    vol = sd * ANNUALIZE
    downs = rets[rets < 0]
    vol_down = (float(np.std(downs, ddof=1)) * ANNUALIZE
                if downs.size >= 2 else None)
    peak = np.maximum.accumulate(adj_win)
    mdd = float(((peak - adj_win) / peak).max())
    sharpe = float(rets.mean() / sd * ANNUALIZE) if sd > 0 else None
    return (round(vol, 4),
            round(vol_down, 4) if vol_down is not None else None,
            round(mdd, 4),
            round(sharpe, 4) if sharpe is not None else None)


def _rolling_vol(rets: np.ndarray) -> tuple:
    """(当前滚动年化波动率, 其在自身历史滚动序列的百分位 0~1)。"""
    if rets.size < ROLL_WINDOW:
        return None, None
    win = sliding_window_view(rets, ROLL_WINDOW)
    roll = win.std(axis=1, ddof=1) * ANNUALIZE
    roll = roll[np.isfinite(roll)]
    if roll.size == 0:
        return None, None
    cur = float(roll[-1])
    return round(cur, 4), round(float((roll <= cur).mean()), 4)


def compute_risk_metrics(dates, adj) -> dict:
    """单基金风险指标（ROW_FIELDS 各键）；净值不足 2 个 → 全 None 行。"""
    adj = np.asarray(adj, dtype=float)
    n = adj.size
    out = {"roll_vol": None, "vol_pctile": None,
           "as_of": str(dates[-1].date()) if n else None,
           "nav_count": n}
    rets = adj[1:] / adj[:-1] - 1.0 if n >= 2 else np.array([])
    out["roll_vol"], out["vol_pctile"] = _rolling_vol(rets)
    for suf, w in WINDOWS:
        if w is None:
            rets_w, adj_w = rets, adj
        elif rets.size >= w:
            rets_w, adj_w = rets[-w:], adj[-(w + 1):]
        else:
            rets_w, adj_w = np.array([]), adj
        vol, vol_down, mdd, sharpe = _window_metrics(rets_w, adj_w)
        out[f"vol_{suf}"] = vol
        out[f"vol_down_{suf}"] = vol_down
        out[f"mdd_{suf}"] = mdd
        out[f"sharpe_{suf}"] = sharpe
    return out


def risk_row(code: str, metrics: dict) -> tuple:
    """落库元组：(code, *ROW_FIELDS 值)，列序与 dca_store._RISK_COLS 对齐。"""
    return (code,) + tuple(metrics[k] for k in ROW_FIELDS)
