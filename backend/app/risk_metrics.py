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
OSC_THETA = 0.10                  # 震荡检测反转阈值（净值 ±10% 确认一次摆动）
OSC_MIN_NAVS = 250                # 震荡指标最少净值日（≈1 年：年化轮数在极短
                                  # 样本上会被放大成无意义的虚高分）

ROW_FIELDS = tuple(
    f"{metric}_{suf}"
    for suf, _ in WINDOWS
    for metric in ("vol", "vol_down", "mdd", "sharpe")
) + ("roll_vol", "vol_pctile", "osc_per_year", "osc_regularity",
     "as_of", "nav_count")


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


def zigzag_oscillation(adj, theta: float = OSC_THETA) -> dict:
    """全史净值的 zigzag 摆动检测（±theta 确认反转，2026-11-18 震荡分位）。

    「上上下下」一轮 = 一个低点极值经上行 leg 触达高点极值、再经下行 leg
    回落的完整往返；间隔（同向极值间净值日个数）越均匀越像余弦。
    极值一律「确认制」：反向变动达 theta 才入列，趋势中的未确认极值不记
    （单调上涨永不确认高点 → 零轮）。

    - osc_per_year：年均规则摆动轮数 = 完整轮数 / (净值日数/252)
      × 规则度 1/(1+CV)；无完整轮 → 0.0；净值样本 < OSC_MIN_NAVS → None
      （年化轮数在极短样本上会被放大成虚高分，如 0.1 年 1 轮 → 10 轮/年）；
    - osc_regularity：规则度（同向极值间隔 < 2 个 → None）。
    横截面百分位（osc_pctile）由 dca_store 回填 SQL 统一计算。
    """
    adj = np.asarray(adj, dtype=float)
    n = adj.size
    if n < OSC_MIN_NAVS:
        return {"osc_per_year": None, "osc_regularity": None}
    pivots: list[int] = []      # 已确认极值点索引，低/高交替
    direction = 0               # 0 未定向 / 1 上行找高点 / -1 下行找低点
    hi = lo = ext = 0
    for i in range(1, n):
        if adj[i] > adj[hi]:
            hi = i
        if adj[i] < adj[lo]:
            lo = i
        if direction == 0:
            if adj[hi] >= adj[lo] * (1 + theta):
                pivots.append(lo)       # 低点确认（首个反向变动起点）
                direction = 1
                ext = hi
            elif adj[lo] <= adj[hi] * (1 - theta):
                pivots.append(hi)       # 高点确认（自高点回落起步）
                direction = -1
                ext = lo
        elif direction == 1:
            if adj[i] > adj[ext]:
                ext = i
            elif adj[i] <= adj[ext] * (1 - theta):
                pivots.append(ext)      # 高点确认
                direction = -1
                ext = i
        else:
            if adj[i] < adj[ext]:
                ext = i
            elif adj[i] >= adj[ext] * (1 + theta):
                pivots.append(ext)      # 低点确认
                direction = 1
                ext = i
    cycles = max(len(pivots) - 1, 0) // 2
    years = n / 252.0
    freq = cycles / years if years > 0 else 0.0
    # 同向极值间隔（净值日个数）：低点列 pivots[0::2]、高点列 pivots[1::2]
    durs = [b - a for a, b in zip(pivots, pivots[2:])]
    reg = round(1.0 / (1.0 + float(np.std(durs, ddof=1) / np.mean(durs))), 4) \
        if len(durs) >= 2 and np.mean(durs) > 0 else None
    osc = round(freq * (reg if reg is not None else 1.0), 4)
    return {"osc_per_year": osc, "osc_regularity": reg}


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
    out.update(zigzag_oscillation(adj))
    return out


def risk_row(code: str, metrics: dict) -> tuple:
    """落库元组：(code, *ROW_FIELDS 值)，列序与 dca_store._RISK_COLS 对齐。"""
    return (code,) + tuple(metrics[k] for k in ROW_FIELDS)
