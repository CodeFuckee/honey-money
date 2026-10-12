"""绩效指标与交易成本：全部自算（不依赖回测库），合成净值可手算验证。

指标口径（教学页固定展示集）：
- total_return  期末净值 - 1
- annual_return 按自然日年化 (1+total)^(365/天数) - 1
- max_drawdown  净值峰谷最大回撤（正的小数）
- sharpe        日收益均值/日收益标准差 × sqrt(252)，无风险利率 0
- vol_days      回测区间自然日数（前端展示用）
- win_rate / profit_loss_ratio / trades  按平仓交易统计

成本口径（A 股）：
- 买入：佣金（比例，最低 min_commission 元）
- 卖出：佣金 + 印花税（仅卖出收，2023-08 减半后 0.05%）
"""

import numpy as np
import pandas as pd


def max_drawdown(net: pd.Series) -> float:
    """净值峰谷最大回撤，正的小数（恒涨序列为 0）。"""
    net = net.dropna()
    if net.empty:
        return 0.0
    peak = net.cummax()
    return float(((peak - net) / peak).max())


def perf_metrics(net: pd.Series) -> dict:
    net = net.dropna()
    if net.empty or len(net) < 2:
        return {"total_return": 0.0, "annual_return": 0.0, "max_drawdown": 0.0,
                "sharpe": 0.0, "vol_days": len(net)}
    total = float(net.iloc[-1] / net.iloc[0] - 1)
    days = max((net.index[-1] - net.index[0]).days, 1)
    annual = float((1 + total) ** (365 / days) - 1)
    ret = net.pct_change().dropna()
    vol = float(ret.std())
    sharpe = float(ret.mean() / vol * np.sqrt(252)) if vol > 0 else 0.0
    return {"total_return": total, "annual_return": annual,
            "max_drawdown": max_drawdown(net), "sharpe": sharpe, "vol_days": days}


def trade_stats(pnls) -> dict:
    """按平仓交易收益序列统计胜率与盈亏比（无亏损交易时不返回 inf）。

    None 为未平仓交易的浮动盈亏（JSON null；退市股尾部无价时 vectorbt
    算不出）——不计入统计，否则 float(None) 崩。
    """
    pnls = [float(p) for p in pnls if p is not None]
    wins = [p for p in pnls if p > 0]
    losses = [abs(p) for p in pnls if p < 0]
    avg_win = sum(wins) / len(wins) if wins else 0.0
    avg_loss = sum(losses) / len(losses) if losses else 0.0
    return {
        "trades": len(pnls),
        "win_rate": len(wins) / len(pnls) if pnls else 0.0,
        "profit_loss_ratio": avg_win / avg_loss if avg_loss else 0.0,
    }


def round_trip_cost(value: float, commission: float, sell_tax: float,
                    min_commission: float = 5.0) -> float:
    """一买一卖的交易成本（金额口径）：双边佣金（各按最低保护）+ 卖出印花税。"""
    buy_commission = max(value * commission, min_commission)
    sell_amount = value - buy_commission
    sell_commission = max(sell_amount * commission, min_commission)
    return buy_commission + sell_commission + sell_amount * sell_tax
