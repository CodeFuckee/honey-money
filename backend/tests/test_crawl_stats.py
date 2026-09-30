"""爬虫数据统计接口测试：up 路径 / 表未初始化 / PG 不可达 / API 路由透传。

沿用 test_api.py 的离线模式：monkeypatch psycopg.connect 注入假连接。
"""

import datetime as dt
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # backend/ 加入导入路径

from app import crawl_stats as stats_mod  # noqa: E402
from app.main import create_app  # noqa: E402


class FakeCursor:
    def __init__(self, results):
        self.results = list(results)   # 每次 execute 依次弹出一个结果
        self.queries: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        self.queries.append(query)
        self._pending = self.results.pop(0) if self.results else None
        if isinstance(self._pending, Exception):
            raise self._pending

    def fetchone(self):
        return self._pending

    def fetchall(self):
        return self.fetchall_rows


class FakeConn:
    def __init__(self, results):
        self._cursor = FakeCursor(results)

    def cursor(self):
        return self._cursor

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def patch_connect(monkeypatch, results):
    conn = FakeConn(results)
    monkeypatch.setattr(stats_mod.psycopg, "connect", lambda *a, **kw: conn)
    return conn


class TestFetchCrawlStats:
    def test_up_with_data(self, monkeypatch):
        """正常路径：各计数与最近 run 汇总正确，待爬 = 清单 - 已覆盖，按来源分组。"""
        conn = patch_connect(monkeypatch, [
            (2_500_000,),                        # daily_bar count
            (5_561,),                            # stock_basic count
            (1_200,),                            # distinct code
            None,                                # by_source（execute 占位，数据走 fetchall）
        ])
        conn._cursor.fetchall_rows = [("sina", 1_500_000), ("eastmoney", 1_000_000)]
        conn._cursor.results.append(
            ("incremental", dt.datetime(2026, 9, 29, 21, 0, 0),   # last run
             dt.datetime(2026, 9, 29, 21, 2, 30), 5_561, 5_560, 1, 5_560, "sina"),
        )
        got = stats_mod.fetch_crawl_stats("dsn")
        assert got["status"] == "up"
        assert got["initialized"] is True
        assert got["daily_bars"] == 2_500_000
        assert got["stocks_listed"] == 5_561
        assert got["stocks_covered"] == 1_200
        assert got["stocks_pending"] == 4_361
        assert got["by_source"] == {"sina": 1_500_000, "eastmoney": 1_000_000}
        assert got["last_run"]["mode"] == "incremental"
        assert got["last_run"]["ok_stocks"] == 5_560
        assert got["last_run"]["failed_stocks"] == 1
        assert got["last_run"]["source"] == "sina"

    def test_no_runs_last_run_null(self, monkeypatch):
        conn = patch_connect(monkeypatch, [(10,), (100,), (5,)])
        conn._cursor.fetchall_rows = [("sina", 10)]
        conn._cursor.results.append(None)
        got = stats_mod.fetch_crawl_stats("dsn")
        assert got["last_run"] is None
        assert got["stocks_pending"] == 95
        assert got["by_source"] == {"sina": 10}

    def test_pending_never_negative(self, monkeypatch):
        """清库后清单未刷新等场景：已覆盖 > 清单时待爬按 0 展示。"""
        conn = patch_connect(monkeypatch, [(10,), (5,), (8,)])
        conn._cursor.fetchall_rows = []
        conn._cursor.results.append(None)
        got = stats_mod.fetch_crawl_stats("dsn")
        assert got["stocks_pending"] == 0

    def test_tables_missing_reports_uninitialized(self, monkeypatch):
        """爬虫从未运行（表不存在）：status=up + initialized=False + 全 0，不是 down。"""
        from psycopg import errors
        patch_connect(monkeypatch, [errors.UndefinedTable("daily_bar 不存在")])
        got = stats_mod.fetch_crawl_stats("dsn")
        assert got["status"] == "up"
        assert got["initialized"] is False
        assert got["daily_bars"] == 0
        assert got["stocks_pending"] == 0
        assert got["last_run"] is None

    def test_pg_down_degrades(self, monkeypatch):
        """PG 不可达：status=down 带原因，不抛异常。"""
        def boom(*a, **kw):
            raise ConnectionError("pg gone")
        monkeypatch.setattr(stats_mod.psycopg, "connect", boom)
        got = stats_mod.fetch_crawl_stats("bad-dsn")
        assert got["status"] == "down"
        assert got["detail"]


class TestApiRoute:
    def test_route_passes_through(self, monkeypatch):
        """/api/crawl/stats 返回 200 并透传统计结构。"""
        monkeypatch.setattr(
            "app.main.fetch_crawl_stats",
            lambda dsn: {"status": "up", "initialized": False, "daily_bars": 0,
                         "stocks_listed": 0, "stocks_covered": 0, "stocks_pending": 0,
                         "by_source": {}, "last_run": None},
        )
        client = TestClient(create_app())
        resp = client.get("/api/crawl/stats")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "up"
        assert set(body) == {"status", "initialized", "daily_bars", "stocks_listed",
                             "stocks_covered", "stocks_pending", "by_source", "last_run"}
