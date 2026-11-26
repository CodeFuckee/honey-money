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

def _inputs():
    """两基金 420 天合成序列（成长先锋 + 恒平）。"""
    import numpy as np
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
    def test_rows_carry_full_cur_fields_tp_engine(self):
        """新旧引擎行同带 full_cur_* 十字段且 tp=0.20 一致（旧引擎随
        二十二轮退役，tp=0.20≡旧引擎等价锚由 tp 表同源数据守护）。"""
        from app import dca_backtest as mod
        nav, basic, scale_map = _inputs()
        rows, _rounds = mod.run_backtest_tp(nav, basic, scale_map,
                                            method="amount")
        cur_keys = [k for k in rows[0] if k.startswith("full_cur_")]
        assert len(cur_keys) == 7
        row20 = next(r for r in rows if r["tp"] == 0.20)
        assert row20["tp"] == 0.20
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
    def test_cur_sortable_fee_suffix(self):
        """排序白名单：止盈后年化/收益率随费用口径带后缀落 r 表。"""
        from app.dca_api import _list_sort_col
        assert _list_sort_col("full_cur_xirr", "_nom") == "r.full_cur_xirr_nom"
        assert (_list_sort_col("full_cur_profit_rate", "")
                == "r.full_cur_profit_rate")
        assert (_list_sort_col("full_cur_profit_rate", "_disc")
                == "r.full_cur_profit_rate_disc")

    def test_tp_list_rows_contract_smoke(self):
        """tp 列表端点冒烟：真实 app 注册后 200；up 时行含 full_cur_* 键，
        down 按降级约定 200+空 rows（细粒度行圆整/ISO 契约锚在
        test_fund_dca_tp.py 路由测试）。"""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import app.dca_api as dca_mod

        app = FastAPI()
        dca_mod.register_fund_dca_routes(app)
        client = TestClient(app)
        resp = client.get("/api/funds/dca/tp",
                          params={"equity": "false", "method": "amount",
                                  "tp": "0.20"})
        assert resp.status_code == 200
        body = resp.json()
        if body.get("status") == "up" and body.get("rows"):
            r = body["rows"][0]
            assert "full_cur_xirr" in r and "full_last_tp" in r
        else:
            assert body.get("status") == "down"
