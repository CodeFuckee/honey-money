"""单策略全量回测（backtest_method，2026-11-25 九轮用户定案）测试。

口径定案（三问澄清）：设置页全部策略（DCA_METHODS 事实源，二十三轮起
八种）全可选；运行记录进任务系统（预置
手动任务「单策略全量回测」，source='backtest_method'，mode 列携带目标
策略，crawl_run 同 source 隔离日更快照判定）；日更运行中直接 409 拒绝。

写入口径：只算选定 method 的全市场 8 档止盈 × 3 费率；合并导入按 method
范围替换 fund_dca_tp_result/rounds（间隔列 method 域重算），**不触
fund_dca_tp_meta（归日更全量口径所有——局部运行不改全局新鲜度戳）**、
不写旧口径表/时机表/风险表/指纹表——引擎陈旧判定留给日更兜底全量
（正确性优先，接受一次重复计算）；alive 全表刷新（基金级口径与 method
无关）；对比快照独立事务整表重建（失败只告警，API 回落现算次日自愈）。
"""

import asyncio
import concurrent.futures
import datetime as dt
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.dca_store import _TP_COLS  # noqa: E402
from app.task_store import TaskStore  # noqa: E402


class _FakePool:
    """提交即执行的执行器替身（spawn 池进不了 monkeypatch 的闭包）。"""

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


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self.executed: list[str] = []
        self.execute_calls: list[tuple] = []
        self.executemany_calls: list[tuple[str, list]] = []
        self.fetchone_rows: list = []
        self.fetchall_rows: list = []
        self.rowcount = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        self.executed.append(query)
        self.execute_calls.append((query, params))

    def executemany(self, query, params_seq):
        self.executemany_calls.append((query, list(params_seq)))

    def fetchone(self):
        # 弹出式：多段 fetchone（净值新鲜度行 → start_run RETURNING → …）
        # 依序消费预置行
        return self.fetchone_rows.pop(0) if self.fetchone_rows else None

    def fetchall(self):
        return self.fetchall_rows


class FakeConn:
    def __init__(self):
        self.commits = 0
        self.cursor_obj = FakeCursor(self)

    def cursor(self):
        return self.cursor_obj

    def execute(self, query, params=None):
        # psycopg conn.execute 返回游标（daily job 的链式 .fetchone() 依赖）
        self.cursor_obj.execute(query, params)
        return self.cursor_obj

    def fetchone(self):
        return self.cursor_obj.fetchone()

    def commit(self):
        self.commits += 1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _tp_row(code="000001", tp=0.20, method="pl_ladder", alive=False,
            full_end="2026-11-21"):
    row = {c: None for c in _TP_COLS}
    row.update({"code": code, "tp": tp, "method": method, "alive": alive,
                "full_end": full_end, "name": "测试基金", "fund_type": "混合型"})
    return row


class TestSeedBacktestMethodTask:
    def test_seed_manual_task_idempotent(self):
        """预置手动任务「单策略全量回测」：source='backtest_method'、
        mode 列携带缺省目标策略 pl_ladder、无调度时刻、默认禁用（仅
        设置页/任务页手动触发）；播种幂等（按名 NOT EXISTS，内联字面量
        与其余手动任务同款）。"""
        conn = FakeConn()
        TaskStore().init_schema(conn)
        seeds = [c for c in conn.cursor_obj.executed
                 if "INSERT INTO crawl_task" in c]
        target = [s for s in seeds if "单策略全量回测" in s]
        assert len(target) == 1, seeds
        sql = target[0]
        assert "WHERE NOT EXISTS" in sql
        assert "'单策略全量回测'" in sql
        assert "'backtest_method'" in sql
        assert "'pl_ladder'" in sql
        assert "NULL::time" in sql
        assert "FALSE" in sql

    def test_source_name_registered(self):
        """source 中文名映射（任务页/自动命名显示「单策略回测」）。"""
        from app.task_store import SOURCE_NAMES
        assert SOURCE_NAMES.get("backtest_method") == "单策略回测"


class TestMergeTpMethod:
    """merge_tp_rows_method / merge_tp_rounds_method：method 域替换。"""

    def test_rows_replaced_by_method_without_meta(self):
        from app import dca_store
        conn = FakeConn()
        row = _tp_row()
        n = dca_store.merge_tp_rows_method(conn, [row], "pl_ladder")
        assert n == 1
        sqls = [s.strip() for s in conn.cursor_obj.executed]
        dele = [s for s in sqls
                if s.startswith("DELETE FROM fund_dca_tp_result")]
        assert dele and "method = %s" in dele[0]
        assert conn.cursor_obj.execute_calls[0][1] == ("pl_ladder",)
        # 不触 meta：局部运行不改全局新鲜度戳（fund_count/generated_at）
        assert not any("fund_dca_tp_meta" in s for s in sqls)
        ins = conn.cursor_obj.executemany_calls
        assert ins and "INSERT INTO fund_dca_tp_result" in ins[0][0]
        assert ins[0][1][0] == tuple(row[c] for c in _TP_COLS)

    def test_rows_empty_only_deletes(self):
        from app import dca_store
        conn = FakeConn()
        assert dca_store.merge_tp_rows_method(conn, [], "dd_ladder") == 0
        assert not conn.cursor_obj.executemany_calls
        assert any("DELETE FROM fund_dca_tp_result" in s
                   for s in conn.cursor_obj.executed)

    def test_rounds_replaced_and_gap_recomputed_by_method(self):
        from app import dca_store
        conn = FakeConn()
        rounds = [("000001", "pl_ladder", 0.20, "gross", 1,
                   "2024-03-01", 30, 10, 200.0, 1.1, 1000.0, 0.0, 0.0, 200.0)]
        n = dca_store.merge_tp_rounds_method(conn, rounds, "pl_ladder")
        assert n == 1
        sqls = [s.strip() for s in conn.cursor_obj.executed]
        dele = [s for s in sqls
                if s.startswith("DELETE FROM fund_dca_tp_rounds")]
        assert dele and "method = %s" in dele[0]
        ins = conn.cursor_obj.executemany_calls
        assert ins and "INSERT INTO fund_dca_tp_rounds" in ins[0][0]
        # 间隔列 method 域重算：RESET+APPLY 均带 method 参数
        resets = [(s, p) for s, p in conn.cursor_obj.execute_calls
                  if "gap_avg_days = NULL" in s]
        assert resets and "method = %s" in resets[0][0]
        assert resets[0][1] == ("pl_ladder",)
        applies = [(s, p) for s, p in conn.cursor_obj.execute_calls
                   if "gap_avg_days = t.g_avg" in s]
        assert applies and applies[0][1] == ("pl_ladder",)
        assert "r.method = t.method" in applies[0][0]


def _synthetic_nav():
    dates = pd.date_range("2024-01-01", periods=400, freq="D")
    rows = []
    for code, growth in (("000001", 0.002), ("000002", 0.0)):
        a = np.cumprod(1.0 + np.full(400, growth))
        for i, d in enumerate(dates):
            rows.append((code, d, round(float(a[i]), 6),
                         round(float(a[i]), 6), 0.25))
    return pd.DataFrame(rows, columns=["code", "nav_date", "unit_nav",
                                       "accum_nav", "pct_chg"])


def _basic_scale():
    basic = pd.DataFrame(
        {"name": {"000001": "成长先锋混合A", "000002": "稳如磐石混合A"},
         "fund_type": {"000001": "混合型-偏股", "000002": "混合型-偏股"},
         "delisted": {"000001": False, "000002": False}})
    scale_map = {"000001": {"scale_yi": 12.5, "scale_date": "2026-09-30"},
                 "000002": {"scale_yi": 3.2, "scale_date": "2026-09-30"}}
    return basic, scale_map


class _NullCur:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, *a, **k):
        return None

    def fetchone(self):
        return None


class _Ctx:
    """编排器假连接：cursor 全空应答（max(nav_date)→None、设置读取→回退）。"""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return _NullCur()


class TestChunkWorkerMethods:
    """_backtest_chunk_worker methods 参数：单策略运行只算选定方式。"""

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
        monkeypatch.setattr(mod.psycopg, "connect", lambda *a, **k: _Ctx())
        return nav

    def test_worker_single_method_only(self, monkeypatch):
        from app import dca_backtest as mod
        self._patch_loaders(monkeypatch)
        out = mod._backtest_chunk_worker("postgresql://fake",
                                         ("000001", "000002"), 90,
                                         None, None, None,
                                         methods=("pl_ladder",))
        tp_rows, tp_rounds, _t, _r = out
        assert tp_rows
        assert {r["method"] for r in tp_rows} == {"pl_ladder"}
        assert len(tp_rows) == 2 * len(mod.TP_THRESHOLDS)
        assert all(t[1] == "pl_ladder" for t in tp_rounds)

    def test_worker_default_all_methods(self, monkeypatch):
        from app import dca_backtest as mod
        self._patch_loaders(monkeypatch)
        out = mod._backtest_chunk_worker("postgresql://fake",
                                         ("000001",), 90, None, None, None)
        tp_rows, _tr, _t, _r = out
        assert {r["method"] for r in tp_rows} == set(mod.DCA_METHODS)


class TestRunFullBacktestTpMethod:
    """单策略全量编排器：全市场分片只算选定方式，alive 按自身窗口末重盖。"""

    def _patch_full(self, monkeypatch):
        from app import dca_backtest as mod
        nav = _synthetic_nav()
        basic, scale_map = _basic_scale()
        monkeypatch.setattr(mod, "_load_codes",
                            lambda conn: ["000001", "000002"])
        monkeypatch.setattr(mod, "_load_basic",
                            lambda conn, codes: basic.loc[list(codes)])
        monkeypatch.setattr(mod, "_load_scale", lambda conn: scale_map)
        monkeypatch.setattr(
            mod, "_load_nav",
            lambda conn, codes: nav[nav["code"].isin(list(codes))]
            .reset_index(drop=True))
        monkeypatch.setattr(mod.psycopg, "connect", lambda *a, **k: _Ctx())
        return nav

    def test_orchestrator_single_method_inline(self, monkeypatch):
        from app import dca_backtest as mod
        self._patch_full(monkeypatch)
        monkeypatch.setattr(mod, "_chunk_size", lambda: 250)
        monkeypatch.setattr(mod, "_worker_count", lambda: 1)   # 串行内联
        tp_rows, tp_rounds, stats = mod.run_full_backtest_tp_method(
            "postgresql://fake", "pl_ladder", log=lambda *_: None)
        assert tp_rows
        assert {r["method"] for r in tp_rows} == {"pl_ladder"}
        assert len(tp_rows) == 2 * len(mod.TP_THRESHOLDS)
        assert stats["method"] == "pl_ladder"
        assert stats["fund_count"] == 2
        assert stats["window_end"]
        # alive 按本次结果自身窗口末 + 45 天 cutoff 重盖（合成数据末日在
        # 窗口内 → 双基金活）
        assert all(r["alive"] is True for r in tp_rows)

    def test_dispatch_passes_methods_tuple(self, monkeypatch):
        """分片派发缝：args 携带 methods=("pl_ladder",)（worker 只算该方式）。"""
        from app import dca_backtest as mod
        self._patch_full(monkeypatch)
        seen = {}

        def fake_run_tasks(args_list, workers):
            seen["args"] = args_list
            seen["workers"] = workers
            rows = [_tp_row(code="000001", alive=False),
                    _tp_row(code="000002", alive=False)]
            return [(rows, [], [], [])]

        monkeypatch.setattr(mod, "_run_chunk_tasks", fake_run_tasks)
        monkeypatch.setattr(mod, "_chunk_size", lambda: 1)
        monkeypatch.setattr(mod, "_worker_count", lambda: 4)
        tp_rows, _rounds, _stats = mod.run_full_backtest_tp_method(
            "postgresql://fake", "pl_ladder", log=lambda *_: None)
        assert seen["args"]
        for a in seen["args"]:
            assert a[6] == ("pl_ladder",)     # methods 位
        # workers = min(_worker_count, 块数)：2 块（2 基金 × 1 只/块）
        assert seen["workers"] == 2
        # fake 行窗口末 2026-11-21 → cutoff 后重盖 alive=True
        assert all(r["alive"] is True for r in tp_rows)

    def test_orchestrator_progress_sequence(self, monkeypatch):
        """progress 参数（2026-11-25 十轮）：先报 (0, 总数)，每块完成报
        (累计基金数, 总数)——单策略全量的真实进度与日更同通道。"""
        from app import dca_backtest as mod
        self._patch_full(monkeypatch)
        monkeypatch.setattr(mod, "_chunk_size", lambda: 1)
        monkeypatch.setattr(mod, "_worker_count", lambda: 1)   # 串行内联
        seen = []
        mod.run_full_backtest_tp_method(
            "postgresql://fake", "pl_ladder", log=lambda *_: None,
            progress=lambda d, t: seen.append((d, t)))
        assert seen[0] == (0, 2)
        assert seen[-1] == (2, 2)
        assert [d for d, _t in seen] == sorted(d for d, _t in seen)


class TestBacktestMethodJob:
    """调度器 _run_backtest_method_job：合并序列守护与禁止触碰清单。"""

    def _run_job(self, monkeypatch, task, tp_rows=None, tp_rounds=None,
                 stats=None):
        from app import dca_backtest, dca_store, scheduler

        conn = FakeConn()
        # 预置 fetchone 队列：净值新鲜度行 → start_run RETURNING id
        conn.cursor_obj.fetchone_rows = [(dt.date(2026, 11, 21),), (1,)]
        monkeypatch.setattr(scheduler.psycopg, "connect",
                            lambda *a, **k: _RoutedJobConn(conn))
        monkeypatch.setattr(
            dca_backtest, "run_full_backtest_tp_method",
            lambda dsn, method, **k: (
                tp_rows if tp_rows is not None
                else [_tp_row("000001", alive=True)],
                tp_rounds if tp_rounds is not None else [],
                stats or {"method": task["mode"], "fund_count": 1,
                          "tp_round_count": 0, "thresholds": [0.2],
                          "window_end": "2026-11-21", "engine": "vTEST"}))
        compared = []
        monkeypatch.setattr(dca_store, "rebuild_tp_compare",
                            lambda conn, **k: compared.append(1) or 3)
        # job 经 spawn 进程池跑计算——monkeypatch 的 lambda 进不了池，
        # 伪造提交即执行的执行器（submit 内联解出结果）
        monkeypatch.setattr(concurrent.futures, "ProcessPoolExecutor",
                            _FakePool)
        progress_calls = []
        summary = asyncio.run(scheduler._run_backtest_method_job(
            "postgresql://fake", task,
            progress=lambda c, t: progress_calls.append((c, t)),
            stop_event=asyncio.Event()))
        return summary, conn, compared, progress_calls

    def test_merge_sequence_and_forbidden_targets(self, monkeypatch):
        summary, conn, compared, progress = self._run_job(
            monkeypatch, {"id": 9, "source": "backtest_method",
                          "mode": "pl_ladder", "_trigger": "manual"})
        # method 域替换 + alive method 域刷新
        joined = "\n".join(conn.cursor_obj.executed)
        assert "DELETE FROM fund_dca_tp_result" in joined
        assert "DELETE FROM fund_dca_tp_rounds" in joined
        assert "SET alive" in joined
        # 禁止触碰（DML 域，DDL 幂等建表不算触碰）：旧口径表/时机表/
        # 风险表/指纹表/meta 一律无数据写入
        dml = [s for s in conn.cursor_obj.executed
               if s.lstrip().startswith(("DELETE", "INSERT", "UPDATE"))]
        joined_dml = "\n".join(dml)
        assert "fund_dca_result" not in joined_dml
        assert "fund_dip_timing" not in joined_dml
        assert "fund_risk_metrics" not in joined_dml
        assert "fund_dca_fp" not in joined_dml
        assert "fund_dca_tp_meta" not in joined_dml
        for q, _p in conn.cursor_obj.executemany_calls:
            assert "fund_dca_result" not in q
        # 对比快照重建被调用（独立事务）；进度推已推进；摘要带 method
        assert compared == [1]
        assert progress == []    # job 不写占位进度（引擎独占生命周期）
        assert summary.get("method") == "pl_ladder"

    def test_invalid_method_aborts_run(self, monkeypatch):
        from app import scheduler

        conn = FakeConn()
        conn.cursor_obj.fetchone_rows = [(dt.date(2026, 11, 21),), (1,)]
        monkeypatch.setattr(scheduler.psycopg, "connect",
                            lambda *a, **k: _RoutedJobConn(conn))
        with pytest.raises(ValueError):
            asyncio.run(scheduler._run_backtest_method_job(
                "postgresql://fake",
                {"id": 9, "source": "backtest_method", "mode": "bogus",
                 "_trigger": "manual"},
                progress=lambda c, t: None,
                stop_event=asyncio.Event()))
        joined = "\n".join(conn.cursor_obj.executed)
        # 运行行收尾 aborted（不产半途脏数据）
        assert "aborted" in joined

    def test_dispatch_routes_backtest_method(self, monkeypatch):
        """build_crawl_executor 执行体按 source 分派到方法任务。"""
        from app import scheduler
        called = {}

        async def fake_job(dsn, task, progress, stop_event):
            called["task"] = task
            return {"method": task["mode"]}

        monkeypatch.setattr(scheduler, "_run_backtest_method_job", fake_job)
        execute = scheduler.build_crawl_executor("postgresql://fake", "")
        summary = asyncio.run(execute(
            {"id": 9, "source": "backtest_method", "mode": "pl_ladder"},
            progress=lambda c, t: None, stop_event=asyncio.Event()))
        assert called["task"]["mode"] == "pl_ladder"
        assert summary == {"method": "pl_ladder"}


class _RoutedJobConn:
    """调度器 job 假连接：多段 psycopg.connect 共享同一 FakeCursor。"""

    def __init__(self, fake: FakeConn):
        self.fake = fake

    def __enter__(self):
        return self.fake

    def __exit__(self, *exc):
        return False


class TestJobProgressWiring:
    """两个回测 job 的基金级进度接线（2026-11-25 十轮）：引擎回调经
    functools.partial(task_runtime.write_progress, dsn, task_id) 跨 spawn
    池落库 task_runtime.progress；job 层不再写 (0,1)/(1,1) 占位——引擎
    独占进度生命周期（末块写满格，merge 段保持 100% 至 finish 清除）。"""

    def test_method_job_passes_picklable_progress(self, monkeypatch):
        from app import dca_backtest, dca_store, scheduler, task_runtime

        conn = FakeConn()
        # fetchone 队列：start_run RETURNING id → finish_run RETURNING id
        # （方法 job 无净值新鲜度预检）
        conn.cursor_obj.fetchone_rows = [(1,), (1,)]
        rt_conn = FakeConn()
        # ⚠️ scheduler.psycopg 与 task_runtime.psycopg 是同一全局模块对象
        # ——补丁只能打一份，按调用序路由：第 1 次=job 主连接（start_run），
        # 其后=引擎 write_progress 的进度落库连接
        rt_conn.cursor_obj.fetchone_rows = [(1,), (1,)]
        conns = [conn, rt_conn]
        calls = {"n": 0}

        def fake_connect(*a, **kwargs):
            calls["n"] += 1
            return _RoutedJobConn(conns[0] if calls["n"] == 1 else rt_conn)

        monkeypatch.setattr(scheduler.psycopg, "connect", fake_connect)
        captured = {}

        def fake_engine(dsn, method, progress=None, **kwargs):
            captured["progress"] = progress
            return ([_tp_row("000001", alive=True)], [],
                    {"method": method, "fund_count": 1, "tp_round_count": 0,
                     "thresholds": [0.2], "window_end": "2026-11-21",
                     "engine": "vTEST"})

        monkeypatch.setattr(dca_backtest, "run_full_backtest_tp_method",
                            fake_engine)
        monkeypatch.setattr(dca_store, "rebuild_tp_compare",
                            lambda conn, **kwargs: 3)
        monkeypatch.setattr(concurrent.futures, "ProcessPoolExecutor",
                            _FakePool)
        asyncio.run(scheduler._run_backtest_method_job(
            "postgresql://fake",
            {"id": 9, "source": "backtest_method", "mode": "pl_ladder",
             "_trigger": "manual"},
            progress=lambda c, t: None, stop_event=asyncio.Event()))
        cb = captured["progress"]
        assert callable(cb)
        cb(7, 26)     # 跨进程边界后的回调：真写 task_runtime.progress
        sqls = [s for s in rt_conn.cursor_obj.executed
                if "UPDATE task_runtime SET progress" in s]
        assert sqls, rt_conn.cursor_obj.executed
        done_calls = [(s, p) for s, p in rt_conn.cursor_obj.execute_calls
                      if "SET progress" in s]
        assert done_calls[0][1][1] == 9     # task_id 位（partial 绑定）

    def test_dirty_job_passes_picklable_progress(self, monkeypatch):
        from app import dca_backtest, dca_store, scheduler, task_runtime
        from app.dca_backtest import engine_version

        conn = FakeConn()
        # fetchone 队列：净值预检 → 退让检查 → start_run RETURNING
        conn.cursor_obj.fetchone_rows = [(dt.date(2026, 11, 21),),
                                         (None, 0), (7,)]
        rt_conn = FakeConn()
        # 同上：scheduler/task_runtime 共享全局 psycopg 模块对象，按调用序
        # 路由（第 1 次=job 主连接；其后=min_span 设置读 + 合并事务 +
        # 进度落库全落 rt_conn）
        rt_conn.cursor_obj.fetchone_rows = [(90,)]
        calls = {"n": 0}

        def fake_connect(*a, **kwargs):
            calls["n"] += 1
            return _RoutedJobConn(conn if calls["n"] == 1 else rt_conn)

        monkeypatch.setattr(scheduler.psycopg, "connect", fake_connect)
        captured = {}
        fps = {"000001": ("fp",)}
        # 指纹前置（二十轮）：job 池内聚合一次；prev 引擎匹配 → 脏快路径
        monkeypatch.setattr(scheduler, "_fps_worker",
                            lambda dsn: (fps, fps, engine_version(90)))

        def fake_engine(dsn, progress=None, cur_fps=None):
            captured["progress"] = progress
            captured["cur_fps"] = cur_fps
            return ([], [], [], [],
                    {"fund_count": 2, "tp_round_count": 0,
                     "timing_count": 0, "risk_count": 0, "thresholds": [],
                     "methods": [], "window_end": "2026-11-21",
                     "engine": "vTEST", "dirty": ["000001"], "gone": []},
                    fps)

        monkeypatch.setattr(dca_backtest, "run_full_backtest_tp_dirty",
                            fake_engine)
        monkeypatch.setattr(dca_store, "rebuild_tp_compare",
                            lambda conn, **kwargs: 3)
        monkeypatch.setattr(concurrent.futures, "ProcessPoolExecutor",
                            _FakePool)
        monkeypatch.delenv("PAGES_REPO_URL", raising=False)
        asyncio.run(scheduler._run_backtest_job(
            "postgresql://fake",
            {"id": 7, "source": "backtest", "mode": "daily",
             "_trigger": "manual"},
            progress=lambda c, t: None, stop_event=asyncio.Event()))
        cb = captured["progress"]
        assert callable(cb)
        assert captured["cur_fps"] is fps   # job 预聚合指纹传入复用（不重算）
        cb(5, 26548)
        assert any("UPDATE task_runtime SET progress" in s
                   for s in rt_conn.cursor_obj.executed)
        done_calls = [(s, p) for s, p in rt_conn.cursor_obj.execute_calls
                      if "SET progress" in s]
        assert done_calls[0][1][1] == 7     # task_id 位（partial 绑定）


def _fp(fund_type="混合型-偏股", min_date="2023-01-01",
        max_date="2025-06-01"):
    """指纹元组（同 load_input_fingerprints 输出形态；窗口 ~517 天在宇宙内）。"""
    return (420, min_date, max_date, "1000.5", "1000.5", "105.0",
            "测试基金", fund_type, False, "12.5", "2026-09-30")


class TestMethodUniverseFilter:
    """单策略编排的宇宙过滤（2026-11-25 十五轮）：进度分母=权益宇宙+指纹
    未知的新基金——被排除基金（指纹在且非权益/年轻）既不进分片也不计数；
    fp 表缺失/读取失败回落全量分母（保守方向，worker 层规则仍兜底）。"""

    def test_fp_table_filters_codes_and_denominator(self, monkeypatch):
        import pandas as pd
        from app import dca_backtest as mod, dca_store

        nav = _synthetic_nav()
        basic, scale_map = _basic_scale()
        # codes 含 000009（fp 表未知的新基金——保留，worker 规则兜底）
        monkeypatch.setattr(mod, "_load_codes",
                            lambda conn: ["000001", "000002", "000009"])
        monkeypatch.setattr(mod, "_load_basic",
                            lambda conn, codes: basic.loc[list(codes)])
        monkeypatch.setattr(mod, "_load_scale", lambda conn: scale_map)
        monkeypatch.setattr(
            mod, "_load_nav",
            lambda conn, codes: nav[nav["code"].isin(list(codes))]
            .reset_index(drop=True))
        monkeypatch.setattr(mod.psycopg, "connect", lambda *a, **k: _Ctx())
        fps = {"000001": _fp(),                      # 权益老 → 宇宙
               "000004": _fp(fund_type="债券型-长债")}  # 非权益 → 排除
        # 注意：000002 不在 fp 表（新基金）→ 保留
        monkeypatch.setattr(dca_store, "load_fingerprints",
                            lambda conn: (fps, "vTEST"))
        monkeypatch.setattr(mod, "_chunk_size", lambda: 250)
        monkeypatch.setattr(mod, "_worker_count", lambda: 1)
        seen = {}
        rows_by_code = {"000001": [_tp_row("000001", alive=True)],
                        "000002": [_tp_row("000002", alive=True)],
                        "000009": [_tp_row("000009", alive=True)]}

        def fake_dispatch(args, workers, on_done=None):
            seen["codes"] = [c for a in args for c in tuple(a[1])]
            out = []
            for a in args:
                for c in tuple(a[1]):
                    on_done(0)
                    out.append((rows_by_code.get(c, []), [], [], []))
            return out

        monkeypatch.setattr(mod, "_run_chunk_tasks", fake_dispatch)
        tp_rows, _rounds, stats = mod.run_full_backtest_tp_method(
            "postgresql://fake", "pl_ladder", log=lambda *_: None,
            progress=lambda d, t: seen.setdefault("prog", []).append((d, t)))
        assert set(seen["codes"]) == {"000001", "000002", "000009"}
        assert seen["prog"][0] == (0, 3)
        assert stats["fund_count"] == 3

    def test_fp_empty_falls_back_to_direct_sql_universe(self, monkeypatch):
        """fp 表空（首跑/全量被中断未落指纹）→ 直接查库判定宇宙（权益
        白名单 + 全史窗口 ≥ 下限，与 worker 规则同语义）：分母只含宇宙
        基金（十五轮反馈：26831 全市场分母不符合「需要回测的基金数」）。"""
        from app import dca_backtest as mod, dca_store

        nav = _synthetic_nav()
        basic, scale_map = _basic_scale()
        monkeypatch.setattr(mod, "_load_codes",
                            lambda conn: ["000001", "000002"])
        monkeypatch.setattr(mod, "_load_basic",
                            lambda conn, codes: basic.loc[list(codes)])
        monkeypatch.setattr(mod, "_load_scale", lambda conn: scale_map)
        monkeypatch.setattr(
            mod, "_load_nav",
            lambda conn, codes: nav[nav["code"].isin(list(codes))]
            .reset_index(drop=True))
        monkeypatch.setattr(mod.psycopg, "connect", lambda *a, **k: _Ctx())
        monkeypatch.setattr(dca_store, "load_fingerprints",
                            lambda conn: ({}, "vTEST"))   # fp 表空
        # 直接查库宇宙：假游标 fetchall 回 000001（000002 由 SQL 语义排除）
        seen_sql = []

        class _SqlCur(_NullCur):
            def execute(self, q, p=None):
                seen_sql.append(q)
            def fetchall(self):
                return [("000001",)]

        class _SqlCtx(_Ctx):
            def cursor(self):
                return _SqlCur()

        monkeypatch.setattr(mod.psycopg, "connect",
                            lambda *a, **k: _SqlCtx())
        monkeypatch.setattr(mod, "_chunk_size", lambda: 250)
        monkeypatch.setattr(mod, "_worker_count", lambda: 1)
        seen = []
        mod.run_full_backtest_tp_method(
            "postgresql://fake", "pl_ladder", log=lambda *_: None,
            progress=lambda d, t: seen.append((d, t)))
        assert seen[0] == (0, 1) and seen[-1] == (1, 1)
        assert any("GROUP BY n.code" in q for q in seen_sql), seen_sql

    def test_direct_sql_type_safe_shape(self, monkeypatch):
        """直查 SQL 类型锚（二十一轮真凶回归防）：fund_nav.nav_date 是
        DATE 列，max-min 得**整数天**——必须 ::date 显式 cast 后与整数
        参数比较。旧写法 `max-min >= make_interval(days=>%s)` 是
        integer >= interval，PG 无此操作符 → 真实库必抛 UndefinedFunction
        → 被 except 吞掉回落全市场分母 26832（用户三次反馈的假修根因：
        mock 假游标不执行 SQL 抓不住类型错误，只能锚 SQL 文本 + 真库
        冒烟双保险）。"""
        from app import dca_backtest as mod, dca_store
        nav = _synthetic_nav()
        basic, scale_map = _basic_scale()
        monkeypatch.setattr(mod, "_load_codes",
                            lambda conn: ["000001", "000002"])
        monkeypatch.setattr(mod, "_load_basic",
                            lambda conn, codes: basic.loc[list(codes)])
        monkeypatch.setattr(mod, "_load_scale", lambda conn: scale_map)
        monkeypatch.setattr(
            mod, "_load_nav",
            lambda conn, codes: nav[nav["code"].isin(list(codes))]
            .reset_index(drop=True))
        monkeypatch.setattr(dca_store, "load_fingerprints",
                            lambda conn: ({}, "vTEST"))   # fp 表空 → 直查分支

        seen = []

        class _SqlCur(_NullCur):
            def execute(self, q, p=None):
                seen.append((q, p))
            def fetchall(self):
                return [("000001",)]

        class _SqlCtx(_Ctx):
            def cursor(self):
                return _SqlCur()

        monkeypatch.setattr(mod.psycopg, "connect",
                            lambda *a, **k: _SqlCtx())
        monkeypatch.setattr(mod, "_chunk_size", lambda: 250)
        monkeypatch.setattr(mod, "_worker_count", lambda: 1)
        mod.run_full_backtest_tp_method(
            "postgresql://fake", "pl_ladder", log=lambda *_: None)
        direct = [(q, p) for q, p in seen if "GROUP BY n.code" in q]
        assert direct, seen
        sql, params = direct[0]
        assert "(max(n.nav_date)::date - min(n.nav_date)::date) >= %s" in sql
        assert "make_interval" not in sql              # 旧写法禁止回潮
        assert params[0] == list(mod.BACKTEST_UNIVERSE_TYPES)
        assert params[1] == 90                          # 缺省运作期限下限

    def test_direct_sql_failure_falls_back_to_all_codes(self, monkeypatch):
        """直接查库也失败（库异常）→ 回落全 codes（不因口径丢基金，
        worker 层规则兜底行级排除）。"""
        from app import dca_backtest as mod, dca_store

        nav = _synthetic_nav()
        basic, scale_map = _basic_scale()
        monkeypatch.setattr(mod, "_load_codes",
                            lambda conn: ["000001", "000002"])
        monkeypatch.setattr(mod, "_load_basic",
                            lambda conn, codes: basic.loc[list(codes)])
        monkeypatch.setattr(mod, "_load_scale", lambda conn: scale_map)
        monkeypatch.setattr(
            mod, "_load_nav",
            lambda conn, codes: nav[nav["code"].isin(list(codes))]
            .reset_index(drop=True))

        class _BoomCur:
            def __enter__(self):
                return self
            def __exit__(self, *e):
                return False
            def execute(self, q, p=None):
                if "GROUP BY n.code" in q:      # 仅宇宙直查失败
                    raise RuntimeError("db down")

            def fetchall(self):
                return []
            def fetchone(self):
                return None

        class _BoomCtx:
            def __enter__(self):
                return self
            def __exit__(self, *e):
                return False
            def cursor(self):
                return _BoomCur()

        monkeypatch.setattr(mod.psycopg, "connect", lambda *a, **k: _BoomCtx())
        monkeypatch.setattr(dca_store, "load_fingerprints",
                            lambda conn: ({}, "vTEST"))
        monkeypatch.setattr(mod, "_chunk_size", lambda: 250)
        monkeypatch.setattr(mod, "_worker_count", lambda: 1)
        seen = []
        mod.run_full_backtest_tp_method(
            "postgresql://fake", "pl_ladder", log=lambda *_: None,
            progress=lambda d, t: seen.append((d, t)))
        assert seen[0] == (0, 2) and seen[-1] == (2, 2)


class TestMethodMetaStore:
    """方法级新鲜度表（2026-11-26 二十轮，日更逐方法分阶段根治项）：
    fund_dca_method_meta 记录每方法的 (engine, window_end) 戳——中断续跑
    的跳过判定事实源；DDL 幂等、load 空、mark upsert、touch_tp_meta
    （收尾事务补全局 meta 戳——merge_tp_rows_method 刻意不触 meta）。"""

    def test_ddl_idempotent(self):
        from app import dca_store
        conn = FakeConn()
        dca_store.ensure_method_meta_schema(conn)
        dca_store.ensure_method_meta_schema(conn)
        ddl = [c for c in conn.cursor_obj.executed
               if "CREATE TABLE IF NOT EXISTS fund_dca_method_meta" in c]
        assert len(ddl) == 2
        assert "VARCHAR(16) PRIMARY KEY" in ddl[0]
        assert "TEXT NOT NULL" in ddl[0]
        assert "DATE" in ddl[0] and "window_end" in ddl[0]

    def test_load_empty_and_mark_roundtrip(self):
        from app import dca_store
        conn = FakeConn()
        dca_store.ensure_method_meta_schema(conn)
        assert dca_store.load_method_meta(conn) == {}
        dca_store.mark_method_fresh(conn, "pl_ladder", "vTEST", "2026-11-25")
        upserts = [(s, p) for s, p in conn.cursor_obj.execute_calls
                   if "INSERT INTO fund_dca_method_meta" in s]
        assert upserts and "ON CONFLICT (method) DO UPDATE" in upserts[0][0]
        assert upserts[0][1][:3] == ("pl_ladder", "vTEST", "2026-11-25")

    def test_touch_tp_meta(self):
        from app import dca_store
        conn = FakeConn()
        dca_store.touch_tp_meta(conn, 13441, {"engine": "vTEST"})
        ups = [(s, p) for s, p in conn.cursor_obj.execute_calls
               if "INSERT INTO fund_dca_tp_meta" in s]
        assert ups and ups[0][1][0] == 13441


class TestSideTablesFlag:
    """run_full_backtest_tp_method 的 with_side_tables / cutoff_iso
    （2026-11-26 二十轮）：日更分阶段全量的 amount 首班차搭车产旧口径/
    时机/风险行（worker 本来就算、原实现丢弃）；cutoff_iso 下传 worker
    让 alive 门控真实生效（否则时机行恒空）+ 父进程重盖基准同源。缺省
    调用 stats 无 side 键——单策略按钮 job 兼容零改动。"""

    def _patch(self, monkeypatch):
        from app import dca_backtest as mod, dca_store
        monkeypatch.setattr(mod, "_load_codes", lambda conn: ["000001"])
        monkeypatch.setattr(mod, "_chunk_size", lambda: 250)
        monkeypatch.setattr(mod, "_worker_count", lambda: 1)
        monkeypatch.setattr(dca_store, "load_fingerprints",
                            lambda conn: ({}, "vTEST"))
        monkeypatch.setattr(mod.psycopg, "connect", lambda *a, **k: _Ctx())
        return mod

    def test_default_has_no_side_key(self, monkeypatch):
        mod = self._patch(monkeypatch)
        seen = {}

        def fake_dispatch(args, workers, on_done=None):
            seen["cutoffs"] = [a[3] for a in args]
            seen["methods"] = [a[6] for a in args]
            if on_done:
                on_done(0)
            return [([_tp_row("000001", alive=True)], [], [], [])]

        monkeypatch.setattr(mod, "_run_chunk_tasks", fake_dispatch)
        _rows, _rounds, stats = mod.run_full_backtest_tp_method(
            "postgresql://fake", "pl_ladder", log=lambda *_: None)
        assert "side" not in stats
        assert seen["cutoffs"] == [None]      # 缺省 cutoff_iso=None（原行为）
        assert seen["methods"] == [("pl_ladder",)]

    def test_side_tables_and_cutoff_plumbed(self, monkeypatch):
        mod = self._patch(monkeypatch)
        timing = ("000001", -0.22, 12, "20-25", "2026-11-21")
        risk = ("000001", 0.18, 0.12, -0.35, 0.9)

        def fake_dispatch(args, workers, on_done=None):
            seen = fake_dispatch.seen
            seen["cutoffs"] = [a[3] for a in args]
            if on_done:
                on_done(0)
            return [([_tp_row("000001", alive=True)], [], [timing], [risk])]

        fake_dispatch.seen = {}
        monkeypatch.setattr(mod, "_run_chunk_tasks", fake_dispatch)
        _rows, _rounds, stats = mod.run_full_backtest_tp_method(
            "postgresql://fake", "amount", log=lambda *_: None,
            with_side_tables=True, cutoff_iso="2025-10-07")
        assert fake_dispatch.seen["cutoffs"] == ["2025-10-07"]
        side = stats["side"]
        assert side["timing_rows"] == [timing]
        assert side["risk_rows"] == [risk]

    def test_side_tables_only_kept_when_requested(self, monkeypatch):
        mod = self._patch(monkeypatch)

        def fake_dispatch(args, workers, on_done=None):
            if on_done:
                on_done(0)
            return [([_tp_row("000001")], [], [("000001", None, None,
                                                "none", None)],
                     [("000001", None, None, None, None)])]

        monkeypatch.setattr(mod, "_run_chunk_tasks", fake_dispatch)
        _rows, _rounds, stats = mod.run_full_backtest_tp_method(
            "postgresql://fake", "pl_ladder", log=lambda *_: None,
            with_side_tables=True)
        # 编排层只搬运 worker 产出；alive 重盖照常应用
        assert "side" in stats
        assert set(stats["side"]) == {"timing_rows", "risk_rows"}


class TestDirtyCurFpsParam:
    """run_full_backtest_tp_dirty 的 cur_fps 可选参数（2026-11-26 二十轮）：
    日更 job 分支判定已先算全市场指纹（约 10 分钟 GROUP BY），传入复用
    避免同夜重复聚合；缺省 None 保持引擎内算（既有测试/脚本零改动）。"""

    def test_cur_fps_reused_and_returned(self, monkeypatch):
        from app import dca_backtest as mod, dca_store

        fps = {"000001": _fp(), "000002": _fp()}
        monkeypatch.setattr(mod, "load_input_fingerprints",
                            lambda conn: (_ for _ in ()).throw(
                                AssertionError("不应重复聚合指纹")))
        monkeypatch.setattr(dca_store, "load_fingerprints",
                            lambda conn: (None, None))
        monkeypatch.setattr(mod.settings_store, "get_min_span_days",
                            lambda conn, default=90: default)
        monkeypatch.setattr(mod.psycopg, "connect", lambda *a, **k: _Ctx())
        monkeypatch.setattr(mod, "_chunk_codes", lambda codes, size: [])
        rows = mod.run_full_backtest_tp_dirty(
            "postgresql://fake", log=lambda *_: None, cur_fps=fps)
        stats = rows[4]
        assert stats["full_run"] is True          # prev None → 全量
        assert stats["dirty"] == ["000001", "000002"]
        assert rows[5] is fps                     # 指纹原样返回（收尾落库用）


# ===== 日更逐方法分阶段（2026-11-26 二十轮根治项）测试共用锚 =====
_ENG = None     # 延迟初始化（engine_version(90)，测试模块级一次）


def _eng() -> str:
    global _ENG
    if _ENG is None:
        from app.dca_backtest import engine_version
        _ENG = engine_version(90)
    return _ENG


_WE = "2025-06-01"      # _fp() 指纹的最大净值日（nav_count≥2 过滤后的窗口末）


class TestStagedDailyJob:
    """日更全量路径分阶段改造（2026-11-26 二十轮）：fp 空/引擎不匹配时
    逐方法「算完即合并即盖戳」，中断只丢未完成方法、次晚续跑；全部方法
    新鲜后一次性落指纹回归脏检测快路径——破除「全量被中断→指纹永不落地
    →每晚全量→再被中断」的恶性循环（crawl_run 147~165 六连 aborted 实证）。
    日更退让：单策略回测排队/运行中 → 本轮跳过并落 skipped 运行行（日历
    可见），绝不与单方法 4-worker 池并发叠加内存。"""

    def _drive(self, monkeypatch, *, meta_rows=None, fps=None, prev=(None, None),
               stop_setter=None, force=False):
        """驱动 _run_backtest_job 走分阶段全量路径，返回记录器集合。
        fetchone 队列序：净值最新日 → 退让检查 → start_run → min_span。"""
        from app import dca_backtest, dca_store, scheduler

        fps = fps or {"000001": _fp()}
        conn = FakeConn()
        conn.cursor_obj.fetchone_rows = [
            (dt.date(2026, 11, 25),),   # max(nav_date) 新鲜度预检
            (None, 0),                  # 退让检查：(phase, 敞开运行行数)
            (7,),                       # start_run RETURNING id
            (90,),                      # get_min_span_days
        ]
        conn.cursor_obj.fetchall_rows = list(meta_rows or [])
        monkeypatch.setattr(scheduler.psycopg, "connect",
                            lambda *a, **k: _RoutedJobConn(conn))
        monkeypatch.setattr(scheduler, "_fps_worker",
                            lambda dsn: (fps, prev[0], prev[1]))
        called = {"methods": [], "side_flags": [], "cutoffs": [], "prog": []}

        def fake_offset(dsn, task_id, base, total, done, n):
            called["prog"].append((base + done, total))
        monkeypatch.setattr(scheduler, "_write_progress_offset", fake_offset)

        def fake_method(dsn, method, *, log=None, progress=None,
                        with_side_tables=False, cutoff_iso=None, **kw):
            called["methods"].append(method)
            called["side_flags"].append(with_side_tables)
            called["cutoffs"].append(cutoff_iso)
            if progress:
                progress(0, 1)
                progress(1, 1)
            if stop_setter is not None and len(called["methods"]) \
                    >= stop_setter:
                stop_event.set()
            stats = {"method": method, "fund_count": 1, "tp_round_count": 3,
                     "window_end": _WE, "engine": _eng(), "workers": 1,
                     "chunks": 1, "thresholds": [0.2]}
            if with_side_tables:
                stats["side"] = {
                    "timing_rows": [("000001", -0.22, 12, "20-25", _WE)],
                    "risk_rows": [("000001", 0.18, 0.12, -0.35, 0.9)]}
            return ([_tp_row("000001", method=method, alive=True)],
                    [("000001", method, 0.2, "gross", 1, *([0] * 9))], stats)

        monkeypatch.setattr(dca_backtest, "run_full_backtest_tp_method",
                            fake_method)
        side_calls = {"merge_timing": [], "merge_risk": []}

        def _rec(key):
            def _f(conn, *a, **k):
                side_calls[key].append((a, k))
                return 0
            return _f

        monkeypatch.setattr(dca_store, "merge_timing", _rec("merge_timing"))
        monkeypatch.setattr(dca_store, "merge_risk", _rec("merge_risk"))
        monkeypatch.setattr(dca_store, "rebuild_tp_compare",
                            lambda conn, **kwargs: 3)
        monkeypatch.setattr(concurrent.futures, "ProcessPoolExecutor",
                            _FakePool)
        monkeypatch.delenv("PAGES_REPO_URL", raising=False)
        if force:
            monkeypatch.setenv("BACKTEST_FORCE_FULL", "1")
        stop_event = asyncio.Event()
        summary = asyncio.run(scheduler._run_backtest_job(
            "postgresql://fake",
            {"id": 7, "source": "backtest", "mode": "daily",
             "_trigger": "manual"},
            progress=lambda c, t: called["prog"].append((c, t)),
            stop_event=stop_event))
        return {"summary": summary, "conn": conn, "called": called,
                "side": side_calls, "stop_event": stop_event}

    def test_staged_full_merges_per_method_and_marks_fresh(self,
                                                           monkeypatch):
        r = self._drive(monkeypatch)
        from app.dca_backtest import DCA_METHODS
        assert r["called"]["methods"] == list(DCA_METHODS)
        assert r["called"]["side_flags"] == [m == "amount"
                                             for m in DCA_METHODS]
        # cutoff 同源下传（fp 派生窗口末 - 45 天）
        assert all(c == "2025-04-17" for c in r["called"]["cutoffs"])
        # 每方法合并：method 域 DELETE/UPDATE 落行 + 盖新鲜度戳
        merges = [(s, p) for s, p in r["conn"].cursor_obj.execute_calls
                  if "DELETE FROM fund_dca_tp_result WHERE method = %s" in s]
        assert [m[1][0] for m in merges] == list(DCA_METHODS)
        marks = [(s, p) for s, p in r["conn"].cursor_obj.execute_calls
                 if "INSERT INTO fund_dca_method_meta" in s]
        assert [m[1][0] for m in marks] == list(DCA_METHODS)
        assert all(m[1][1] == _eng() and m[1][2] == _WE for m in marks)
        # amount 搭车：时机/风险同事务合并（各一次）
        assert len(r["side"]["merge_timing"]) == 1
        # 收尾：指纹落库 + 全局 alive 兜底 + meta 戳
        assert any("DELETE FROM fund_dca_fp" in s
                   for s in r["conn"].cursor_obj.executed)
        assert any("UPDATE fund_dca_tp_result SET alive" in s
                   for s in r["conn"].cursor_obj.executed)
        assert any("INSERT INTO fund_dca_tp_meta" in s
                   for s in r["conn"].cursor_obj.executed)
        assert r["summary"]["aborted"] is False
        assert r["summary"]["ok"] == 1

    def test_side_tables_merged_with_amount_not_per_method(self,
                                                           monkeypatch):
        """时机/风险只在 amount 首班次合并一次（其余方法班次零 side 调用）
        ——新鲜度与 amount 戳绑定，中断不留半套。"""
        r = self._drive(monkeypatch)
        assert len(r["side"]["merge_timing"]) == 1
        assert len(r["side"]["merge_risk"]) == 1

    def test_resume_skips_fresh_methods(self, monkeypatch):
        """续跑：amount/share 已新鲜（engine+window_end 匹配）→ 跳过重算、
        不搭车 side tables，从第三个方法接着算；进度分母=待算方法×宇宙。"""
        from app.dca_backtest import DCA_METHODS
        r = self._drive(monkeypatch,
                        meta_rows=[("amount", _eng(), _WE),
                                   ("share", _eng(), _WE)])
        assert r["called"]["methods"] == list(DCA_METHODS)[2:]
        assert "amount" not in r["called"]["methods"]
        assert len(r["side"]["merge_timing"]) == 0   # amount 班次没跑
        # 进度分母 = 6 待算方法（二十三轮起八方式）× 1 宇宙基金
        assert r["called"]["prog"][-1] == (6, 6)
        assert r["summary"]["aborted"] is False

    def test_engine_change_recomputes_all(self, monkeypatch):
        """引擎版本变化 → 新鲜度戳全部失配 → 七方法全重算（十四轮排除
        旧行清理机制在新编排下继续成立）。"""
        from app.dca_backtest import DCA_METHODS
        r = self._drive(monkeypatch,
                        meta_rows=[(m, "vOLD", _WE) for m in DCA_METHODS])
        assert r["called"]["methods"] == list(DCA_METHODS)

    def test_window_end_mismatch_recomputes(self, monkeypatch):
        """同引擎但窗口推进（新净值日）→ 戳失配重算。"""
        from app.dca_backtest import DCA_METHODS
        r = self._drive(monkeypatch,
                        meta_rows=[(m, _eng(), "2025-05-31")
                                   for m in DCA_METHODS])
        assert r["called"]["methods"] == list(DCA_METHODS)

    def test_stop_between_methods_keeps_merged_prefix(self, monkeypatch):
        """方法间协作式停止：完成当前方法合并后停——已合并方法+新鲜度戳
        保留，指纹不落（下次仍判全量走续跑），run 收尾 aborted。"""
        r = self._drive(monkeypatch, stop_setter=1)
        from app.dca_backtest import DCA_METHODS
        assert r["called"]["methods"] == [DCA_METHODS[0]]
        marks = [p for s, p in r["conn"].cursor_obj.execute_calls
                 if "INSERT INTO fund_dca_method_meta" in s]
        assert [p[0] for p in marks] == [DCA_METHODS[0]]
        assert not any("DELETE FROM fund_dca_fp" in s
                       for s in r["conn"].cursor_obj.executed)
        assert r["summary"]["aborted"] is True

    def test_force_full_ignores_freshness(self, monkeypatch):
        """BACKTEST_FORCE_FULL=1 → 新鲜方法也强制重算（逃生口语义不变）。"""
        from app.dca_backtest import DCA_METHODS
        r = self._drive(monkeypatch,
                        meta_rows=[(m, _eng(), _WE) for m in DCA_METHODS],
                        force=True)
        assert r["called"]["methods"] == list(DCA_METHODS)

    def test_yield_to_method_run_records_skipped(self, monkeypatch):
        """日更退让：单策略回测运行中（task_runtime phase=running）→ 本轮
        不算任何东西，落 skipped 运行行（aborted=False——计划性让路非失败），
        日历可见。"""
        from app import dca_backtest, scheduler
        conn = FakeConn()
        conn.cursor_obj.fetchone_rows = [
            (dt.date(2026, 11, 25),),   # 净值预检
            ("running", 1),             # 退让检查命中
            (7,),                       # start_run
        ]
        monkeypatch.setattr(scheduler.psycopg, "connect",
                            lambda *a, **k: _RoutedJobConn(conn))
        called = {"n": 0}

        def _boom(*a, **k):
            called["n"] += 1
            return ([], [], {}, )
        monkeypatch.setattr(dca_backtest, "run_full_backtest_tp_method",
                            _boom)
        monkeypatch.setattr(concurrent.futures, "ProcessPoolExecutor",
                            _FakePool)
        summary = asyncio.run(scheduler._run_backtest_job(
            "postgresql://fake",
            {"id": 7, "source": "backtest", "mode": "daily",
             "_trigger": "schedule"},
            progress=lambda c, t: None, stop_event=asyncio.Event()))
        assert called["n"] == 0
        assert summary.get("skipped") is True
        assert summary["aborted"] is False
        # skipped 运行行：detail 携带 skipped 键（日历按此归类）
        ups = [(s, p) for s, p in conn.cursor_obj.execute_calls
               if "UPDATE crawl_run SET" in s]
        assert ups and '"skipped"' in str(ups[0][1][5])
        assert ups[0][1][4] is False            # aborted=False
