"""脏检测日更测试（2026-11-24）：基金级输入指纹 + 引擎版本 + 按 code 合并导入。

- 引擎版本：engine_version 确定性 / 对 extra（设置页口径）与常量敏感；
- compute_dirty 纯函数五情形：首跑全量 / 引擎变全量 / 无变化全跳 / 净值
  聚合变（含 count/max 不变仅和值变的"历史修正"）/ 新基金与消失基金；
- 存储合并：merge_* 系列范围 DELETE（未变基金行保留）+ 间隔聚合限范围 +
  meta 显式计数 + alive 全表刷新 + 时机死行清理 + 分位回填；
- 编排器 run_full_backtest_tp_dirty 三级：周末 dirty=∅ 零加载快路径 /
  部分重算只喂脏基金 / 引擎变化自动全量。
"""

import concurrent.futures
import datetime as dt
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import dca_store  # noqa: E402


# ===== Fake 连接（同 test_fund_dca_tp 模式 + fetchall 支持指纹读回） =====

class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.executed: list = []
        self.batch: list = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        self.executed.append((query, params))

    def executemany(self, query, seq):
        self.batch.append((query, list(seq)))

    def fetchall(self):
        return self.conn.fetchall_rows


class FakeConn:
    def __init__(self, fetchall_rows=None):
        self.cursor_obj = FakeCursor(self)
        self.fetchall_rows = fetchall_rows or []

    def cursor(self):
        return self.cursor_obj

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _patch_connect(monkeypatch, module, conn):
    def fake_connect(*a, **k):
        return conn
    monkeypatch.setattr(module.psycopg, "connect", fake_connect)


# ===== 引擎版本 =====

class TestEngineVersion:
    def test_deterministic_and_base_prefixed(self):
        from app import dca_backtest as mod
        assert mod.engine_version() == mod.engine_version()
        assert mod.engine_version().startswith(mod.ENGINE_VERSION_BASE)

    def test_extra_changes_version(self):
        """设置页口径（min_span_days）变化 → 版本串变化（自动全量）。"""
        from app import dca_backtest as mod
        assert mod.engine_version(90) != mod.engine_version(120)

    def test_constant_change_changes_version(self):
        """改止盈档位清单（monkeypatch 模拟源码常量变更）→ 版本串变化。"""
        from app import dca_backtest as mod
        old = mod.engine_version()
        monkey_orig = mod.TP_THRESHOLDS
        try:
            mod.TP_THRESHOLDS = (0.05, 0.10)
            assert mod.engine_version() != old
        finally:
            mod.TP_THRESHOLDS = monkey_orig


# ===== compute_dirty 纯函数 =====

def _fp(nav_count=420, max_date="2024-02-24", s_unit="1000.5",
        fund_type="混合型-偏股"):
    """指纹元组（同 load_input_fingerprints 输出形态，逐位可比）；
    窗口 2023-01-01~2024-02-24 ≈ 420 天 ≥ 缺省运作期限下限 90（在宇宙内）。"""
    return (nav_count, "2023-01-01", max_date, s_unit, "1000.5", "105.0",
            "成长先锋混合A", fund_type, False, "12.5", "2026-09-30")


class TestComputeDirty:
    def test_first_run_full(self):
        from app import dca_backtest as mod
        cur = {"000001": _fp(), "000002": _fp()}
        dirty, gone = mod.compute_dirty(cur, "v2", {}, None)
        assert dirty == {"000001", "000002"} and gone == set()

    def test_engine_change_full(self):
        from app import dca_backtest as mod
        cur = {"000001": _fp()}
        prev = {"000001": _fp()}
        dirty, gone = mod.compute_dirty(cur, "v2", prev, "v1")
        assert dirty == {"000001"} and gone == set()

    def test_no_change_skips_all(self):
        from app import dca_backtest as mod
        cur = {"000001": _fp(), "000002": _fp(s_unit="3.2")}
        dirty, gone = mod.compute_dirty(cur, "v1",
                                        {c: f for c, f in cur.items()}, "v1")
        assert dirty == set() and gone == set()

    def test_checksum_only_change_is_dirty(self):
        """历史净值被修正：行数/首末日期不变、精确和变 → 必须重算。"""
        from app import dca_backtest as mod
        cur = {"000001": _fp(s_unit="1000.7")}
        dirty, gone = mod.compute_dirty(cur, "v1", {"000001": _fp()}, "v1")
        assert dirty == {"000001"} and gone == set()

    def test_new_and_gone(self):
        from app import dca_backtest as mod
        cur = {"000001": _fp()}
        prev = {"000001": _fp(), "000009": _fp()}
        dirty, gone = mod.compute_dirty(cur, "v1", prev, "v1")
        assert dirty == set() and gone == {"000009"}


# ===== 存储合并 =====

class TestFingerprintStore:
    def test_load_fingerprints_roundtrip(self):
        conn = FakeConn(fetchall_rows=[("000001", ["a", 1, 2], "v1"),
                                       ("000002", ["b", 3], "v1")])
        fps, engine = dca_store.load_fingerprints(conn)
        assert engine == "v1"
        assert fps == {"000001": ("a", 1, 2), "000002": ("b", 3)}
        assert all(isinstance(v, tuple) for v in fps.values())

    def test_load_fingerprints_empty_table(self):
        fps, engine = dca_store.load_fingerprints(FakeConn())
        assert fps == {} and engine is None

    def test_replace_fingerprints_sequence(self):
        conn = FakeConn()
        n = dca_store.replace_fingerprints(conn, {"000001": ("a", 1)}, "v7")
        assert n == 1
        sqls = [sql for sql, _ in conn.cursor_obj.executed]
        assert sqls[0] == "DELETE FROM fund_dca_fp"
        # INSERT 走 executemany → batch
        assert "INSERT INTO fund_dca_fp" in conn.cursor_obj.batch[0][0]
        assert conn.cursor_obj.batch[0][1] == [("000001", '["a", 1]', "v7")]

    def test_ensure_fp_schema_ddl(self):
        conn = FakeConn()
        dca_store.ensure_fp_schema(conn)
        sqls = [sql for sql, _ in conn.cursor_obj.executed]
        assert any("CREATE TABLE IF NOT EXISTS fund_dca_fp" in s
                   for s in sqls)


def _old_row(code="000001"):
    """旧口径结果行全列 fixture（列齐防 merge_rows KeyError，同
    test_fund_dca_tp._tp_row 的保列齐全约定）。"""
    return {"code": code, "name": "成长先锋混合A", "fund_type": "混合型-偏股",
            "alive": True, "scale_yi": 12.5, "scale_date": None,
            "w10_start": None, "w10_end": None, "w10_months": 10,
            "w10_rounds": 3, "w10_invested": 10000.0, "w10_final_value": 12000.0,
            "w10_profit_rate": 0.2, "w10_xirr": 0.15,
            "full_start": None, "full_end": None, "full_months": 12,
            "full_rounds": 4, "full_invested": 12000.0,
            "full_final_value": 15000.0, "full_profit_rate": 0.25,
            "full_xirr": 0.18, "w10_xirr_disc": 0.14, "w10_xirr_nom": 0.13,
            "full_xirr_disc": 0.17, "full_xirr_nom": 0.16,
            "w10_rounds_disc": 3, "w10_rounds_nom": 3,
            "full_rounds_disc": 4, "full_rounds_nom": 4,
            "w10_profit_rate_disc": 0.19, "w10_profit_rate_nom": 0.18,
            "full_profit_rate_disc": 0.24, "full_profit_rate_nom": 0.23,
            "full_last_tp": None, "full_last_tp_disc": None,
            "full_last_tp_nom": None, "full_cur_invested": None,
            "full_cur_profit_rate": None, "full_cur_profit_rate_disc": None,
            "full_cur_profit_rate_nom": None,
            "full_cur_xirr": None, "full_cur_xirr_disc": None,
            "full_cur_xirr_nom": None}


class TestMergeImports:
    def test_merge_rows_scoped_delete_and_meta_count(self):
        rows = [_old_row("000001")]
        conn = FakeConn()
        n = dca_store.merge_rows(conn, rows, ["000001", "000009"],
                                 fund_count=26677,
                                 params={"engine": "v1"})
        assert n == 1
        sqls = [sql for sql, _ in conn.cursor_obj.executed]
        assert sqls[0].startswith("DELETE FROM fund_dca_result")
        assert conn.cursor_obj.executed[0][1] == ("{000001,000009}",)
        assert any("fund_dca_meta" in s for s in sqls)
        meta_call = [p for s, p in conn.cursor_obj.executed
                     if "fund_dca_meta" in s][0]
        assert meta_call[0] == 26677 and "engine" in meta_call[1]
        assert "INSERT INTO fund_dca_result" in conn.cursor_obj.batch[0][0]

    def test_merge_rows_empty_codes_keeps_all_rows(self):
        """周末快路径：rows/codes 皆空 → 只刷 meta，不动任何数据行。"""
        conn = FakeConn()
        dca_store.merge_rows(conn, [], [], fund_count=26677)
        assert conn.cursor_obj.batch == []
        sqls = [sql for sql, _ in conn.cursor_obj.executed]
        assert not any(s.startswith("DELETE FROM fund_dca_result")
                       for s in sqls)

    def test_merge_rounds_gap_scoped_not_whole_table(self):
        """间隔聚合只对范围内基金 NULL 化 + 重聚合（绝不全表 RESET）。"""
        conn = FakeConn()
        dca_store.merge_rounds(conn, [("000001", "gross", 1, "2024-03-01",
                                       30, 10, 200.0, 1.1, 1000.0, 0.0,
                                       0.0, 200.0)], ["000001"])
        sqls = [s.strip() for s, _ in conn.cursor_obj.executed]
        assert any(s.startswith("DELETE FROM fund_dca_rounds")
                   and "ANY(%s)" in s for s in sqls)
        resets = [s for s in sqls
                  if s.startswith("UPDATE fund_dca_result SET gap_avg_days")]
        assert len(resets) == 1 and "ANY(%s)" in resets[0]
        applies = [s for s in sqls
                   if s.startswith("UPDATE fund_dca_result r SET")]
        assert len(applies) == 1 and "ANY(%s)" in applies[0]

    def test_merge_tp_rows_and_rounds_scoped(self):
        conn = FakeConn()
        tp_row = {**_old_row("000001"), "tp": 0.2, "method": "amount"}
        dca_store.merge_tp_rows(conn, [tp_row], ["000001"],
                                fund_count=5, params={})
        dca_store.merge_tp_rounds(conn, [("000001", "amount", 0.2, "gross",
                                          1, "2024-03-01", 30, 10, 200.0,
                                          1.1, 1000.0, 0.0, 0.0, 200.0)],
                                  ["000001"])
        sqls = [s.strip() for s, _ in conn.cursor_obj.executed]
        assert any(s.startswith("DELETE FROM fund_dca_tp_result")
                   for s in sqls)
        assert any(s.startswith("DELETE FROM fund_dca_tp_rounds")
                   for s in sqls)
        assert any(s.startswith("UPDATE fund_dca_tp_result SET gap_avg_days")
                   and "ANY(%s)" in s for s in sqls)
        meta_call = [p for s, p in conn.cursor_obj.executed
                     if "fund_dca_tp_meta" in s][0]
        assert meta_call[0] == 5

    def test_refresh_alive_both_tables_and_str_cutoff(self):
        conn = FakeConn()
        dca_store.refresh_alive(conn, "2026-10-10")
        sqls = [sql for sql, _ in conn.cursor_obj.executed]
        assert len(sqls) == 2
        assert all("SET alive =" in s for s, _ in
                   [(s, p) for s, p in conn.cursor_obj.executed])
        assert all(p[0] == dt.date(2026, 10, 10)
                   for _, p in conn.cursor_obj.executed)

    def test_refresh_alive_none_noop(self):
        conn = FakeConn()
        dca_store.refresh_alive(conn, None)
        assert conn.cursor_obj.executed == []

    def test_merge_timing_cleanup_insert_stamp(self):
        rows = [("000001", -0.22, 12, "25-30", "2026-11-22")]
        conn = FakeConn()
        n = dca_store.merge_timing(conn, rows, computed_at="2026-11-22")
        assert n == 1
        sqls = [s.strip() for s, _ in conn.cursor_obj.executed]
        assert "NOT EXISTS" in sqls[0] and "fund_dip_timing" in sqls[0]
        # INSERT 走 executemany → batch
        assert any("INSERT INTO fund_dip_timing" in s
                   for s, _ in conn.cursor_obj.batch)
        stamp = [p for s, p in conn.cursor_obj.executed
                 if s.strip().startswith("UPDATE fund_dip_timing SET")][0]
        assert stamp == ("2026-11-22",)

    def test_merge_risk_runs_cross_section_backfill(self):
        conn = FakeConn()
        dca_store.merge_risk(conn, [("000001", *([0.1] * 22))], ["000001"])
        sqls = [sql for sql, _ in conn.cursor_obj.executed]
        assert any(s.startswith("DELETE FROM fund_risk_metrics")
                   and "ANY(%s)" in s for s in sqls)
        assert any("percent_rank" in s for s in sqls)   # 横截面回填必执行


# ===== 编排器 run_full_backtest_tp_dirty =====

def _synthetic_nav():
    """两只基金：A 持续上行（有轮次），B 平躺（无轮次）。"""
    dates = pd.date_range("2024-01-01", periods=420, freq="D")
    import numpy as np
    a = np.cumprod(1.0 + np.full(420, 0.0025))
    navs = []
    for i, d in enumerate(dates):
        navs.append(("000001", d, round(float(a[i]), 6),
                     round(float(a[i]), 6), 0.25))
        navs.append(("000002", d, 1.0, 1.0, 0.0))
    return pd.DataFrame(navs, columns=["code", "nav_date", "unit_nav",
                                       "accum_nav", "pct_chg"])


def _basic_scale():
    basic = pd.DataFrame(
        {"name": {"000001": "成长先锋混合A", "000002": "稳如磐石混合A"},
         "fund_type": {"000001": "混合型-偏股", "000002": "混合型-偏股"},
         "delisted": {"000001": False, "000002": False}})
    scale_map = {"000001": {"scale_yi": 12.5, "scale_date": "2026-09-30"},
                 "000002": {"scale_yi": 3.2, "scale_date": "2026-09-30"}}
    return basic, scale_map


class TestRunFullBacktestTpDirty:
    def _run(self, monkeypatch, cur_fps, prev_fps, prev_engine):
        from app import dca_backtest as mod
        # 锁串行内联（monkeypatch 只在本进程生效，真拉池会让子进程
        # 脱桩连真库）；并行派发缝另有专测
        monkeypatch.setenv("BACKTEST_WORKERS", "1")
        nav = _synthetic_nav()
        basic, scale_map = _basic_scale()
        calls = {"basic": [], "nav": []}

        def fake_basic(conn, codes):
            calls["basic"].append(list(codes))
            return basic.loc[codes]

        def fake_scale(conn):
            return scale_map

        def fake_nav(conn, codes):
            calls["nav"].append(list(codes))
            return nav[nav["code"].isin(codes)].reset_index(drop=True)

        monkeypatch.setattr(mod, "load_input_fingerprints",
                            lambda conn: cur_fps)
        monkeypatch.setattr(mod, "_load_basic", fake_basic)
        monkeypatch.setattr(mod, "_load_scale", fake_scale)
        monkeypatch.setattr(mod, "_load_nav", fake_nav)
        monkeypatch.setattr(mod, "engine_version", lambda *a: "vTEST")
        monkeypatch.setattr(dca_store, "load_fingerprints",
                            lambda conn: (prev_fps, prev_engine))
        _patch_connect(monkeypatch, mod, FakeConn())
        out = mod.run_full_backtest_tp_dirty("postgresql://fake")
        assert len(out) == 8
        return out, calls

    def test_zero_dirty_weekend_fast_path(self, monkeypatch):
        """指纹无变化：不加载任何净值、不产生结果行，stats 全跳。"""
        fps = {"000001": _fp(), "000002": _fp(nav_count=420, s_unit="3.2")}
        (old_rows, old_rounds, tp_rows, tp_rounds, timing_rows, risk_rows,
         stats, fingerprints), calls = self._run(
            monkeypatch, fps, fps, "vTEST")
        assert calls["nav"] == [] and calls["basic"] == []
        assert (old_rows, old_rounds, tp_rows, tp_rounds, timing_rows,
                risk_rows) == ([], [], [], [], [], [])
        assert stats["full_run"] is False and stats["dirty_count"] == 0
        assert stats["skipped_count"] == 2 and stats["fund_count"] == 2
        assert fingerprints is fps or fingerprints == fps

    def test_window_end_ignores_single_row_fund(self, monkeypatch):
        """单行基金不产生结果行 → 不参与全局窗口末日（与旧口径一致）。"""
        fps = {"000001": _fp(max_date="2024-02-24"),
               "000002": _fp(max_date="2024-02-24", s_unit="3.2"),
               "000003": _fp(nav_count=1, max_date="2024-06-01")}
        (_rows, _r, _tp, _tpr, _ti, _ri, stats, _fps), _ = self._run(
            monkeypatch, fps, fps, "vTEST")
        assert stats["window_end"] == "2024-02-24"

    def test_partial_recompute_only_dirty(self, monkeypatch):
        """仅 B 指纹变：只加载/重算 B；消失基金进 gone。"""
        cur = {"000001": _fp(), "000002": _fp(s_unit="9.9")}
        prev = {"000001": _fp(), "000002": _fp(), "000009": _fp()}
        (old_rows, _o, tp_rows, _t, _ti, risk_rows, stats, _fps), calls = \
            self._run(monkeypatch, cur, prev, "vTEST")
        assert calls["nav"] == [["000002"]]
        # skipped = cur 全集 2 - 重算 1（gone 基金不计入 cur 全集）
        assert stats["dirty_count"] == 1 and stats["skipped_count"] == 1
        assert stats["gone_count"] == 1 and stats["gone"] == ["000009"]
        assert stats["full_run"] is False
        assert {r["code"] for r in old_rows} == {"000002"}
        assert {r["code"] for r in tp_rows} == {"000002"}
        assert {r[0] for r in risk_rows} == {"000002"}
        assert all("alive" in r for r in old_rows)

    def test_engine_change_full_run(self, monkeypatch):
        """引擎版本变化（如设置页口径调整）：全部基金重算。"""
        cur = {"000001": _fp(), "000002": _fp(s_unit="3.2")}
        (_rows, _o, _tp, _t, _ti, _ri, stats, _fps), calls = self._run(
            monkeypatch, cur, cur, "vOLD")
        assert stats["full_run"] is True
        assert stats["dirty_count"] == 2 and stats["skipped_count"] == 0
        assert sorted(calls["nav"][0]) == ["000001", "000002"]

    def test_force_full_env(self, monkeypatch, ):
        """BACKTEST_FORCE_FULL=1：无视相同指纹与版本，强制全量。"""
        monkeypatch.setenv("BACKTEST_FORCE_FULL", "1")
        fps = {"000001": _fp(), "000002": _fp(s_unit="3.2")}
        (_rows, _o, _tp, _t, _ti, _ri, stats, _fps), calls = self._run(
            monkeypatch, fps, fps, "vTEST")
        assert stats["full_run"] is True
        assert sorted(calls["nav"][0]) == ["000001", "000002"]


# ===== 单机并行：分片 / worker 数 / 派发缝 / worker 内联一致性 =====

class TestChunkCodes:
    def test_empty(self):
        from app import dca_backtest as mod
        assert mod._chunk_codes([], 250) == []

    def test_exact_division(self):
        from app import dca_backtest as mod
        chunks = mod._chunk_codes(["a", "b", "c", "d"], 2)
        assert chunks == [["a", "b"], ["c", "d"]]

    def test_remainder(self):
        from app import dca_backtest as mod
        assert mod._chunk_codes(["a", "b", "c"], 2) == [["a", "b"], ["c"]]

    def test_size_over_len_single_chunk(self):
        from app import dca_backtest as mod
        assert mod._chunk_codes(["a"], 250) == [["a"]]

    def test_bad_size_falls_back_to_default(self):
        from app import dca_backtest as mod
        assert mod._chunk_codes(["a", "b"], 0) == [["a", "b"]]

    def test_order_preserved_and_concat_roundtrip(self):
        from app import dca_backtest as mod
        codes = [f"{i:06d}" for i in range(501)]
        chunks = mod._chunk_codes(codes, 250)
        assert sum(len(c) for c in chunks) == 501
        assert [c for ch in chunks for c in ch] == codes   # 无重无漏有序


class TestWorkerCountAndChunkSize:
    def test_default_is_min_four_cpu(self):
        import os
        from app import dca_backtest as mod
        monkey_free = max(1, min(4, os.cpu_count() or 1))
        assert mod._worker_count() == monkey_free

    def test_env_override(self, monkeypatch):
        from app import dca_backtest as mod
        monkeypatch.setenv("BACKTEST_WORKERS", "8")
        assert mod._worker_count() == 8
        monkeypatch.setenv("BACKTEST_WORKERS", "1")
        assert mod._worker_count() == 1

    def test_env_invalid_falls_back(self, monkeypatch):
        from app import dca_backtest as mod
        for bad in ("abc", "0", "-3", ""):
            monkeypatch.setenv("BACKTEST_WORKERS", bad)
            assert mod._worker_count() >= 1

    def test_chunk_size_env(self, monkeypatch):
        from app import dca_backtest as mod
        assert mod._chunk_size() == 250
        monkeypatch.setenv("BACKTEST_CHUNK_SIZE", "100")
        assert mod._chunk_size() == 100
        monkeypatch.setenv("BACKTEST_CHUNK_SIZE", "bad")
        assert mod._chunk_size() == 250


class TestParallelDispatch:
    def _run_parallel(self, monkeypatch, chunk_results):
        """patch 派发缝为 fake（不真拉池），验证切块数/合并完整性/全局锚。
        chunk_results 按 (首代码 → 6 元组) 索引，fake 严格按 args 逐块返回。"""
        from app import dca_backtest as mod
        monkeypatch.setenv("BACKTEST_WORKERS", "4")
        monkeypatch.setenv("BACKTEST_CHUNK_SIZE", "1")   # 每块一基金 → 2 块
        fps = {"000001": _fp(max_date="2024-02-24"),
               "000002": _fp(max_date="2024-02-24", s_unit="3.2")}
        recorded = {}
        by_first = {chunk_result_codes: res for chunk_result_codes, res
                    in chunk_results.items()}

        def fake_dispatch(args, workers):
            recorded["workers"] = workers
            recorded["args"] = args
            return [by_first[tuple(a[1])] for a in args]

        monkeypatch.setattr(mod, "_run_chunk_tasks", fake_dispatch)
        monkeypatch.setattr(mod, "engine_version", lambda *a: "vTEST")
        monkeypatch.setattr(mod, "load_input_fingerprints",
                            lambda conn: fps)
        # prev 引擎版本不同 → 全量重算（保证走进并行派发分支）
        monkeypatch.setattr(dca_store, "load_fingerprints",
                            lambda conn: (fps, "vOLD"))
        _patch_connect(monkeypatch, mod, FakeConn())
        out = mod.run_full_backtest_tp_dirty("postgresql://fake")
        return out, recorded

    def test_merge_and_stats(self, monkeypatch):
        from app import dca_backtest as mod
        r1 = ([{"code": "000001", "full_end": "2023-11-01"}],   # 早于 cutoff → 死
              [], [{"code": "000001", "tp": 0.2, "method": "amount"}], [], [], [])
        r2 = ([{"code": "000002", "full_end": "2024-02-24"}],
              [], [{"code": "000002", "tp": 0.2, "method": "amount"}], [], [], [])
        (old_rows, old_rounds, tp_rows, tp_rounds, timing_rows, risk_rows,
         stats, _fps), recorded = self._run_parallel(
            monkeypatch, {("000001",): r1, ("000002",): r2})
        assert {r["code"] for r in old_rows} == {"000001", "000002"}
        alive = {r["code"]: r["alive"] for r in old_rows}
        # cutoff = window_end 2024-02-24 - 45d = 2024-01-10
        assert alive == {"000001": False, "000002": True}
        # worker 数被块数截断：min(env 4, 2 块) = 2
        assert stats["chunks"] == 2 and stats["workers"] == 2
        assert stats["round_count"] == 0
        # 全局锚点下传：两块同 end_ts/computed_at/cutoff
        for (dsn, codes, span, cutoff_iso, computed_at, end_ts) \
                in recorded["args"]:
            assert end_ts == "2024-02-24" and computed_at == "2024-02-24"
            assert cutoff_iso == "2024-01-10"
            assert span == 90 and dsn == "postgresql://fake"
        assert [tuple(a[1]) for a in recorded["args"]] == \
            [("000001",), ("000002",)]   # 每块一基金（250>2）

    def test_worker_capped_by_chunks(self, monkeypatch):
        """worker 数被块数截断（2 只基金 1 块 → workers=1）。"""
        from app import dca_backtest as mod
        monkeypatch.setenv("BACKTEST_WORKERS", "8")
        monkeypatch.setenv("BACKTEST_CHUNK_SIZE", "250")
        fps = {"000001": _fp()}
        captured = {}

        def fake_dispatch(args, workers):
            captured["workers"] = workers
            return [([{"code": "000001", "full_end": "2024-02-24"}], [],
                     [{"code": "000001", "tp": 0.2, "method": "amount"}],
                     [], [], [])]

        monkeypatch.setattr(mod, "_run_chunk_tasks", fake_dispatch)
        monkeypatch.setattr(mod, "engine_version", lambda *a: "vTEST")
        monkeypatch.setattr(mod, "load_input_fingerprints",
                            lambda conn: fps)
        monkeypatch.setattr(dca_store, "load_fingerprints",
                            lambda conn: (fps, "vOLD"))
        _patch_connect(monkeypatch, mod, FakeConn())
        out = mod.run_full_backtest_tp_dirty("postgresql://fake")
        assert captured["workers"] == 1 and out[6]["workers"] == 1


class TestChunkWorkerInline:
    """worker 函数内联真跑（串行路径），与顺序版逐字段对照。"""

    def _patch_loaders(self, monkeypatch):
        from app import dca_backtest as mod
        nav = _synthetic_nav()
        basic, scale_map = _basic_scale()
        monkeypatch.setattr(mod, "_load_basic",
                            lambda conn, codes: basic.loc[list(codes)])
        monkeypatch.setattr(mod, "_load_scale", lambda conn: scale_map)
        monkeypatch.setattr(
            mod, "_load_nav",
            lambda conn, codes: nav[nav["code"].isin(list(codes))]
            .reset_index(drop=True))
        _patch_connect(monkeypatch, mod, FakeConn())
        return nav, basic, scale_map

    def test_shape_and_alive_no_cutoff(self, monkeypatch):
        from app import dca_backtest as mod
        self._patch_loaders(monkeypatch)
        out = mod._backtest_chunk_worker("postgresql://fake",
                                         ("000001", "000002"), 90,
                                         None, None, None)
        old_rows, old_rounds, tp_rows, tp_rounds, timing_rows, risk_rows = out
        assert len(tp_rows) == (2 * len(mod.DCA_METHODS)
                                * len(mod.TP_THRESHOLDS))
        assert {r["code"] for r in old_rows} == {"000001", "000002"}
        assert all(r["alive"] is False for r in old_rows)   # cutoff None → 全死
        assert timing_rows == []                            # alive 门控
        assert {r[0] for r in risk_rows} == {"000001", "000002"}

    def test_rows_match_sequential_per_method(self, monkeypatch):
        """worker 产出行与顺序版 run_backtest_tp 逐字段一致（alive 除外）。"""
        from app import dca_backtest as mod
        nav, basic, scale_map = self._patch_loaders(monkeypatch)
        out = mod._backtest_chunk_worker("postgresql://fake",
                                         ("000001", "000002"), 90,
                                         None, None, None)
        _, _, tp_rows, tp_rounds, _t, _r = out
        for method in mod.DCA_METHODS:
            seq_rows, seq_rounds = mod.run_backtest_tp(
                nav, basic, scale_map, mod.TP_THRESHOLDS, method=method)
            w_rows = [r for r in tp_rows if r["method"] == method]
            key = lambda r: (r["code"], r["tp"])
            assert sorted(map(key, w_rows)) == sorted(map(key, seq_rows))
            for wr, sr in zip(sorted(w_rows, key=key),
                              sorted(seq_rows, key=key)):
                assert {k: v for k, v in wr.items() if k != "alive"} == sr
            w_rounds_m = [t for t in tp_rounds if t[1] == method]
            assert sorted(w_rounds_m) == sorted(seq_rounds)

    def test_alive_cutoff_gates_timing(self, monkeypatch):
        """cutoff 在末净值日之前 → 两基金 alive → 时机行产出且时间戳正确。"""
        from app import dca_backtest as mod
        self._patch_loaders(monkeypatch)
        out = mod._backtest_chunk_worker("postgresql://fake",
                                         ("000001", "000002"), 90,
                                         "2025-01-01", "2025-02-23",
                                         "2025-02-23")
        old_rows, _o, _tp, _tpr, timing_rows, _r = out
        assert all(r["alive"] is True for r in old_rows)
        assert {t[0] for t in timing_rows} == {"000001", "000002"}
        assert all(t[4] == "2025-02-23" for t in timing_rows)


class TestChunkProgressCallback:
    """_run_chunk_tasks on_done 回调（2026-11-25 十轮：基金级进度回传缝）：
    串行与池两路逐块回调、池路结果顺序与输入一致（map 兼容）、worker 异常
    原样抛回（同 map 语义）、不传回调零改动。"""

    def test_serial_calls_on_done_per_chunk(self, monkeypatch):
        from app import dca_backtest as mod
        seen = []
        monkeypatch.setattr(mod, "_backtest_chunk_worker", lambda *a: ("r",))
        out = mod._run_chunk_tasks([("a",), ("b",), ("c",)], 1,
                                   on_done=lambda i: seen.append(i))
        assert out == [("r",), ("r",), ("r",)]
        assert seen == [0, 1, 2]

    def test_pool_calls_on_done_and_preserves_order(self, monkeypatch):
        from app import dca_backtest as mod
        seen = []
        monkeypatch.setattr(mod, "_backtest_chunk_worker", lambda tag: tag)
        monkeypatch.setattr(concurrent.futures, "ProcessPoolExecutor",
                            _FakeInlinePool)
        out = mod._run_chunk_tasks([("a",), ("b",), ("c",)], 3,
                                   on_done=seen.append)
        assert out == ["a", "b", "c"]            # 顺序与输入一致（map 兼容）
        assert sorted(seen) == [0, 1, 2]

    def test_pool_worker_exception_propagates(self, monkeypatch):
        from app import dca_backtest as mod

        def fake_worker(tag):
            if tag == "b":
                raise RuntimeError("boom")
            return tag

        monkeypatch.setattr(mod, "_backtest_chunk_worker", fake_worker)
        monkeypatch.setattr(concurrent.futures, "ProcessPoolExecutor",
                            _FakeInlinePool)
        with pytest.raises(RuntimeError):
            mod._run_chunk_tasks([("a",), ("b",), ("c",)], 3)

    def test_no_callback_default_unchanged(self, monkeypatch):
        """不传 on_done：行为与原 map 逐位一致（既有调用零改动锚）。"""
        from app import dca_backtest as mod
        monkeypatch.setattr(mod, "_backtest_chunk_worker", lambda tag: tag * 2)
        monkeypatch.setattr(concurrent.futures, "ProcessPoolExecutor",
                            _FakeInlinePool)
        assert mod._run_chunk_tasks([("a",), ("b",)], 2) == ["aa", "bb"]


class _FakeInlinePool:
    """提交即执行的进程池替身（闭包进不了 spawn 池，测试内联解结果）。"""

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def submit(self, fn, *args, **kwargs):
        fut = concurrent.futures.Future()
        fut.set_result(fn(*args, **kwargs))
        return fut


class TestDirtyProgressCallback:
    """run_full_backtest_tp_dirty progress 参数（2026-11-25 十轮）：dirty 集
    算出后先报 (0, 需重算总数)，每块完成报 (累计基金数, 总数)——设置页
    进度条的真实分母即「本次需要回测的基金数」。"""

    def test_progress_sequence_monotonic(self, monkeypatch):
        from app import dca_backtest as mod
        monkeypatch.setenv("BACKTEST_WORKERS", "1")     # 串行内联（不拉池）
        monkeypatch.setenv("BACKTEST_CHUNK_SIZE", "1")  # 每块 1 基金 → 2 块
        fps = {"000001": _fp(max_date="2024-02-24"),
               "000002": _fp(max_date="2024-02-24", s_unit="3.2")}
        monkeypatch.setattr(mod, "engine_version", lambda *a: "vTEST")
        monkeypatch.setattr(mod, "load_input_fingerprints", lambda conn: fps)
        # prev 引擎不同 → 全量（dirty=全部）
        monkeypatch.setattr(dca_store, "load_fingerprints",
                            lambda conn: (fps, "vOLD"))
        _patch_connect(monkeypatch, mod, FakeConn())
        # worker 打桩（进度测试只看回调序列，不算真净值）
        monkeypatch.setattr(mod, "_backtest_chunk_worker",
                            lambda *a: ([], [], [], [], [], []))
        seen = []
        mod.run_full_backtest_tp_dirty(
            "postgresql://fake", log=lambda *_: None,
            progress=lambda d, t: seen.append((d, t)))
        assert seen[0] == (0, 2)
        assert seen[-1] == (2, 2)
        assert [d for d, _t in seen] == sorted(d for d, _t in seen)
        assert all(t == 2 for _d, t in seen)

    def test_no_progress_kwarg_unchanged(self, monkeypatch):
        """不传 progress：派发缝不收到 on_done（既有调用零改动锚）。"""
        from app import dca_backtest as mod
        monkeypatch.setenv("BACKTEST_WORKERS", "1")
        fps = {"000001": _fp()}
        monkeypatch.setattr(mod, "engine_version", lambda *a: "vTEST")
        monkeypatch.setattr(mod, "load_input_fingerprints", lambda conn: fps)
        monkeypatch.setattr(dca_store, "load_fingerprints",
                            lambda conn: (fps, "vOLD"))
        captured = {}

        def fake_dispatch(args, workers, on_done=None):
            captured["on_done"] = on_done
            return [([], [], [], [], [], [])]

        monkeypatch.setattr(mod, "_run_chunk_tasks", fake_dispatch)
        _patch_connect(monkeypatch, mod, FakeConn())
        mod.run_full_backtest_tp_dirty("postgresql://fake",
                                       log=lambda *_: None)
        assert captured["on_done"] is None


class TestBacktestUniverse:
    """回测宇宙排除（2026-11-25 十四轮用户定案）：非权益类（fund_type 不在
    BACKTEST_UNIVERSE_TYPES，8 类权益白名单，NULL/未知视为非权益）与年轻
    基金（全史窗口 < 运作期限下限）不产生任何回测结果行——此前仅年化置空；
    排除只作用于回测结果（旧口径表+tp 两表），时机表/波动指标表维持全市场。
    被排除基金的历史行由「引擎版本变化→自动全量→merge 全删重插」清理，
    无需专门清理机制。"""

    def test_universe_constant_pinned(self):
        """白名单口径锚：权益类 8 类（与排行默认筛选同一事实源，
        dca_api.EQUITY_TYPES 别名引用）。"""
        from app.dca_backtest import BACKTEST_UNIVERSE_TYPES
        assert BACKTEST_UNIVERSE_TYPES == ("股票型", "混合型-偏股",
                                           "混合型-灵活", "混合型-平衡",
                                           "指数型-股票", "指数型-海外股票",
                                           "QDII-普通股票", "QDII-混合偏股")

    def test_rule_equity_types_included(self):
        import datetime as dt
        from app.dca_backtest import BACKTEST_UNIVERSE_TYPES, fund_excluded
        d1, d2 = dt.date(2024, 1, 1), dt.date(2025, 2, 1)
        for ft in BACKTEST_UNIVERSE_TYPES:
            assert not fund_excluded(ft, d1, d2, 365), ft

    def test_rule_non_equity_excluded(self):
        import datetime as dt
        from app.dca_backtest import fund_excluded
        d1, d2 = dt.date(2024, 1, 1), dt.date(2025, 2, 1)
        for ft in ("债券型-长债", "债券型-混合二级", "货币型", "FOF-稳健型",
                   "Reits", "商品", "QDII-商品", "混合型-偏债", "指数型-固收",
                   "指数型-其他", "", None):
            assert fund_excluded(ft, d1, d2, 365), ft

    def test_rule_young_boundary_same_as_xirr_gate(self):
        """窗口边界 ≥ 与引擎年化门槛同约定：364 天排除、365 天保留。"""
        import datetime as dt
        from app.dca_backtest import fund_excluded
        d1 = dt.date(2024, 1, 1)
        assert fund_excluded("股票型", d1, d1 + dt.timedelta(days=364), 365)
        assert not fund_excluded("股票型", d1, d1 + dt.timedelta(days=365), 365)

    def test_rule_missing_dates_excluded(self):
        from app.dca_backtest import fund_excluded
        assert fund_excluded("股票型", None, None, 365)

    def test_universe_types_in_version_material(self):
        """白名单纳入引擎版本串（改口径自动全量——防脏数据不变量）。"""
        from app import dca_backtest as mod
        old = mod.engine_version()
        saved = mod.BACKTEST_UNIVERSE_TYPES
        try:
            mod.BACKTEST_UNIVERSE_TYPES = saved + ("混合型-偏债",)
            assert mod.engine_version() != old
        finally:
            mod.BACKTEST_UNIVERSE_TYPES = saved
        assert mod.engine_version() == old

    def test_run_backtest_tp_skips_excluded(self):
        """编排层排除（脚本离线路径同享）：债券基金与年轻权益基金无结果行
        无轮次行，权益老基金正常产出 8 档。"""
        import numpy as np
        from app import dca_backtest as mod
        dates = pd.date_range("2024-01-01", periods=400, freq="D")
        a = np.cumprod(1.0 + np.full(400, 0.002))
        navs = []
        for code in ("000001", "000004"):
            for i, d in enumerate(dates):
                navs.append((code, d, round(float(a[i]), 6),
                             round(float(a[i]), 6), 0.25))
        young = pd.date_range("2025-08-01", periods=100, freq="D")
        for code in ("000003", "000005"):
            for d in young:
                navs.append((code, d, 1.0, 1.0, 0.0))
        nav = pd.DataFrame(navs, columns=["code", "nav_date", "unit_nav",
                                          "accum_nav", "pct_chg"])
        basic = pd.DataFrame(
            {"name": {"000001": "权益老A", "000003": "权益年轻C",
                      "000004": "债基B", "000005": "权益超短F"},
             "fund_type": {"000001": "混合型-偏股", "000003": "混合型-偏股",
                           "000004": "债券型-长债", "000005": "股票型"},
             "delisted": {c: False for c in
                          ("000001", "000003", "000004", "000005")}})
        # 显式 365（对齐生产设置）：100 天窗口的年轻权益基金排除在先，
        # 「年化置空」不再发生——结果行/轮次行都不产出
        rows, rounds = mod.run_backtest_tp(nav, basic, {}, method="amount",
                                           min_span_days=365)
        codes = {r["code"] for r in rows}
        assert codes == {"000001"}, codes
        assert all(t[0] == "000001" for t in rounds)

    def test_dirty_stats_universe_count(self, monkeypatch):
        """脏检测编排统计增 universe_count/excluded_count（全市场 3 只中
        债基排除 → 宇宙 2）。"""
        from app import dca_backtest as mod
        monkeypatch.setenv("BACKTEST_WORKERS", "1")
        # 窗口须 ≥ 运作期限下限（缺省 90）：min 2024-01-01 → max 2025-06-01
        fps = {"000001": _fp(max_date="2025-06-01"),
               "000002": _fp(max_date="2025-06-01", s_unit="3.2"),
               "000004": _fp(max_date="2025-06-01", fund_type="债券型-长债")}
        monkeypatch.setattr(mod, "engine_version", lambda *a: "vTEST")
        monkeypatch.setattr(mod, "load_input_fingerprints", lambda conn: fps)
        monkeypatch.setattr(dca_store, "load_fingerprints",
                            lambda conn: (fps, "vOLD"))
        captured = {}

        def fake_dispatch(args, workers, on_done=None):
            captured["on_done"] = on_done
            return [([], [], [], [], [], [])]

        monkeypatch.setattr(mod, "_run_chunk_tasks", fake_dispatch)
        _patch_connect(monkeypatch, mod, FakeConn())
        _s, _o, _t, _tr, _ti, _r, stats, _fps = mod.run_full_backtest_tp_dirty(
            "postgresql://fake", log=lambda *_: None)
        assert stats["universe_count"] == 2
        assert stats["excluded_count"] == 1

    def test_excluded_funds_not_chunked(self, monkeypatch):
        """被排除基金不进分片也不进进度分母（十五轮）——merge 的 dirty 集
        仍含它们（stats["dirty"]），历史行清理由全量 merge 全删重插完成。"""
        from app import dca_backtest as mod
        monkeypatch.setenv("BACKTEST_WORKERS", "1")
        fps = {"000001": _fp(max_date="2025-06-01"),
               "000002": _fp(max_date="2025-06-01", s_unit="3.2"),
               "000004": _fp(max_date="2025-06-01", fund_type="债券型-长债")}
        monkeypatch.setattr(mod, "engine_version", lambda *a: "vTEST")
        monkeypatch.setattr(mod, "load_input_fingerprints", lambda conn: fps)
        monkeypatch.setattr(dca_store, "load_fingerprints",
                            lambda conn: (fps, "vOLD"))
        _patch_connect(monkeypatch, mod, FakeConn())
        monkeypatch.setattr(mod, "_backtest_chunk_worker",
                            lambda *a: ([], [], [], [], [], []))
        captured = {}

        def fake_dispatch(args, workers, on_done=None):
            captured["codes"] = [tuple(a[1]) for a in args]
            out = []
            for a in args:
                if on_done:
                    on_done(0)
                out.append(([], [], [], [], [], []))
            return out

        monkeypatch.setattr(mod, "_run_chunk_tasks", fake_dispatch)
        seen = []
        _out = mod.run_full_backtest_tp_dirty(
            "postgresql://fake", log=lambda *_: None,
            progress=lambda d, t: seen.append((d, t)))
        # 缺省 250 只/片 → 两基金同一块；债基 000004 不进分片不分母
        assert captured["codes"] == [("000001", "000002")]
        assert seen[0] == (0, 2) and seen[-1] == (2, 2)
