"""策略信号层：6 个经典入门策略，纯 pandas，不含撮合逻辑。

教学契约：信号在「当日收盘」判定（布尔序列 / 目标权重序列）——是否当日收盘
成交或次日开盘成交由引擎的 exec_mode 决定，本层绝不前视。

多标的约定：momentum_weights 输入 DataFrame（列=代码），其余策略输入 Series。
"""

import numpy as np
import pandas as pd


def buy_hold_target(close: pd.Series) -> pd.Series:
    """买入持有基准：首日满仓持有到期末（所有策略的对照线）。"""
    return pd.Series(1.0, index=close.index)


def ma_cross_signals(close: pd.Series, fast: int = 5, slow: int = 20):
    """双均线交叉：MA(fast) 上穿 MA(slow) 金叉入场，下穿离场。

    教学点：趋势跟随的 Hello World；震荡市的鞭尸（whipsaw）问题。
    """
    ma_f = close.rolling(fast).mean()
    ma_s = close.rolling(slow).mean()
    up = (ma_f > ma_s) & ~(ma_f > ma_s).shift(1, fill_value=False)
    down = (ma_f < ma_s) & ~(ma_f < ma_s).shift(1, fill_value=False)
    return up.fillna(False), down.fillna(False)


def donchian_signals(high: pd.Series, low: pd.Series, close: pd.Series,
                     entry_n: int = 20, exit_n: int = 10):
    """唐奇安通道突破（海龟内核）：突破前 entry_n 日最高买入，跌破前 exit_n 日最低卖出。

    通道用 shift(1) 滚动——不含当日，否则当日新高算「自我突破」（前视）。
    """
    prior_high = high.shift(1).rolling(entry_n).max()
    prior_low = low.shift(1).rolling(exit_n).min()
    entries = (close > prior_high).fillna(False)
    exits = (close < prior_low).fillna(False)
    return entries, exits


def bollinger_signals(close: pd.Series, window: int = 20, alpha: float = 2.0):
    """布林带均值回归：跌破均线下 alpha 倍标准差买入，回到均线上方卖出。

    教学点：与趋势策略哲学对立；单边下跌越抄越深（用 sd>0 防护横盘除零）。
    """
    ma = close.rolling(window).mean()
    sd = close.rolling(window).std()
    entries = ((close < ma - alpha * sd) & (sd > 0)).fillna(False)
    exits = (close >= ma).fillna(False)
    return entries, exits


def grid_target(close: pd.Series, step_pct: float = 5.0, levels: int = 4) -> pd.Series:
    """网格交易：以首日收盘为基准，每跌 step% 升一档、每档投 1/levels 资金；
    反弹降档减仓，涨回基准及以上清仓（只低买高卖、不追高）。

    返回目标仓位比例序列（0~1），由引擎按 targetpercent 撮合。
    """
    base = close.iloc[0]
    steps_down = ((base - close) / base / (step_pct / 100)).apply(np.floor)
    n_levels = steps_down.clip(lower=0, upper=levels)
    return (n_levels / levels).astype(float)


def momentum_weights(close: pd.DataFrame, window: int = 20, top_k: int = 2,
                     ) -> pd.DataFrame:
    """横截面动量轮动：每月末按过去 window 日涨幅排名，等权买入前 top_k 名。

    返回目标权重矩阵：仅月末调仓行有值（其余 NaN = 不调仓、持仓自然延续），
    由引擎按 targetpercent 撮合。教学点：强者恒强；动量崩溃风险。
    """
    close = close.copy()
    if not isinstance(close.index, pd.DatetimeIndex):
        close.index = pd.to_datetime(close.index)  # 容忍非 DatetimeIndex 输入
    mom = close.pct_change(window)
    month_end = mom.index.to_series().groupby(mom.index.to_period("M")).max()
    rebalance = mom.loc[month_end]
    rank = rebalance.rank(axis=1, ascending=False)
    picked = (rank <= top_k) & rebalance.notna()
    weights = picked.astype(float)
    weights = weights.div(weights.sum(axis=1), axis=0).where(picked)
    out = pd.DataFrame(np.nan, index=close.index, columns=close.columns)
    out.loc[weights.index] = weights
    return out
