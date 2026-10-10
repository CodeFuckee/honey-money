"""回测 API：POST /api/backtest（同步，秒级返回）。

约定（同项目降级风格）：PG 不可达/区间无数据 → 200 + status 表达；
参数非法由 pydantic 422，轮动池 <2 只 → 400。
"""

import datetime as dt
from typing import Literal

import psycopg
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, field_validator

from . import config
from .adjust import apply_hfq, ensure_factors, load_factors
from .backtest.data import load_history
from .backtest.runner import STRATEGIES, run_strategy

_RANGE_DAYS = {"1y": 365, "3y": 365 * 3, "5y": 365 * 5}


class BacktestIn(BaseModel):
    strategy: Literal["buy_hold", "ma_cross", "donchian", "bollinger",
                      "grid", "momentum"]
    codes: list[str] = Field(min_length=1)
    range: Literal["1y", "3y", "5y", "all"] = "3y"
    params: dict = Field(default_factory=dict)
    exec_mode: Literal["close", "next_open"] = "next_open"
    commission: float = Field(default=2.5e-4, ge=0, le=0.01)
    sell_tax: float = Field(default=5e-4, ge=0, le=0.01)

    @field_validator("codes")
    @classmethod
    def _codes_ok(cls, v):
        codes = [c.strip().upper() for c in v if c.strip()]
        if not codes:
            raise ValueError("codes 不能为空")
        return codes

    @field_validator("params")
    @classmethod
    def _params_json_safe(cls, v):
        return {k: val for k, val in v.items() if val is not None}


def register_backtest_routes(app: FastAPI) -> None:
    def _with_factors(bars: dict, codes: list[str]) -> tuple[dict, dict | None]:
        """复权因子懒加载：拉取入库后把 bars 合成为后复权价。

        因子不可用（新浪断连等）→ 返回原 bars + None（调用方降级不复权）。"""
        from .backtest.engine import detect_gap_dates
        latest, gaps = {}, {}
        cl = bars["close"]
        for code in codes:
            if code in cl.columns and cl[code].notna().any():
                sub = cl[[code]].dropna()
                latest[code] = sub.index.max().date()
                prev = sub[code].shift(1)
                gap_mask = ((sub[code] / prev - 1).abs() > 0.098) & prev.notna()
                if gap_mask.any():
                    gaps[code] = [d.date() for d in sub.index[gap_mask]]
        status = ensure_factors(config.DATABASE_URL, codes,
                                latest_trade_dates=latest, gap_dates=gaps)
        if not all(v == "ok" for v in status.values()):
            return bars, None
        with psycopg.connect(config.DATABASE_URL, connect_timeout=5) as conn:
            factors = {code: load_factors(conn, code) for code in codes}
        return apply_hfq(bars, factors), factors

    @app.post("/api/backtest")
    def backtest(body: BacktestIn):
        if body.strategy == "momentum" and len(body.codes) < 2:
            raise HTTPException(400, "动量轮动需要至少 2 只标的")
        today = dt.date.today()
        start = (today - dt.timedelta(days=_RANGE_DAYS[body.range])).isoformat() \
            if body.range != "all" else None
        try:
            bars = load_history(config.DATABASE_URL, body.codes,
                                start=start, end=today.isoformat())
        except Exception as exc:                   # PG 不可达：降级而非 5xx
            return {"status": "down",
                    "detail": f"{type(exc).__name__}: {str(exc)[:120]}"}
        if not bars:
            return {"status": "up", "adjust_applied": False, "metrics": {},
                    "equity": [], "drawdown": [], "trades": [], "gap_dates": [],
                    "message": "区间内无行情数据（请检查代码或扩大区间）"}
        try:
            bars_adj, factors = _with_factors(bars, body.codes)
            adjust_applied = factors is not None
        except Exception:                          # 复权链路异常：降级不复权
            bars_adj, factors, adjust_applied = bars, None, False
        result = run_strategy(
            bars_adj, strategy=body.strategy, params=body.params,
            exec_mode=body.exec_mode, commission=body.commission,
            sell_tax=body.sell_tax, factors=factors)
        return {"status": "up", "codes": body.codes,
                "strategy": body.strategy, "range": body.range,
                "exec_mode": body.exec_mode, "adjust_applied": adjust_applied,
                **result}
