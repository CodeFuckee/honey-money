"""脏检测日更测试（2026-11-24）：基金级输入指纹 + 引擎版本 + 按 code 合并导入。

- 引擎版本：engine_version 确定性 / 对 extra（设置页口径）与常量敏感；
- compute_dirty 纯函数五情形：首跑全量 / 引擎变全量 / 无变化全跳 / 净值
  聚合变（含 count/max 不变仅和值变的"历史修正"）/ 新基金与消失基金；
- 存储合并：merge_* 系列范围 DELETE（未变基金行保留）+ 间隔聚合限范围 +
  meta 显式计数 + alive 全表刷新 + 时机死行清理 + 分位回填；
- 编排器 run_full_backtest_tp_dirty 三级：周末 dirty=∅ 零加载快路径 /
  部分重算只喂脏基金 / 引擎变化自动全量。
"""

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

def _fp(nav_count=420, max_date="2024-02-24", s_unit="1000.5"):
    """指纹元组（同 load_input_fingerprints 输出形态，逐位可比）。"""
    return (nav_count, "2024-01-01", max_date, s_unit, "1000.5", "105.0",
            "成长先锋混合A", "混合型-偏股", False, "12.5", "2026-09-30")


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
