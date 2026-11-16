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

    def test_tp20_matches_legacy_run_backtest(self):
        """关键一致性：tp=0.20 结果行/轮次行 ≡ 旧 run_backtest 输出。"""
        from app import dca_backtest as mod
        nav, basic, scale_map = _synthetic_inputs()
        old_rows, old_rounds = mod.run_backtest(nav, basic, scale_map)
        tp_rows, tp_rounds = mod.run_backtest_tp(nav, basic, scale_map)
        old_by_code = {r["code"]: r for r in old_rows}
        new_by_code = {r["code"]: r for r in tp_rows if r["tp"] == 0.20}
        for code, orow in old_by_code.items():
            nrow = new_by_code[code]
            assert set(orow) == (set(nrow) - {"tp"})
            for k, v in orow.items():
                if v is None and nrow[k] is None:
                    continue
                assert nrow[k] == v, (code, k)
        # 轮次行：旧 (code, fee, seq, *ev) ≡ 新 tp=0.20 去掉 tp 位
        old_r = [t for t in old_rounds if t[0] == "000001"]
        new_r = [t[:1] + t[2:] for t in tp_rounds
                 if t[0] == "000001" and t[1] == 0.20]
        assert old_r == new_r

    def test_rounds_profit_respects_threshold(self):
        """轮次市值口径收益 ≥ 对应阈值（跳空越过如实记录）。"""
        from app import dca_backtest as mod
        nav, basic, scale_map = _synthetic_inputs()
        _, rounds = mod.run_backtest_tp(nav, basic, scale_map)
        by_tp = {}
        for _code, tp, _fee, _seq, *_ev in rounds:
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


# ===== 存储：三新表 DDL/写入/间隔聚合 =====

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
         "tp": tp}
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
        # 三新表主键：结果 (code, tp)、轮次 (code, tp, fee, seq)
        assert any("PRIMARY KEY (code, tp)" in s for s in sqls)
        assert any("PRIMARY KEY (code, tp, fee, seq)" in s for s in sqls)

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
        """列序守护：INSERT 列清单必须与引擎元组 (code, tp, fee, seq, *ev)
        逐位对齐——错位会被 PG 隐式类型转换'成功'吞掉（'000001'→1.0），
        只能靠列清单与元组序一致防错（2026-11-15 生产实跑事故）。"""
        from app.dca_store import _TP_ROUNDS_COLS, _ROUNDS_COLS
        assert _TP_ROUNDS_COLS[:2] == ["code", "tp"]
        assert _TP_ROUNDS_COLS[2:] == _ROUNDS_COLS[1:]
        assert len(_TP_ROUNDS_COLS) == 13
        # 引擎轮次元组同长度（元素序 (code, tp, fee, seq, *ev) 由
        # run_backtest_tp 的 append 处锚定，另有 tp=0.20≡旧引擎测试守护）


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
                                                "sort": "w10_xirr"})
        sqls = conn.executed
        assert any("FROM fund_dca_tp_result" in s for s in sqls)
        assert any("tp = %s" in s for s in sqls)
        assert any("w10_xirr_nom" in s for s in sqls)   # 排序列带口径后缀

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
        assert "w10_xirr_nom" in cmp_sql[0] and "full_xirr_nom" in cmp_sql[0]

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
