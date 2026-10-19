"""MCP server 测试：工具层全离线（monkeypatch _api）+ stdio 协议冒烟。

工具层：断言提交 body 形状、错误转可读文本（不抛异常——MCP 工具错误
以文本返回 agent 才能读懂并自愈）、参数白名单与钳制。
协议层：subprocess 起 stdio server，JSON-RPC 握手 + tools/list 冒烟
（不含 tools/call——无后端；call 逻辑与工具层同一函数，已由上面覆盖）。
"""

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import mcp_server as ms  # noqa: E402


class FakeApi:
    """替换 ms._api：记录调用并返回预置结果/异常。"""

    def __init__(self, result=None, exc=None):
        self.calls = []
        self.result = result or {}
        self.exc = exc

    def __call__(self, method, path, *, json_body=None, params=None):
        self.calls.append((method, path, json_body, params))
        if self.exc is not None:
            raise self.exc
        return self.result


def _status_error(status: int, detail: str) -> httpx.HTTPStatusError:
    req = httpx.Request("POST", "http://x")
    resp = httpx.Response(status, request=req, json={"detail": detail})
    return httpx.HTTPStatusError(f"{status}", request=req, response=resp)


class TestRunFullBacktest:
    def test_submit_ma_cross_returns_run_id(self, monkeypatch):
        """正常提交：POST body 形状正确，文本含 run_id 与轮询指引。"""
        fake = FakeApi(result={"run_id": 42})
        monkeypatch.setattr(ms, "_api", fake)
        out = ms.run_full_backtest(
            "ma_cross", params={"fast": 5, "slow": 20}, range="3y")
        method, path, body, _ = fake.calls[0]
        assert (method, path) == ("POST", "/api/backtest/full")
        assert body["strategy"] == "ma_cross"
        assert body["params"] == {"fast": 5, "slow": 20}
        assert body["range"] == "3y"
        assert body["exec_mode"] == "next_open"      # 默认值
        assert body["reuse"] is True
        assert "42" in out and "get_backtest_run" in out

    def test_conflict_returns_guidance_not_exception(self, monkeypatch):
        """409（已有任务运行）：转可读文本，指向 list/cancel，不抛异常。"""
        fake = FakeApi(exc=_status_error(409, "已有全量回测任务在运行"))
        monkeypatch.setattr(ms, "_api", fake)
        out = ms.run_full_backtest("ma_cross")
        assert "409" in out and "cancel_backtest_run" in out

    def test_unreachable_backend_returns_hint(self, monkeypatch):
        """后端不可达：网络错误转提示文本（含目标 URL），不抛异常。"""
        req = httpx.Request("POST", "http://x")
        fake = FakeApi(exc=httpx.ConnectError("refused", request=req))
        monkeypatch.setattr(ms, "_api", fake)
        out = ms.run_full_backtest("ma_cross")
        assert "不可达" in out or "refused" in out

    def test_momentum_rejected_before_request(self, monkeypatch):
        """momentum（横截面组合）后端不支持：直接校验拒绝，不发请求。"""
        fake = FakeApi()
        monkeypatch.setattr(ms, "_api", fake)
        out = ms.run_full_backtest("momentum")
        assert fake.calls == []
        assert "ma_cross" in out                     # 提示可用策略

    def test_unknown_strategy_rejected(self, monkeypatch):
        fake = FakeApi()
        monkeypatch.setattr(ms, "_api", fake)
        assert "ma_cross" in ms.run_full_backtest("nope")
        assert fake.calls == []

    def test_invalid_range_rejected(self, monkeypatch):
        fake = FakeApi()
        monkeypatch.setattr(ms, "_api", fake)
        assert "1y" in ms.run_full_backtest("ma_cross", range="2y")
        assert fake.calls == []


class TestGetBacktestRun:
    def test_running_progress_text(self, monkeypatch):
        """运行中：文本含进度 done/total 与 run_id。"""
        fake = FakeApi(result={"id": 7, "status": "running",
                               "done": 30, "total": 100, "failed": 2})
        monkeypatch.setattr(ms, "_api", fake)
        out = ms.get_backtest_run(7)
        method, path, _, _ = fake.calls[0]
        assert (method, path) == ("GET", "/api/backtest/full/runs/7")
        assert "running" in out and "30" in out and "100" in out

    def test_done_summary_text(self, monkeypatch):
        fake = FakeApi(result={"id": 7, "status": "done", "done": 100,
                               "total": 100, "failed": 0,
                               "stats": {"ok": 98}})
        monkeypatch.setattr(ms, "_api", fake)
        out = ms.get_backtest_run(7)
        assert "done" in out and "get_backtest_rows" in out   # 指引取明细

    def test_not_found_text(self, monkeypatch):
        monkeypatch.setattr(ms, "_api", FakeApi(
            exc=_status_error(404, "任务不存在")))
        assert "404" in ms.get_backtest_run(999)


class TestListRuns:
    def test_lists_runs(self, monkeypatch):
        fake = FakeApi(result={"runs": [{"id": 3, "status": "done",
                                         "strategy": "ma_cross"}]})
        monkeypatch.setattr(ms, "_api", fake)
        out = ms.list_backtest_runs()
        assert fake.calls[0][1] == "/api/backtest/full/runs"
        assert "3" in out and "ma_cross" in out


class TestRows:
    def test_rows_passes_params_clamped(self, monkeypatch):
        """明细分页：sort/size 钳制到白名单/上限（size≤200）。"""
        fake = FakeApi(result={"rows": [], "total": 0})
        monkeypatch.setattr(ms, "_api", fake)
        ms.get_backtest_rows(5, sort="hacker", desc=False,
                             page=2, size=9999, delisted="nope")
        _, path, _, params = fake.calls[0]
        assert path == "/api/backtest/full/runs/5/rows"
        assert params["sort"] == "total_return"      # 非白名单回默认
        assert params["size"] == 200
        assert params["delisted"] == "all"
        assert params["desc"] is False and params["page"] == 2


class TestCancel:
    def test_cancel_ok(self, monkeypatch):
        fake = FakeApi(result={"ok": True})
        monkeypatch.setattr(ms, "_api", fake)
        out = ms.cancel_backtest_run(9)
        assert fake.calls[0][:2] == ("POST", "/api/backtest/full/runs/9/cancel")
        assert "9" in out


class TestRunSingleBacktest:
    """二期：同步单股/批量回测工具（POST /api/backtest，秒级）。"""

    def test_body_shape_and_result_summary(self, monkeypatch):
        """body 形状正确；大体积曲线降采样后文本仍含 metrics 与 trades。"""
        equity = [[f"2026-01-{d:02d}", float(d)] for d in range(1, 10)]
        fake = FakeApi(result={"status": "up", "codes": ["600519"],
                               "metrics": {"total_return": 0.5},
                               "equity": equity, "drawdown": equity,
                               "trades": [{"pnl": 1.0}], "gap_dates": []})
        monkeypatch.setattr(ms, "_api", fake)
        out = ms.run_backtest("ma_cross", ["600519"],
                              params={"fast": 5, "slow": 20}, range="1y")
        method, path, body, _ = fake.calls[0]
        assert (method, path) == ("POST", "/api/backtest")
        assert body["codes"] == ["600519"]
        assert body["strategy"] == "ma_cross" and body["range"] == "1y"
        assert "total_return" in out and "equity" in out

    def test_equity_downsampled_to_cap(self, monkeypatch):
        """3y 曲线 ~750 点：降采样到 ≤120 点（控 agent 上下文体积）。"""
        equity = [[str(i), float(i)] for i in range(750)]
        fake = FakeApi(result={"status": "up", "metrics": {}, "equity": equity,
                               "drawdown": equity, "trades": [], "gap_dates": []})
        monkeypatch.setattr(ms, "_api", fake)
        out = ms.run_backtest("buy_hold", ["600519"])
        payload = json.loads(out[out.index("\n{") + 1:])
        assert len(payload["equity"]) <= 120
        assert payload["equity"][0] == equity[0]      # 首点保留
        assert payload["equity"][-1] == equity[-1]    # 尾点保留

    def test_down_status_passthrough(self, monkeypatch):
        """PG 不可达降级（status=down）：文本可读不抛异常。"""
        fake = FakeApi(result={"status": "down", "detail": "pg boom"})
        monkeypatch.setattr(ms, "_api", fake)
        out = ms.run_backtest("buy_hold", ["600519"])
        assert "数据库不可达" in out and "pg boom" in out

    def test_invalid_strategy_rejected_before_request(self, monkeypatch):
        fake = FakeApi()
        monkeypatch.setattr(ms, "_api", fake)
        out = ms.run_backtest("nope", ["600519"])
        assert fake.calls == [] and "ma_cross" in out

    def test_empty_codes_rejected(self, monkeypatch):
        fake = FakeApi()
        monkeypatch.setattr(ms, "_api", fake)
        out = ms.run_backtest("ma_cross", [])
        assert fake.calls == [] and "600519" in out or "代码" in out

    def test_momentum_needs_two_codes_hint(self, monkeypatch):
        """momentum 单只：后端 400 → 文本含"至少 2 只"指引。"""
        fake = FakeApi(exc=_status_error(400, "动量轮动需要至少 2 只标的"))
        monkeypatch.setattr(ms, "_api", fake)
        out = ms.run_backtest("momentum", ["600519"])
        assert "2 只" in out


class TestListStocks:
    def test_list_stocks_passes_params(self, monkeypatch):
        fake = FakeApi(result={"status": "up", "total": 2, "stocks": [
            {"code": "000003", "name": "PT金田Ａ", "delisted": True}]})
        monkeypatch.setattr(ms, "_api", fake)
        out = ms.list_stocks(q="退", page=1, size=30, delisted="true")
        _, path, _, params = fake.calls[0]
        assert path == "/api/stocks"
        assert params == {"q": "退", "limit": 30, "offset": 30,
                          "source": "", "delisted": "true"}
        assert "000003" in out

    def test_list_stocks_clamps_size(self, monkeypatch):
        fake = FakeApi(result={"status": "up", "total": 0, "stocks": []})
        monkeypatch.setattr(ms, "_api", fake)
        ms.list_stocks(size=9999, page=-3)
        _, _, _, params = fake.calls[0]
        assert params["limit"] == 100                  # 钳到上限
        assert params["offset"] == 0                   # 负页码钳 0


class TestGetStockBars:
    def test_bars_params_and_clamp(self, monkeypatch):
        fake = FakeApi(result={"status": "up", "count": 2, "bars": [
            {"date": "2026-10-09", "close": 9.18}]})
        monkeypatch.setattr(ms, "_api", fake)
        out = ms.get_stock_bars("600519", limit=9999, source="sina",
                                adjust="qfq")
        _, path, _, params = fake.calls[0]
        assert path == "/api/stocks/600519/bars"
        assert params == {"limit": 1000, "offset": 0,        # 钳到上限
                          "source": "sina", "adjust": "qfq"}
        assert "9.18" in out


class TestHttpTransportSmoke:
    """streamable HTTP 传输冒烟：子进程 --http 起服务，官方 client 真连。"""

    pytest.importorskip("mcp", reason="mcp SDK 未安装")

    def test_http_tools_list(self, tmp_path):
        import socket

        import anyio
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client

        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        # 优先用非回环本机 IP 连接（等价远程 agent 场景）：曾因 FastMCP 对
        # localhost 构造自动启用 DNS rebinding 保护，IP 直连被 421 拒绝
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("10.255.255.255", 1))
            host = probe.getsockname()[0] or "127.0.0.1"
        except OSError:
            host = "127.0.0.1"
        finally:
            probe.close()
        backend_dir = Path(__file__).resolve().parents[1]
        proc = subprocess.Popen(
            [sys.executable, "-m", "app.mcp_server",
             "--http", "--host", "0.0.0.0", "--port", str(port)],
            cwd=backend_dir, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True)

        async def main():
            async with streamablehttp_client(
                    f"http://{host}:{port}/mcp") as (r, w, _):
                async with ClientSession(r, w) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    return {t.name for t in tools.tools}

        try:
            deadline = time.time() + 15
            last = None
            while time.time() < deadline:            # 服务就绪轮询
                try:
                    names = anyio.run(main)
                    break
                except Exception as exc:             # 连接被拒 → 服务未就绪
                    last = exc
                    time.sleep(0.5)
            else:
                raise AssertionError(f"HTTP server 未就绪: {last}")
            assert {"run_full_backtest", "get_backtest_run",
                    "list_backtest_runs", "get_backtest_rows",
                    "cancel_backtest_run", "run_backtest", "list_stocks",
                    "get_stock_bars"} <= names
        finally:
            proc.kill()


class TestStdioProtocol:
    """stdio 协议冒烟：真实子进程握手 + tools/list（mcp 未装则跳过）。"""

    pytest.importorskip("mcp", reason="mcp SDK 未安装")

    def test_initialize_and_tools_list(self, tmp_path):
        backend_dir = Path(__file__).resolve().parents[1]
        proc = subprocess.Popen(
            [sys.executable, "-m", "app.mcp_server"],
            cwd=backend_dir, stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        def send(line):
            proc.stdin.write(json.dumps(line) + "\n")
            proc.stdin.flush()

        def recv():
            line = proc.stdout.readline()
            assert line, f"server 无响应: {proc.stderr.read()}"
            return json.loads(line)

        try:
            send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "0"}}})
            init = recv()
            assert init["result"]["serverInfo"]["name"] == "honey-money"
            send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            tools = recv()["result"]["tools"]
            names = {t["name"] for t in tools}
            assert {"run_full_backtest", "get_backtest_run",
                    "list_backtest_runs", "get_backtest_rows",
                    "cancel_backtest_run", "run_backtest", "list_stocks",
                    "get_stock_bars"} <= names
            schema = next(t for t in tools if t["name"] == "run_full_backtest")
            assert "strategy" in schema["inputSchema"]["properties"]
        finally:
            proc.kill()
