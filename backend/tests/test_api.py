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
        for svc in body.values():
            assert set(svc) == {"status", "detail"}
            assert svc["status"] in ("up", "down")
