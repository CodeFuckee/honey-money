"""全量回测 API：教学页「全量回测」按钮的后端（后台任务 + 进度 + 落库）。

POST /api/backtest/full                     创建任务（动量 400；已有 running 409）
GET  /api/backtest/full/runs                历史 run 列表（回看）
GET  /api/backtest/full/runs/{id}           run 详情（进度/统计）
GET  /api/backtest/full/runs/{id}/rows      明细分页（列排序）
GET  /api/backtest/full/runs/{id}/top       Top-N 归一化净值曲线（现算）
POST /api/backtest/full/runs/{id}/cancel    取消
GET  /api/backtest/full/runs/{id}/export    CSV 导出

store 调用一律经 `bf.` 模块属性访问（测试 monkeypatch 接缝）。
"""

import datetime as dt
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Response
from pydantic import BaseModel, Field, field_validator

from . import backtest_full as bf
from . import config
from .backtest.data import load_history
from .backtest.runner import run_strategy
from .backtest_full import SORTABLE, _range_start


class FullBacktestIn(BaseModel):
    strategy: Literal["buy_hold", "ma_cross", "donchian", "bollinger",
                      "grid", "momentum"]
    range: Literal["1y", "3y", "5y", "all"] = "3y"
    params: dict = Field(default_factory=dict)
    exec_mode: Literal["close", "next_open"] = "next_open"
    commission: float = Field(default=2.5e-4, ge=0, le=0.01)
    sell_tax: float = Field(default=5e-4, ge=0, le=0.01)
    adjust: bool = True
    filters: dict = Field(default_factory=dict)

    @field_validator("params")
    @classmethod
    def _params_json_safe(cls, v):
        return {k: val for k, val in v.items() if val is not None}


def register_full_backtest_routes(app: FastAPI) -> None:
    @app.post("/api/backtest/full")
    async def create_full(body: FullBacktestIn):
        if body.strategy == "momentum":
            raise HTTPException(400, "动量轮动暂不支持全量回测（横截面组合策略）")
        if bf.has_running_run():
            raise HTTPException(409, "已有全量回测任务在运行，请等待完成或取消")
        spec = {
            "strategy": body.strategy, "range": body.range,
            "params": body.params, "exec_mode": body.exec_mode,
            "commission": body.commission, "sell_tax": body.sell_tax,
            "adjust": body.adjust,
            "filters": {**bf.DEFAULT_FILTERS, **body.filters},
        }
        run_id = bf.create_run(spec)
        bf.start_run_job(run_id, spec)
        return {"run_id": run_id}

    @app.get("/api/backtest/full/runs")
    def full_runs():
        return {"runs": bf.list_runs()}

    @app.get("/api/backtest/full/runs/{run_id}")
    def full_run_detail(run_id: int):
        run = bf.get_run(run_id)
        if run is None:
            raise HTTPException(404, "任务不存在")
        # 运行中实时部分统计（逐股上屏的统计卡数据源；完成态用落库终值）
        # 统计一律动态重算：run 15 等历史 run 落库的是旧形状（无 best/worst/
        # delisted），落库缓存反而制造新旧形状分裂；重算仅毫秒级（两列聚合）。
        if run["status"] in ("running", "done") and run["status"] != "failed":
            try:
                run["stats"] = bf.compute_stats(bf.ok_rows(run_id)) or run.get("stats")
            except Exception:
                pass
        return run

    @app.get("/api/backtest/full/runs/{run_id}/rows")
    def full_run_rows(run_id: int, sort: str = "total_return",
                      desc: bool = True, page: int = 0,
                      size: int = Query(default=50, ge=1, le=200)):
        if bf.get_run(run_id) is None:
            raise HTTPException(404, "任务不存在")
        if sort not in SORTABLE:
            sort = "total_return"
        return bf.get_rows(run_id, sort, desc, page, size)

    @app.get("/api/backtest/full/runs/{run_id}/top")
    def full_run_top(run_id: int, n: int = Query(default=20, ge=1, le=100)):
        """Top-N 归一化净值曲线：按明细排行现算（明细表不存曲线，控体积）。"""
        run = bf.get_run(run_id)
        if run is None:
            raise HTTPException(404, "任务不存在")
        picks = bf.top_codes(run_id, n)
        start = _range_start(run["range"])
        try:
            bars = load_history(config.DATABASE_URL,
                                [p["code"] for p in picks], start=start,
                                end=dt.date.today().isoformat())
        except Exception:
            bars = {}
        curves = []
        for p in picks:
            code = p["code"]
            if code not in bars.get("close", {}).columns:
                continue
            single = {k: df[[code]] for k, df in bars.items()}
            try:
                result = run_strategy(
                    single, strategy=run["strategy"],
                    params=run.get("params") or {},
                    exec_mode=run.get("exec_mode", "next_open"),
                    commission=run.get("commission", 2.5e-4),
                    sell_tax=run.get("sell_tax", 5e-4))
            except Exception:
                continue
            equity = [[row[0], row[1]] for row in result.get("equity") or []]
            if equity:
                curves.append({"code": code, "name": p["name"],
                               "total_return": (result.get("metrics") or {})
                               .get("total_return"),
                               "equity": equity})
        return {"curves": curves}

    @app.post("/api/backtest/full/runs/{run_id}/cancel")
    def full_run_cancel(run_id: int):
        if bf.get_run(run_id) is None:
            raise HTTPException(404, "任务不存在")
        bf.request_cancel(run_id)
        return {"ok": True}

    @app.get("/api/backtest/full/runs/{run_id}/export")
    def full_run_export(run_id: int):
        if bf.get_run(run_id) is None:
            raise HTTPException(404, "任务不存在")
        csv_text = bf.export_csv(run_id)
        return Response(
            content="\ufeff" + csv_text,          # BOM：Excel 直开不乱码
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition":
                     f"attachment; filename=backtest_full_run{run_id}.csv"})
