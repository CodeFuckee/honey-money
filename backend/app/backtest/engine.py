"""vectorbt 撮合引擎：信号/目标权重 → 组合模拟 → 教学页统一输出。

撮合契约：
- exec_mode="close"：信号当日收盘成交（教学：未来函数作弊模式）
- exec_mode="next_open"：信号次日开盘成交（正确模式，默认）
- 成本：vectorbt 1.x 仅支持双边统一比例费用——印花税按双边均摊近似
  （fees = 佣金 + 印花税/2，往返金额相近时误差可忽略）；未含最低佣金保护
- 买入持有基准：与策略同区间同费率的等权持有对照线

疑似除权跳空：隔夜开盘相对前收跳空超 9.8%（A 股主板跌停幅度）——不复权数据
的固有噪声，前端明示局限（honey-money 尚未接入复权因子）。
"""

import numpy as np
import pandas as pd

from .metrics import perf_metrics, trade_stats

INIT_CASH = 1_000_000
GAP_THRESHOLD = 0.098


def detect_gap_dates(open_: pd.DataFrame, close: pd.DataFrame) -> list[str]:
    """隔夜跳空检测：|open / 前收 - 1| > 9.8% 的日期（任意标的触发即记录）。"""
    prev_close = close.shift(1)
    gap = (open_ / prev_close - 1).abs()
    mask = (gap > GAP_THRESHOLD) & prev_close.notna() & open_.notna()
    return [d.strftime("%Y-%m-%d") for d in mask.any(axis=1)[lambda s: s].index]


def _to_frame(x) -> pd.DataFrame:
    return x.to_frame() if isinstance(x, pd.Series) else x


def run_backtest(bars: dict, *, entries=None, exits=None, target=None, weights=None,
        exec_mode: str = "next_open", commission: float = 2.5e-4,
        sell_tax: float = 5e-4) -> dict:
    """运行回测并输出教学页统一形状。

    三种输入（互斥）：entries/exits（信号策略）、target（网格目标仓位）、
    weights（轮动权重矩阵）。输出：
    {metrics, equity: [(date, 策略净值, 基准净值)], drawdown, trades, gap_dates}
    """
    import vectorbt as vbt

    close = _to_frame(bars["close"])
    open_ = _to_frame(bars["open"])

    def norm_signals(s):
        if s is None:
            return None
        s = _to_frame(s)
        # 无名 Series to_frame() 列名是 0——先对齐列名，reindex 否则整列丢弃
        if list(s.columns) != list(close.columns):
            s.columns = close.columns
        s = s.reindex(columns=close.columns, index=close.index).fillna(False)
        return s.astype(bool)

    # 1.x 无卖出差分费用：印花税均摊到双边（卖出-only 的精确模拟不值得引入自研撮合）
    # 1.x 的 size 默认 None（不成交）——信号策略显式全仓；轮动/网格用 targetpercent
    kwargs = dict(fees=commission + sell_tax / 2, init_cash=INIT_CASH)

    if weights is not None:
        w = _to_frame(weights).reindex(columns=close.columns, index=close.index)
        if exec_mode == "next_open":
            w, price = w.shift(1), open_
        else:
            price = close
        pf = vbt.Portfolio.from_orders(
            close, size=w, size_type="targetpercent", price=price,
            group_by=True, cash_sharing=True, call_seq="auto", **kwargs)
    elif target is not None:
        t = _to_frame(target).reindex(columns=close.columns, index=close.index)
        if exec_mode == "next_open":
            t, price = t.shift(1), open_
        else:
            price = close
        pf = vbt.Portfolio.from_orders(
            close, size=t, size_type="targetpercent", price=price,
            group_by=True, cash_sharing=True, **kwargs)
    else:
        e, x = norm_signals(entries), norm_signals(exits)
        if exec_mode == "next_open":
            # pandas 3 的 shift 会把 bool 列退化为 object（引入 NaN），numba 拒收
            e = e.shift(1).fillna(False).astype(bool)
            x = x.shift(1).fillna(False).astype(bool)
            price = open_
        else:
            price = close
        pf = vbt.Portfolio.from_signals(close, e, x, price=price,
                                        size=np.inf, size_type="amount", **kwargs)

    net = pf.value()
    if isinstance(net, pd.DataFrame):
        net = net.sum(axis=1)
    net = net / INIT_CASH

    # 基准：同区间等权买入持有（含费率，对照公平）
    bh_pf = vbt.Portfolio.from_holding(close, group_by=True, cash_sharing=True,
                                       init_cash=INIT_CASH,
                                       fees=commission + sell_tax / 2)
    bh = bh_pf.value()
    if isinstance(bh, pd.DataFrame):
        bh = bh.sum(axis=1)
    bh = bh / INIT_CASH

    equity = [[d.strftime("%Y-%m-%d"), round(float(v), 6), round(float(b), 6)]
              for d, v, b in zip(net.index, net, bh)]
    dd = net / net.cummax() - 1
    drawdown = [[d.strftime("%Y-%m-%d"), round(float(v), 6)]
                for d, v in dd.items()]

    trades = []
    try:
        rec = pf.trades.records_readable
        col_map = {"Entry Timestamp": "entry_date", "Exit Timestamp": "exit_date",
                   "Entry Price": "entry_price", "Exit Price": "exit_price",
                   "Return": "pnl_pct", "Direction": "direction",
                   "Column": "symbol"}
        for _, row in rec.iterrows():
            item = {}
            for src, dst in col_map.items():
                if src in rec.columns:
                    v = row[src]
                    if isinstance(v, pd.Timestamp):
                        v = v.strftime("%Y-%m-%d")
                    elif dst == "direction":
                        v = str(v).title().lower() or "long"
                    item[dst] = v
            trades.append(item)
    except Exception:                              # 兼容不同版本 records 形状
        trades = []

    metrics = perf_metrics(net)
    metrics.update(trade_stats(
        (t["pnl_pct"] for t in trades if "pnl_pct" in t)))
    return {"metrics": metrics, "equity": equity, "drawdown": drawdown,
            "trades": trades, "gap_dates": detect_gap_dates(open_, close)}


def warmup() -> None:
    """numba JIT 编译预热：部署后容器内首次回测需编译数十秒（用户感知为
    网关超时），启动时用微型回测把编译成本提前吃掉。失败不影响服务。"""
    try:
        idx = pd.bdate_range("2026-01-01", periods=5)
        close = pd.Series(np.linspace(10.0, 11.0, 5), index=idx).to_frame("WARMUP")
        bars = {"close": close, "open": close - 0.1,
                "high": close + 0.1, "low": close - 0.2,
                "volume": close * 1e6}
        run_backtest(bars, entries=close.notna(), exits=~close.notna(),
                     exec_mode="next_open")
        print("[warmup] vectorbt 编译预热完成")
    except Exception as exc:                       # 预热失败不阻塞服务
        print(f"[warmup] vectorbt 预热失败（不影响服务）: {exc}")
