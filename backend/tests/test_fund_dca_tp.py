"""多止盈点回测测试（市场级阈值对比：5%~30% 六档）。

新数据通道与旧表完全隔离（grilling 定案 2026-11-15）：
- 引擎：simulate_multi 单遍多阈值（每档独立状态机），单档结果与
  simulate(take_profit=...) 逐字段一致；run_backtest_tp 产出带 tp 维度的
  结果行/轮次行，tp=0.20 行与旧 run_backtest 输出完全一致（旧表由它派生）；
- 存储：fund_dca_tp_result / fund_dca_tp_rounds / fund_dca_tp_meta 三新表，
  写入路径 DELETE+INSERT 幂等，间隔聚合按 (code, tp) 分组；
- API：/api/funds/dca/tp（排行切换）、/api/funds/dca/tp/compare（市场级
  对比，权益类+商品池）、/api/funds/dca/tp/{code}/rounds（阈值轮次明细）；
  降级约定同旧端点（始终 200，白名单外回落）。
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from app.main import create_app  # noqa: E402


@pytest.fixture
def client():
    return TestClient(create_app())


DEAD_DSN = "postgresql://nobody:x@127.0.0.1:59999/none"


# ===== 引擎：阈值参数与单遍多阈值 =====

class TestSimulateThreshold:
    """simulate 的 take_profit 参数：缺省保留旧口径，显式值改变触发线。"""

    def test_default_is_legacy_20(self):
        from app import dca_backtest as mod
        assert mod.TAKE_PROFIT == pytest.approx(0.20)

    def test_lower_threshold_triggers_sooner(self):
        """+10% 涨幅日：tp=0.05 触发止盈，tp=0.20 不触发。"""
        from app import dca_backtest as mod
        dates = pd.DatetimeIndex(pd.date_range("2024-01-01", periods=30,
                                               freq="D"))
        g = np.zeros(30)
        g[14] = 0.10
        adj = np.cumprod(1.0 + g)
        lo = mod.simulate(dates, adj, take_profit=0.05)
        hi = mod.simulate(dates, adj, take_profit=0.20)
        assert lo["rounds"] == 1
        assert hi["rounds"] == 0
        assert lo["round_events"][0][3] == pytest.approx(0.10)  # 市值口径收益

    def test_gap_between_threshold_jump(self):
        """单日跳空越过阈值：profit 记实际跳越值（≥阈值），非阈值本身。"""
        from app import dca_backtest as mod
        dates = pd.DatetimeIndex(pd.date_range("2024-01-01", periods=30,
                                               freq="D"))
        g = np.zeros(30)
        g[14] = 0.33
        adj = np.cumprod(1.0 + g)
        res = mod.simulate(dates, adj, take_profit=0.25)
        assert res["rounds"] == 1
        assert res["round_events"][0][3] == pytest.approx(0.33)


class TestSimulateMulti:
    """simulate_multi 单遍多阈值：与逐阈值 simulate 逐字段一致。"""

    def _mixed_series(self):
        """混合行情：涨跌交替+跳空，足以触发多轮止盈。"""
        dates = pd.DatetimeIndex(pd.date_range("2024-01-01", periods=400,
                                               freq="D"))
        rng = np.random.default_rng(42)
        g = rng.normal(0.002, 0.015, 400)
        g[50] = 0.30   # 跳空
        g[150] = 0.28
        g[260] = 0.35
        g[100:120] = -0.02   # 一段回撤
        g[300:330] = -0.01
        return dates, np.cumprod(1.0 + g)

    @pytest.mark.parametrize("fee_name", ["gross", "schedule"])
    def test_multi_matches_simulate_per_threshold(self, fee_name):
        from app import dca_backtest as mod
        dates, adj = self._mixed_series()
        fee = None
        if fee_name == "schedule":
            fee = mod.FeeSchedule(0.015, mod.REDEEM_EQUITY)
        multi = mod.simulate_multi(dates, adj, fee, mod.TP_THRESHOLDS)
        assert set(multi) == set(mod.TP_THRESHOLDS)
        for tp in mod.TP_THRESHOLDS:
            single = mod.simulate(dates, adj, fee, take_profit=tp)
            m = multi[tp]
            assert m["rounds"] == single["rounds"], tp
            assert m["invested"] == pytest.approx(single["invested"]), tp
            assert m["profit_rate"] == pytest.approx(single["profit_rate"]), tp
            assert m["xirr"] == pytest.approx(single["xirr"], abs=1e-9), tp
            assert m["round_events"] == single["round_events"], tp
            assert m["flows"] == single["flows"], tp

    def test_rounds_monotonic_decreasing_with_threshold(self):
        """持续上行序列：阈值越低触发越频繁（轮次单调不增）。"""
        from app import dca_backtest as mod
        dates = pd.DatetimeIndex(pd.date_range("2024-01-01", periods=420,
                                               freq="D"))
        adj = np.cumprod(1.0 + np.full(420, 0.0025))
        multi = mod.simulate_multi(dates, adj, None, mod.TP_THRESHOLDS)
        counts = [multi[tp]["rounds"] for tp in mod.TP_THRESHOLDS]
        assert all(c > 0 for c in counts)
        assert counts == sorted(counts, reverse=True)
        assert counts[0] > counts[-1]      # 5% 明显多于 30%

    def test_single_threshold_equals_helper(self):
        """单阈值便捷形态：simulate_multi 只给一档 ≡ simulate。"""
        from app import dca_backtest as mod
        dates, adj = self._mixed_series()
        m = mod.simulate_multi(dates, adj, None, [0.20])[0.20]
        s = mod.simulate(dates, adj, take_profit=0.20)
        assert m == s


# ===== run_backtest_tp：结果行/轮次行 + tp=0.20 与旧引擎一致 =====

def _synthetic_inputs():
    """两只基金：A 持续上行（多轮止盈），B 平躺（从未止盈）。"""
    dates = pd.date_range("2024-01-01", periods=420, freq="D")
    navs = []
    a = np.cumprod(1.0 + np.full(420, 0.0025))
    for i, d in enumerate(dates):
        navs.append(("000001", d, round(float(a[i]), 6),
                     round(float(a[i]), 6), 0.25))
        navs.append(("000002", d, 1.0, 1.0, 0.0))
    nav = pd.DataFrame(navs, columns=["code", "nav_date", "unit_nav",
                                      "accum_nav", "pct_chg"])
    basic = pd.DataFrame(
        {"name": {"000001": "成长先锋混合A", "000002": "稳如磐石混合A"},
         "fund_type": {"000001": "混合型-偏股", "000002": "混合型-偏股"},
         "delisted": {"000001": False, "000002": False}})
    scale_map = {"000001": {"scale_yi": 12.5, "scale_date": "2026-09-30"},
                 "000002": {"scale_yi": 3.2, "scale_date": "2026-09-30"}}
    return nav, basic, scale_map


class TestRunBacktestTp:
    def test_row_per_code_per_threshold(self):
        from app import dca_backtest as mod
        nav, basic, scale_map = _synthetic_inputs()
        rows, rounds = mod.run_backtest_tp(nav, basic, scale_map)
        assert len(rows) == 2 * len(mod.TP_THRESHOLDS)
        tps = sorted(r["tp"] for r in rows if r["code"] == "000001")
        assert tps == sorted(mod.TP_THRESHOLDS)
        codes = {t[0] for t in rounds}
        assert codes == {"000001"}          # B 从未止盈 → 无轮次行

    def test_rounds_profit_respects_threshold(self):
        """轮次市值口径收益 ≥ 对应阈值（跳空越过如实记录）。"""
        from app import dca_backtest as mod
        nav, basic, scale_map = _synthetic_inputs()
        _, rounds = mod.run_backtest_tp(nav, basic, scale_map)
        by_tp = {}
        for _code, _method, tp, _fee, _seq, *_ev in rounds:
            by_tp.setdefault(tp, []).append(_ev)
        # (tp_date, round_days, gap_days, profit, ...) → profit 在 ev[3]
        for tp, evs in by_tp.items():
            for ev in evs:
                assert ev[3] >= tp - 1e-9

    def test_flat_fund_never_triggers(self):
        from app import dca_backtest as mod
        nav, basic, scale_map = _synthetic_inputs()
        rows, _ = mod.run_backtest_tp(nav, basic, scale_map)
        for r in rows:
            if r["code"] == "000002":
                assert r["w10_rounds"] == 0
                assert r["w10_xirr"] == pytest.approx(0.0)


# ===== run_full_backtest_tp：日更编排（修 use-before-def + 风险行产出） =====


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


class FakeConn:
    def __init__(self):
        self.cursor_obj = FakeCursor(self)

    def cursor(self):
        return self.cursor_obj

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _tp_row(code="000001", tp=0.15):
    r = {"code": code, "name": "成长先锋混合A", "fund_type": "混合型-偏股",
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
         # 「最近一次止盈后」当前轮（2026-11-19；本 fixture 不关心取值，
         # 仅保列齐全防 import_tp_rows KeyError）
         "full_last_tp": None, "full_last_tp_disc": None,
         "full_last_tp_nom": None, "full_cur_invested": None,
         "full_cur_profit_rate": None, "full_cur_profit_rate_disc": None,
         "full_cur_profit_rate_nom": None,
         "full_cur_xirr": None, "full_cur_xirr_disc": None,
         "full_cur_xirr_nom": None,
         "tp": tp, "method": "amount"}
    return r


class TestTpStore:
    def test_ensure_tp_schema_ddl(self):
        from app.dca_store import ensure_tp_schema
        conn = FakeConn()
        ensure_tp_schema(conn)
        sqls = [sql for sql, _ in conn.cursor_obj.executed]
        assert any("CREATE TABLE IF NOT EXISTS fund_dca_tp_result" in s
                   for s in sqls)
        assert any("CREATE TABLE IF NOT EXISTS fund_dca_tp_rounds" in s
                   for s in sqls)
        assert any("CREATE TABLE IF NOT EXISTS fund_dca_tp_meta" in s
                   for s in sqls)
        # 三新表主键：结果 (code, tp, method)、轮次 (code, tp, method, fee, seq)
        #（method 定投方式维度 2026-11-23 加入；等份额口径见 test_fund_dca_share）
        assert any("PRIMARY KEY (code, tp, method)" in s for s in sqls)
        assert any("PRIMARY KEY (code, tp, method, fee, seq)" in s for s in sqls)

    def test_import_tp_rows_sequence(self):
        from app.dca_store import import_tp_rows
        conn = FakeConn()
        rows = [_tp_row(tp=0.05), _tp_row(tp=0.15),            # 同基金两阈值
                _tp_row(code="000002", tp=0.05)]
        import_tp_rows(conn, rows,
                       params={"thresholds": [0.05, 0.15, 0.30],
                               "amount": 1000})
        sqls = [sql for sql, _ in conn.cursor_obj.executed]
        assert sqls[0] == "DELETE FROM fund_dca_tp_result"
        assert any("fund_dca_tp_meta" in s for s in sqls)
        assert len(conn.cursor_obj.batch) == 1
        sql, seq = conn.cursor_obj.batch[0]
        assert "INSERT INTO fund_dca_tp_result" in sql
        assert len(seq) == 3
        # meta：fund_count 为去重基金数（3 行 = 2 只基金），参数带阈值清单
        last_sql, last_params = conn.cursor_obj.executed[-1]
        assert "fund_dca_tp_meta" in last_sql
        assert last_params[0] == 2 and "thresholds" in last_params[1]

    def test_import_tp_rows_idempotent(self):
        from app.dca_store import import_tp_rows
        rows = [_tp_row(tp=0.10)]
        seqs = []
        for _ in range(2):
            conn = FakeConn()
            import_tp_rows(conn, rows, params={"thresholds": [0.10]})
            seqs.append((len(conn.cursor_obj.executed),
                         len(conn.cursor_obj.batch[0][1])))
        assert seqs[0] == seqs[1]

    def test_import_tp_rounds_sequence_and_gap(self):
        from app.dca_store import import_tp_rounds
        conn = FakeConn()
        rows = [("000001", 0.15, "nom", 1, "2024-02-10", 74, 74, 0.213,
                 1.213, 5000.0, 36.9, 12.8, 1050.3)]
        import_tp_rounds(conn, rows)
        sqls = [sql for sql, _ in conn.cursor_obj.executed]
        assert sqls[0] == "DELETE FROM fund_dca_tp_rounds"
        # 间隔聚合：重置 + 回填两条 UPDATE，均作用于 tp 结果表、按 (code, tp) 关联
        updates = [s for s in sqls if "UPDATE fund_dca_tp_result" in s]
        assert len(updates) == 2
        apply_sql = [s for s in updates if "gap_avg_days = t.g_avg" in s][0]
        assert "GROUP BY code, tp" in apply_sql
        assert "r.code = t.code AND r.tp = t.tp" in apply_sql
        assert len(conn.cursor_obj.batch) == 1
        sql, seq_rows = conn.cursor_obj.batch[0]
        assert "INSERT INTO fund_dca_tp_rounds" in sql and len(seq_rows) == 1

    def test_tp_rounds_insert_columns_align_with_engine_tuple(self):
        """列序守护：INSERT 列清单必须与引擎元组 (code, method, tp, fee,
        seq, *ev) 逐位对齐——错位会被 PG 隐式类型转换'成功'吞掉（'000001'
        →1.0），只能靠列清单与元组序一致防错（2026-11-15 生产实跑事故）。"""
        from app.dca_store import _TP_ROUNDS_COLS
        assert _TP_ROUNDS_COLS == ["code", "method", "tp", "fee", "seq",
                                   "tp_date", "round_days", "gap_days",
                                   "profit", "tp_nav", "invested",
                                   "sub_fee", "redeem_fee", "net_profit"]
        # 引擎轮次元组同长度（元素序 (code, method, tp, fee, seq, *ev) 由
        # run_backtest_tp 的 append 处锚定，另有 tp=0.20≡旧引擎测试守护）


# ===== 对比快照：rebuild_tp_compare 全组合重建（2026-11-25） =====

class AggCursor(FakeCursor):
    """fetchall 返回固定聚合行（rebuild_tp_compare 每个 (method, equity)
    查询共用一份桩行）。"""

    def __init__(self, conn, agg_rows):
        super().__init__(conn)
        self._agg_rows = agg_rows

    def fetchall(self):
        return self._agg_rows


def _agg_row(tp=0.20):
    """单遍聚合查询的一行（34 元组：tp + 三费用块各 11 列，块序 gross /
    disc / nom 同 _TP_COMPARE_AGG_SQL；三块取值互异防跨块错位混过——
    生产首跑曾因恒取 gross 块给三口径标签，桩行列数不足旧形状漏过）。
    列内序：count / 年化中位 / 年化均值 / 正收益占比 / 成功率 / 平均轮次 /
    平均间隔 / 总收益率中位 / 总收益率均值 / 总收益额中位 / 总收益额均值
    （总收益四列 2026-11-27 加入，金额 = 总收益率 × 总投入）。"""
    def blk(base):
        return (15029,                       # count
                0.12345612345 + base,        # 中位（全史年化）
                0.23456712345 + base,        # 均值
                0.34567812345 + base,        # 正收益占比
                0.45678912345 + base,        # 成功率（轮次口径）
                1.931612345 + base,          # 平均轮次
                703.14159265 + base,         # 平均间隔
                0.5678912345 + base,         # 总收益率中位
                0.6789123456 + base,         # 总收益率均值
                12345.678901 + base,         # 总收益额中位
                23456.789012 + base)         # 总收益额均值
    return (tp,) + blk(0.0) + blk(0.02) + blk(0.04)


class TestTpCompareSnapshot:
    def test_rebuild_writes_full_grid_with_filter_anchor(self):
        from app.dca_backtest import DCA_METHODS
        from app.dca_store import rebuild_tp_compare
        n_combo = len(DCA_METHODS)      # × 2 池 × 3 费用 × 8 档（方式清单随引擎扩展）
        conn = FakeConn()
        conn.cursor_obj = AggCursor(conn, [_agg_row(tp) for tp in
                                           (0.05, 0.10, 0.15, 0.20, 0.25,
                                            0.30, 0.35, 0.40)])
        n = rebuild_tp_compare(conn, equity_types=("股票型", "商品"))
        assert n == n_combo * 2 * 3 * 8
        sqls = [sql for sql, _ in conn.cursor_obj.executed]
        assert any("CREATE TABLE IF NOT EXISTS fund_dca_tp_compare" in s
                   for s in sqls)
        aggs = [sql for sql in sqls if "GROUP BY tp" in sql]
        assert len(aggs) == n_combo * 2     # 每 (方式, 池) 单遍三费用口径
        # 口径锚点（2026-11-25 真机证伪无过滤版）：年化 NULL 行其轮次/间隔
        # 列仍非空，每项聚合必须 FILTER (WHERE full_xirr_* IS NOT NULL)
        #（十七轮起聚合门与分位源统一全史年化，w10 五分位列退役）
        assert all("FILTER (WHERE full_xirr IS NOT NULL)" in s
                   for s in aggs)
        assert all("FILTER (WHERE full_xirr_disc IS NOT NULL)" in s
                   for s in aggs)
        assert all("FILTER (WHERE full_xirr_nom IS NOT NULL)" in s
                   for s in aggs)
        assert all("w10_xirr" not in s for s in aggs)   # 退役列不得残留
        # 总收益四聚合（2026-11-27）：三口径各带 总收益率中位/均值 + 总收益额
        # （=总收益率×总投入）中位/均值；门沿用年化（宇宙锚不因新列漂移）
        for sfx in ("", "_disc", "_nom"):
            assert all("percentile_cont(0.5) WITHIN GROUP "
                       f"(ORDER BY full_profit_rate{sfx})" in s for s in aggs)
            assert all(f"avg(full_profit_rate{sfx})" in s for s in aggs)
            assert all("percentile_cont(0.5) WITHIN GROUP "
                       f"(ORDER BY full_profit_rate{sfx} * full_invested)"
                       in s for s in aggs)
            assert all(f"avg(full_profit_rate{sfx} * full_invested)"
                       in s for s in aggs)
        # 口径锚点不漂移：总收益聚合的门仍是年化非空，不得换成按收益过滤
        assert all("FILTER (WHERE full_profit_rate" not in s for s in aggs)
        assert all("FROM fund_dca_tp_result" in s for s in aggs)
        assert "DELETE FROM fund_dca_tp_compare" in sqls
        # equity 两池：白名单参数（fund_type = ANY）vs 全市场无池参数
        eq_params = [p for _, p in conn.cursor_obj.executed
                     if p and len(p) == 2]
        all_params = [p for _, p in conn.cursor_obj.executed
                      if p and len(p) == 1]
        assert len(eq_params) == n_combo and len(all_params) == n_combo
        assert ("share", ["股票型", "商品"]) in eq_params
        assert ("amount",) in all_params
        # 整表替换：一次 executemany，三费用块各取各的列（块错位回归守护：
        # 生产首跑曾恒取 gross 块写满三口径标签）、值按现算同精度圆整
        assert len(conn.cursor_obj.batch) == 1
        ins_sql, rows = conn.cursor_obj.batch[0]
        assert "INSERT INTO fund_dca_tp_compare" in ins_sql
        assert len(rows) == n_combo * 2 * 3 * 8

        def blk_want(base, sfx_want):
            return ("amount", True, sfx_want, 0.20, 15029,
                    round(0.12345612345 + base, 4),
                    round(0.23456712345 + base, 4),
                    round(0.34567812345 + base, 4),
                    round(0.45678912345 + base, 4),
                    round(1.931612345 + base, 2),
                    round(703.14159265 + base, 1),
                    round(0.5678912345 + base, 4),
                    round(0.6789123456 + base, 4),
                    round(12345.678901 + base, 2),
                    round(23456.789012 + base, 2))

        assert blk_want(0.0, "gross") in rows
        assert blk_want(0.02, "disc") in rows
        assert blk_want(0.04, "nom") in rows
        want = ("amount", True, "gross", 0.20, 15029,
                round(0.12345612345, 4), round(0.23456712345, 4),
                round(0.34567812345, 4), round(0.45678912345, 4),
                round(1.931612345, 2), round(703.14159265, 1),
                round(0.5678912345, 4), round(0.6789123456, 4),
                round(12345.678901, 2), round(23456.789012, 2))
        assert want in rows

    def test_rebuild_empty_table_still_replaces(self):
        from app.dca_store import rebuild_tp_compare
        conn = FakeConn()
        conn.cursor_obj = AggCursor(conn, [])
        assert rebuild_tp_compare(conn, equity_types=("股票型",)) == 0
        sqls = [sql for sql, _ in conn.cursor_obj.executed]
        assert "DELETE FROM fund_dca_tp_compare" in sqls
        assert conn.cursor_obj.batch == []      # 空结果不 executemany

    def test_compare_ddl_adds_profit_columns(self):
        """总收益四列幂等扩列（2026-11-27）：存量快照表 ALTER 补列，
        重建前旧行该四列为 NULL → API 快照路径透传 NULL 前端显 —，
        夜更重建后自愈。"""
        from app.dca_store import _TP_COMPARE_DDL
        for col in ("median_profit_rate", "mean_profit_rate",
                    "median_profit_amt", "mean_profit_amt"):
            assert (f"ADD COLUMN IF NOT EXISTS {col} DOUBLE PRECISION"
                    in _TP_COMPARE_DDL)


# ===== API：/api/funds/dca/tp 三端点契约 =====

def _patch_connect(monkeypatch, module, conn):
    def fake_connect(*a, **k):
        return conn
    monkeypatch.setattr(module.psycopg, "connect", fake_connect)


class TestDcaTpRoutes:
    def test_tp_list_registered_and_degrades(self, client, monkeypatch):
        """PG 不可达：仍 200，status=down 空形状（降级约定同旧端点）。"""
        import app.dca_api as dca_mod
        monkeypatch.setattr("app.config.DATABASE_URL", DEAD_DSN)
        resp = client.get("/api/funds/dca/tp", params={"tp": "0.15"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "down" and body["rows"] == []

    def test_tp_whitelist_fallback(self, client, monkeypatch):
        """tp 非白名单（0.13/乱值）：回落 0.2 并回显实际使用的 tp；
        全部八档（5%~40%）精确通过。"""
        import app.dca_api as dca_mod
        one = (None, None, None)
        conn = FakeConnOne(one)
        _patch_connect(monkeypatch, dca_mod, conn)
        resp = client.get("/api/funds/dca/tp", params={"tp": "0.13"})
        assert resp.status_code == 200
        assert resp.json()["tp"] == 0.2
        resp = client.get("/api/funds/dca/tp", params={"tp": "abc"})
        assert resp.json()["tp"] == 0.2
        for tp in ("0.05", "0.1", "0.15", "0.2", "0.25", "0.3", "0.35", "0.4"):
            resp = client.get("/api/funds/dca/tp", params={"tp": tp})
            assert resp.json()["tp"] == float(tp), tp

    def test_tp_thresholds_pin_eight_values(self):
        """阈值口径锚定：八档 5%~40%（2026-11-15 用户追加 35%/40%）。"""
        from app.dca_backtest import TP_THRESHOLDS
        assert TP_THRESHOLDS == (0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40)

    def test_tp_list_sql_targets_new_table(self, client, monkeypatch):
        """SQL 形状：查 fund_dca_tp_result 且带 tp 过滤；排序随费用口径后缀。"""
        import app.dca_api as dca_mod
        conn = FakeConnOne((None, None, None))
        _patch_connect(monkeypatch, dca_mod, conn)
        client.get("/api/funds/dca/tp", params={"tp": "0.15", "fee": "nom",
                                                "sort": "full_xirr"})
        sqls = conn.executed
        assert any("FROM fund_dca_tp_result" in s for s in sqls)
        assert any("tp = %s" in s for s in sqls)
        assert any("full_xirr_nom" in s for s in sqls)   # 排序列带口径后缀

    def test_tp_list_profit_amount_column_and_sort(self, client, monkeypatch):
        """总收益额列（2026-11-27）：行 SELECT 带 r.full_invested（行内额=
        总收益率×总投入，前端计算）；sort=full_profit_amt 白名单放行，
        ORDER BY 落在 费率口径总收益率×总投入 表达式上。"""
        import app.dca_api as dca_mod
        conn = FakeConnOne((None, None, None))
        _patch_connect(monkeypatch, dca_mod, conn)
        resp = client.get("/api/funds/dca/tp",
                          params={"sort": "full_profit_amt", "fee": "nom"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "up"
        list_sql = [q for q in conn.executed
                    if "FROM fund_dca_tp_result" in q and "ORDER BY" in q][0]
        assert "r.full_invested" in list_sql
        assert "r.full_profit_rate_nom * r.full_invested" in list_sql

    def test_tp_stats_sql_includes_profit_metrics(self, client, monkeypatch):
        """统计卡 SQL 形状（2026-11-27 二十五轮改锚）：总收益分位改**金额
        口径**（ORDER BY 率×总投入——每基金投入不同，金额分位不可由率分位
        换算）+ 投入中位聚合；宇宙门仍为年化非空（与年化分位同宇宙，不做
        收益侧二次过滤）。"""
        import app.dca_api as dca_mod
        conn = _ListDispatchConn((13557,) + (None,) * 12)
        _patch_connect(monkeypatch, dca_mod, conn)
        resp = client.get("/api/funds/dca/tp", params={"fee": "nom"})
        assert resp.status_code == 200
        norm = [" ".join(q.split()) for q, _ in conn.executed]
        stats_sql = [q for q in norm if "percentile_cont" in q][0]
        for p in ("0.25", "0.5", "0.75", "0.9"):
            assert (f"percentile_cont({p}) WITHIN GROUP (ORDER BY "
                    "r.full_profit_rate_nom * r.full_invested)") in stats_sql, p
        assert ("percentile_cont(0.5) WITHIN GROUP "
                "(ORDER BY r.full_invested)") in stats_sql   # 投入中位
        assert "avg((r.full_profit_rate_nom > 0)::int)::float" in stats_sql
        assert "r.full_xirr_nom IS NOT NULL" in stats_sql

    def test_tp_stats_profit_keys_in_response(self, client, monkeypatch):
        """统计卡响应（二十五轮改锚）：总收益**金额**四分位（amt_*）+ 投入
        中位（invested_median）+ 为正占比随 stats 一并返回。"""
        import app.dca_api as dca_mod
        stats_row = (13557, 0.01, 0.02, 0.03, 0.04, 0.5, 0.6,
                     1100.0, 1200.0, 1300.0, 1400.0, 0.15, 15000.0)
        conn = _ListDispatchConn(stats_row)
        _patch_connect(monkeypatch, dca_mod, conn)
        resp = client.get("/api/funds/dca/tp", params={"fee": "nom"})
        assert resp.status_code == 200
        st = resp.json()["stats"]
        assert st["count"] == 13557
        assert st["median"] == 0.02
        assert st["amt_p25"] == 1100.0 and st["amt_median"] == 1200.0
        assert st["amt_p75"] == 1300.0 and st["amt_p90"] == 1400.0
        assert st["pr_positive_share"] == 0.15
        assert st["invested_median"] == 15000.0

    def test_tp_compare_registered_and_degrades(self, client, monkeypatch):
        import app.dca_api as dca_mod
        monkeypatch.setattr("app.config.DATABASE_URL", DEAD_DSN)
        resp = client.get("/api/funds/dca/tp/compare", params={"fee": "nom"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "down" and body["rows"] == []

    def test_tp_compare_sql_groups_by_threshold(self, client, monkeypatch):
        import app.dca_api as dca_mod
        conn = FakeConnOne((None,))
        _patch_connect(monkeypatch, dca_mod, conn)
        client.get("/api/funds/dca/tp/compare", params={"fee": "nom"})
        sqls = conn.executed
        cmp_sql = [s for s in sqls if "GROUP BY tp" in s]
        assert cmp_sql and "FROM fund_dca_tp_result" in cmp_sql[0]
        assert "full_xirr_nom" in cmp_sql[0]
        assert "w10_xirr" not in cmp_sql[0]   # 退役列不得残留

    def _snap_row(self, tp=0.20):
        """快照表一行（13 元组：7 聚合列已圆整 + 总收益四列 + computed_at），
        列序同 _TP_COMPARE_SNAP_SQL（与现算行前 12 列同序，末列为快照时间）。"""
        import datetime as dtm
        return (tp, 15029, 0.1235, 0.2346, 0.3457, 0.4568, 1.93, 703.1,
                0.1111, 0.2222, 12345.67, 23456.78,
                dtm.datetime(2026, 11, 25, 12, 0, 0,
                             tzinfo=dtm.timezone.utc))

    def _live_row(self, tp=0.20):
        """现算聚合一行（13 元组：tp + count + 11 聚合列，总收益列未圆整），
        列序同 /api/funds/dca/tp/compare 现算 SQL。"""
        return (tp, 15029, 0.1235, 0.2346, 0.3457, 0.4568, 1.93, 703.1,
                0.111123, 0.222234, 12345.678, 23456.789)

    def test_compare_snapshot_hit_skips_live_sql(self, client, monkeypatch):
        """快照满八档：只查 fund_dca_tp_compare 一条 SQL（不落百万行现算），
        source=snapshot、computed_at 透传、行值原样返回（含总收益四键）。"""
        import app.dca_api as dca_mod
        rows = [self._snap_row(tp) for tp in (0.05, 0.10, 0.15, 0.20, 0.25,
                                              0.30, 0.35, 0.40)]
        conn = FakeConnOne((None,), rows=rows)
        _patch_connect(monkeypatch, dca_mod, conn)
        resp = client.get("/api/funds/dca/tp/compare",
                          params={"fee": "nom", "method": "share"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "up" and body["source"] == "snapshot"
        assert body["computed_at"] == "2026-11-25T12:00:00+00:00"
        assert body["method"] == "share"
        r = body["rows"][3]             # tp=0.20
        assert r["tp"] == 0.20 and r["count"] == 15029
        assert r["median"] == 0.1235 and r["mean"] == 0.2346
        assert r["positive_share"] == 0.3457 and r["success_share"] == 0.4568
        assert r["avg_rounds"] == 1.93 and r["avg_gap_days"] == 703.1
        assert r["median_profit_rate"] == 0.1111
        assert r["mean_profit_rate"] == 0.2222
        assert r["median_profit_amt"] == 12345.67
        assert r["mean_profit_amt"] == 23456.78
        assert len(conn.executed) == 1 and "GROUP BY tp" not in conn.executed[0]
        assert "fund_dca_tp_compare" in conn.executed[0]
        # 快照查询带总收益四列（列序扩员后旧 9 元组查询不得回潮）
        assert "median_profit_rate" in conn.executed[0]
        assert "mean_profit_amt" in conn.executed[0]

    def test_compare_snapshot_incomplete_falls_back_live(self,
                                                         client, monkeypatch):
        """快照该组合不满八档（部署早期/重建中断）：回落现算聚合仍 up。"""
        import app.dca_api as dca_mod
        conn = FakeConnOne((None,), rows=[self._live_row()])
        _patch_connect(monkeypatch, dca_mod, conn)
        resp = client.get("/api/funds/dca/tp/compare", params={"fee": "nom"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "up" and body["source"] == "live"
        assert body["computed_at"] is None
        cmp_sql = [s for s in conn.executed if "GROUP BY tp" in s]
        assert cmp_sql and "fund_dca_tp_result" in cmp_sql[0]

    def test_compare_snapshot_missing_table_falls_back(self,
                                                       client, monkeypatch):
        """快照缺表（建表前）：现算路径照常工作，绝不 500。"""
        import psycopg
        import app.dca_api as dca_mod

        class SnapFailConn(FakeConnOne):
            def execute(self, query, params=None):
                if "fund_dca_tp_compare" in query:
                    raise psycopg.errors.UndefinedTable(
                        "relation does not exist")
                self.executed.append(query)

        conn = SnapFailConn((None,), rows=[self._live_row()])
        _patch_connect(monkeypatch, dca_mod, conn)
        resp = client.get("/api/funds/dca/tp/compare", params={"fee": "nom"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "up" and body["source"] == "live"
        assert body["rows"][0]["count"] == 15029

    def test_compare_live_sql_has_profit_aggregates(self, client, monkeypatch):
        """现算 SQL 形状：总收益四聚合（三口径带后缀）进 GROUP BY tp 查询。"""
        import app.dca_api as dca_mod
        conn = FakeConnOne((None,), rows=[self._live_row()])
        _patch_connect(monkeypatch, dca_mod, conn)
        resp = client.get("/api/funds/dca/tp/compare", params={"fee": "nom"})
        assert resp.status_code == 200
        cmp_sql = [s for s in conn.executed if "GROUP BY tp" in s][0]
        assert ("percentile_cont(0.5) WITHIN GROUP "
                "(ORDER BY full_profit_rate_nom)") in cmp_sql
        assert "avg(full_profit_rate_nom)" in cmp_sql
        assert ("percentile_cont(0.5) WITHIN GROUP "
                "(ORDER BY full_profit_rate_nom * full_invested)") in cmp_sql
        assert "avg(full_profit_rate_nom * full_invested)" in cmp_sql

    def test_compare_live_row_maps_profit_keys(self, client, monkeypatch):
        """现算行映射：总收益四键输出并按现算精度圆整（率 4 位 / 金额 2 位）。"""
        import app.dca_api as dca_mod
        conn = FakeConnOne((None,), rows=[self._live_row()])
        _patch_connect(monkeypatch, dca_mod, conn)
        resp = client.get("/api/funds/dca/tp/compare", params={"fee": "nom"})
        assert resp.status_code == 200
        r = resp.json()["rows"][0]
        assert r["median_profit_rate"] == 0.1111
        assert r["mean_profit_rate"] == 0.2222
        assert r["median_profit_amt"] == 12345.68
        assert r["mean_profit_amt"] == 23456.79

    def test_compare_equity_pool_includes_commodity(self):
        """对比池口径（2026-11-15 用户确认）：权益 8 类 + QDII-商品 + 商品。"""
        from app.dca_api import EQUITY_TYPES, TP_COMPARE_EQUITY_TYPES
        assert set(EQUITY_TYPES) < set(TP_COMPARE_EQUITY_TYPES)
        assert "QDII-商品" in TP_COMPARE_EQUITY_TYPES
        assert "商品" in TP_COMPARE_EQUITY_TYPES

    def test_tp_rounds_registered_and_degrades(self, client, monkeypatch):
        import app.dca_api as dca_mod
        monkeypatch.setattr("app.config.DATABASE_URL", DEAD_DSN)
        resp = client.get("/api/funds/dca/tp/000001/rounds",
                          params={"tp": "0.15"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "down" and body["rounds"] == []

    def test_tp_rounds_tp_echo_and_fallback(self, client, monkeypatch):
        import app.dca_api as dca_mod
        conn = FakeConnOne([])
        _patch_connect(monkeypatch, dca_mod, conn)
        resp = client.get("/api/funds/dca/tp/000001/rounds",
                          params={"tp": "0.30"})
        assert resp.json()["tp"] == 0.30
        resp = client.get("/api/funds/dca/tp/000001/rounds",
                          params={"tp": "0.99"})
        assert resp.json()["tp"] == 0.2

    def test_tp_summary_degrades(self, client, monkeypatch):
        """summary 端点：PG 不可达仍 200 down（降级约定同旧端点）。"""
        monkeypatch.setattr("app.config.DATABASE_URL", DEAD_DSN)
        resp = client.get("/api/funds/dca/tp/000001/summary")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "down" and body["rows"] == []
        assert body["code"] == "000001"

    def test_tp_summary_sql_and_rows(self, client, monkeypatch):
        """summary SQL 形状：WHERE code 精确匹配 ORDER BY tp；行映射带
        窗口起始/运作年限与止盈后当前轮十二字段（十七轮起 w10 年化/总
        收益率六指标键退役不得残留，列序见 _TP_SUMMARY_SQL）。"""
        import app.dca_api as dca_mod
        import datetime as dt
        row = (0.15,
               dt.date(2026, 11, 16) - dt.timedelta(days=3653),
               None, None, None, None, None, None, None, None, None, None)
        # ref 查询先行（fetchone 给 ref；fetchall 走 rows）
        conn = FakeConnOne((dt.date(2026, 11, 16),), rows=[row])
        _patch_connect(monkeypatch, dca_mod, conn)
        resp = client.get("/api/funds/dca/tp/000001/summary")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "up" and body["code"] == "000001"
        r = body["rows"][0]
        assert r["tp"] == 0.15
        assert r["w10_start_iso"] == (dt.date(2026, 11, 16)
                                      - dt.timedelta(days=3653)).isoformat()
        assert r["age_years"] == pytest.approx(10.0, abs=0.1)
        assert r["full_last_tp"] is None and r["full_cur_xirr"] is None
        for gone in ("w10_profit_rate", "w10_xirr", "w10_profit_rate_disc",
                     "w10_xirr_disc", "w10_profit_rate_nom", "w10_xirr_nom"):
            assert gone not in r
        assert "tp = %s" not in "".join(conn.executed)
        assert any("WHERE code = %s" in sql and "ORDER BY tp" in sql
                   for sql in conn.executed)


class FakeConnOne:
    """fetchone/fetchall 可配置的最小连接桩（psycopg.connect 替身）。"""

    def __init__(self, one, rows=None):
        self.one = one
        self.rows = rows or []
        self.executed: list = []

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        self.executed.append(query)

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.rows


class _ListDispatchConn:
    """按查询内容分发的连接桩（tp 列表端点一次请求四次 fetchone：参考日 /
    count / 统计卡聚合 / meta，各回各的值——FakeConnOne 恒返同一元组会
    让 meta 解析踩到统计行形状）。"""

    def __init__(self, stats_row):
        self.stats_row = stats_row
        self.executed: list = []

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        self.executed.append((query, params))

    def fetchone(self):
        q = self.executed[-1][0]
        if "max(full_end)" in q:
            return (None,)                  # 参考日
        if "percentile_cont" in q:
            return self.stats_row           # 统计卡聚合
        if "fund_dca_meta" in q:
            return (None, None, None)       # meta（表名无 tp 前缀）
        return (0,)                         # count(*)

    def fetchall(self):
        return []
