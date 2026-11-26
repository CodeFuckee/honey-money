"""应用设置（app_settings 表）与回测运作期限下限参数化测试。

设置项 backtest_min_span_days（2026-11-24 用户定案）：回测年化的基金
运作期限下限（天），参数化引擎常量 MIN_SPAN_DAYS 的两处判定（全窗口
年化 :249 与止盈后当前轮年化 :237，同值联动）；取值 90~3650、默认 90、
不可关闭；修改后下一次回测生效（存量行不重算）。
"""

import sys
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi import FastAPI
from fastapi.testclient import TestClient


class _FakeConn:
    """conn/cursor 同体的假连接（沿 test_fund_dca_dd_ladder 惯例）。"""

    def __init__(self, one=None, raises=False):
        self.one = one
        self.raises = raises
        self.executed: list = []
        self.params: list = []
        self.committed = False

    def __enter__(self):
        if self.raises:
            raise RuntimeError("db down")
        return self

    def __exit__(self, *exc):
        return False

    def commit(self):
        self.committed = True

    def cursor(self):
        return self

    def execute(self, query, params=None):
        self.executed.append(query)
        self.params.append(params)

    def fetchone(self):
        return self.one


def _series(g):
    dates = pd.DatetimeIndex(pd.date_range("2024-01-01", periods=len(g)))
    adj = np.cumprod(1.0 + g)
    return dates, adj


class TestSettingsStore:
    def test_ensure_schema_ddl(self):
        from app import settings_store
        conn = _FakeConn()
        settings_store.ensure_settings_schema(conn)
        assert len(conn.executed) == 1
        assert "CREATE TABLE IF NOT EXISTS app_settings" in conn.executed[0]
        assert "PRIMARY KEY" in conn.executed[0]

    def test_get_setting_selects_by_key(self):
        from app import settings_store
        conn = _FakeConn(one=("120",))
        assert settings_store.get_setting(conn, settings_store.KEY_MIN_SPAN_DAYS) == "120"
        assert conn.executed and "app_settings" in conn.executed[0]
        assert conn.params[-1] == (settings_store.KEY_MIN_SPAN_DAYS,)

    def test_get_setting_missing_returns_none(self):
        from app import settings_store
        conn = _FakeConn(one=None)
        assert settings_store.get_setting(conn, settings_store.KEY_MIN_SPAN_DAYS) is None

    def test_set_setting_upsert(self):
        from app import settings_store
        conn = _FakeConn()
        settings_store.set_setting(conn, settings_store.KEY_MIN_SPAN_DAYS, "120")
        assert conn.executed and "ON CONFLICT" in conn.executed[0]
        assert conn.params[-1] == (settings_store.KEY_MIN_SPAN_DAYS, "120")

    def test_get_min_span_default_when_missing(self):
        from app import settings_store
        conn = _FakeConn(one=None)
        assert settings_store.get_min_span_days(conn) == 90
        assert settings_store.get_min_span_days(conn, default=100) == 100

    @pytest.mark.parametrize("raw, expected",
                             [("120", 120), ("90", 90), ("3650", 3650)])
    def test_get_min_span_parses_int(self, raw, expected):
        from app import settings_store
        conn = _FakeConn(one=(raw,))
        assert settings_store.get_min_span_days(conn) == expected

    @pytest.mark.parametrize("raw", ["abc", "89", "3651", "", "90.5"])
    def test_get_min_span_fallback_on_bad_value(self, raw):
        from app import settings_store
        conn = _FakeConn(one=(raw,))
        assert settings_store.get_min_span_days(conn) == 90

    @pytest.mark.parametrize("raw, expected",
                             [(120, 120), (90, 90), (3650, 3650), ("120", 120)])
    def test_validate_accepts(self, raw, expected):
        from app import settings_store
        assert settings_store.validate_min_span_days(raw) == expected

    @pytest.mark.parametrize("raw",
                             [89, 3651, 0, -5, "abc", None, True, 90.5, ""])
    def test_validate_rejects(self, raw):
        from app import settings_store
        with pytest.raises(ValueError):
            settings_store.validate_min_span_days(raw)


class TestEngineMinSpan:
    def test_full_xirr_span_boundary(self):
        """span = 末值-首值（不含首日）：87/88 天默认置 None，恰好 90 天
        过线（>=）——+20 只次新基金事件的同款边界。"""
        from app import dca_backtest as mod
        for points, expected_none in ((88, True), (89, True), (91, False)):
            dates, adj = _series(np.full(points, 0.002))
            res = mod.simulate(dates, adj, take_profit=99.0)
            assert res["rounds"] == 0
            assert (res["xirr"] is None) is expected_none, points

    def test_min_span_param_unlocks_short_window(self):
        from app import dca_backtest as mod
        dates, adj = _series(np.full(88, 0.002))
        assert mod.simulate(dates, adj, take_profit=99.0)["xirr"] is None
        res = mod.simulate(dates, adj, take_profit=99.0, min_span_days=60)
        assert res["xirr"] is not None

    def test_min_span_param_locks_longer_threshold(self):
        from app import dca_backtest as mod
        dates, adj = _series(np.full(91, 0.002))
        assert mod.simulate(dates, adj, take_profit=99.0)["xirr"] is not None
        res = mod.simulate(dates, adj, take_profit=99.0, min_span_days=200)
        assert res["xirr"] is None

    def test_boundary_span_equals_param_passes(self):
        """span == min_span_days 过线（>= 含等号）。"""
        from app import dca_backtest as mod
        dates, adj = _series(np.full(88, 0.002))
        res = mod.simulate(dates, adj, take_profit=99.0, min_span_days=87)
        assert res["xirr"] is not None

    def test_cur_xirr_follows_param(self):
        """止盈后当前轮同规则：01-15 止盈 → 距末日 60 天，默认 None、
        min_span_days=60 解出、61 再度置 None。"""
        from app import dca_backtest as mod
        g = np.zeros(75)
        g[14] = 0.25
        dates, adj = _series(g)
        assert mod.simulate(dates, adj)["cur_xirr"] is None
        res = mod.simulate(dates, adj, min_span_days=60)
        assert res["cur_xirr"] is not None
        assert mod.simulate(dates, adj, min_span_days=61)["cur_xirr"] is None

    def test_run_backtest_tp_passthrough(self):
        from app import dca_backtest as mod
        dates = pd.date_range("2024-01-01", periods=88)
        a = np.cumprod(1.0 + np.full(88, 0.002))
        nav = pd.DataFrame([("000009", d, round(float(a[i]), 6),
                             round(float(a[i]), 6), 0.2)
                            for i, d in enumerate(dates)],
                           columns=["code", "nav_date", "unit_nav",
                                    "accum_nav", "pct_chg"])
        basic = pd.DataFrame({"name": {"000009": "短窗测试混合A"},
                              "fund_type": {"000009": "混合型-偏股"},
                              "delisted": {"000009": False}})
        rows_def, _ = mod.run_backtest_tp(nav, basic, {})
        rows_low, _ = mod.run_backtest_tp(nav, basic, {}, min_span_days=60)
        assert all(r["full_xirr"] is None for r in rows_def)
        assert all(r["full_xirr"] is not None for r in rows_low)
        assert all(r["full_cur_xirr"] is None for r in rows_def)
        assert all(r["w10_xirr"] is None for r in rows_def)
        assert all(r["w10_xirr"] is not None for r in rows_low)

    def test_monkeypatch_constant_still_works(self):
        """缺省参数走 None→读模块常量（晚绑定），既有 fees 测试的
        monkeypatch MIN_SPAN_DAYS 用法不回归。"""
        from app import dca_backtest as mod
        dates, adj = _series(np.full(88, 0.002))
        original = mod.MIN_SPAN_DAYS
        mod.MIN_SPAN_DAYS = 0
        try:
            assert mod.simulate(dates, adj, take_profit=99.0)["xirr"] is not None
        finally:
            mod.MIN_SPAN_DAYS = original


class TestRunFullBacktestSettings:
    def test_full_backtest_reads_setting(self, monkeypatch):
        """日更编排读设置表并下传：get_min_span_days→60 时 87 天窗基金
        年化解出；回 90 时置 None（+20 事件的服务端路径锚）。"""
        from app import dca_backtest as mod
        from app import settings_store

        calls = []

        def fake_get(conn, default=90):
            calls.append(default)
            return 60

        dates = pd.date_range("2024-01-01", periods=88)
        a = np.cumprod(1.0 + np.full(88, 0.002))
        nav = pd.DataFrame([("000009", d, round(float(a[i]), 6),
                             round(float(a[i]), 6), 0.2)
                            for i, d in enumerate(dates)],
                           columns=["code", "nav_date", "unit_nav",
                                    "accum_nav", "pct_chg"])
        basic = pd.DataFrame({"name": {"000009": "短窗测试混合A"},
                              "fund_type": {"000009": "混合型-偏股"},
                              "delisted": {"000009": False}})

        class _Ctx:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(settings_store, "get_min_span_days", fake_get)
        monkeypatch.setattr(mod, "_load_codes", lambda conn: ["000009"])
        monkeypatch.setattr(mod, "_load_basic", lambda conn, codes: basic)
        monkeypatch.setattr(mod, "_load_scale", lambda conn: {})
        monkeypatch.setattr(mod, "_load_nav", lambda conn, codes: nav)
        monkeypatch.setattr(mod.psycopg, "connect", lambda *a, **k: _Ctx())
        _, _, tp_rows, _, _, _, _ = mod.run_full_backtest_tp("postgresql://fake")
        assert calls and calls[0] == 90        # 缺省参数传的是引擎常量 90
        assert all(r["full_xirr"] is not None for r in tp_rows)


class TestSettingsRoutes:
    def _client(self, one=None, raises=False):
        from app import settings_api

        def fake_connect(*a, **k):
            return _FakeConn(one, raises=raises)

        app = FastAPI()
        settings_api.register_settings_routes(app, connect=fake_connect)
        return TestClient(app)

    def test_get_up(self):
        from datetime import datetime as _dt
        from app import settings_api

        def fake_connect(*a, **k):
            return _RoutedConn([
                ("app_settings", ("120",)),
                ("FROM crawl_task", (7,)),
                ("FROM crawl_run", (False, False, _dt(2026, 11, 24, 9),
                                    _dt(2026, 11, 24, 9, 30))),
                ("fund_dca_fp", (26548, True))])

        app = FastAPI()
        settings_api.register_settings_routes(app, connect=fake_connect)
        body = TestClient(app).get("/api/settings").json()
        assert body["status"] == "up"
        assert body["settings"]["backtest_min_span_days"] == 120
        assert body["backtest_task"]["task_id"] == 7

    def test_get_down_degrades_200(self):
        client = self._client(raises=True)
        resp = client.get("/api/settings")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "down"
        assert body["settings"] is None

    def test_put_saves_and_commits(self):
        conn = _FakeConn()
        client = self._client()
        # 上面 _client 不暴露 conn——此处直接再造一个绑定固定 conn 的 client
        from app import settings_api
        app = FastAPI()
        settings_api.register_settings_routes(app, connect=lambda *a, **k: conn)
        client = TestClient(app)
        body = client.put("/api/settings/backtest-min-span",
                          json={"value": 120}).json()
        assert body == {"status": "up", "value": 120,
                        "note": "下一次回测生效"}
        assert conn.committed
        assert "ON CONFLICT" in conn.executed[-1]
        assert conn.params[-1] == (settings_api.settings_store.KEY_MIN_SPAN_DAYS,
                                   "120")

    @pytest.mark.parametrize("value", [89, 3651, "abc", None, True, 90.5])
    def test_put_invalid_400(self, value):
        client = self._client()
        resp = client.put("/api/settings/backtest-min-span", json={"value": value})
        assert resp.status_code == 400

    def test_put_missing_body_400(self):
        client = self._client()
        assert client.put("/api/settings/backtest-min-span",
                          json={}).status_code == 400

    def test_put_down_degrades_200(self):
        client = self._client(raises=True)
        resp = client.put("/api/settings/backtest-min-span", json={"value": 120})
        assert resp.status_code == 200
        assert resp.json()["status"] == "down"


class _RoutedConn:
    """按 SQL 关键词分发的假连接（GET /api/settings 多查询编排场景）。"""

    def __init__(self, routes):
        self.routes = routes      # [(SQL 关键词, fetchone 结果 | Exception)]
        self.executed: list = []
        self.params: list = []
        self.committed = False
        self._last = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def commit(self):
        self.committed = True

    def cursor(self):
        return self

    def execute(self, query, params=None):
        self.executed.append(query)
        self.params.append(params)
        self._last = None
        for keyword, value in self.routes:
            if keyword in query:
                if isinstance(value, Exception):
                    raise value
                self._last = value
                return

    def fetchone(self):
        return self._last

    def fetchall(self):
        return self._last or []


class TestSettingsBacktestTask:
    """GET /api/settings 的 backtest_task 字段：任务定位/运行中判定/
    增量-全量预估（指纹表 vs 引擎版本串）。"""

    def _client(self, routes):
        from app import settings_api

        def fake_connect(*a, **k):
            return _RoutedConn(routes)

        app = FastAPI()
        settings_api.register_settings_routes(app, connect=fake_connect)
        return TestClient(app)

    BASE = [("FROM crawl_task", (7,)),
            ("FROM crawl_run", (True, False, datetime(2026, 11, 24, 10),
                                None))]

    def test_running_and_task_found(self):
        client = self._client(list(self.BASE)
                              + [("fund_dca_fp", (12, True))])
        body = client.get("/api/settings").json()
        task = body["backtest_task"]
        assert task["task_id"] == 7
        assert task["running"] is True
        assert body["settings"]["backtest_min_span_days"] == 90

    def test_estimate_dirty_when_fp_matches_engine(self):
        client = self._client([("FROM crawl_task", (7,)),
                               ("FROM crawl_run", (False, False,
                                                   datetime(2026, 11, 24, 9),
                                                   datetime(2026, 11, 24, 9, 30))),
                               ("fund_dca_fp", (26548, True))])
        assert client.get("/api/settings").json()["backtest_task"]["estimate"] \
            == "dirty"

    def test_estimate_full_when_fp_empty(self):
        client = self._client(list(self.BASE) + [("fund_dca_fp", (0, False))])
        assert client.get("/api/settings").json()["backtest_task"]["estimate"] \
            == "full"

    def test_estimate_full_when_engine_mismatch(self):
        """指纹在但引擎串不匹配（如刚改过运作期限设置）→ 全量预估。"""
        client = self._client(list(self.BASE) + [("fund_dca_fp", (26548, False))])
        assert client.get("/api/settings").json()["backtest_task"]["estimate"] \
            == "full"

    def test_estimate_full_when_fp_table_missing(self):
        """fp 表不存在（旧库未升级）→ 兜底全量预估不炸端点。"""
        client = self._client(list(self.BASE)
                              + [("fund_dca_fp", RuntimeError("no table"))])
        body = client.get("/api/settings").json()
        assert body["status"] == "up"
        assert body["backtest_task"]["estimate"] == "full"

    def test_task_missing_when_not_seeded(self):
        client = self._client([("FROM crawl_task", None),
                               ("FROM crawl_run", None),
                               ("fund_dca_fp", (12, True))])
        task = client.get("/api/settings").json()["backtest_task"]
        assert task["task_id"] is None
        assert task["running"] is False


class TestBacktestTaskServerNow:
    """backtest_task.server_now：进度推算的时间基准（客户端时钟不可信，
    进度条按服务端时刻差渲染）。"""

    def _routes(self):
        return [("FROM crawl_task", (7,)),
                ("FROM crawl_run", (False, False,
                                    datetime(2026, 11, 24, 9),
                                    datetime(2026, 11, 24, 9, 30))),
                ("fund_dca_fp", (26548, True))]

    def test_server_now_present_and_iso(self):
        from app import settings_api

        def fake_connect(*a, **k):
            return _RoutedConn(self._routes())

        app = FastAPI()
        settings_api.register_settings_routes(app, connect=fake_connect)
        task = TestClient(app).get("/api/settings").json()["backtest_task"]
        assert "server_now" in task
        # ISO 串含 T 且带时区偏移（+00:00），前端可直接 Date 解析
        assert "T" in task["server_now"]
        assert task["server_now"][-6] in "+-"


class TestSettingsMethodTask:
    """GET /api/settings 的 method_task 字段：单策略全量回测任务快照
    （2026-11-25 九轮：预置任务「单策略全量回测」source='backtest_method'，
    mode 列携带目标策略；running 判定按 crawl_run 同 source 隔离——方法
    运行行绝不污染日更快照，反之亦然）。"""

    BASE = [("FROM crawl_task WHERE source = 'backtest_method'",
             (9, "pl_ladder")),
            ("FROM task_runtime", ("idle",)),
            ("FROM crawl_run WHERE source = 'backtest_method'",
             (False, False, datetime(2026, 11, 24, 10),
              datetime(2026, 11, 24, 11)))]

    def _client(self, routes):
        from app import settings_api

        def fake_connect(*a, **k):
            return _RoutedConn(routes)

        app = FastAPI()
        settings_api.register_settings_routes(app, connect=fake_connect)
        return TestClient(app)

    def test_snapshot_fields(self):
        body = self._client(list(self.BASE)
                            + [("FROM crawl_task", (7,)),
                               ("FROM crawl_run", (False, False,
                                                   datetime(2026, 11, 24, 9),
                                                   None)),
                               ("fund_dca_fp", (12, True))]).get("/api/settings").json()
        mt = body["method_task"]
        assert mt["task_id"] == 9
        assert mt["method"] == "pl_ladder"
        assert mt["phase"] == "idle"
        assert mt["running"] is False
        assert mt["last_finished_at"].startswith("2026-11-24T11")
        # 日更快照不受方法任务查询影响（路由各归各）
        assert body["backtest_task"]["task_id"] == 7

    def test_running_and_phase_queued(self):
        routes = [("FROM crawl_task WHERE source = 'backtest_method'",
                   (9, "dd_ladder")),
                  ("FROM task_runtime", ("running",)),
                  ("FROM crawl_run WHERE source = 'backtest_method'",
                   (True, False, datetime(2026, 11, 24, 10), None)),
                  ("FROM crawl_task", (7,)),
                  ("FROM crawl_run", (False, False,
                                      datetime(2026, 11, 24, 9),
                                      datetime(2026, 11, 24, 9, 30))),
                  ("fund_dca_fp", (12, True))]
        mt = self._client(routes).get("/api/settings").json()["method_task"]
        assert mt["running"] is True
        assert mt["phase"] == "running"
        assert mt["method"] == "dd_ladder"

    def test_task_missing_none_fields(self):
        routes = [("FROM crawl_task WHERE source = 'backtest_method'", None),
                  ("FROM task_runtime", None),
                  ("FROM crawl_run WHERE source = 'backtest_method'", None),
                  ("FROM crawl_task", (7,)),
                  ("FROM crawl_run", (False, False,
                                      datetime(2026, 11, 24, 9),
                                      datetime(2026, 11, 24, 9, 30))),
                  ("fund_dca_fp", (12, True))]
        mt = self._client(routes).get("/api/settings").json()["method_task"]
        assert mt["task_id"] is None
        assert mt["method"] is None
        assert mt["running"] is False
        assert mt["phase"] == "idle"

    def test_snapshot_failure_degrades_to_none(self):
        """方法任务快照异常（如旧假连接形状不符）→ method_task=None 而
        整个 GET 仍 up（与 fp 表缺失兜底同一降级哲学）。"""
        body = self._client([("FROM crawl_task", (7,)),          # 1 元组陷阱
                             ("FROM crawl_run", (False, False,
                                                 datetime(2026, 11, 24, 9),
                                                 None)),
                             ("fund_dca_fp", (12, True))]).get("/api/settings").json()
        assert body["status"] == "up"
        assert body["method_task"] is None


class TestSettingsMethodRun:
    """POST /api/settings/backtest-method-run：改目标策略 + 触发前置校验
    （body{method}；日更运行中 409、方法任务排队/运行中 409；触发本身由
    前端复用任务系统 POST /api/tasks/{id}/run——零新触发逻辑）。"""

    def _client(self, routes):
        from app import settings_api

        conn = _RoutedConn(routes)

        def fake_connect(*a, **k):
            return conn

        app = FastAPI()
        settings_api.register_settings_routes(app, connect=fake_connect)
        return TestClient(app), conn

    ROLES = [("FROM crawl_task WHERE source = 'backtest_method'",
              (9, "pl_ladder")),
             ("FROM crawl_run WHERE source = 'backtest' ORDER", (False,)),
             ("FROM task_runtime", ("idle",)),
             ("FROM crawl_task", (7,)),
             ("FROM crawl_run", (False, False,
                                 datetime(2026, 11, 24, 9), None)),
             ("fund_dca_fp", (12, True))]

    def test_run_ok_updates_mode(self):
        client, conn = self._client(list(self.ROLES))
        resp = client.post("/api/settings/backtest-method-run",
                           json={"method": "dd_ladder_deep"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "up"
        assert body["task_id"] == 9
        assert body["method"] == "dd_ladder_deep"
        # UPDATE 落库且提交（mode 列改写为目标策略）
        assert any("UPDATE crawl_task SET mode" in s for s in conn.executed)
        assert conn.committed is True

    def test_invalid_method_400(self):
        client, _ = self._client(list(self.ROLES))
        resp = client.post("/api/settings/backtest-method-run",
                           json={"method": "bogus"})
        assert resp.status_code == 400

    def test_daily_running_409(self):
        routes = [("FROM crawl_task WHERE source = 'backtest_method'",
                   (9, "pl_ladder")),
                  ("FROM crawl_run WHERE source = 'backtest' ORDER", (True,)),
                  ("FROM task_runtime", ("idle",))]
        client, conn = self._client(routes)
        resp = client.post("/api/settings/backtest-method-run",
                           json={"method": "pl_ladder"})
        assert resp.status_code == 409
        assert "日更" in resp.json()["detail"]
        assert conn.committed is False        # 拒绝路径不落任何写

    def test_method_queued_409(self):
        routes = [("FROM crawl_task WHERE source = 'backtest_method'",
                   (9, "pl_ladder")),
                  ("FROM crawl_run WHERE source = 'backtest' ORDER", (False,)),
                  ("FROM task_runtime", ("queued",))]
        client, _ = self._client(routes)
        resp = client.post("/api/settings/backtest-method-run",
                           json={"method": "pl_ladder"})
        assert resp.status_code == 409
        assert "排队" in resp.json()["detail"]

    def test_method_running_409(self):
        routes = [("FROM crawl_task WHERE source = 'backtest_method'",
                   (9, "pl_ladder")),
                  ("FROM crawl_run WHERE source = 'backtest' ORDER", (False,)),
                  ("FROM task_runtime", ("running",))]
        client, _ = self._client(routes)
        resp = client.post("/api/settings/backtest-method-run",
                           json={"method": "pl_ladder"})
        assert resp.status_code == 409

    def test_down_degrades(self):
        from app import settings_api

        class _Boom:
            def __enter__(self):
                raise RuntimeError("pg down")

            def __exit__(self, *exc):
                return False

        def fake_connect(*a, **k):
            return _Boom()

        app = FastAPI()
        settings_api.register_settings_routes(app, connect=fake_connect)
        resp = TestClient(app).post("/api/settings/backtest-method-run",
                                    json={"method": "pl_ladder"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "down"


class TestBacktestProgressFields:
    """GET /api/settings 的基金级进度字段（2026-11-25 十轮）：任务运行态
    task_runtime.progress JSONB {done,total} → progress_done/progress_total
    （引擎分块回调写入；无行/形状不符一律 None——前端回落时间推算）。"""

    def _client(self, routes):
        from app import settings_api

        def fake_connect(*a, **k):
            return _RoutedConn(routes)

        app = FastAPI()
        settings_api.register_settings_routes(app, connect=fake_connect)
        return TestClient(app)

    BASE = [("SELECT progress FROM task_runtime",
             ({"done": 12345, "total": 26548},)),
            ("FROM crawl_task", (7,)),
            ("FROM crawl_run", (True, False, datetime(2026, 11, 24, 10),
                                None)),
            ("fund_dca_fp", (12, True))]

    def test_backtest_task_progress_dict(self):
        body = self._client(list(self.BASE)).get("/api/settings").json()
        task = body["backtest_task"]
        assert task["progress_done"] == 12345
        assert task["progress_total"] == 26548

    def test_progress_missing_none_fallback(self):
        """无进度行（指纹阶段）→ 双字段 None，前端回落时间推算。"""
        routes = [("FROM crawl_task", (7,)),
                  ("FROM crawl_run", (True, False,
                                      datetime(2026, 11, 24, 10), None)),
                  ("fund_dca_fp", (12, True))]
        task = self._client(routes).get("/api/settings").json()["backtest_task"]
        assert task["progress_done"] is None
        assert task["progress_total"] is None

    def test_progress_bad_shape_none(self):
        """phase 字符串误路由到 progress 查询（假连接关键词重叠）→ 非 dict
        一律 None，不炸端点。"""
        routes = [("SELECT progress FROM task_runtime", ("idle",)),
                  ("FROM crawl_task", (7,)),
                  ("FROM crawl_run", (True, False,
                                      datetime(2026, 11, 24, 10), None)),
                  ("fund_dca_fp", (12, True))]
        task = self._client(routes).get("/api/settings").json()["backtest_task"]
        assert task["progress_done"] is None

    def test_method_task_progress_fields(self):
        routes = [("SELECT progress FROM task_runtime",
                   ({"done": 40, "total": 120},)),
                  ("FROM crawl_task WHERE source = 'backtest_method'",
                   (9, "pl_ladder")),
                  ("FROM task_runtime", ("running",)),
                  ("FROM crawl_run WHERE source = 'backtest_method'",
                   (True, False, datetime(2026, 11, 25, 10), None)),
                  ("FROM crawl_task", (7,)),
                  ("FROM crawl_run", (False, False,
                                      datetime(2026, 11, 24, 9),
                                      datetime(2026, 11, 24, 9, 30))),
                  ("fund_dca_fp", (12, True))]
        mt = self._client(routes).get("/api/settings").json()["method_task"]
        assert mt["phase"] == "running"
        assert mt["progress_done"] == 40
        assert mt["progress_total"] == 120


class TestMethodTaskBlockingAndError:
    """method_task 快照增补（2026-11-25 十一轮：排队态可见性 + 静默失败
    修复——用户点击发生在旧 worker 时代被 KeyError 吞掉，UI 只显示排队中
    且失败后无任何提示）：blocking=串行队列前方运行中任务名（排队提示），
    last_error=上次运行错误摘要（last_result.error 时取 detail）。"""

    def _client(self, routes):
        from app import settings_api

        def fake_connect(*a, **k):
            return _RoutedConn(routes)

        app = FastAPI()
        settings_api.register_settings_routes(app, connect=fake_connect)
        return TestClient(app)

    def test_blocking_and_last_error_surfaced(self):
        routes = [("SELECT c.name FROM crawl_run r",
                   ("东财每日增量",)),
                  ("SELECT last_result FROM task_runtime",
                   ({"error": True, "detail": "KeyError: 'backtest_method'",
                     "trigger": "manual"},)),
                  ("FROM crawl_task WHERE source = 'backtest_method'",
                   (9, "pl_ladder")),
                  ("FROM task_runtime", ("queued",)),
                  ("FROM crawl_run WHERE source = 'backtest_method'",
                   (None, False, datetime(2026, 11, 25, 6, 20), None)),
                  ("SELECT progress FROM task_runtime",
                   (None,)),
                  ("FROM crawl_task", (7,)),
                  ("FROM crawl_run", (False, False,
                                      datetime(2026, 11, 24, 9),
                                      datetime(2026, 11, 24, 9, 30))),
                  ("fund_dca_fp", (12, True))]
        mt = self._client(routes).get("/api/settings").json()["method_task"]
        assert mt["blocking"] == "东财每日增量"
        assert mt["last_error"] == "KeyError: 'backtest_method'"
        assert mt["phase"] == "queued"

    def test_no_blocking_no_error_none(self):
        """无运行中任务、last_result 非错误 → 双字段 None（不误报）。"""
        routes = [("SELECT c.name FROM crawl_run r", None),
                  ("SELECT last_result FROM task_runtime",
                   ({"ok": 3, "error": False},)),
                  ("FROM crawl_task WHERE source = 'backtest_method'",
                   (9, "pl_ladder")),
                  ("FROM task_runtime", ("idle",)),
                  ("FROM crawl_run WHERE source = 'backtest_method'",
                   (False, False, datetime(2026, 11, 25, 6, 20),
                    datetime(2026, 11, 25, 6, 25))),
                  ("SELECT progress FROM task_runtime", (None,)),
                  ("FROM crawl_task", (7,)),
                  ("FROM crawl_run", (False, False,
                                      datetime(2026, 11, 24, 9),
                                      datetime(2026, 11, 24, 9, 30))),
                  ("fund_dca_fp", (12, True))]
        mt = self._client(routes).get("/api/settings").json()["method_task"]
        assert mt["blocking"] is None
        assert mt["last_error"] is None

    def test_non_string_blocking_ignored(self):
        """假连接形状不符（如日快照元组误路由到 blocking 查询）→ 非 str
        一律 None，不炸端点不透出垃圾值。"""
        routes = [("SELECT c.name FROM crawl_run r", None),
                  ("SELECT last_result FROM task_runtime", None),
                  ("FROM crawl_task WHERE source = 'backtest_method'",
                   (9, "pl_ladder")),
                  ("FROM task_runtime", ("idle",)),
                  ("FROM crawl_run WHERE source = 'backtest_method'",
                   (False, False, datetime(2026, 11, 25, 6, 20),
                    datetime(2026, 11, 25, 6, 25))),
                  ("SELECT progress FROM task_runtime", (None,)),
                  ("FROM crawl_task", (7,)),
                  ("FROM crawl_run", (False, False,
                                      datetime(2026, 11, 24, 9),
                                      datetime(2026, 11, 24, 9, 30))),
                  ("fund_dca_fp", (12, True))]
        mt = self._client(routes).get("/api/settings").json()["method_task"]
        assert mt["blocking"] is None
        assert mt["last_error"] is None


class TestSettingsBacktestCalendar:
    """GET /api/settings/backtest-calendar（2026-11-26 二十轮）：日更日历
    数据源——crawl_run source='backtest' 按日聚合（成功/中断/退让跳过），
    同日多跑按 成功>中断>跳过 取日状态；days 钳制 1~365；DB down 降级
    200 空形状（沿设置端点惯例，日历缺失不拖垮设置页）。"""

    def _client(self, routes):
        from app import settings_api

        conn = _RoutedConn(routes)

        def fake_connect(*a, **k):
            return conn

        app = FastAPI()
        settings_api.register_settings_routes(app, connect=fake_connect)
        return TestClient(app), conn

    def test_day_status_precedence_and_runs(self):
        day_rows = [("2026-11-26", 2, 1, 1, 0),    # 成功+中断 → ok
                    ("2026-11-25", 1, 0, 0, 1),    # 仅跳过 → skip
                    ("2026-11-24", 1, 0, 1, 0)]    # 仅中断 → fail
        run_rows = [("2026-11-26", "daily", datetime(2026, 11, 26, 10, 2),
                     datetime(2026, 11, 26, 11, 0), False, "", 13441, 13441),
                    ("2026-11-26", "daily", datetime(2026, 11, 26, 23, 0),
                     datetime(2026, 11, 27, 3, 55), True, "", 13441, 13441),
                    ("2026-11-25", "daily", datetime(2026, 11, 25, 23, 0),
                     datetime(2026, 11, 25, 23, 1), False,
                     "单策略回测running中", 0, 0),
                    ("2026-11-24", "daily", datetime(2026, 11, 24, 23, 0),
                     datetime(2026, 11, 25, 3, 55), True, "", 0, 0)]
        client, _ = self._client([("GROUP BY day", day_rows),
                                  ("= ANY(%s", run_rows)])
        body = client.get("/api/settings/backtest-calendar").json()
        assert body["status"] == "up"
        days = body["days"]
        assert [d["date"] for d in days] == ["2026-11-26", "2026-11-25",
                                             "2026-11-24"]
        assert [d["status"] for d in days] == ["ok", "skip", "fail"]
        assert days[0]["runs"][1]["aborted"] is True
        assert days[0]["runs"][0]["ok_stocks"] == 13441
        assert days[1]["runs"][0]["skipped"] == "单策略回测running中"
        assert "server_now" in body

    def test_days_clamped_to_365(self):
        client, conn = self._client([("GROUP BY day", []),
                                     ("= ANY(%s", [])])
        body = client.get("/api/settings/backtest-calendar",
                          params={"days": 9999}).json()
        assert body == {"status": "up", "days": [], "server_now": body["server_now"]}
        assert any("GROUP BY day" in q for q in conn.executed)
        limit_calls = [p for q, p in zip(conn.executed, conn.params)
                       if "GROUP BY day" in q]
        assert limit_calls == [(365,)]          # 钳制上界

    def test_down_degrades_200(self):
        client, _ = self._client([("GROUP BY day", RuntimeError("db down"))])
        resp = client.get("/api/settings/backtest-calendar")
        assert resp.status_code == 200
        assert resp.json() == {"status": "down", "days": []}
