"""「最近一次止盈后」当前轮统计测试（全史窗口，2026-11-19 用户需求）。

口径（grilling 确认）：
- 全史窗口：上次止盈日在全史回测里取，当前轮 = 上次止盈日重新开始定投
  至窗口末日（进行中），与表格其余收益率列（近10年）口径不同、不可比；
- 未止盈基金（rounds=0）四字段全 None，前端显示 —；
- 年化 XIRR 同窗口口径：<90 天（MIN_SPAN_DAYS）置 None；
- 费用口径：last_tp / cur_profit_rate / cur_xirr 三口径各一份
  （份额折损使触发日与回款位移），cur_invested 与费用无关记一份；
- 存储：fund_dca_result / fund_dca_tp_result 各加 10 列（DDL 幂等迁移）；
- API：排行两端点 SELECT/_LIST_COLS/排序白名单（full_cur_xirr /
  full_cur_profit_rate 随费用口径），summary 端点行内附同组字段
  （弹窗按 tp/fee 切换取值）。
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient

from app.main import create_app  # noqa: E402

from test_fund_dca_tp import (FakeConn, FakeConnOne,  # noqa: E402
                              _patch_connect)

CUR_COLS = ["full_last_tp", "full_last_tp_disc", "full_last_tp_nom",
            "full_cur_invested",
            "full_cur_profit_rate", "full_cur_profit_rate_disc",
            "full_cur_profit_rate_nom",
            "full_cur_xirr", "full_cur_xirr_disc", "full_cur_xirr_nom"]

DEAD_DSN = "postgresql://nobody:x@127.0.0.1:59999/none"


@pytest.fixture
def client():
    return TestClient(create_app())


def _series(growth, start="2024-01-01"):
    """日频复权净值序列：growth 为每日增长率列表。"""
    dates = pd.DatetimeIndex(pd.date_range(start, periods=len(growth),
                                           freq="D"))
    adj = np.cumprod(1.0 + np.asarray(growth, dtype=float))
    return dates, adj


# ===== 引擎：_finalize_tp_state 当前轮字段 =====

class TestSimulateCurrentRound:
    """simulate / simulate_multi 输出的当前轮（最近一次止盈后）四字段。"""

    def test_cur_fields_after_single_tp(self):
        """单轮止盈后：last_tp=止盈日、cur_invested=止盈后月投合计、
        cur_profit_rate=期末市值口径收益、cur_xirr 为正。"""
        from app import dca_backtest as mod
        g = np.zeros(240)
        g[14] = 0.25      # 2024-01-15 +25% 越过 20% 止盈线
        g[-1] = 0.10      # 末日 +10%：当前轮市值口径收益 = +10%
        dates, adj = _series(g)
        res = mod.simulate(dates, adj)
        assert res["rounds"] == 1
        assert res["last_tp"] == "2024-01-15"
        # 止盈后月投：2/1~8/1 每月首个净值日 ×7 = 7000（复权净值恒 1.25，
        # 每月 800 份 ×7）
        assert res["cur_invested"] == pytest.approx(7000.0)
        assert res["cur_profit_rate"] == pytest.approx(0.10)
        # 止盈后约 7.5 个月赚 700 元 → XIRR 为正且有限
        # （资金加权+窗口短，年化偏高属正常——知识库「短窗口年化爆炸」结论）
        assert res["cur_xirr"] is not None and np.isfinite(res["cur_xirr"])
        assert res["cur_xirr"] > 0

    def test_cur_fields_none_when_no_tp(self):
        """从未止盈：四字段全 None（「最近一次止盈后」前提不存在）。"""
        from app import dca_backtest as mod
        dates, adj = _series(np.zeros(200))
        res = mod.simulate(dates, adj)
        assert res["rounds"] == 0
        assert res["last_tp"] is None
        assert res["cur_invested"] is None
        assert res["cur_profit_rate"] is None
        assert res["cur_xirr"] is None

    def test_cur_fields_when_tp_on_last_day(self):
        """末日止盈：轮刚清仓、尚未再投入 → invested 0、收益/年化 None。"""
        from app import dca_backtest as mod
        g = np.zeros(200)
        g[-1] = 0.25
        dates, adj = _series(g)
        res = mod.simulate(dates, adj)
        assert res["rounds"] == 1
        assert res["last_tp"] == str(dates[-1].date())
        assert res["cur_invested"] == 0.0
        assert res["cur_profit_rate"] is None
        assert res["cur_xirr"] is None

    def test_cur_xirr_none_when_short_span(self):
        """止盈后不足 90 天：XIRR 置 None（MIN_SPAN_DAYS 同窗口口径），
        总收益率照常给出。"""
        from app import dca_backtest as mod
        g = np.zeros(75)
        g[14] = 0.25      # 01-15 止盈 → 距末日仅 ~60 天
        dates, adj = _series(g)
        res = mod.simulate(dates, adj)
        assert res["rounds"] == 1
        assert res["cur_profit_rate"] == pytest.approx(0.0)
        assert res["cur_xirr"] is None

    def test_cur_fee_variants(self):
        """含费口径：cur_invested 与费用无关；nom 回款扣赎回费/份额折损 →
        cur_profit_rate 低于 gross；三口径字段齐备。"""
        from app import dca_backtest as mod
        g = np.zeros(240)
        g[14] = 0.25
        g[-1] = 0.10
        dates, adj = _series(g)
        fee = mod.fee_schedules_for("测试混合A", "混合型-偏股")
        res_g = mod.simulate(dates, adj, fee=None)
        res_d = mod.simulate(dates, adj, fee["disc"])
        res_n = mod.simulate(dates, adj, fee["nom"])
        for res in (res_g, res_d, res_n):
            assert set(res) >= {"last_tp", "cur_invested",
                                "cur_profit_rate", "cur_xirr"}
        # +25% 跳空下三口径同日触发（nom 市值 1230 仍越过 1200 触发线）
        assert res_g["last_tp"] == res_d["last_tp"] == res_n["last_tp"]
        assert res_n["cur_invested"] == res_g["cur_invested"]
        assert res_n["cur_profit_rate"] < res_g["cur_profit_rate"]
        assert res_n["cur_xirr"] is not None


# ===== 编排：run_backtest / run_backtest_tp 结果行携带全史当前轮字段 =====

def _nav_frame(growth, code="000001"):
    dates = pd.date_range("2024-01-01", periods=len(growth), freq="D")
    unit = np.cumprod(1.0 + np.asarray(growth, dtype=float))
    return pd.DataFrame({
        "code": code, "nav_date": dates, "unit_nav": unit,
        "accum_nav": unit, "pct_chg": np.asarray(growth, dtype=float) * 100,
    })


class TestRunBacktestCurFields:
    def test_rows_carry_full_cur_fields_both_engines(self):
        """新旧引擎结果行同带 full_cur_* 十字段（tp=0.20≡旧引擎锚点测试
        依赖键集合一致）；w10 不带（口径只收全史）。"""
        from app import dca_backtest as mod
        g = np.zeros(240)
        g[14] = 0.25
        g[-1] = 0.10
        nav = _nav_frame(g)
        basic = pd.DataFrame({"name": ["测试混合A"], "fund_type": ["混合型-偏股"],
                              "delisted": [False]},
                             index=pd.Index(["000001"], name="code"))
        old_rows, _ = mod.run_backtest(nav, basic, {})
        tp_rows, _ = mod.run_backtest_tp(nav, basic, {}, thresholds=(0.20,))
        for row in (*old_rows, *tp_rows):
            assert set(row) >= set(CUR_COLS)
            assert not any(k.startswith("w10_cur_") for k in row)
            assert not any(k.endswith("_cur_xirr") and
                           k.startswith("w10") for k in row)
        tp_row = next(r for r in tp_rows if r["tp"] == 0.20)
        old_row = old_rows[0]
        assert tp_row["full_last_tp"] == old_row["full_last_tp"] == "2024-01-15"
        assert tp_row["full_cur_profit_rate"] == pytest.approx(
            old_row["full_cur_profit_rate"])
        assert tp_row["full_cur_xirr"] == pytest.approx(old_row["full_cur_xirr"])
        assert tp_row["full_cur_invested"] == pytest.approx(7000.0)

    def test_flat_fund_rows_none(self):
        """从未止盈基金：行内十字段全 None（存储写 NULL，前端显示 —）。"""
        from app import dca_backtest as mod
        nav = _nav_frame(np.zeros(200))
        basic = pd.DataFrame({"name": ["测试混合A"], "fund_type": ["混合型-偏股"],
                              "delisted": [False]},
                             index=pd.Index(["000001"], name="code"))
        rows, _ = mod.run_backtest_tp(nav, basic, {}, thresholds=(0.20,))
        row = rows[0]
        for col in CUR_COLS:
            assert row[col] is None, col


# ===== 存储：DDL 迁移与列清单 =====

class TestStoreCurColumns:
    def test_result_ddl_has_cur_columns(self):
        """旧表 DDL 幂等补列（已有部署 ALTER TABLE ADD COLUMN IF NOT EXISTS）。"""
        from app.dca_store import DDL
        for col in CUR_COLS:
            assert f"ADD COLUMN IF NOT EXISTS {col}" in DDL, col

    def test_tp_ddl_has_cur_columns(self):
        """多阈值表同组十列（CREATE 内或 ALTER 幂等补列均可）。"""
        from app.dca_store import TP_DDL
        for col in CUR_COLS:
            assert col in TP_DDL, col

    def test_cols_classification(self):
        """INSERT 列清单含十字段；日期/浮点分类正确（CSV 导入类型转换）。"""
        from app.dca_store import _COLS, _DATE_COLS, _FLOAT_COLS
        for col in CUR_COLS:
            assert col in _COLS, col
        for col in ("full_last_tp", "full_last_tp_disc", "full_last_tp_nom"):
            assert col in _DATE_COLS, col
        for col in ("full_cur_invested", "full_cur_profit_rate",
                    "full_cur_profit_rate_disc", "full_cur_profit_rate_nom",
                    "full_cur_xirr", "full_cur_xirr_disc", "full_cur_xirr_nom"):
            assert col in _FLOAT_COLS, col
            assert col not in _DATE_COLS, col

    def test_import_rows_insert_contains_cur_columns(self):
        """旧表 INSERT 列清单带十字段且元组逐位取值（dict(zip) 对齐）。"""
        from app.dca_store import _COLS, import_rows
        row = {c: None for c in _COLS}
        row.update({"code": "000001", "name": "x", "alive": True,
                    "full_last_tp": "2024-01-15", "full_cur_invested": 7000.0,
                    "full_cur_profit_rate": 0.1, "full_cur_xirr": 0.15})
        conn = FakeConn()
        import_rows(conn, [row], params={})
        sql, seq = conn.cursor_obj.batch[0]
        cols = sql.split("(", 1)[1].split(")", 1)[0].split(", ")
        assert cols == _COLS
        i = cols.index("full_cur_xirr")
        assert seq[0][i] == 0.15
        i = cols.index("full_last_tp")
        assert seq[0][i] == "2024-01-15"

    def test_tp_row_builder_and_insert_align(self):
        """引擎行 dict → tp 表 INSERT：十字段值逐位落位。"""
        from app.dca_backtest import run_backtest_tp
        from app.dca_store import _TP_COLS, import_tp_rows
        g = np.zeros(240)
        g[14] = 0.25
        g[-1] = 0.10
        nav = _nav_frame(g)
        basic = pd.DataFrame({"name": ["测试混合A"], "fund_type": ["混合型-偏股"],
                              "delisted": [False]},
                             index=pd.Index(["000001"], name="code"))
        tp_rows, _ = run_backtest_tp(nav, basic, {}, thresholds=(0.20,))
        for r in tp_rows:            # 引擎行无 alive（日更路径回填），导入需齐列
            r["alive"] = True
        conn = FakeConn()
        import_tp_rows(conn, tp_rows, params={})
        sql, seq = conn.cursor_obj.batch[0]
        cols = sql.split("(", 1)[1].split(")", 1)[0].split(", ")
        assert cols == _TP_COLS
        i = cols.index("full_last_tp")
        assert seq[0][i] == "2024-01-15"


# ===== API：排行两端点与 summary 端点 =====

class TestApiCurColumns:
    def test_list_sql_select_matches_row_mapping(self):
        """两个排行端点 SELECT 列与行映射清单一致（dict(zip) 错位防线）：
        旧端点 = _LIST_COLS，tp 端点 = ["tp"] + _LIST_COLS。"""
        from app.dca_api import _DCA_LIST_SQL, _LIST_COLS, _TP_LIST_COLS, \
            _TP_LIST_SQL

        def select_cols_of(sql):
            seg = sql.split("SELECT", 1)[1].split("FROM", 1)[0]
            return [c.strip().split(".", 1)[-1]
                    for c in seg.split(",") if c.strip()]

        assert select_cols_of(_DCA_LIST_SQL) == _LIST_COLS
        assert select_cols_of(_TP_LIST_SQL) == _TP_LIST_COLS
        for col in CUR_COLS:
            assert col in _LIST_COLS, col

    def test_cur_sortable_fee_suffix(self):
        """排序白名单：止盈后年化/收益率随费用口径带后缀落 r 表。"""
        from app.dca_api import _list_sort_col
        assert _list_sort_col("full_cur_xirr", "_nom") == "r.full_cur_xirr_nom"
        assert (_list_sort_col("full_cur_profit_rate", "")
                == "r.full_cur_profit_rate")
        assert (_list_sort_col("full_cur_profit_rate", "_disc")
                == "r.full_cur_profit_rate_disc")

    def test_list_rows_round_and_iso(self, client, monkeypatch):
        """行后处理：cur 收益率/年化保留 4 位，止盈日转 ISO 日期串。"""
        import datetime as dt

        import app.dca_api as dca_mod
        from app.dca_api import _LIST_COLS
        vals = [None] * len(_LIST_COLS)
        by_name = {c: i for i, c in enumerate(_LIST_COLS)}
        vals[by_name["code"]] = "000001"
        vals[by_name["full_last_tp"]] = dt.date(2025, 6, 1)
        vals[by_name["full_cur_profit_rate"]] = 0.123456
        vals[by_name["full_cur_xirr"]] = -0.987654
        # fetchone 按调用序返回：ref → count → stats → meta
        calls = {"i": 0}

        class SeqConn(FakeConnOne):
            def __init__(self):
                super().__init__(None, rows=[tuple(vals)])
                self.seq = [(dt.date(2026, 11, 19),), (1,),
                            (1, 0.1, 0.2, 0.3, 0.4, 0.9, 0.5),
                            (dt.datetime(2026, 11, 19), 2, {})]

            def execute(self, query, params=None):
                self.executed.append(query)

            def fetchone(self):
                v = self.seq[min(calls["i"], len(self.seq) - 1)]
                calls["i"] += 1
                return v

        conn = SeqConn()
        _patch_connect(monkeypatch, dca_mod, conn)
        resp = client.get("/api/funds/dca", params={"equity": "false"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "up"
        r = body["rows"][0]
        assert r["full_last_tp"] == "2025-06-01"
        assert r["full_cur_profit_rate"] == 0.1235
        assert r["full_cur_xirr"] == -0.9877

    def test_summary_rows_carry_cur_fields(self, client, monkeypatch):
        """summary 行内附十字段（弹窗按 tp/fee 切换取值）：收益率/年化
        4 位、止盈日 ISO。SELECT 列序 = (tp, w10 收益/年化×三口径, full_start,
        cur 十字段)。"""
        import datetime as dt

        import app.dca_api as dca_mod
        row = (0.15, 0.20, 0.12, 0.19, 0.11, 0.18, 0.10,
               dt.date(2016, 11, 16),
               dt.date(2025, 6, 1), dt.date(2025, 6, 1), dt.date(2025, 6, 2),
               7000.0, 0.123456, 0.12, 0.11,
               0.345678, 0.33, 0.32)
        conn = FakeConnOne((dt.date(2026, 11, 19),), rows=[row])
        _patch_connect(monkeypatch, dca_mod, conn)
        resp = client.get("/api/funds/dca/tp/000001/summary")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "up"
        r = body["rows"][0]
        assert r["full_last_tp"] == "2025-06-01"
        assert r["full_last_tp_nom"] == "2025-06-02"
        assert r["full_cur_invested"] == 7000.0
        assert r["full_cur_profit_rate"] == 0.1235
        assert r["full_cur_xirr"] == 0.3457
        assert r["full_cur_xirr_nom"] == 0.32

    def test_list_degrades_with_cur_cols(self, client, monkeypatch):
        """PG 不可达：仍 200 down 空形状（新列不影响降级约定）。"""
        monkeypatch.setattr("app.config.DATABASE_URL", DEAD_DSN)
        resp = client.get("/api/funds/dca/tp", params={"tp": "0.15"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "down" and body["rows"] == []
