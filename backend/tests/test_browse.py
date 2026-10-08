"""数据浏览接口测试：全离线（TestClient 下 PG 必然不可达 → 降级路径）+
SQL 行为用 sqlite 不现实，此处验证路由契约与降级形状；SQL 正确性由
真实数据冒烟（NAS 实机）覆盖。"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from app.main import create_app  # noqa: E402


@pytest.fixture
def client():
    return TestClient(create_app())


class TestStocksRoutes:
    def test_list_down_degrades_gracefully(self, client, monkeypatch):
        """PG 不可达：仍 200，status=down 且带空 stocks（不允许 5xx）。"""
        monkeypatch.setattr(
            "app.config.DATABASE_URL", "postgresql://nobody:x@127.0.0.1:59999/none")
        resp = client.get("/api/stocks")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "down"
        assert body["stocks"] == [] and body["total"] == 0

    def test_list_param_clamping(self, client):
        """limit/offset 越界值被钳制（limit≤200、offset≥0），不抛错。"""
        resp = client.get("/api/stocks", params={"limit": 99999, "offset": -5})
        assert resp.status_code == 200
        body = resp.json()
        assert body["limit"] == 200
        assert body["offset"] == 0

    def test_bars_down_degrades_gracefully(self, client, monkeypatch):
        monkeypatch.setattr(
            "app.config.DATABASE_URL", "postgresql://nobody:x@127.0.0.1:59999/none")
        resp = client.get("/api/stocks/600519/bars")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "down" and body["code"] == "600519"
        assert body["bars"] == []
