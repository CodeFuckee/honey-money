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
    def test_momentum_rejected(self, client):
        r = client.post("/api/backtest/full", json={
            "strategy": "momentum", "range": "3y"})
        assert r.status_code == 400
        assert "动量" in r.json()["detail"]

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

    def test_run_detail_done_uses_stored_stats(self, client, monkeypatch):
        """完成态用落库 stats，不再实时重算。"""
        stored = {**RUN, "status": "done",
                  "stats": {"count": 99, "positive_pct": 0.5,
                            "median_return": 0.1, "beat_buyhold_pct": 0.6}}
        monkeypatch.setattr(bf, "get_run", lambda rid: stored)
        monkeypatch.setattr(
            bf, "ok_rows",
            lambda rid: pytest.fail("done 态不应实时重算"))
        detail = client.get("/api/backtest/full/runs/7").json()
        assert detail["stats"]["count"] == 99

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
        monkeypatch.setattr(bf, "get_rows", lambda rid, sort, desc, page, size:
                            {"total": 2, "rows": TestRowsAndExportApi.ROWS})
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
