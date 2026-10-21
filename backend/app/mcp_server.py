"""honey-money MCP server：让 agent 调用本应用做回测与行情查询（stdio/HTTP）。

形态：独立 MCP server（官方 mcp SDK FastMCP），工具层经 httpx 调现有
FastAPI REST——主应用零改动。缺省 stdio 传输（agent 侧子进程）；
`--http --port N` 切 streamable HTTP（compose 里的 mcp 服务，远程 agent
经 http://<NAS>:<port>/mcp 直连）。

回测工具对应两类任务形态：
- 全市场（分钟~小时级后台任务）：提交/轮询/取数拆分，规避 MCP 单次调用超时；
- 单股/批量（秒级同步）：run_backtest 一个工具拿结果，大体积曲线降采样控上下文。

错误约定：工具不抛异常——HTTP 错误/网络不可达一律转成含状态码与下一步
指引的可读文本返回，agent 据此自愈（重试/换参/先取消再提交）。

启动：cd backend && python -m app.mcp_server [--http --host H --port P]
（agent 侧 stdio 配置的 command+args）；目标后端用 HONEY_MONEY_BACKEND_URL
覆盖（缺省 http://127.0.0.1:8000）。
"""

import argparse
import json
import os

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

# host 必须非 localhost 系（0.0.0.0 对所有接口），否则 FastMCP 构造时自动启用
# DNS rebinding 保护（Host 白名单仅 localhost）——远程 agent 用 IP/域名直连会
# 被 421 Invalid Host header 拒绝。显式关闭该保护：本服务无鉴权、仅内网
# （与 backend 同安全级别），工具全为回测/查询类操作，rebinding 最坏后果是
# 触发一次回测任务；对外公网暴露请走带鉴权的反向代理。
mcp = FastMCP("honey-money", host="0.0.0.0",
              transport_security=TransportSecuritySettings(
                  enable_dns_rebinding_protection=False))

BASE_URL = os.environ.get(
    "HONEY_MONEY_BACKEND_URL", "http://127.0.0.1:8000").rstrip("/")

# 与 backtest_full_api 的白名单一致（momentum 在全市场为组合级：输出单组合
# 行 _PORTFOLIO，排行表仅 1 行）
STRATEGIES = ("buy_hold", "ma_cross", "donchian", "bollinger", "grid",
              "momentum")
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


def _downsample(points, cap: int = 120) -> list:
    """曲线降采样到 ≤cap 点（等距取整点索引，首尾必保留）——控 agent 上下文。"""
    points = points or []
    if len(points) <= cap:
        return points
    step = (len(points) - 1) / (cap - 1)
    return [points[round(i * step)] for i in range(cap)]


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
    momentum（动量轮动）为组合级：按月末动量取全市场前 top_k 等权持有，
    结果为单组合行 _PORTFOLIO（排行表无逐股明细，组合净值在 curves）；
    params 可加 "rebalance"（"month" 每月末调仓=默认 / "week" 每周末）；
    池默认截断至 K 线数最多的 1000 只（filters.max_universe 可调，0=不限）。

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
        return f"未知策略: {strategy}。可选：{STRATEGY_HINT}"
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


SINGLE_STRATEGIES = ("buy_hold", "ma_cross", "donchian", "bollinger",
                     "grid", "momentum")
SINGLE_STRATEGY_HINT = "、".join(SINGLE_STRATEGIES)


@mcp.tool()
def run_backtest(strategy: str, codes: list[str], range: str = "3y",
                 params: dict | None = None, exec_mode: str = "next_open",
                 commission: float = 2.5e-4, sell_tax: float = 5e-4) -> str:
    """对指定股票（1~多只）做同步回测，秒级直接返回结果（无需轮询）。

    与 run_full_backtest 的区别：本工具只测你指定的代码、同步返回；
    momentum（动量轮动）在此可用，但需要 ≥2 只标的。

    Args:
        strategy: """ + SINGLE_STRATEGY_HINT + """
        codes: 股票代码列表，如 ["600519"] 或 ["600519", "000001"]
        range: 回看区间，"1y"/"3y"/"5y"/"all"
        params: 策略参数（ma_cross: fast/slow；momentum: window/top_k）
        exec_mode: "next_open"（信号次日开盘成交）或 "close"（当日收盘）
        commission: 买入佣金率（默认 2.5e-4）
        sell_tax: 卖出成本率（默认 5e-4）
    """
    if strategy not in SINGLE_STRATEGIES:
        return f"未知策略: {strategy}。可选：{SINGLE_STRATEGY_HINT}"
    if range not in RANGES:
        return f"非法区间: {range}。可选：{'、'.join(RANGES)}"
    cleaned = [c.strip().upper() for c in (codes or []) if c and c.strip()]
    if not cleaned:
        return "codes 不能为空：请传入至少一个股票代码，如 [\"600519\"]"
    base = {"strategy": strategy, "range": range, "params": params or {},
            "exec_mode": exec_mode, "commission": commission,
            "sell_tax": sell_tax}

    def _one(code: str) -> dict:
        """单标的策略逐只回测（对齐前端批量语义：后端「多代码+单标的」
        存在信号单列 vs close 全列对齐的既有边界，前端从不踩、不在引擎外触发）。"""
        try:
            result = _api("POST", "/api/backtest",
                          json_body={**base, "codes": [code]})
        except httpx.HTTPError as exc:
            return {"code": code, "failed": _err_text(exc)}
        if result.get("status") == "down":
            return {"code": code,
                    "failed": f"数据库不可达（{result.get('detail')}）"}
        if result.get("status") != "up":
            return {"code": code,
                    "failed": result.get("message") or _dumps(result)}
        return {"code": code, "metrics": result.get("metrics") or {},
                "trades": result.get("trades") or [],
                "gap_dates": result.get("gap_dates") or [],
                "equity": _downsample(result.get("equity")),
                "drawdown": _downsample(result.get("drawdown")),
                "adjust_applied": result.get("adjust_applied")}

    if strategy == "momentum":                       # 横截面组合：一次调用
        return _format_batch_one(cleaned, base)
    if len(cleaned) == 1:                            # 单只：完整格式（含曲线/逐笔）
        return _format_batch_one(cleaned, base)

    per = [_one(c) for c in cleaned]
    ok = [r for r in per if "failed" not in r]
    failed = [r for r in per if "failed" in r]
    lines = [f"批量回测完成：{strategy} {len(ok)}/{len(per)} 只成功"]
    for r in ok:
        m = r["metrics"]
        lines.append(
            f"  {r['code']}: 总收益 {(m.get('total_return') or 0) * 100:.2f}%"
            f" · 年化 {(m.get('annual_return') or 0) * 100:.2f}%"
            f" · 最大回撤 {(m.get('max_drawdown') or 0) * 100:.2f}%"
            f" · 夏普 {m.get('sharpe')}"
            f" · 交易 {len(r.get('trades') or [])} 笔"
            + ("" if r.get("adjust_applied") else "（不复权口径）"))
    for r in failed:
        lines.append(f"  {r['code']}: 失败——{r['failed']}")
    lines.append("（批量模式每股仅摘要指标；单只调用本工具可另取降采样曲线与逐笔交易）")
    return "\n".join(lines)


def _format_batch_one(codes: list[str], base: dict) -> str:
    """momentum（多标的组合）一次调用并按单次格式输出。"""
    try:
        result = _api("POST", "/api/backtest",
                      json_body={**base, "codes": codes})
    except httpx.HTTPError as exc:
        return _err_text(exc)
    if result.get("status") == "down":
        return f"回测失败：数据库不可达（{result.get('detail')}）"
    if result.get("status") != "up":
        return "回测失败：" + _dumps(result)
    metrics = result.get("metrics") or {}
    payload = {"metrics": metrics,
               "trades": result.get("trades") or [],
               "gap_dates": result.get("gap_dates") or [],
               "equity": _downsample(result.get("equity")),
               "drawdown": _downsample(result.get("drawdown")),
               "adjust_applied": result.get("adjust_applied")}
    summary = (f"回测完成：{base['strategy']} {'/'.join(codes)} "
               f"range={base['range']} · "
               f"总收益 {(metrics.get('total_return') or 0) * 100:.2f}% · "
               f"年化 {(metrics.get('annual_return') or 0) * 100:.2f}% · "
               f"最大回撤 {(metrics.get('max_drawdown') or 0) * 100:.2f}% · "
               f"夏普 {metrics.get('sharpe')} · 交易 {len(payload['trades'])} 笔"
               + ("" if payload.get("adjust_applied")
                  else "（复权因子缺失，降级不复权口径）"))
    return summary + "\n\n原始数据（equity/drawdown 已降采样 ≤120 点）：\n" + \
        _dumps(payload)


@mcp.tool()
def list_stocks(q: str = "", page: int = 0, size: int = 50,
                source: str = "", delisted: str = "") -> str:
    """搜索已入库股票列表（代码/名称模糊匹配），含最新价与退市标记。

    Args:
        q: 关键词（代码或名称片段，如 "600519" 或 "茅台"）
        page: 页码（0 起）
        size: 每页条数（≤100）
        source: 按数据源过滤（"sina"/"eastmoney"，空=全部）
        delisted: 退市过滤，"true"=仅退市、"false"=仅在市、空=全部
    """
    size = max(1, min(int(size), 100))
    page = max(0, int(page))
    if source not in ("sina", "eastmoney"):
        source = ""
    if delisted not in ("true", "false"):
        delisted = ""
    try:
        data = _api("GET", "/api/stocks",
                    params={"q": q, "limit": size, "offset": page * size,
                            "source": source, "delisted": delisted})
    except httpx.HTTPError as exc:
        return _err_text(exc)
    head = (f"共 {data.get('total')} 只（第 {page + 1} 页，每页 {size}）"
            if data.get("status") == "up" else
            f"查询失败：{data.get('detail') or data.get('status')}")
    return head + "\n" + _dumps(data.get("stocks") or [])


@mcp.tool()
def get_stock_bars(code: str, limit: int = 60, offset: int = 0,
                   source: str = "", adjust: str = "") -> str:
    """取单股日K线（倒序：最新在前）。

    Args:
        code: 6 位股票代码，如 "600519"
        limit: 根数（≤1000；默认 60 根≈近一季度）
        offset: 偏移（往前翻历史）
        source: 数据源 "sina"/"eastmoney"，空=不过滤（同日两源各一行时勿留空）
        adjust: "qfq"=前复权（最新价=真实价），空=不复权
    """
    limit = max(1, min(int(limit), 1000))
    offset = max(0, int(offset))
    if source not in ("sina", "eastmoney"):
        source = ""
    if adjust != "qfq":
        adjust = ""
    try:
        data = _api("GET", f"/api/stocks/{code}/bars",
                    params={"limit": limit, "offset": offset,
                            "source": source, "adjust": adjust})
    except httpx.HTTPError as exc:
        return _err_text(exc)
    if data.get("status") != "up":
        return f"查询失败：{data.get('detail') or data.get('status')}"
    head = f"{code} 共返回 {data.get('count')} 根日K" + \
        ("" if data.get("adjust_applied", True)
         else "（复权因子缺失，降级不复权）")
    return head + "\n" + _dumps(data.get("bars") or [])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="honey-money MCP server")
    parser.add_argument("--http", action="store_true",
                        help="streamable HTTP 传输（缺省 stdio）")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8081)
    args = parser.parse_args()
    if args.http:
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.run(transport="streamable-http")
    else:
        mcp.run()          # stdio（默认传输）
