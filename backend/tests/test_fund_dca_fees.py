"""基金定投止盈回测的费用建模单测。

被测对象是 backend/app/dca_backtest.py——回测核心（脚本与调度器日更共用）。

费用口径：
- 申购费外扣法：净申购 = 金额/(1+费率)，买入份额按净申购折算，现金流仍记 -全额；
- 赎回费按每笔份额持有天数套阶梯，止盈/期末清算现金流为 Σ 份额市值×(1-费率)；
- 三场景：gross（无费，回归现状）/ disc（申购名义×0.1 一折）/ nom（申购名义）；
  赎回费三场景一致不折扣。

测试夹具的净值序列：自然日连涨，月内首日买入 → 2024-01 起的月首日序号
[0, 31, 60, 91, 121, ...]（闰年 2 月 29 天）。
"""

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import dca_backtest as mod  # noqa: E402

# 2024-01-01 起自然日序列里的「每月首个净值日」下标（2024 闰年）
MONTH_STARTS = [0, 31, 60, 91, 121, 152, 182, 213, 243, 274, 304, 334,
                365, 396]   # 365=2025-01-01


def _daily_adj(n: int, bumps: dict[int, float]) -> tuple[pd.DatetimeIndex, np.ndarray]:
    """n 个自然日净值日；bumps={日序: 当日涨幅}，其余日 0 涨跌。"""
    dates = pd.DatetimeIndex(pd.date_range("2024-01-01", periods=n, freq="D"))
    g = np.zeros(n)
    for i, r in bumps.items():
        g[i] = r
    return dates, np.cumprod(1.0 + g)


# ---------- 费率表与场景选择 ----------

class TestFeeSchedules:
    def test_sub_rate_by_type(self):
        assert mod.sub_rate_for("混合型-偏股") == 0.015
        assert mod.sub_rate_for("股票型-普通") == 0.015
        assert mod.sub_rate_for("指数型-股票") == 0.015
        assert mod.sub_rate_for("债券型-长债") == 0.008
        assert mod.sub_rate_for("QDII") == 0.015
        assert mod.sub_rate_for("货币型-普通") == 0.0

    def test_sub_rate_unknown_defaults_equity(self):
        assert mod.sub_rate_for("") == 0.015
        assert mod.sub_rate_for(None) == 0.015
        assert mod.sub_rate_for("另类型-其他") == 0.015

    def test_c_class_detection(self):
        assert mod.is_c_class("中欧医疗健康混合C")
        assert mod.is_c_class("XX纯债债券发起C ")
        assert not mod.is_c_class("永赢先锋半导体智选混合发起A")
        assert not mod.is_c_class("华夏成长混合")

    def test_schedule_shape(self):
        s = mod.fee_schedules_for("华夏成长混合", "混合型-偏股")
        assert set(s) == {"gross", "disc", "nom"}
        assert s["gross"] is None
        assert s["disc"].sub_rate == pytest.approx(0.015 * 0.1)
        assert s["nom"].sub_rate == pytest.approx(0.015)
        # 赎回费不折扣：disc 与 nom 同阶梯
        assert s["disc"].redeem_tiers == s["nom"].redeem_tiers

    def test_c_class_schedule_zero_sub(self):
        s = mod.fee_schedules_for("中欧医疗健康混合C", "混合型-偏股")
        assert s["nom"].sub_rate == 0.0
        assert s["disc"].sub_rate == 0.0
        assert s["nom"].redeem_tiers == mod.REDEEM_BOND_C   # C 类结构：≥7 天免

    def test_money_fund_schedule_zero_sub(self):
        s = mod.fee_schedules_for("天弘余额宝货币", "货币型-普通")
        assert s["nom"].sub_rate == 0.0
        assert s["nom"].redeem_tiers == mod.REDEEM_BOND_C

    def test_bond_redeem_tiers_short(self):
        a = mod.fee_schedules_for("XX债券A", "债券型-长债")
        c = mod.fee_schedules_for("XX债券C", "债券型-长债")
        assert a["nom"].redeem_tiers == mod.REDEEM_BOND_C
        assert c["nom"].redeem_tiers == mod.REDEEM_BOND_C

    def test_equity_redeem_tiers(self):
        s = mod.fee_schedules_for("华夏成长混合", "混合型-偏股")
        assert s["nom"].redeem_tiers == mod.REDEEM_EQUITY


class TestRedeemRate:
    def test_equity_tiers(self):
        tiers = mod.REDEEM_EQUITY
        assert mod.redeem_rate(0, tiers) == 0.015
        assert mod.redeem_rate(6, tiers) == 0.015
        assert mod.redeem_rate(7, tiers) == 0.005
        assert mod.redeem_rate(364, tiers) == 0.005
        assert mod.redeem_rate(365, tiers) == 0.0025
        assert mod.redeem_rate(729, tiers) == 0.0025
        assert mod.redeem_rate(730, tiers) == 0.0
        assert mod.redeem_rate(3000, tiers) == 0.0

    def test_bond_c_tiers(self):
        tiers = mod.REDEEM_BOND_C
        assert mod.redeem_rate(6, tiers) == 0.015
        assert mod.redeem_rate(7, tiers) == 0.0


# ---------- 模拟：现金流与份额 ----------

class TestSimulateAccounting:
    def test_gross_matches_legacy(self):
        """fee=None 与旧口径一致：Jan/Feb 各买 1000 份（净值 1.0），
        day45 +20% 市值 2400 达到 2000×1.2 触发止盈。"""
        dates, adj = _daily_adj(46, {45: 0.20})
        res = mod.simulate(dates, adj)
        assert res["rounds"] == 1
        assert res["invested"] == 2000.0
        assert res["flows"][0] == (dates[0], -1000.0)
        assert res["flows"][1] == (dates[31], -1000.0)
        assert res["flows"][2][1] == pytest.approx(2400.0)

    def test_zero_fee_schedule_identical_to_gross(self):
        """0 费率调度的模拟结果必须与 gross 完全一致（费用管线不扰动无费路径）。"""
        dates, adj = _daily_adj(46, {45: 0.20})
        r0 = mod.simulate(dates, adj)
        rz = mod.simulate(dates, adj, mod.FeeSchedule(sub_rate=0.0, redeem_tiers=()))
        assert r0["flows"] == rz["flows"]
        assert r0["rounds"] == rz["rounds"] == 1

    def test_sub_fee_external_deduction(self):
        """外扣法：净申购 1000/1.015 折份额（两笔），现金流仍各记 -1000；
        day45 +25% 触发止盈，持有 45 天（7≤d<365）赎回费 0.5%。"""
        dates, adj = _daily_adj(46, {45: 0.25})
        fee = mod.FeeSchedule(sub_rate=0.015, redeem_tiers=mod.REDEEM_EQUITY)
        res = mod.simulate(dates, adj, fee)
        assert res["rounds"] == 1
        assert res["invested"] == 2000.0
        expect = 2 * (1000.0 / 1.015) * 1.25 * (1 - 0.005)
        tp = [a for _, a in res["flows"] if a > 0]
        assert tp == [pytest.approx(expect)]

    def test_punitive_redeem_within_7_days(self):
        """止盈落在最近一笔买入 7 天内：该笔按 1.5%，旧笔按 0.5%。

        day0、day31 各买 1000 份（净值 1.0）；day33 +25% → 市值 2500
        ≥ 2000×1.2 触发止盈。day0 笔持有 33 天（0.5%），day31 笔 2 天（1.5%）。
        """
        dates, adj = _daily_adj(40, {33: 0.25})
        fee = mod.FeeSchedule(sub_rate=0.0, redeem_tiers=mod.REDEEM_EQUITY)
        res = mod.simulate(dates, adj, fee)
        assert res["rounds"] == 1
        expect = 1250.0 * (1 - 0.005) + 1250.0 * (1 - 0.015)
        tp_flows = [a for _, a in res["flows"] if a > 0]
        assert tp_flows == [pytest.approx(expect)]

    def test_end_liquidation_fee_per_lot(self):
        """不止盈到期清算：14 笔月买按各自持有天数套阶梯（三档全出现）。

        day398 +10% → adj[399]=1.1，无止盈；期末清算 day399。
        持有天数 = 399 - 买入日：399/368 天→0.25%，3→1.5%，其余→0.5%。
        """
        n = 400
        dates, adj = _daily_adj(n, {398: 0.10})
        fee = mod.FeeSchedule(sub_rate=0.0, redeem_tiers=mod.REDEEM_EQUITY)
        res = mod.simulate(dates, adj, fee)
        assert res["rounds"] == 0
        starts = [i for i in MONTH_STARTS if i <= 399]
        expect = sum(1000.0 * 1.1 * (1 - mod.redeem_rate(399 - i, mod.REDEEM_EQUITY))
                     for i in starts)
        assert len(starts) == 14
        assert res["flows"][-1][1] == pytest.approx(expect)

    def test_xirr_ordering_gross_gt_disc_gt_nom(self, monkeypatch):
        """盈利场景：不含费 ≥ 一折 ≥ 名义，且费用拖累严格存在。"""
        monkeypatch.setattr(mod, "MIN_SPAN_DAYS", 0)   # 短窗口夹具绕过年化下限
        dates, adj = _daily_adj(46, {45: 0.25})
        s = mod.fee_schedules_for("华夏成长混合", "混合型-偏股")
        xs = [mod.simulate(dates, adj, s[k])["xirr"] for k in ("gross", "disc", "nom")]
        assert xs[0] >= xs[1] >= xs[2]
        assert xs[0] > xs[2]

    def test_xirr_math_consistent(self, monkeypatch):
        """单笔单轮现金流 XIRR 与闭式解一致：day0 买入，day15 +25% 止盈。"""
        monkeypatch.setattr(mod, "MIN_SPAN_DAYS", 0)
        dates, adj = _daily_adj(16, {15: 0.25})
        res = mod.simulate(dates, adj)
        assert res["rounds"] == 1
        closed = (res["flows"][1][1] / 1000.0) ** (365.25 / 15) - 1
        assert res["xirr"] == pytest.approx(closed, rel=1e-4)

    def test_money_fund_scenarios_identical(self):
        """货币类三场景费率全零 → 年化一致。"""
        dates, adj = _daily_adj(46, {45: 0.25})
        s = mod.fee_schedules_for("天弘余额宝货币", "货币型-普通")
        xs = {k: mod.simulate(dates, adj, s[k])["xirr"] for k in s}
        assert xs["gross"] == pytest.approx(xs["disc"])
        assert xs["gross"] == pytest.approx(xs["nom"])

    def test_multi_round(self):
        """两轮止盈场景：轮次与现金流序列正确。"""
        dates, adj = _daily_adj(130, {20: 0.20, 25: -0.05, 120: 0.30})
        fee = mod.FeeSchedule(sub_rate=0.0, redeem_tiers=mod.REDEEM_EQUITY)
        res = mod.simulate(dates, adj, fee)
        assert res["rounds"] == 2
        # 正现金流 = 2 次止盈 + 1 次期末清算（末笔为清算款）
        assert len([a for _, a in res["flows"] if a > 0]) == 3

    def test_short_span_xirr_none(self):
        """窗口 <90 天年化仍为 None（回归）。"""
        dates, adj = _daily_adj(30, {29: 0.20})
        res = mod.simulate(dates, adj)
        assert res["xirr"] is None


class TestRoundEventFields:
    def test_round_event_fields_with_fees(self, monkeypatch):
        """轮次事件 9 元组：止盈净值/投入/申购费/赎回费/净收益数值正确。"""
        monkeypatch.setattr(mod, "MIN_SPAN_DAYS", 0)
        dates, adj = _daily_adj(46, {45: 0.25})
        fee = mod.FeeSchedule(sub_rate=0.015, redeem_tiers=mod.REDEEM_EQUITY)
        res = mod.simulate(dates, adj, fee)
        assert res["rounds"] == 1
        ev = res["round_events"][0]
        assert ev[0] == str(dates[45].date())
        assert ev[1] == 45 and ev[2] == 45
        # 市值口径收益率（脚本按 4 位小数舍入）：含费份额更少
        assert ev[3] == round(2 * (1000 / 1.015) * 1.25 / 2000 - 1, 4)
        assert ev[4] == pytest.approx(1.25, abs=1e-3)     # 止盈日复权净值
        assert ev[5] == 2000.0                            # 本轮投入
        assert ev[6] == pytest.approx(2 * (1000 - 1000 / 1.015), abs=0.01)
        gross_v = 2 * (1000 / 1.015) * 1.25
        assert ev[7] == pytest.approx(gross_v * 0.005, abs=0.01)  # 赎回费 0.5%
        assert ev[8] == pytest.approx(gross_v * 0.995 - 2000, abs=0.01)

    def test_round_event_fields_gross_zero_fees(self):
        """无费场景：两项费用为 0，净收益 = 市值 - 投入。"""
        dates, adj = _daily_adj(46, {45: 0.20})
        res = mod.simulate(dates, adj)
        ev = res["round_events"][0]
        assert ev[6] == 0.0 and ev[7] == 0.0
        assert ev[8] == pytest.approx(400.0)


class TestRunBacktestFeeFields:
    """run_backtest 行携带三口径轮次/总收益率字段（排行表格口径切换数据源）。"""

    def _nav_basic(self):
        dates = pd.date_range("2024-01-01", periods=120, freq="D")
        frames = []
        for code in ("000001", "000002"):
            frames.append(pd.DataFrame({
                "code": [code] * 120,
                "nav_date": dates,
                "unit_nav": [1.0] * 45 + [1.25] * 75,
                "accum_nav": [1.0] * 45 + [1.25] * 75,
                "pct_chg": [0] * 45 + [25.0] + [0] * 74,
            }))
        nav = pd.concat(frames, ignore_index=True)
        basic = pd.DataFrame(
            {"name": ["测试混合A", "测试混合C"],
             "fund_type": ["混合型-偏股", "混合型-偏股"],
             "delisted": [False, False]},
            index=pd.Index(["000001", "000002"], name="code"))
        return nav, basic

    def test_rows_carry_per_fee_rounds_and_profit_rate(self):
        nav, basic = self._nav_basic()
        rows, _ = mod.run_backtest(nav, basic, {}, log=lambda *a: None)
        by_code = {r["code"]: r for r in rows}
        for r in by_code.values():
            # 触发线市值口径三场景共用：轮次一致
            assert r["w10_rounds_disc"] == r["w10_rounds_nom"] == 1
            for k in ("full_rounds_disc", "full_rounds_nom",
                      "full_profit_rate_disc", "full_profit_rate_nom"):
                assert r[k] is not None
            # 费用拖累单调：gross ≥ disc ≥ nom
            assert r["w10_profit_rate"] >= r["w10_profit_rate_disc"] \
                >= r["w10_profit_rate_nom"]
        # A 类有申购费：nom 严格低于 gross；C 类零申购费三者相等
        assert by_code["000001"]["w10_profit_rate_nom"] \
            < by_code["000001"]["w10_profit_rate"]

    def test_c_class_disc_equals_nom(self):
        """C 类零申购费：disc 与 nom 的轮次/收益率完全一致（单测锚定）。"""
        nav, basic = self._nav_basic()
        rows, _ = mod.run_backtest(nav, basic, {}, log=lambda *a: None)
        c = next(r for r in rows if r["code"] == "000002")
        assert c["w10_profit_rate_disc"] == c["w10_profit_rate_nom"]
        assert c["w10_xirr_disc"] == c["w10_xirr_nom"]


class TestRunFullBacktest:
    def test_orchestration_alive_and_rounds(self, monkeypatch):
        """日更编排：拉数→计算→alive 45 天新鲜度→三口径轮次行。"""
        dates = pd.date_range("2024-01-01", periods=120, freq="D")
        nav = pd.DataFrame({
            "code": ["000001"] * 120,
            "nav_date": dates,
            "unit_nav": [1.0] * 45 + [1.25] * 75,
            "accum_nav": [1.0] * 45 + [1.25] * 75,
            "pct_chg": [0] * 45 + [25.0] + [0] * 74,
        })
        basic = pd.DataFrame(
            {"name": ["测试混合A"], "fund_type": ["混合型-偏股"],
             "delisted": [False]},
            index=pd.Index(["000001"], name="code"))
        scale_map = {"000001": {"scale_yi": 5.0, "scale_date": "2026-09-30"}}

        class _NullCM:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(mod, "psycopg",
                            type("P", (), {"connect": lambda *a, **k: _NullCM()}))
        monkeypatch.setattr(mod, "_load_codes", lambda conn: ["000001"])
        monkeypatch.setattr(mod, "_load_basic", lambda conn, codes: basic)
        monkeypatch.setattr(mod, "_load_scale", lambda conn: scale_map)
        monkeypatch.setattr(mod, "_load_nav", lambda conn, codes: nav)
        rows, rounds, stats = mod.run_full_backtest("postgresql://fake")
        assert stats["fund_count"] == 1
        assert stats["window_end"] == "2024-04-29"
        # alive：full_end=max → 距 cutoff 内
        assert rows[0]["alive"] is True
        assert rows[0]["scale_yi"] == 5.0
        # 三口径轮次全入库（gross/disc/nom 各 1 行）
        assert {r[1] for r in rounds} == {"gross", "disc", "nom"}
        assert len(rounds) == 3
