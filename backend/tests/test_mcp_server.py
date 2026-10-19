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
            assert names == {"run_full_backtest", "get_backtest_run",
                             "list_backtest_runs", "get_backtest_rows",
                             "cancel_backtest_run"}
            schema = next(t for t in tools if t["name"] == "run_full_backtest")
            assert "strategy" in schema["inputSchema"]["properties"]
        finally:
            proc.kill()
