"""后端骨架测试：全部离线可跑（不依赖真实 PG/代理池）。"""

import pytest
from fastapi.testclient import TestClient

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/ 加入导入路径

from app.main import create_app  # noqa: E402


@pytest.fixture
def client():
    return TestClient(create_app())


class TestHealthz:
    def test_returns_ok_with_version(self, client):
        resp = client.get("/healthz")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["service"] == "honey-money-backend"
        assert body["version"]  # 版本号非空

    def test_openapi_served(self, client):
        # FastAPI 自带文档可用（调试依赖）
        assert client.get("/openapi.json").status_code == 200


class TestInfra:
    def test_postgres_down_degrades_gracefully(self, client, monkeypatch):
        """PG 不可达：仍返回 200，postgres.status=down 且带原因（不允许 5xx）。"""
        monkeypatch.setattr(
            "app.config.DATABASE_URL",
            "postgresql://nobody:x@127.0.0.1:59999/none",  # 必然连不上的端口
        )
        resp = client.get("/api/infra")
        assert resp.status_code == 200
        assert resp.json()["postgres"]["status"] == "down"
        assert resp.json()["postgres"]["detail"]

    def test_proxy_pool_down_degrades_gracefully(self, client, monkeypatch):
        monkeypatch.setattr(
            "app.config.PROXY_POOL_URL", "http://127.0.0.1:59998",
        )
        resp = client.get("/api/infra")
        assert resp.status_code == 200
        assert resp.json()["proxy_pool"]["status"] == "down"

    def test_bad_dsn_reported_not_raised(self, client, monkeypatch):
        """连接串非法也归为 down，不抛异常。"""
        monkeypatch.setattr("app.config.DATABASE_URL", "not-a-dsn")
        resp = client.get("/api/infra")
        assert resp.status_code == 200
        assert resp.json()["postgres"]["status"] == "down"

    def test_infra_shape(self, client):
        body = client.get("/api/infra").json()
        assert set(body) == {"postgres", "proxy_pool"}
        assert set(body["postgres"]) == {"status", "detail"}
        assert set(body["proxy_pool"]) >= {"status", "detail", "count", "count_useful"}
        for svc in body.values():
            assert svc["status"] in ("up", "down")

    # —— 代理池计数（对齐原型「已连接 · 在池 N」）——

    @staticmethod
    def _fake_pool(monkeypatch, *, get_json=None, count_json=None, count_status=200):
        class FakeResp:
            def __init__(self, code, text="", payload=None):
                self.status_code, self.text, self._p = code, text, payload
            def json(self):
                return self._p
        def fake_get(url, timeout=None):
            if url.endswith("/get/"):
                return FakeResp(200, text=get_json or '{"proxy": "1.2.3.4:8080"}')
            if url.endswith("/count"):
                return FakeResp(count_status, payload=count_json)
            return FakeResp(404)
        monkeypatch.setattr("app.infra.httpx.get", fake_get)
        monkeypatch.setattr("app.config.PROXY_POOL_URL", "http://proxy_pool:5010")

    def test_proxy_pool_up_with_count(self, client, monkeypatch):
        """探活 200 且 /count 正常 → up + count 字段 + detail 按原型口径。"""
        self._fake_pool(monkeypatch, count_json={"count": [12, 38]})
        body = client.get("/api/infra").json()["proxy_pool"]
        assert body["status"] == "up"
        assert body["count"] == 38 and body["count_useful"] == 12
        assert body["detail"] == "proxy_pool:5010 · 在池 38"

    def test_proxy_pool_up_count_unavailable(self, client, monkeypatch):
        """/count 挂了（404/坏格式）→ 仍 up，count=None，detail 只剩地址。"""
        self._fake_pool(monkeypatch, count_status=404)
        body = client.get("/api/infra").json()["proxy_pool"]
        assert body["status"] == "up"
        assert body["count"] is None and body["count_useful"] is None
        assert body["detail"] == "proxy_pool:5010"

    def test_proxy_pool_count_bad_payload(self, client, monkeypatch):
        """/count 返回意外结构 → 静默降级 count=None。"""
        self._fake_pool(monkeypatch, count_json={"unexpected": True})
        body = client.get("/api/infra").json()["proxy_pool"]
        assert body["status"] == "up"
        assert body["count"] is None
