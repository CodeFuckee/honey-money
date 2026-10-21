"""全量回测（教学页）测试：过滤/统计纯函数 + worker 循环 + API 契约（全离线）。

引擎 vectorbt 撮合正确性由 test_backtest_engine.py 覆盖；本文件只测全量编排层：
universe 过滤、统计聚合、逐股落库循环、取消、互斥、重启恢复、分页导出契约。
"""

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from app import backtest_full as bf  # noqa: E402
from app.main import create_app  # noqa: E402

# ---------------------------------------------------------------- 过滤 ----

STOCKS = [{"code": "600519", "name": "贵州茅台"},
          {"code": "000001", "name": "平安银行"},
          {"code": "600596", "name": "*ST新海"},
          {"code": "600070", "name": "ST富润"},
          {"code": "000023", "name": "退市深浅"},
          {"code": "301001", "name": "次新股份"}]

BARS_COUNTS = {"600519": 750, "000001": 700, "600596": 500, "600070": 480,
               "000023": 900, "301001": 30}


class TestFilterUniverse:
    def test_exclude_st_and_delist_by_name(self):
        kept, skipped = bf.filter_universe(
            STOCKS, BARS_COUNTS, exclude_st=True, min_bars=0)
        codes = [s["code"] for s in kept]
        assert "600596" not in codes and "600070" not in codes \
            and "000023" not in codes
        assert skipped == 3

    def test_min_bars_excludes_newcomers(self):
        kept, skipped = bf.filter_universe(
            STOCKS, BARS_COUNTS, exclude_st=False, min_bars=60)
        assert [s["code"] for s in kept] == [s["code"] for s in STOCKS][:2] + \
            ["600596", "600070", "000023"]
        assert skipped == 1                       # 301001 只有 30 根

    def test_both_filters_combined_and_counts(self):
        kept, skipped = bf.filter_universe(
            STOCKS, BARS_COUNTS, exclude_st=True, min_bars=60)
        assert [s["code"] for s in kept] == ["600519", "000001"]
        assert skipped == 4

    def test_min_bars_zero_keeps_all_non_st(self):
        kept, _ = bf.filter_universe(
            STOCKS, {"600519": 0}, exclude_st=False, min_bars=0)
        assert len(kept) == len(STOCKS)           # 0 根也放行（worker 再判失败）

    def test_empty_universe(self):
        kept, skipped = bf.filter_universe([], {}, exclude_st=True, min_bars=60)
        assert kept == [] and skipped == 0


# ---------------------------------------------------------------- 统计 ----

def _row(ret, bench, status="ok"):
    return {"status": status, "total_return": ret, "buyhold_return": bench}


class TestComputeStats:
    def test_exact_median_positive_and_beat_pct(self):
        rows = [_row(0.10, 0.05), _row(-0.20, 0.05), _row(0.30, 0.30),
                _row(0.05, 0.10)]
        stats = bf.compute_stats(rows)
        assert stats["count"] == 4
        assert stats["positive_pct"] == pytest.approx(0.75)     # 3/4 正
        assert stats["median_return"] == pytest.approx(0.075)   # (0.05+0.10)/2
        assert stats["beat_buyhold_pct"] == pytest.approx(0.25)  # 仅 0.10>0.05

    def test_odd_count_median(self):
        stats = bf.compute_stats([_row(0.01, 0.02), _row(0.05, 0.02),
                                  _row(0.09, 0.02)])
        assert stats["median_return"] == pytest.approx(0.05)

    def test_failed_rows_and_none_returns_ignored(self):
        rows = [_row(0.10, 0.05), _row(None, None),
                _row(None, None, status="failed")]
        stats = bf.compute_stats(rows)
        assert stats["count"] == 1

    def test_empty_rows_returns_none(self):
        assert bf.compute_stats([]) is None
        assert bf.compute_stats([_row(None, None)]) is None


# ------------------------------------------------------- 结果行映射 ----

class TestRowFromResult:
    def test_ok_result_maps_metrics_and_buyhold(self):
        result = {
            "metrics": {"total_return": 0.26, "annual_return": 0.08,
                        "max_drawdown": 0.18, "sharpe": 0.85, "win_rate": 0.5,
                        "trades": 3},
            "equity": [["2026-01-02", 1.0, 1.0], ["2026-03-01", 1.26, 1.10]],
        }
        row = bf.row_from_result("600519", "贵州茅台", result)
        assert row["code"] == "600519" and row["name"] == "贵州茅台"
        assert row["status"] == "ok"
        assert row["total_return"] == pytest.approx(0.26)
        assert row["buyhold_return"] == pytest.approx(0.10)     # 1.10-1
        assert row["trades"] == 3

    def test_message_result_maps_failed(self):
        row = bf.row_from_result("301001", "次新股份",
                                 {"message": "区间内无行情数据"})
        assert row["status"] == "failed"
        assert "区间内无行情数据" in row["reason"]
        assert row["total_return"] is None

    def test_empty_equity_buyhold_none(self):
        result = {"metrics": {"total_return": 0.0}, "equity": []}
        row = bf.row_from_result("600519", "茅台", result)
        assert row["buyhold_return"] is None


# ---------------------------------------------------------------- API ----

RUN = {"id": 7, "strategy": "donchian", "range": "3y", "status": "running",
       "params": {}, "filters": {"exclude_st": True, "min_bars": 60},
       "adjust": True, "exec_mode": "next_open", "commission": 2.5e-4,
       "sell_tax": 5e-4, "total": 100, "done": 40, "failed": 2, "skipped": 5,
       "stats": None, "error": None, "created_at": "2026-10-11T10:00:00",
       "finished_at": None}


@pytest.fixture
def client(monkeypatch):
    """TestClient：store 全部替换为内存实现，worker 启动替换为记录器。"""
    started = []
    monkeypatch.setattr(bf, "has_running_run", lambda: False)
    monkeypatch.setattr(bf, "create_run", lambda spec: 7)
    monkeypatch.setattr(bf, "start_run_job",
                        lambda run_id, spec: started.append((run_id, spec)))
    monkeypatch.setattr(bf, "list_runs", lambda: [dict(RUN)])
    monkeypatch.setattr(bf, "ok_rows", lambda rid: [])
    monkeypatch.setattr(bf, "get_run", lambda rid: dict(RUN) if rid == 7 else None)
    monkeypatch.setattr(bf, "request_cancel", lambda rid: rid == 7)

    app = create_app()
    app.state._full_started = started
    return TestClient(app)


class TestFullBacktestApi:
    def test_momentum_accepted_for_full(self, client):
        """三期：momentum 全市场放开（组合级路径），spec 原样透传 worker。"""
        r = client.post("/api/backtest/full", json={
            "strategy": "momentum", "range": "3y",
            "params": {"window": 20, "top_k": 10}})
        assert r.status_code == 200
        run_id, spec = client.app.state._full_started[0]
        assert spec["strategy"] == "momentum"
        assert spec["params"] == {"window": 20, "top_k": 10}

    def test_conflict_when_run_already_running(self, client, monkeypatch):
        monkeypatch.setattr(bf, "has_running_run", lambda: True)
        r = client.post("/api/backtest/full", json={
            "strategy": "ma_cross", "range": "3y"})
        assert r.status_code == 409

    def test_create_returns_run_id_and_kicks_worker(self, client):
        r = client.post("/api/backtest/full", json={
            "strategy": "donchian", "range": "3y",
            "filters": {"exclude_st": True, "min_bars": 60},
            "adjust": True})
        assert r.status_code == 200
        assert r.json()["run_id"] == 7
        run_id, spec = client.app.state._full_started[0]
        assert run_id == 7
        assert spec["strategy"] == "donchian"
        assert spec["filters"]["min_bars"] == 60

    def test_default_filters_applied(self, client):
        client.post("/api/backtest/full", json={
            "strategy": "ma_cross", "range": "1y"})
        spec = client.app.state._full_started[-1][1]
        assert spec["filters"] == {"exclude_st": True, "min_bars": 60,
                                   "include_delisted": True}
        assert spec["adjust"] is True

    def test_reuse_flag_passed_to_worker_spec(self, client):
        """复用开关透传 worker spec（默认开；显式关→False）。"""
        client.post("/api/backtest/full", json={
            "strategy": "ma_cross", "range": "1y", "reuse": False})
        assert client.app.state._full_started[-1][1]["reuse"] is False
        client.post("/api/backtest/full", json={
            "strategy": "ma_cross", "range": "1y"})
        assert client.app.state._full_started[-1][1]["reuse"] is True

    def test_runs_list_and_detail(self, client):
        runs = client.get("/api/backtest/full/runs").json()["runs"]
        assert runs[0]["id"] == 7 and runs[0]["status"] == "running"
        detail = client.get("/api/backtest/full/runs/7").json()
        assert detail["done"] == 40 and detail["total"] == 100
        assert client.get("/api/backtest/full/runs/999").status_code == 404

    def test_run_detail_live_stats_when_running(self, client, monkeypatch):
        """运行中详情实时返回部分统计（逐股上屏的统计卡数据源）。"""
        monkeypatch.setattr(bf, "ok_rows", lambda rid: [
            {"status": "ok", "total_return": 0.10, "buyhold_return": 0.05},
            {"status": "ok", "total_return": -0.20, "buyhold_return": 0.05},
            {"status": "ok", "total_return": 0.30, "buyhold_return": 0.30},
        ])
        detail = client.get("/api/backtest/full/runs/7").json()
        assert detail["status"] == "running"
        assert detail["stats"]["count"] == 3
        assert detail["stats"]["beat_buyhold_pct"] == pytest.approx(1 / 3)

    def test_run_detail_done_recomputes_stats(self, client, monkeypatch):
        """完成态也动态重算：历史 run 落库的是旧形状 stats（无 best/worst/
        delisted），重算让新旧 run 统一为新形状（重算仅毫秒级）。"""
        stored = {**RUN, "status": "done",
                  "stats": {"count": 99, "positive_pct": 0.5,
                            "median_return": 0.1, "beat_buyhold_pct": 0.6}}
        monkeypatch.setattr(bf, "get_run", lambda rid: stored)
        monkeypatch.setattr(bf, "ok_rows", lambda rid: [
            {"status": "ok", "total_return": 0.3, "buyhold_return": 0.1,
             "delisted": True},
            {"status": "ok", "total_return": -0.2, "buyhold_return": -0.05,
             "delisted": False},
        ])
        detail = client.get("/api/backtest/full/runs/7").json()
        assert detail["stats"]["count"] == 2
        assert detail["stats"]["best_return"] == 0.3
        assert detail["stats"]["delisted"]["count"] == 1

    def test_cancel(self, client):
        assert client.post("/api/backtest/full/runs/7/cancel").json()["ok"]
        assert client.post("/api/backtest/full/runs/8/cancel").status_code == 404


class TestRowsAndExportApi:
    ROWS = [{"code": "600519", "name": "贵州茅台", "status": "ok",
             "total_return": 0.26, "annual_return": 0.08, "max_drawdown": 0.18,
             "sharpe": 0.85, "win_rate": 0.5, "trades": 3,
             "buyhold_return": 0.10, "reason": None},
            {"code": "000001", "name": "平安银行", "status": "failed",
             "total_return": None, "annual_return": None, "max_drawdown": None,
             "sharpe": None, "win_rate": None, "trades": None,
             "buyhold_return": None, "reason": "区间内无行情数据"}]

    @pytest.fixture
    def rows_client(self, client, monkeypatch):
        monkeypatch.setattr(bf, "get_rows",
                            lambda rid, sort, desc, page, size, delisted="all":
                            {"total": 2, "rows": TestRowsAndExportApi.ROWS,
                             "stats": None})
        monkeypatch.setattr(bf, "export_csv", lambda rid:
                            "code,name,total_return\n600519,贵州茅台,0.26\n")
        return client

    def test_rows_sorted_contract(self, rows_client):
        body = rows_client.get(
            "/api/backtest/full/runs/7/rows?sort=total_return&desc=1&page=0"
        ).json()
        assert body["total"] == 2
        assert body["rows"][0]["code"] == "600519"
        assert body["rows"][1]["reason"] == "区间内无行情数据"

    def test_export_csv_content_type(self, rows_client):
        r = rows_client.get("/api/backtest/full/runs/7/export")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/csv")
        body = r.text.lstrip("\ufeff")             # BOM 供 Excel 直开，断言前剥掉
        assert "600519" in body and body.splitlines()[0].startswith("code,")

    def test_rows_delisted_group_with_stats(self, client, monkeypatch):
        """退市股分组：rows 接口 delisted=only 过滤透传 + 响应带该口径统计。"""
        captured = {}

        def fake_get_rows(rid, sort, desc, page, size, delisted="all"):
            captured["delisted"] = delisted
            return {"total": 1, "rows": [TestRowsAndExportApi.ROWS[1]],
                    "stats": {"count": 1, "positive_pct": 0.0,
                              "median_return": -0.9, "beat_buyhold_pct": 0.0}}

        monkeypatch.setattr(bf, "get_rows", fake_get_rows)
        body = client.get(
            "/api/backtest/full/runs/7/rows?delisted=only").json()
        assert captured["delisted"] == "only"
        assert body["stats"]["count"] == 1
        assert body["rows"][0]["code"] == "000001"

    def test_rows_delisted_param_whitelist(self, client, monkeypatch):
        """delisted 参数白名单：非法值回退 all。"""
        captured = {}

        def fake_get_rows(rid, sort, desc, page, size, delisted="all"):
            captured["delisted"] = delisted
            return {"total": 0, "rows": [], "stats": None}

        monkeypatch.setattr(bf, "get_rows", fake_get_rows)
        client.get("/api/backtest/full/runs/7/rows?delisted=bogus")
        assert captured["delisted"] == "all"


# ---------------------------------------------------------------- worker ----

class TestLoadUniverseSql:
    """回归：`%s IS NULL` 在 psycopg3 服务端绑定下生成 `$1 IS NULL`，
    Postgres 无法推断 $1 类型 → IndeterminateDatatype（生产全量回测必挂）。
    无起始日时不得把参数放进 IS NULL 位——SQL 按分支拼装。"""

    class FakeCur:
        def __init__(self, executed):
            self.executed = executed
            self.rows = []

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, q, p=None):
            self.executed.append((q, p))
            return self

        def fetchall(self):
            return self.rows

        def __iter__(self):
            return iter(self.rows)

    class FakeConn:
        def __init__(self, executed):
            self.executed = executed

        def cursor(self):
            return TestLoadUniverseSql.FakeCur(self.executed)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def commit(self):
            pass

    def _run(self, monkeypatch, range_key):
        executed = []
        monkeypatch.setattr(bf, "_connect",
                            lambda: self.FakeConn(executed))
        monkeypatch.setattr(bf, "set_run_totals", lambda *a, **k: None)
        bf._load_universe(7, {"range": range_key, "filters": {}})
        assert len(executed) >= 2
        return executed

    def test_all_range_sql_has_no_null_param_placeholder(self, monkeypatch):
        executed = self._run(monkeypatch, "all")
        counts_sql = executed[1][0]
        assert "GROUP BY code" in counts_sql
        assert "%s" not in counts_sql            # 全史：无条件参数化

    def test_dated_range_sql_single_typed_param(self, monkeypatch):
        executed = self._run(monkeypatch, "3y")
        counts_sql, params = executed[1]
        assert "trade_date >= %s" in counts_sql
        assert "IS NULL" not in counts_sql       # 原实现 `%s IS NULL OR ...` 必挂
        assert len(params) == 1 and params[0]


class TestRunOneStock:
    """回归：真实 pack（OHLC DataFrame 宽表）下 _run_one_stock 的取列与缺码分支。
    原实现 `(bars.get("close") or {}).columns` 对 DataFrame 求真值 →
    ValueError: The truth value of a DataFrame is ambiguous（每股必挂）。"""

    def _pack(self, codes):
        import pandas as pd
        idx = pd.to_datetime(["2026-01-02", "2026-01-03"])
        frame = pd.DataFrame({c: [10.0, 10.5] for c in codes}, index=idx)
        return {"bars": {k: frame.copy() for k in
                         ("open", "high", "low", "close", "volume")},
                "factors": None}

    SPEC = {"strategy": "buy_hold", "params": {}, "exec_mode": "next_open",
            "commission": 2.5e-4, "sell_tax": 5e-4}

    def test_slices_single_column_and_passes_factors(self, monkeypatch):
        captured = {}

        def fake_run(bars, **kw):
            captured["cols"] = list(bars["close"].columns)
            captured["factors"] = kw.get("factors")
            return {"metrics": {"total_return": 0.1}, "equity": [
                ["2026-01-02", 1.0, 1.0], ["2026-01-03", 1.1, 1.05]]}

        monkeypatch.setattr(bf, "run_strategy", fake_run)
        row = bf._run_one_stock("600519", "贵州茅台",
                                self._pack(["600519", "000001"]), self.SPEC)
        assert captured["cols"] == ["600519"]       # 只切该股一列
        assert row["status"] == "ok"
        assert row["buyhold_return"] == pytest.approx(0.05)

    def test_missing_code_fails_gracefully_not_ambiguous(self, monkeypatch):
        monkeypatch.setattr(
            bf, "run_strategy",
            lambda *a, **k: pytest.fail("缺码不应进入引擎"))
        row = bf._run_one_stock("999999", "不存在",
                                self._pack(["600519"]), self.SPEC)
        assert row["status"] == "failed"
        assert "区间内无行情数据" in row["reason"]


class TestExecuteRun:
    """worker 编排循环：monkeypatch 取数/引擎/落库，验证推进、失败隔离、取消。"""

    def _worker_env(self, monkeypatch, universe, fail_codes=(),
                    canceled_at=None, rows_out=None):
        """构造 worker 依赖：universe 3 只、第 fail_codes 只抛错、
        canceled_at=处理完第 N 只后置取消。返回记录器 dict。"""
        rec = {"rows": [], "progress": [], "final": [], "engine_calls": []}

        def fake_universe(rid, spec):
            return universe, 99                      # (kept, skipped)

        def fake_bars(codes, spec):
            rec["engine_calls"].extend(codes)
            return {c: {"stub": c} for c in codes}

        def fake_one(code, name, bars, spec):
            if code in fail_codes:
                raise RuntimeError("引擎爆炸")
            return bf.row_from_result(code, name, {
                "metrics": {"total_return": 0.1, "annual_return": 0.05,
                            "max_drawdown": 0.1, "sharpe": 1.0,
                            "win_rate": 0.5, "trades": 1},
                "equity": [["2026-01-02", 1.0, 1.0], ["2026-02-02", 1.1, 1.05]]})

        monkeypatch.setattr(bf, "_load_universe", fake_universe)
        monkeypatch.setattr(bf, "_load_bars_chunk", fake_bars)
        monkeypatch.setattr(bf, "_run_one_stock", fake_one)
        monkeypatch.setattr(bf, "insert_row",
                            lambda rid, row: rec["rows"].append(row))
        monkeypatch.setattr(bf, "update_progress",
                            lambda rid, done, failed:
                            rec["progress"].append((done, failed)))
        monkeypatch.setattr(bf, "finish_run",
                            lambda rid, status, stats=None, error=None:
                            rec["final"].append((status, stats, error)))
        monkeypatch.setattr(bf, "get_run", lambda rid: dict(RUN))
        # 复用路径默认关闭（无历史候选源）；统计终值聚合 fake 落库行
        monkeypatch.setattr(bf, "find_reusable_run", lambda h, rid: None)
        monkeypatch.setattr(bf, "ok_rows", lambda rid: [
            {"status": r["status"], "total_return": r["total_return"],
             "buyhold_return": r["buyhold_return"], "delisted": False}
            for r in rec["rows"] if r["status"] == "ok"])
        flags = {"n": 0}

        def cancel_check(rid):
            flags["n"] += 1
            return canceled_at is not None and flags["n"] > canceled_at

        monkeypatch.setattr(bf, "_canceled", cancel_check)
        return rec

    def _final(self, rec):
        return rec["final"][0]

    def test_happy_path_rows_progress_stats_done(self, monkeypatch):
        rec = self._worker_env(
            monkeypatch, [{"code": "600519", "name": "贵州茅台"},
                          {"code": "000001", "name": "平安银行"},
                          {"code": "600000", "name": "浦发银行"}])
        asyncio.run(bf.execute_run(7, {"strategy": "donchian", "range": "3y"}))
        assert len(rec["rows"]) == 3
        assert all(r["status"] == "ok" for r in rec["rows"])
        assert rec["progress"][-1] == (3, 0)
        status, stats, error = self._final(rec)
        assert status == "done" and error is None
        assert stats["count"] == 3
        assert stats["positive_pct"] == 1.0

    def test_per_stock_failure_isolated(self, monkeypatch):
        rec = self._worker_env(
            monkeypatch, [{"code": "600519", "name": "A"},
                          {"code": "301001", "name": "B"},
                          {"code": "000001", "name": "C"}],
            fail_codes=("301001",))
        asyncio.run(bf.execute_run(7, {"strategy": "ma_cross", "range": "3y"}))
        assert rec["progress"][-1] == (3, 1)
        failed = [r for r in rec["rows"] if r["status"] == "failed"]
        assert len(failed) == 1 and "引擎爆炸" in failed[0]["reason"]
        assert len([r for r in rec["rows"] if r["status"] == "ok"]) == 2
        status, stats, _ = self._final(rec)
        assert status == "done" and stats["count"] == 2

    def test_cancel_stops_loop(self, monkeypatch):
        rec = self._worker_env(
            monkeypatch, [{"code": f"{600000 + i}", "name": f"股{i}"}
                          for i in range(5)],
            canceled_at=2)
        asyncio.run(bf.execute_run(7, {"strategy": "ma_cross", "range": "3y"}))
        assert len(rec["rows"]) == 2               # 只处理到取消前
        status, stats, _ = self._final(rec)
        assert status == "canceled" and stats is None

    def test_total_universe_failure_marks_failed(self, monkeypatch):
        rec = self._worker_env(monkeypatch, [])

        def boom(*a, **k):
            raise RuntimeError("PG 不可达")

        monkeypatch.setattr(bf, "_load_universe", boom)
        asyncio.run(bf.execute_run(7, {"strategy": "ma_cross", "range": "3y"}))
        status, stats, error = rec["final"][0]
        assert status == "failed" and "PG 不可达" in error


class TestSpecHash:
    """复用指纹：键序无关、语义字段敏感、reuse 开关不参与。"""

    BASE = {"strategy": "buy_hold", "range": "3y", "params": {},
            "exec_mode": "next_open", "commission": 2.5e-4,
            "sell_tax": 5e-4, "adjust": True,
            "filters": {"exclude_st": True, "min_bars": 60}}

    def test_key_order_irrelevant(self):
        flipped = {k: self.BASE[k] for k in reversed(list(self.BASE))}
        assert bf.spec_hash(self.BASE) == bf.spec_hash(flipped)

    def test_semantic_fields_sensitive_reuse_flag_not(self):
        h = bf.spec_hash(self.BASE)
        assert bf.spec_hash({**self.BASE, "range": "1y"}) != h
        assert bf.spec_hash({**self.BASE, "commission": 3e-4}) != h
        assert bf.spec_hash({**self.BASE, "filters": {"min_bars": 0}}) != h
        assert bf.spec_hash({**self.BASE, "reuse": False}) == h


class TestExecuteRunReuse:
    """worker 复用编排：成功股免算、失败股重跑、新进股票补算、统计含复制行。"""

    UNIVERSE = [{"code": "600519", "name": "贵州茅台"},    # 源里 ok → 复制
                {"code": "000001", "name": "平安银行"},    # 源里失败 → 重跑
                {"code": "600000", "name": "浦发银行"}]    # 源里没有 → 新算

    def _reuse_env(self, monkeypatch):
        rec = TestExecuteRun()._worker_env(monkeypatch, self.UNIVERSE)
        monkeypatch.setattr(bf, "find_reusable_run",
                            lambda h, rid: {"id": 5, "created_at": None})
        rec["copies"] = []
        monkeypatch.setattr(bf, "reusable_ok_codes", lambda rid: ["600519"])
        monkeypatch.setattr(bf, "copy_ok_rows",
                            lambda s, d, codes: rec["copies"].append(
                                (s, d, list(codes))) or len(codes))
        rec["marks"] = []
        monkeypatch.setattr(bf, "mark_reuse",
                            lambda rid, s, n: rec["marks"].append((rid, s, n)))
        # 统计终值 = 落库行（复制行 + 新算行）
        monkeypatch.setattr(bf, "ok_rows", lambda rid: [
            {"status": "ok", "total_return": 0.1,
             "buyhold_return": 0.05, "delisted": False},   # 复制的 600519
            *[{"status": r["status"], "total_return": r["total_return"],
               "buyhold_return": r["buyhold_return"], "delisted": False}
              for r in rec["rows"] if r["status"] == "ok"]])
        return rec

    def test_ok_reused_failed_rerun_new_computed(self, monkeypatch):
        rec = self._reuse_env(monkeypatch)
        asyncio.run(bf.execute_run(7, {"strategy": "ma_cross", "range": "3y"}))
        assert rec["copies"] == [(5, 7, ["600519"])]
        assert rec["marks"] == [(7, 5, 1)]
        # 引擎只算失败股 + 新股（复用股不重算）
        assert rec["engine_calls"] == ["000001", "600000"]
        assert rec["progress"][-1] == (3, 0)          # done = 1 复制 + 2 新算
        status, stats, _ = rec["final"][0]
        assert status == "done" and stats["count"] == 3

    def test_reuse_off_recomputes_everything(self, monkeypatch):
        rec = self._reuse_env(monkeypatch)
        called = []
        monkeypatch.setattr(bf, "find_reusable_run",
                            lambda h, rid: called.append(rid))
        asyncio.run(bf.execute_run(
            7, {"strategy": "ma_cross", "range": "3y", "reuse": False}))
        assert called == []                           # 关开关不查复用源
        assert rec["copies"] == [] and rec["marks"] == []
        assert len(rec["rows"]) == 3

    def test_no_candidate_source_full_run(self, monkeypatch):
        rec = TestExecuteRun()._worker_env(monkeypatch, self.UNIVERSE)
        asyncio.run(bf.execute_run(7, {"strategy": "ma_cross", "range": "3y"}))
        assert len(rec["rows"]) == 3
        assert rec["progress"][-1] == (3, 0)
        status, _, _ = rec["final"][0]
        assert status == "done"


class TestCopyOkRowsSql:
    """复制 SQL：服务端整批、带溯源 source_run_id、限 codes 清单、
    名称跟 stock_basic 现值；空清单不打库。"""

    class FakeCur:
        def __init__(self, executed, rowcount=0):
            self.executed = executed
            self.rowcount = rowcount

        def execute(self, q, p=None):
            self.executed.append((q, p))
            return self

    class FakeConn:
        def __init__(self, executed, rowcount):
            self.executed = executed
            self.rowcount = rowcount

        def cursor(self):
            return TestCopyOkRowsSql.FakeCur(self.executed, self.rowcount)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def commit(self):
            pass

    def test_sql_shape_and_params(self, monkeypatch):
        executed = []
        monkeypatch.setattr(bf, "_connect",
                            lambda: self.FakeConn(executed, 42))
        n = bf.copy_ok_rows(5, 7, ["600519", "000001"])
        assert n == 42
        q, p = executed[0]
        assert "INSERT INTO backtest_full_row" in q
        assert "source_run_id" in q
        assert "ANY(%s)" in q
        assert "COALESCE(b.name, r.name)" in q
        assert "r.status='ok'" in q
        # 参数序：目标 run、溯源 run（列与 WHERE）、codes 清单
        assert p == (7, 5, 5, ["600519", "000001"])

    def test_empty_codes_no_query(self, monkeypatch):
        executed = []
        monkeypatch.setattr(bf, "_connect",
                            lambda: self.FakeConn(executed, 0))
        assert bf.copy_ok_rows(5, 7, []) == 0
        assert executed == []


class TestRowsOkRowsIntegration:
    """真实 get_rows → ok_rows 函数链（不 mock 函数，只假连接）：
    两处并发演进曾签名分裂（ok_rows 不收 delisted → 生产 500）。"""

    class FakeCur:
        def __init__(self, executed):
            self.executed = executed
            self.rows = []

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, q, p=None):
            self.executed.append(q)
            return self

        def fetchone(self):
            return (0,)

        def fetchall(self):
            return []

        def __iter__(self):
            return iter([])

    class FakeConn:
        def __init__(self, executed):
            self.executed = executed

        def cursor(self):
            return TestRowsOkRowsIntegration.FakeCur(self.executed)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def test_get_rows_delisted_only_flows_to_ok_rows(self, monkeypatch):
        executed = []
        monkeypatch.setattr(bf, "_connect", lambda: self.FakeConn(executed))
        bf.get_rows(7, delisted="only")
        ok_sql = executed[-1]                      # get_rows 末尾调 ok_rows
        assert "COALESCE(b.delisted, FALSE) = TRUE" in ok_sql

    def test_get_rows_all_has_no_group_filter(self, monkeypatch):
        executed = []
        monkeypatch.setattr(bf, "_connect", lambda: self.FakeConn(executed))
        bf.get_rows(7)
        ok_sql = executed[-1]
        assert "delisted, FALSE) = TRUE" not in ok_sql
        assert "delisted, FALSE) = FALSE" not in ok_sql


class TestTopCodesDelisted:
    """退市股收益折线图：top_codes 支持退市分组口径。"""

    def _fake_conn(self, executed):
        conn = TestRowsOkRowsIntegration.FakeConn(executed)
        # top 查询 fetchall 返回两行样例
        return conn

    def test_top_codes_delisted_only_joins_filter(self, monkeypatch):
        executed = []

        class Cur(TestRowsOkRowsIntegration.FakeCur):
            def fetchall(self):
                return [("000003", "PT金田Ａ"), ("000004", "国华退")]

        conn = TestRowsOkRowsIntegration.FakeConn(executed)
        conn.cursor = lambda: Cur(executed)
        monkeypatch.setattr(bf, "_connect", lambda: conn)
        picks = bf.top_codes(7, 20, delisted="only")
        sql = executed[0]
        assert "JOIN stock_basic" in sql and "delisted" in sql
        assert [p["code"] for p in picks] == ["000003", "000004"]

    def test_top_codes_all_no_filter(self, monkeypatch):
        executed = []

        class Cur(TestRowsOkRowsIntegration.FakeCur):
            def fetchall(self):
                return []

        conn = TestRowsOkRowsIntegration.FakeConn(executed)
        conn.cursor = lambda: Cur(executed)
        monkeypatch.setattr(bf, "_connect", lambda: conn)
        bf.top_codes(7, 20)
        assert "JOIN stock_basic" not in executed[0]


class TestEquitySnapshotAndCurves:
    """全部股票曲线：worker 落库降采样快照（5000+ 只不可能查看时重算回测），
    /curves 端点直读快照下发。"""

    def test_row_from_result_snapshots_equity_downsampled(self):
        result = {
            "metrics": {"total_return": 0.26},
            "equity": [[f"2026-{m:02d}-{d:02d}", 1.0 + i * 0.001, 1.0]
                       for i, (m, d) in enumerate(
                           [(1, 2), (1, 3), (1, 6), (1, 7), (2, 2), (2, 3),
                            (2, 4), (3, 1), (3, 2), (3, 3), (3, 4), (3, 5)])],
        }
        row = bf.row_from_result("600519", "贵州茅台", result,
                                 snapshot_points=5)
        snap = row["equity_snapshot"]
        assert len(snap) == 5                          # 12 点降采样到 5 点
        assert snap[0][1] == pytest.approx(1.0)        # 起点保留
        assert snap[-1][1] == pytest.approx(1.011)     # 终点保留

    def test_row_from_result_without_equity(self):
        row = bf.row_from_result("301001", "次新",
                                 {"message": "区间内无行情数据"},
                                 snapshot_points=5)
        assert row["equity_snapshot"] is None

    def test_get_curves_reads_snapshots(self, monkeypatch):
        executed = []

        class Cur(TestRowsOkRowsIntegration.FakeCur):
            def fetchall(self):
                return [("000003", "PT金田Ａ", [[ "2020-01-02", 1.0]]),
                        ("600519", "贵州茅台", [["2020-01-02", 1.0]])]

        conn = TestRowsOkRowsIntegration.FakeConn(executed)
        conn.cursor = lambda: Cur(executed)
        monkeypatch.setattr(bf, "_connect", lambda: conn)
        curves = bf.get_curves(7)
        assert "equity_snapshot IS NOT NULL" in executed[0]
        assert len(curves) == 2
        assert curves[0]["code"] == "000003"


class TestBackfillSpecHashes:
    """老 run 指纹回填：由落库参数现算 spec_hash（幂等，只处理 NULL 行）。"""

    class FakeCur:
        def __init__(self, store):
            self.store = store

        def execute(self, q, p=None):
            self.store.append((q, p))
            return self

        def fetchall(self):
            return [(7, "buy_hold", "3y", '{}', "next_open", 2.5e-4, 5e-4,
                     True, '{"exclude_st": true, "min_bars": 60}')]

    class FakeConn:
        def __init__(self, store):
            self.store = store

        def cursor(self):
            return TestBackfillSpecHashes.FakeCur(self.store)

    def test_recomputes_hash_from_stored_params(self):
        store = []
        bf._backfill_spec_hashes(self.FakeConn(store))
        updates = [(q, p) for q, p in store if q.startswith("UPDATE")]
        assert len(updates) == 1
        expect = bf.spec_hash({
            "strategy": "buy_hold", "range": "3y", "params": {},
            "exec_mode": "next_open", "commission": 2.5e-4,
            "sell_tax": 5e-4, "adjust": True,
            "filters": {"exclude_st": True, "min_bars": 60}})
        assert updates[0][1] == (expect, 7)


# ------------------------------------------------- 组合级全市场（momentum）----

import pandas as pd  # noqa: E402

PORTFOLIO_SPEC = {"strategy": "momentum", "range": "1y",
                  "params": {"window": 5, "top_k": 2},
                  "exec_mode": "next_open", "commission": 2.5e-4,
                  "sell_tax": 5e-4, "adjust": False, "reuse": False,
                  "filters": {"exclude_st": True, "min_bars": 60,
                              "include_delisted": True}}


def _synth_bars(codes, days=60):
    """合成宽表 OHLCV：A 持续涨、B 平、C 跌（动量排序确定）。"""
    idx = pd.bdate_range("2026-01-01", periods=days)
    drift = {"600519": 1.01, "000001": 1.0, "600596": 0.99}
    close = pd.DataFrame({c: 10.0 * (drift[c] ** i)
                          for i, c in enumerate(codes)}, index=idx)
    bars = {"close": close, "open": close.shift(1).fillna(close),
            "high": close * 1.02, "low": close * 0.98,
            "volume": close * 1e6}
    return bars


class TestPortfolioRun:
    """momentum 全市场 = 组合级回测（横截面轮动，输出单组合行而非逐股排行）。"""

    def _run(self, monkeypatch, universe, chunk=2, reuse_src=None):
        """跑 execute_run（momentum spec），捕获落库调用；默认 2 只/批测拼接。"""
        inserts, finished, progress = [], {}, []
        universe = [{"code": c, "name": f"股{c}", "delisted": False}
                    for c in universe]
        monkeypatch.setattr(bf, "_load_universe",
                            lambda rid, spec: (universe, 0))
        monkeypatch.setattr(bf, "set_run_totals",
                            lambda rid, total, skipped:
                            progress.append(("totals", total)))
        monkeypatch.setattr(bf, "insert_row",
                            lambda rid, row: inserts.append(row))
        monkeypatch.setattr(bf, "update_progress",
                            lambda rid, done, failed:
                            progress.append(("prog", done)))
        monkeypatch.setattr(bf, "finish_run",
                            lambda rid, status, stats=None, error=None:
                            finished.update(status=status, stats=stats,
                                            error=error))
        monkeypatch.setattr(bf, "find_reusable_run",
                            lambda *a, **k: reuse_src)
        monkeypatch.setattr(bf, "reusable_ok_codes",
                            lambda rid: {"_PORTFOLIO"})
        monkeypatch.setattr(bf, "copy_ok_rows", lambda *a, **k: 1)
        monkeypatch.setattr(bf, "mark_reuse", lambda *a, **k: None)
        monkeypatch.setattr(bf, "ok_rows", lambda rid, *a, **k: [
            {"status": "ok", "total_return": 0.1,
             "buyhold_return": 0.05, "delisted": False}])
        codes = [s["code"] for s in universe]
        chunks = [codes[i:i + chunk] for i in range(0, len(codes), chunk)]

        def fake_chunk(batch_codes, spec):
            bars = _synth_bars(batch_codes)
            return {"bars": bars, "factors": None}

        monkeypatch.setattr(bf, "_load_bars_chunk", fake_chunk)
        monkeypatch.setattr(bf, "CHUNK", chunk)
        spec = {**PORTFOLIO_SPEC, "reuse": reuse_src is not None}
        asyncio.run(bf.execute_run(99, spec))
        return inserts, finished, progress, chunks

    def test_portfolio_writes_single_row(self, monkeypatch):
        """组合路径：落库恰好 1 行 _PORTFOLIO（ok + 组合快照），终态 done。"""
        inserts, finished, _, _ = self._run(
            monkeypatch, ["600519", "000001", "600596"])
        assert len(inserts) == 1
        row = inserts[0]
        assert row["code"] == "_PORTFOLIO"
        assert row["status"] == "ok"
        assert row["total_return"] is not None
        assert row["equity_snapshot"] and len(row["equity_snapshot"]) > 10
        assert finished["status"] == "done"
        stats = finished["stats"]
        assert stats["count"] == 1                       # 组合即单样本分布
        assert stats["median_return"] == pytest.approx(0.1)

    def test_multi_chunk_concat_uses_all_codes(self, monkeypatch):
        """分批加载后按列拼接：组合收益基于全部 3 只（曲线行数=交易日数）。"""
        inserts, _, _, _ = self._run(
            monkeypatch, ["600519", "000001", "600596"], chunk=2)
        row = inserts[0]
        # 若漏拼某批，末段日期会缺失——快照覆盖完整 60 交易日区间
        assert row["equity_snapshot"][-1][0] == "2026-03-25"  # 60 个 bday 末日

    def test_reuse_copies_portfolio_row(self, monkeypatch):
        """同参复用：找到源 run → 整行复制、不再加载/回测。"""
        inserts, finished, progress, _ = self._run(
            monkeypatch, ["600519", "000001"], reuse_src={"id": 55})
        assert finished["status"] == "done"
        assert inserts == []                             # 无新回测落库
        assert ("prog", 1) in progress                   # 进度直接置完成

    def test_truncate_by_bars_prefers_longest_history(self):
        """max_universe 截断：按区间 K 线数降序取前 N，同数按代码序稳定。"""
        universe = [{"code": c, "name": c, "delisted": False}
                    for c in ("600519", "000001", "600596", "600070")]
        counts = {"600519": 100, "000001": 300, "600596": 200, "600070": 300}
        kept = bf._truncate_by_bars(universe, counts, 2)
        assert [s["code"] for s in kept] == ["000001", "600070"]
        assert bf._truncate_by_bars(universe, counts, 0) == universe  # 0=不限

    def test_top_endpoint_returns_portfolio_snapshot(self, client, monkeypatch):
        """momentum run 的 Top 图：直接返回组合快照曲线，不走逐股现算。"""
        snap = [["2026-01-05", 1.0, 1.0], ["2026-01-06", 1.02, 1.01]]
        run = {**RUN, "strategy": "momentum", "status": "done"}
        monkeypatch.setattr(bf, "get_run", lambda rid: dict(run) if rid == 7 else None)
        monkeypatch.setattr(bf, "portfolio_snapshot", lambda rid: {
            "code": "_PORTFOLIO", "name": "全市场动量轮动组合",
            "status": "ok", "total_return": 0.02, "equity": snap})
        r = client.get("/api/backtest/full/runs/7/top")
        assert r.status_code == 200
        curves = r.json()["curves"]
        assert len(curves) == 1 and curves[0]["code"] == "_PORTFOLIO"
        assert curves[0]["equity"] == snap
