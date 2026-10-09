"""策略编排：API 的 strategy 名 + 参数 → 信号层 → 撮合引擎。

单标的策略（buy_hold/ma_cross/donchian/bollinger/grid）取 codes[0] 的列；
轮动（momentum）用全部列（池 ≥2 只由 API 层校验）。
"""

from . import engine
from .signals import (
    bollinger_signals, buy_hold_target, donchian_signals, grid_target,
    ma_cross_signals, momentum_weights)

STRATEGIES = ("buy_hold", "ma_cross", "donchian", "bollinger", "grid",
              "momentum")

# 各策略参数白名单 + 教学默认值：API 传入的未知参数名被丢弃（防 TypeError 500）
_DEFAULT_PARAMS = {
    "buy_hold": {},
    "ma_cross": {"fast": 5, "slow": 20},
    "donchian": {"entry_n": 20, "exit_n": 10},
    "bollinger": {"window": 20, "alpha": 2.0},
    "grid": {"step_pct": 5.0, "levels": 4},
    "momentum": {"window": 20, "top_k": 2},
}


def run_strategy(bars: dict, *, strategy: str, params: dict,
                 exec_mode: str = "next_open", commission: float = 2.5e-4,
                 sell_tax: float = 5e-4) -> dict:
    if strategy not in _DEFAULT_PARAMS:
        raise ValueError(f"未知策略: {strategy}")
    merged = {**_DEFAULT_PARAMS[strategy],
              **{k: v for k, v in (params or {}).items()
                 if k in _DEFAULT_PARAMS[strategy]}}
    close = bars["close"]
    single = close.iloc[:, 0]                     # 单标的策略取第一列

    def run(**kw):
        return engine.run_backtest(bars, exec_mode=exec_mode,
                                   commission=commission, sell_tax=sell_tax,
                                   **kw)

    if strategy == "buy_hold":
        return run(target=buy_hold_target(single))
    if strategy == "ma_cross":
        entries, exits = ma_cross_signals(single, **merged)
        return run(entries=entries, exits=exits)
    if strategy == "donchian":
        entries, exits = donchian_signals(bars["high"].iloc[:, 0],
                                          bars["low"].iloc[:, 0], single,
                                          **merged)
        return run(entries=entries, exits=exits)
    if strategy == "bollinger":
        entries, exits = bollinger_signals(single, **merged)
        return run(entries=entries, exits=exits)
    if strategy == "grid":
        return run(target=grid_target(single, **merged))
    return run(weights=momentum_weights(close, **merged))
