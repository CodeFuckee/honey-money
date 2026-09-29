"""代理经纪测试：/get 解析、成功复用、失败降分、池空回退直连。

httpx.MockTransport 模拟 proxy_pool Web API，全离线。
"""

import asyncio
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.crawler.proxies import ProxyBroker  # noqa: E402


def make_broker(handler) -> tuple[ProxyBroker, list[httpx.Request]]:
    requests_seen: list[httpx.Request] = []

    def recording_handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(request)
        return handler(request)

    http = httpx.AsyncClient(
        transport=httpx.MockTransport(recording_handler), base_url="http://proxy_pool:5010"
    )
    return ProxyBroker(http=http), requests_seen


def run(coro):
    return asyncio.run(coro)


def ok_get(proxy="1.2.3.4:8080"):
    return lambda req: httpx.Response(200, json={"proxy": proxy, "https": False, "fail_count": 0})


class TestAcquire:
    def test_parses_dict_response(self):
        broker, _ = make_broker(ok_get("1.2.3.4:8080"))
        assert run(broker.acquire()) == "http://1.2.3.4:8080"

    def test_no_proxy_returns_none(self):
        """空池返回 {"code":0,"src":"no proxy"} → None（调用方回退直连）。"""
        broker, _ = make_broker(lambda req: httpx.Response(200, json={"code": 0, "src": "no proxy"}))
        assert run(broker.acquire()) is None

    def test_empty_proxy_value_returns_none(self):
        broker, _ = make_broker(lambda req: httpx.Response(200, json={"proxy": "", "code": 0}))
        assert run(broker.acquire()) is None

    def test_pool_unreachable_returns_none(self):
        """proxy_pool 服务本身挂了 → None，不抛异常（爬虫降级为直连而不是崩溃）。"""
        def handler(request):
            raise httpx.ConnectError("pool down")
        broker, _ = make_broker(handler)
        assert run(broker.acquire()) is None

    def test_acquire_reuses_proxy_until_failure(self):
        """同一代理连续成功就复用，不重复打 /get/（免费代理稀缺，省着用）。"""
        broker, requests_seen = make_broker(ok_get("1.2.3.4:8080"))
        first = run(broker.acquire())
        second = run(broker.acquire())
        assert first == second == "http://1.2.3.4:8080"
        assert len([r for r in requests_seen if r.url.path == "/get/"]) == 1
        run(broker.report_failure(first))
        third = run(broker.acquire())
        assert len([r for r in requests_seen if r.url.path == "/get/"]) == 2
        assert third == "http://1.2.3.4:8080"  # /get/ 再次返回同一格式代理


class TestFailureReport:
    def test_failure_calls_delete_endpoint(self):
        """失败代理要上报 /delete/ 让池子降分。"""
        broker, requests_seen = make_broker(ok_get("1.2.3.4:8080"))
        proxy = run(broker.acquire())
        run(broker.report_failure(proxy))
        deletes = [r for r in requests_seen if r.url.path == "/delete/"]
        assert len(deletes) == 1
        assert deletes[0].url.params["proxy"] == "1.2.3.4:8080"

    def test_failure_with_none_proxy_is_noop(self):
        """直连（None）失败无可降分，静默忽略。"""
        broker, requests_seen = make_broker(ok_get())
        run(broker.report_failure(None))
        assert [r for r in requests_seen if r.url.path == "/delete/"] == []

    def test_failure_clears_cached_proxy(self):
        broker, _ = make_broker(ok_get("1.2.3.4:8080"))
        proxy = run(broker.acquire())
        run(broker.report_failure(proxy))
        assert broker._cached is None
