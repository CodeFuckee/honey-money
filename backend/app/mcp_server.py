"""honey-money MCP server：让 agent 调用本应用做全市场回测（stdio）。

形态：独立 stdio server（官方 mcp SDK FastMCP），工具层经 httpx 调现有
FastAPI REST（/api/backtest/full/*）——主应用零改动，二期支持远程 agent
只需换传输层，工具层原样复用。

工具设计对应异步任务全闭环：提交（run_full_backtest）→ 轮询
（get_backtest_run）→ 取数（get_backtest_rows）+ 历史（list_backtest_runs）
+ 取消（cancel_backtest_run）。全市场回测是分钟~小时级后台任务，MCP 单次
调用会超时，绝不做"提交并阻塞到完成"的组合工具。

错误约定：工具不抛异常——HTTP 错误/网络不可达一律转成含状态码与下一步
指引的可读文本返回，agent 据此自愈（重试/换参/先取消再提交）。

启动：cd backend && python -m app.mcp_server（agent 侧 stdio 配置的
command+args）；目标后端用 HONEY_MONEY_BACKEND_URL 覆盖
（缺省 http://127.0.0.1:8000）。
"""

import json
import os

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("honey-money")

BASE_URL = os.environ.get(
    "HONEY_MONEY_BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")

# 与 backtest_full_api / backtest_full 的白名单一致（momentum 横截面组合
# 策略不支持全量回测，后端 400，故不在候选内）
STRATEGIES = ("buy_hold", "ma_cross", "donchian", "bollinger", "grid")
RANGES = ("1y", "3y", "5y", "all")
SORTABLE = ("total_return", "annual_return", "max_drawdown", "sharpe",
            "win_rate", "trades", "code")
DELISTED_MODES = ("all", "only", "exclude")
STRATEGY_HINT = "、".join(STRATEGIES)


def _api(method: str, path: str, *, json_body=None, params=None) -> dict:
    """调后端 REST；非 2xx 抛 HTTPStatusError，网络错误抛 httpx.HTTPError。"""
    with httpx.Client(timeout=30.0) as client:      # stdio 低频调用，每次新建连接
        r = client.request(method, BASE_URL + path,
                           json=json_body, params=params)
        r.raise_for_status()
        return r.json()


def _err_text(exc: Exception) -> str:
    """异常 → 可读文本：状态码+detail，或网络不可达+目标地址。"""
    if isinstance(exc, httpx.HTTPStatusError):
        detail = ""
        try:
            detail = exc.response.json().get("detail", "")
        except Exception:
            detail = exc.response.text[:120]
        return f"后端返回 HTTP {exc.response.status_code}：{detail}"
    return (f"后端不可达（{BASE_URL}）：{type(exc).__name__}: {exc}；"
            f"请确认后端已启动或用 HONEY_MONEY_BACKEND_URL 指向正确地址")


def _dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=1, default=str)


@mcp.tool()
def run_full_backtest(strategy: str, range: str = "3y",
                      params: dict | None = None,
                      exec_mode: str = "next_open",
                      commission: float = 2.5e-4, sell_tax: float = 5e-4,
                      adjust: bool = True, filters: dict | None = None,
                      reuse: bool = True) -> str:
    """提交 A 股全市场回测后台任务（对全部入库股票逐股回测），立即返回 run_id。

    双均线交叉策略传 strategy="ma_cross"，params={"fast": 5, "slow": 20}
    （fast 上穿 slow 金叉买入、下穿死叉卖出；窗口单位=交易日）。

    Args:
        strategy: 策略名，可选 """ + STRATEGY_HINT + """
        range: 回看区间，"1y"/"3y"/"5y"/"all"
        params: 策略参数（ma_cross: fast/slow，默认 5/20）
        exec_mode: "next_open"（信号次日开盘成交，无未来函数）或 "close"（当日收盘，对照用）
        commission: 买入佣金率（默认 2.5e-4）
        sell_tax: 卖出成本率（默认 5e-4，含印花税）
        adjust: 前复权口径（默认 true）
        filters: 股票池过滤 {"exclude_st": true, "min_bars": 60, "include_delisted": true}
        reuse: 同参已完成任务的结果直接复用、仅重算失败股（默认 true，大幅提速）
    """
    if strategy not in STRATEGIES:
        return (f"未知或不支持的策略: {strategy}。可选：{STRATEGY_HINT}"
                f"（momentum 为横截面组合策略，不支持全量回测）")
    if range not in RANGES:
        return f"非法区间: {range}。可选：{'、'.join(RANGES)}"
    body = {"strategy": strategy, "range": range,
            "params": params or {}, "exec_mode": exec_mode,
            "commission": commission, "sell_tax": sell_tax,
            "adjust": adjust, "filters": filters or {}, "reuse": reuse}
    try:
        result = _api("POST", "/api/backtest/full", json_body=body)
    except httpx.HTTPStatusError as exc:
        text = _err_text(exc)
        if exc.response.status_code == 409:
            text += ("。已有任务在运行：用 list_backtest_runs 查看进度，"
                     "或 cancel_backtest_run 取消后再提交")
        return text
    except httpx.HTTPError as exc:
        return _err_text(exc)
    run_id = result.get("run_id")
    return (f"已提交全市场回测任务 run_id={run_id}"
            f"（strategy={strategy} range={range} params={body['params']}）。"
            f"全市场回测为后台任务，请用 get_backtest_run(run_id={run_id}) "
            f"轮询进度至 status=done，再用 get_backtest_rows 取明细排行。")


@mcp.tool()
def get_backtest_run(run_id: int) -> str:
    """查全市场回测任务状态与进度（轮询用）。

    返回 status（running/done/failed/canceled）、进度 done/total、失败数与
    汇总统计。status=done 后用 get_backtest_rows 取收益排行明细。
    """
    try:
        run = _api("GET", f"/api/backtest/full/runs/{run_id}")
    except httpx.HTTPError as exc:
        return _err_text(exc)
    status = run.get("status")
    total, done = run.get("total") or 0, run.get("done") or 0
    lines = [f"run_id={run_id} status={status} 进度 {done}/{total}"
             f"（失败 {run.get('failed') or 0}）"]
    if status == "running":
        lines.append("仍在运行，请稍后再次调用本工具轮询")
    elif status == "done":
        lines.append("已完成。用 get_backtest_rows(run_id=%d) 取逐股收益排行；"
                     "get_backtest_run 的 stats 字段为全市场汇总统计" % run_id)
    elif status == "failed":
        lines.append(f"失败原因：{run.get('error') or '未知'}")
    if run.get("stats"):
        lines.append("汇总统计：" + _dumps(run["stats"]))
    return "\n".join(lines) + "\n\n原始数据：\n" + _dumps(run)


@mcp.tool()
def list_backtest_runs() -> str:
    """列出历史全市场回测任务（id/策略/区间/状态/时间），可从中选 run_id 回看。"""
    try:
        data = _api("GET", "/api/backtest/full/runs")
    except httpx.HTTPError as exc:
        return _err_text(exc)
    runs = data.get("runs") or []
    if not runs:
        return "尚无历史任务"
    return _dumps(runs)


@mcp.tool()
def get_backtest_rows(run_id: int, sort: str = "total_return",
                      desc: bool = True, page: int = 0, size: int = 50,
                      delisted: str = "all") -> str:
    """取全市场回测任务的逐股明细（分页排行）。

    Args:
        run_id: 任务 id（run_full_backtest 返回或 list_backtest_runs 查得）
        sort: 排序字段，可选 total_return/annual_return/max_drawdown/sharpe/win_rate/trades/code
        desc: 降序（默认 true，收益最高在前）
        page: 页码（0 起）
        size: 每页条数（≤200）
        delisted: 退市股分组 all/only/exclude
    """
    if sort not in SORTABLE:
        sort = "total_return"
    size = max(1, min(int(size), 200))
    if delisted not in DELISTED_MODES:
        delisted = "all"
    try:
        data = _api("GET", f"/api/backtest/full/runs/{run_id}/rows",
                    params={"sort": sort, "desc": desc, "page": page,
                            "size": size, "delisted": delisted})
    except httpx.HTTPError as exc:
        return _err_text(exc)
    return _dumps(data)


@mcp.tool()
def cancel_backtest_run(run_id: int) -> str:
    """请求取消一个全市场回测任务（已完成的任务返回 ok 但无实际作用）。"""
    try:
        _api("POST", f"/api/backtest/full/runs/{run_id}/cancel")
    except httpx.HTTPError as exc:
        return _err_text(exc)
    return f"已请求取消 run_id={run_id}，用 get_backtest_run 确认终态"


if __name__ == "__main__":
    mcp.run()          # stdio（默认传输）
