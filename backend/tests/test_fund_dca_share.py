"""等份额定投回测测试（定投方式维度：等额定投 amount / 等份额定投 share）。

口径（2026-11-23 方案定案）：
- 等份额：每期固定到手份额 = 基准金额 / 窗口首日复权净值（首期恰好投入
  1000 元，跨基金可比、无前视）；含费为外扣法对偶——现金支出 =
  份额×净值×(1+申购费率)，到手份额仍固定；
- 止盈触发/轮次/期末清算/XIRR 与等额共用同一状态机，仅每期现金与份额
  的分派不同（invested 每期可变，触发线仍是 市值 ≥ 已投入×(1+tp)）；
- 存储：fund_dca_tp_result/rounds 增 method 列（存量行 ALTER 就地归
  'amount'，不丢数据防生产空窗——2026-10-26/11-20 教训），PK 纳入 method；
- API：/tp 四端点加 method 白名单参数（外回落 amount），SQL 按 method 过滤。
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


# ===== 引擎：等份额口径 =====

def _three_month_nav(final_scale: float):
    """3 个自然月逐日序列：前两月净值 1.0、第三月跳到 final_scale。

    月度买入发生在 1/1（a=1）、2/1（a=1）、3/1（a=final_scale）。
    """
    dates = pd.DatetimeIndex(pd.date_range("2024-01-01", periods=90, freq="D"))
    adj = np.where(np.arange(90) >= 60, final_scale, 1.0)
    return dates, adj


class TestSimulateShare:
    def test_methods_pin_values(self):
        """定投方式口径锚定：等额 amount / 等份额 share（2026-11-23 定案）；
        四轮追加 dd_ladder、六轮 dd_ladder_deep、七轮 profit_dd_ladder、
        八轮 pl_ladder（见 test_fund_dca_pl_ladder.py）。"""
        from app.dca_backtest import DCA_METHODS
        assert DCA_METHODS == ("amount", "share", "dd_ladder",
                               "dd_ladder_deep", "profit_dd_ladder",
                               "pl_ladder")

    def test_amount_method_equals_default(self):
        """method='amount' 与缺省调用逐字段一致（等额行为零回归锚）。"""
        from app import dca_backtest as mod
        dates = pd.DatetimeIndex(pd.date_range("2024-01-01", periods=400,
                                               freq="D"))
        rng = np.random.default_rng(7)
        adj = np.cumprod(1.0 + rng.normal(0.001, 0.012, 400))
        fee = mod.FeeSchedule(0.015, mod.REDEEM_EQUITY)
        for f in (None, fee):
            legacy = mod.simulate_multi(dates, adj, f, mod.TP_THRESHOLDS)
            amount = mod.simulate_multi(dates, adj, f, mod.TP_THRESHOLDS,
                                        method="amount")
            assert legacy == amount

    def test_invalid_method_raises(self):
        from app import dca_backtest as mod
        dates, adj = _three_month_nav(2.0)
        with pytest.raises(ValueError):
            mod.simulate(dates, adj, method="dca")

    def test_share_constant_nav_equals_amount(self):
        """净值恒定时等份额 ≡ 等额（每期现金同为 1000，结果逐字段一致）。"""
        from app import dca_backtest as mod
        dates = pd.DatetimeIndex(pd.date_range("2024-01-01", periods=400,
                                               freq="D"))
        adj = np.ones(400)
        amt = mod.simulate(dates, adj, take_profit=0.20)
        shar = mod.simulate(dates, adj, take_profit=0.20, method="share")
        assert shar == amt

    def test_share_constant_nav_with_fee_scales_invested(self):
        """恒定净值 + 外扣费：等份额每期现金 = 1000×(1+费率)——投入同比例
        放大、现金流整体同比例缩放 → 收益率与 XIRR 不变（比例不变性）。"""
        from app import dca_backtest as mod
        dates = pd.DatetimeIndex(pd.date_range("2024-01-01", periods=400,
                                               freq="D"))
        adj = np.ones(400)
        fee = mod.FeeSchedule(0.015, ())          # 空赎回阶梯：赎回费 0
        amt = mod.simulate(dates, adj, fee, take_profit=0.20)
        shar = mod.simulate(dates, adj, fee, take_profit=0.20, method="share")
        assert shar["invested"] == pytest.approx(amt["invested"] * 1.015)
        assert shar["profit_rate"] == pytest.approx(amt["profit_rate"])
        assert shar["xirr"] == pytest.approx(amt["xirr"], abs=1e-9)

    def test_share_rising_nav_hand_computed(self):
        """上行行情手算：3 期各买 1000 份（1/2 月 a=1、3 月 a=2）→
        投入 4000、份额 3000、期末市值 6000、收益率 50%。"""
        from app import dca_backtest as mod
        dates, adj = _three_month_nav(2.0)
        r = mod.simulate(dates, adj, take_profit=99.0, method="share")
        assert r["rounds"] == 0
        assert r["invested"] == pytest.approx(4000.0)
        assert r["final_value"] == pytest.approx(6000.0)
        assert r["profit_rate"] == pytest.approx(0.5)
        spend = [-a for d, a in r["flows"] if a < 0]
        assert spend == pytest.approx([1000.0, 1000.0, 2000.0])

    def test_share_falling_nav_hand_computed(self):
        """下行行情手算：3 月净值减半 → 投入 2500、份额 3000、期末 1500、
        收益率 -40%（跌时花得少的成本摊平劣势面）。"""
        from app import dca_backtest as mod
        dates, adj = _three_month_nav(0.5)
        r = mod.simulate(dates, adj, take_profit=99.0, method="share")
        assert r["invested"] == pytest.approx(2500.0)
        assert r["final_value"] == pytest.approx(1500.0)
        assert r["profit_rate"] == pytest.approx(-0.4)

    def test_share_fee_math_external(self):
        """外扣法对偶：到手份额固定，现金 = 份额×净值×(1+申购费率)。
        1.5% 申购费下三期现金 1015/1015/2030。"""
        from app import dca_backtest as mod
        dates, adj = _three_month_nav(2.0)
        fee = mod.FeeSchedule(0.015, ())
        r = mod.simulate(dates, adj, fee, take_profit=99.0, method="share")
        spend = [-a for d, a in r["flows"] if a < 0]
        assert spend == pytest.approx([1015.0, 1015.0, 2030.0])
        assert r["invested"] == pytest.approx(4060.0)
        assert r["final_value"] == pytest.approx(6000.0)  # 份额仍 3000×2

    def test_share_tp_round_records_variable_invested(self):
        """止盈轮次记可变投入：3/1 买入 2000 当日市值 6000 ≥ 4000×1.2
        触发全清——轮次投入 4000、回款 6000、净收益 2000。"""
        from app import dca_backtest as mod
        dates, adj = _three_month_nav(2.0)
        r = mod.simulate(dates, adj, take_profit=0.20, method="share")
        assert r["rounds"] == 1
        ev = r["round_events"][0]
        assert ev[3] == pytest.approx(0.5)      # 市值口径收益（跳空越过）
        assert ev[5] == pytest.approx(4000.0)   # 本轮累计投入
        assert ev[8] == pytest.approx(2000.0)   # 本轮净收益
        assert any(a == pytest.approx(6000.0) for _, a in r["flows"])


# ===== run_backtest_tp：method 维度行/轮次 =====

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


class TestRunBacktestTpShare:
    def test_rows_carry_method(self):
        from app import dca_backtest as mod
        nav, basic, scale_map = _synthetic_inputs()
        for method in mod.DCA_METHODS:
            rows, rounds = mod.run_backtest_tp(nav, basic, scale_map,
                                               method=method)
            assert rows and all(r["method"] == method for r in rows)
            assert all(t[1] == method for t in rounds)

    def test_default_is_amount(self):
        """缺省 method='amount'：与显式等额调用输出一致（零回归锚）。"""
        from app import dca_backtest as mod
        nav, basic, scale_map = _synthetic_inputs()
        implicit = mod.run_backtest_tp(nav, basic, scale_map)
        explicit = mod.run_backtest_tp(nav, basic, scale_map, method="amount")
        assert implicit[0] == explicit[0]
        assert implicit[1] == explicit[1]

    def test_rounds_tuple_shape(self):
        """轮次元组列序锚：(code, method, tp, fee, seq, *ev) 共 14 位。"""
        from app import dca_backtest as mod
        nav, basic, scale_map = _synthetic_inputs()
        _, rounds = mod.run_backtest_tp(nav, basic, scale_map, method="share")
        assert rounds and all(len(t) == 14 for t in rounds)
        assert all(t[2] in mod.TP_THRESHOLDS and t[3] in ("gross", "disc",
                                                          "nom")
                   for t in rounds)

    def test_share_differs_from_amount_on_trending_nav(self):
        """持续上行序列上两种方式结果确实不同（防'参数没接上'假绿）。"""
        from app import dca_backtest as mod
        nav, basic, scale_map = _synthetic_inputs()
        amt_rows, _ = mod.run_backtest_tp(nav, basic, scale_map,
                                          method="amount")
        shar_rows, _ = mod.run_backtest_tp(nav, basic, scale_map,
                                           method="share")
        a = [r for r in amt_rows if r["code"] == "000001" and r["tp"] == 0.20]
        s = [r for r in shar_rows if r["code"] == "000001" and r["tp"] == 0.20]
        assert a[0]["full_invested"] != s[0]["full_invested"]


# ===== run_full_backtest_tp：日更双 method 编排 =====

class TestRunFullBacktestShare:
    def _run(self, monkeypatch, nav):
        from app import dca_backtest as mod
        _, basic, scale_map = _synthetic_inputs()

        monkeypatch.setattr(mod, "_load_codes",
                            lambda conn: sorted(nav["code"].unique()))
        monkeypatch.setattr(mod, "_load_basic",
                            lambda conn, codes: basic.loc[codes])
        monkeypatch.setattr(mod, "_load_scale", lambda conn: scale_map)
        monkeypatch.setattr(mod, "_load_nav",
                            lambda conn, codes:
                            nav[nav["code"].isin(codes)].reset_index(drop=True))

        class _Ctx:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(mod.psycopg, "connect",
                            lambda *a, **k: _Ctx())
        return mod.run_full_backtest_tp("postgresql://fake")

    def test_tp_rows_cover_both_methods(self, monkeypatch):
        """日更产出双 method：tp 行数 = 基金×阈值×2，旧口径行仍按
        amount 派生且不带 method 键。"""
        from app import dca_backtest as mod
        nav, _, _ = _synthetic_inputs()
        old_rows, old_rounds, tp_rows, tp_rounds, _t, _r, stats = \
            self._run(monkeypatch, nav)
        assert len(tp_rows) == 2 * len(mod.TP_THRESHOLDS) * len(mod.DCA_METHODS)
        methods = {r["method"] for r in tp_rows
                   if r["code"] == "000001" and r["tp"] == 0.20}
        assert methods == set(mod.DCA_METHODS)
        assert stats["methods"] == list(mod.DCA_METHODS)
        # 旧口径：amount、tp=0.20 派生，剥掉 method 键（旧表无此列）
        assert len(old_rows) == 2
        assert all("method" not in r and "tp" not in r for r in old_rows)
        # 旧轮次元组剥掉 method/tp 后形状不变：(code, fee, seq, *ev) 共 12 位
        assert all(len(t) == 12 for t in old_rounds)
        assert all(t[1] in ("gross", "disc", "nom") for t in old_rounds)

    def test_share_rows_get_alive_flag(self, monkeypatch):
        """alive（基金级）对两 method 行同一份。"""
        from app import dca_backtest as mod
        nav, _, _ = _synthetic_inputs()
        _old, _or, tp_rows, _tr, _t, _r, _s = self._run(monkeypatch, nav)
        by = {(r["code"], r["method"]): r["alive"] for r in tp_rows}
        for code in ("000001", "000002"):
            assert by[(code, "amount")] == by[(code, "share")]


# ===== 存储：method 列迁移 + 写入 + 间隔聚合 =====

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


class FakeStoreConn:
    def __init__(self):
        self.cursor_obj = FakeCursor(self)

    def cursor(self):
        return self.cursor_obj

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _tp_row(code="000001", tp=0.15, method="amount"):
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
         "full_last_tp": None, "full_last_tp_disc": None,
         "full_last_tp_nom": None, "full_cur_invested": None,
         "full_cur_profit_rate": None, "full_cur_profit_rate_disc": None,
         "full_cur_profit_rate_nom": None,
         "full_cur_xirr": None, "full_cur_xirr_disc": None,
         "full_cur_xirr_nom": None,
         "tp": tp, "method": method}
    return r


class TestShareStore:
    def test_ensure_tp_schema_method_migration(self):
        """迁移三件套：两表 ALTER 加 method 列（存量行归 amount，不丢数据）、
        PK 就地纳入 method（守护式，新装表天然含）、method 维度索引。"""
        from app.dca_store import ensure_tp_schema
        conn = FakeStoreConn()
        ensure_tp_schema(conn)
        ddl = [sql for sql, _ in conn.cursor_obj.executed][0]
        for table in ("fund_dca_tp_result", "fund_dca_tp_rounds"):
            assert (f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS method"
                    in ddl), table
        assert "method VARCHAR(16) NOT NULL DEFAULT 'amount'" in ddl
        # 加宽守护（四轮：dd_ladder 9 字符超初版 VARCHAR(8)，实跑踩过）
        assert "ALTER COLUMN method TYPE VARCHAR(16)" in ddl
        assert "ADD PRIMARY KEY (code, tp, method)" in ddl
        assert "ADD PRIMARY KEY (code, tp, method, fee, seq)" in ddl
        assert "DROP CONSTRAINT fund_dca_tp_result_pkey" in ddl
        assert "DROP CONSTRAINT fund_dca_tp_rounds_pkey" in ddl
        assert "idx_fund_dca_tp_method" in ddl
        # 顺序守护：method 索引只许出现在 ALTER 加列之后——存量表上先建
        # 索引会 UndefinedColumn（2026-11-23 生产首跑踩过，索引一度被放在
        # CREATE TABLE 段，IF NOT EXISTS 建表对旧表不生效）
        assert ddl.count("idx_fund_dca_tp_method") == 1
        assert ddl.rindex("idx_fund_dca_tp_method") > ddl.rindex(
            "ALTER TABLE fund_dca_tp_rounds ADD COLUMN IF NOT EXISTS method")

    def test_tp_cols_include_method(self):
        from app.dca_store import _TP_COLS, _COLS
        assert _TP_COLS[:2] == ["tp", "method"]
        assert _TP_COLS[2:] == _COLS

    def test_tp_rounds_cols_align_with_engine_tuple(self):
        """列序守护：INSERT 列清单与引擎元组 (code, method, tp, fee, seq,
        *ev) 逐位对齐——错位会被 PG 隐式类型转换'成功'吞掉。"""
        from app.dca_store import _TP_ROUNDS_COLS, _ROUNDS_COLS
        assert _TP_ROUNDS_COLS[:3] == ["code", "method", "tp"]
        assert _TP_ROUNDS_COLS[3:] == _ROUNDS_COLS[1:]
        assert len(_TP_ROUNDS_COLS) == 14

    def test_import_tp_rows_carries_method(self):
        from app.dca_store import import_tp_rows
        conn = FakeStoreConn()
        rows = [_tp_row(method="amount"), _tp_row(method="share")]
        import_tp_rows(conn, rows, params={"methods": ["amount", "share"]})
        sql, seq = conn.cursor_obj.batch[0]
        assert "INSERT INTO fund_dca_tp_result" in sql
        assert "method" in sql
        # 行参数逐位对齐 _TP_COLS（method 值在列清单对应位置）
        from app.dca_store import _TP_COLS
        assert len(seq) == 2
        assert all(len(row) == len(_TP_COLS) for row in seq)
        assert {row[_TP_COLS.index("method")] for row in seq} == \
            {"amount", "share"}

    def test_import_tp_rounds_gap_groups_by_method(self):
        """间隔聚合按 (code, tp, method) 分组并按 method 关联回填。"""
        from app.dca_store import import_tp_rounds
        conn = FakeStoreConn()
        rows = [("000001", "share", 0.15, "nom", 1, "2024-02-10", 74, 74,
                 0.213, 1.213, 5000.0, 36.9, 12.8, 1050.3)]
        import_tp_rounds(conn, rows)
        sqls = [sql for sql, _ in conn.cursor_obj.executed]
        apply_sql = [s for s in sqls
                     if "gap_avg_days = t.g_avg" in s][0]
        assert "GROUP BY code, tp, method" in apply_sql
        assert "r.code = t.code AND r.tp = t.tp AND r.method = t.method" \
            in apply_sql


# ===== API：method 白名单与 SQL 过滤 =====

def _patch_connect(monkeypatch, module, conn):
    def fake_connect(*a, **k):
        return conn

    monkeypatch.setattr(module.psycopg, "connect", fake_connect)


class FakeConnOne:
    """fetchone/fetchall 可配置的最小连接桩（psycopg.connect 替身）。"""

    def __init__(self, one, rows=None):
        self.one = one
        self.rows = rows or []
        self.executed: list = []
        self.params: list = []

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, query, params=None):
        self.executed.append(query)
        self.params.append(params)

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.rows


class TestShareRoutes:
    def test_tp_list_degrades(self, client, monkeypatch):
        monkeypatch.setattr("app.config.DATABASE_URL", DEAD_DSN)
        resp = client.get("/api/funds/dca/tp", params={"method": "share"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "down" and body["rows"] == []
        assert body["method"] == "share"

    def test_method_whitelist_fallback(self, client, monkeypatch):
        """method 白名单：share 精确通过；乱值/缺省回落 amount 并回显。"""
        import app.dca_api as dca_mod
        conn = FakeConnOne((None, None, None))
        _patch_connect(monkeypatch, dca_mod, conn)
        resp = client.get("/api/funds/dca/tp", params={"method": "share"})
        assert resp.json()["method"] == "share"
        for bad in ("dca", "AMOUNT", ""):
            resp = client.get("/api/funds/dca/tp", params={"method": bad})
            assert resp.json()["method"] == "amount", bad
        resp = client.get("/api/funds/dca/tp")
        assert resp.json()["method"] == "amount"

    def test_tp_list_sql_filters_method(self, client, monkeypatch):
        """列表/计数/统计三 SQL 均带 method 过滤。"""
        import app.dca_api as dca_mod
        conn = FakeConnOne((None, None, None))
        _patch_connect(monkeypatch, dca_mod, conn)
        client.get("/api/funds/dca/tp",
                   params={"tp": "0.15", "method": "share"})
        hits = [s for s in conn.executed if "method = %s" in s]
        assert len(hits) >= 3
        assert any("r.method = %s" in s for s in hits)
        # 占位参数确实传了 'share'（防只拼串不传参）
        assert any("share" in (p or ()) for p in conn.params)

    def test_compare_carries_method(self, client, monkeypatch):
        import app.dca_api as dca_mod
        conn = FakeConnOne([])
        _patch_connect(monkeypatch, dca_mod, conn)
        resp = client.get("/api/funds/dca/tp/compare",
                          params={"method": "share"})
        assert resp.json()["method"] == "share"
        cmp_sql = [s for s in conn.executed if "GROUP BY tp" in s]
        assert cmp_sql and "method = %s" in cmp_sql[0]

    def test_tp_rounds_method_param(self, client, monkeypatch):
        import app.dca_api as dca_mod
        conn = FakeConnOne([])
        _patch_connect(monkeypatch, dca_mod, conn)
        resp = client.get("/api/funds/dca/tp/000001/rounds",
                          params={"tp": "0.30", "method": "share"})
        body = resp.json()
        assert body["method"] == "share" and body["tp"] == 0.30
        rounds_sql = [s for s in conn.executed
                      if "fund_dca_tp_rounds" in s][0]
        assert "method = %s" in rounds_sql

    def test_tp_summary_method_param(self, client, monkeypatch):
        import app.dca_api as dca_mod
        conn = FakeConnOne((None,))
        _patch_connect(monkeypatch, dca_mod, conn)
        resp = client.get("/api/funds/dca/tp/000001/summary",
                          params={"method": "share"})
        assert resp.json()["method"] == "share"
        assert any("method = %s" in s for s in conn.executed)

    def test_methods_pin_shared_with_engine(self):
        """API 与引擎共用同一 DCA_METHODS 事实源（防两处漂移）。"""
        import app.dca_api as dca_mod
        from app.dca_backtest import DCA_METHODS
        assert dca_mod.DCA_METHODS == DCA_METHODS == \
            ("amount", "share", "dd_ladder", "dd_ladder_deep",
             "profit_dd_ladder", "pl_ladder")


class TestShareScatter:
    """散点/分箱跟随定投方式（2026-11-23 用户需求：切换后所有统计跟着变）：
    share 走 fund_dca_tp_result（tp=0.2 固定档）；amount/乱值保持旧表零回归。"""

    def test_scatter_share_targets_tp_table(self, client, monkeypatch):
        import app.dca_api as dca_mod
        conn = FakeConnOne((None,), rows=[])
        _patch_connect(monkeypatch, dca_mod, conn)
        resp = client.get("/api/funds/dca/scatter", params={"method": "share"})
        assert resp.status_code == 200
        assert resp.json()["method"] == "share"
        assert "FROM fund_dca_tp_result" in " ".join(conn.executed)
        assert "tp = %s" in " ".join(conn.executed)
        assert "method = %s" in " ".join(conn.executed)
        assert any("share" in (p or ()) for p in conn.params), conn.params

    def test_scatter_amount_keeps_legacy_table(self, client, monkeypatch):
        """缺省/amount/乱值三种都走旧表（fund_dca_result，零回归）。"""
        import app.dca_api as dca_mod
        conn = FakeConnOne((None,), rows=[])
        _patch_connect(monkeypatch, dca_mod, conn)
        client.get("/api/funds/dca/scatter")
        client.get("/api/funds/dca/scatter", params={"method": "amount"})
        client.get("/api/funds/dca/scatter", params={"method": "bogus"})
        # 每次请求 = ref 查询 + 主查询共 2 条；主查询恒走旧表
        main_sqls = [s for s in conn.executed if s.startswith("SELECT code,")]
        assert len(main_sqls) == 3
        assert all("fund_dca_tp_result" not in s for s in main_sqls)
        assert all("FROM fund_dca_result" in s for s in main_sqls)

    def test_scatter_degrades_with_method_echo(self, client, monkeypatch):
        monkeypatch.setattr("app.config.DATABASE_URL", DEAD_DSN)
        resp = client.get("/api/funds/dca/scatter", params={"method": "share"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "down" and body.get("method") == "share"

    def test_scatter_follows_tp(self, client, monkeypatch):
        """散点跟随止盈点（三轮需求）：非 20% 档（任意 method）走 tp 表
        且 tp/method 参数真传；amount+20%（缺省/乱值回落）仍走旧表。"""
        import app.dca_api as dca_mod
        conn = FakeConnOne((None,), rows=[])
        _patch_connect(monkeypatch, dca_mod, conn)
        resp = client.get("/api/funds/dca/scatter",
                          params={"tp": "0.25", "method": "amount"})
        assert resp.json()["tp"] == 0.25
        main = [s for s in conn.executed if s.startswith("SELECT code,")][-1]
        assert "FROM fund_dca_tp_result" in main
        assert any(0.25 in (p or ()) for p in conn.params), conn.params

        conn2 = FakeConnOne((None,), rows=[])
        _patch_connect(monkeypatch, dca_mod, conn2)
        client.get("/api/funds/dca/scatter", params={"tp": "0.15"})
        main2 = [s for s in conn2.executed if s.startswith("SELECT code,")][-1]
        assert "FROM fund_dca_tp_result" in main2
        assert any(0.15 in (p or ()) for p in conn2.params), conn2.params

    def test_scatter_tp_fallback_keeps_legacy(self, client, monkeypatch):
        """乱值 tp 回落 0.2：amount → 旧表；share → tp 表 0.2 档。"""
        import app.dca_api as dca_mod
        conn = FakeConnOne((None,), rows=[])
        _patch_connect(monkeypatch, dca_mod, conn)
        resp = client.get("/api/funds/dca/scatter", params={"tp": "0.99"})
        assert resp.json()["tp"] == 0.2
        main = [s for s in conn.executed if s.startswith("SELECT code,")][-1]
        assert "FROM fund_dca_result" in main and "tp = %s" not in main

        conn2 = FakeConnOne((None,), rows=[])
        _patch_connect(monkeypatch, dca_mod, conn2)
        client.get("/api/funds/dca/scatter",
                   params={"tp": "abc", "method": "share"})
        main2 = [s for s in conn2.executed if s.startswith("SELECT code,")][-1]
        assert "FROM fund_dca_tp_result" in main2 and "tp = %s" in main2
        assert any(0.2 in (p or ()) and "share" in (p or ())
                   for p in conn2.params), conn2.params
