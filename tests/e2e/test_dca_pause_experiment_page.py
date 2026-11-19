"""熊市断缴实验区块 + 弹窗断缴对照 E2E：mock /api/funds/dca/pause-experiment
与 /api/funds/dca/bear-pause/{code}（2026-11-19 scratch 实验产品化的静态
快照通道；桩值取自真实实验结果）。

用例表（确认门 1：自主开发模式并入最终前端过目，同 tp 页约定）：
- E2E-450 P0 区块渲染：KPI 四卡（94% / -6,229 元 / 0 元 / +5,764 元）+
  分层表「仅断缴过的基金」行（-6,959 元 / 82%）+ 案例行（510300 钱包
  206,093 / 178,336）+ 免责文字。
- E2E-451 口径切换：止盈 20% → 无止盈（躺平），KPI 与分层表数值跟随
  （all 行 B 钱包差中位 -6,229 → -2,419）。
- E2E-452 弹窗断缴对照：排行行打开弹窗 → data-pause-dialog 四卡
  （A/B/C/D 钱包与年化）+ 断缴月数 note；未参与实验（fund=null）整块隐藏。
- E2E-453 接口 down：pause-experiment status=down → 区块降级提示。

运行：python -m pytest tests/e2e/test_dca_pause_experiment_page.py
"""

import json

import pytest
from playwright.sync_api import expect

from test_fund_dca_page import mock_dca, open_dca

# 桩值取自 2026-11-19 真实实验（止盈20% all/paused_only 与无止盈 all 关键数）


def _blk(n, xirr_a, b_d, b_low, c_d, c_low, d_d, d_xlow,
         xirr_b=0.019, xirr_c=0.069, xirr_d=0.101):
    return {"n": n, "med_out_a": 72000.0, "med_wallet_a": 81150.51,
            "xirr_a_med": xirr_a, "xirr_a_p25": 0.0654, "xirr_a_p75": 0.1307,
            "b_xirr_med": xirr_b, "b_xirr_lower_pct": 0.79,
            "b_wallet_delta_med": b_d, "b_wallet_delta_p25": b_d - 5000.0,
            "b_wallet_delta_p75": b_d + 5000.0, "b_wallet_lower_pct": b_low,
            "c_xirr_med": xirr_c, "c_xirr_lower_pct": 0.71,
            "c_wallet_delta_med": c_d, "c_wallet_delta_p25": c_d - 5000.0,
            "c_wallet_delta_p75": c_d + 5000.0, "c_wallet_lower_pct": 0.47,
            "d_xirr_med": xirr_d, "d_xirr_lower_pct": d_xlow,
            "d_wallet_delta_med": d_d + 590.0,
            "d_wallet_delta_p25": d_d - 5000.0, "d_wallet_delta_p75": d_d + 5000.0,
            "d_wallet_lower_pct": 0.0}


PAUSE_EXP_BODY = {
    "status": "up",
    "meta": {"fund_count": 9576, "params": {
        "universe": {"backtested": 9576, "ever_paused_pct": 0.9419,
                     "ever_paused_n": 9020, "data_end": "2026-11-18"},
        "case_codes": ["510300"]}},
    "market": {
        "tp": {"all": _blk(9576, 0.0935, -6229.28, 0.771, 0.0, 0.445, 5174.0, 0.32),
               "paused_only": _blk(9020, 0.094, -6959.0, 0.819, 68.0, 0.472, 5764.0, 0.339),
               "near_high": _blk(767, 0.087, 0.0, 0.443, 0.0, 0.305, 0.0, 0.091),
               "mid": _blk(2966, 0.092, -5678.0, 0.859, -216.0, 0.586, 5156.0, 0.218),
               "stuck": _blk(5843, 0.096, -7410.0, 0.769, 471.0, 0.391, 5924.0, 0.401)},
        "nt": {"all": _blk(9576, 0.026, -2419.0, 0.628, -4584.0, 0.735,
                           1829.0, 0.191, xirr_b=0.001, xirr_c=0.0, xirr_d=0.031),
               "paused_only": _blk(9020, 0.023, -3037.0, 0.666, -5407.0, 0.781, 2447.0, 0.203),
               "near_high": _blk(767, 0.062, 0.0, 0.455, 0.0, 0.455, 0.0, 0.025),
               "mid": _blk(2966, 0.039, -4903.0, 0.855, -5297.0, 0.891, 4313.0, 0.11),
               "stuck": _blk(5843, -0.002, -692.0, 0.535, -5268.0, 0.693, -102.0, 0.254)},
    },
    "cases": [{
        "code": "510300", "name": "沪深300ETF华泰柏瑞", "fund_type": "指数型-股票",
        "span_days": 5290, "months": 248, "ever_paused": True, "episodes": 6,
        "pause_months": 118, "dd_end": -0.209, "dd_min": -0.48, "in_universe": True,
        "tp_a_wallet": 206092.97, "tp_a_xirr": 0.125, "tp_a_out": 174000.0, "tp_a_rounds": 6,
        "tp_b_wallet": 178335.52, "tp_b_xirr": 0.005, "tp_b_out": 174000.0, "tp_b_rounds": 3,
        "tp_c_wallet": 188018.0, "tp_c_xirr": 0.041, "tp_c_out": 174000.0, "tp_c_rounds": 3,
        "tp_d_wallet": 347925.0, "tp_d_xirr": 0.136, "tp_d_out": 292000.0, "tp_d_rounds": 6,
    }],
}

PAUSE_FUND_BODY = {"status": "up", "fund": {
    "code": "019387", "span_days": 1200, "months": 48, "ever_paused": True,
    "episodes": 2, "pause_months": 18, "dd_end": -0.25, "dd_min": -0.35,
    "in_universe": True,
    "tp_a_wallet": 60000.0, "tp_a_xirr": 0.11, "tp_a_out": 48000.0, "tp_a_rounds": 2,
    "tp_b_wallet": 52000.0, "tp_b_xirr": 0.03, "tp_b_out": 48000.0, "tp_b_rounds": 2,
    "tp_c_wallet": 58500.0, "tp_c_xirr": 0.09, "tp_c_out": 48000.0, "tp_c_rounds": 2,
    "tp_d_wallet": 71000.0, "tp_d_xirr": 0.12, "tp_d_out": 57000.0, "tp_d_rounds": 2,
}}


@pytest.fixture
def mock_pause_exp(page):
    def handler(route):
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(PAUSE_EXP_BODY))
    page.route("**/api/funds/dca/pause-experiment*", handler)


@pytest.fixture
def mock_pause_exp_down(page):
    def handler(route):
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"status": "down", "detail": "db down",
                                       "meta": {}, "market": {}, "cases": []}))
    page.route("**/api/funds/dca/pause-experiment*", handler)


@pytest.fixture
def mock_bear_pause(page):
    def handler(route):
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(PAUSE_FUND_BODY))
    page.route("**/api/funds/dca/bear-pause/*", handler)


@pytest.fixture
def mock_bear_pause_none(page):
    def handler(route):
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"status": "up", "fund": None}))
    page.route("**/api/funds/dca/bear-pause/*", handler)


class TestPauseExperiment:
    def test_pause_exp_block_renders(self, servers, page, mock_dca,
                                     mock_pause_exp, mock_bear_pause):
        """E2E-450 P0：断缴实验区块渲染 KPI 四卡 + 分层表 + 案例行 + 免责。"""
        _, frontend_port = servers
        open_dca(page, frontend_port)
        expect(page.get_by_role("heading", name="熊市断缴实验")).to_be_visible()
        kpis = page.locator("[data-pause-kpis] .lm")
        expect(kpis).to_have_count(4)
        expect(kpis.nth(0)).to_contain_text("94%")
        expect(kpis.nth(1)).to_contain_text("-6,229 元")
        expect(kpis.nth(2)).to_contain_text("0 元")
        expect(kpis.nth(3)).to_contain_text("+5,764 元")
        # 分层表「仅断缴过的基金」主比较行（桩值=真实实验数）
        strata = page.locator("[data-pause-strata]")
        row = strata.locator("tr", has_text="仅断缴过的基金")
        expect(row).to_contain_text("9,020")
        expect(row).to_contain_text("-6,959 元")
        expect(row).to_contain_text("82%")
        expect(row).to_contain_text("+68 元")
        # 案例行：510300 钱包 A/B（取整千分位）
        cases = page.locator("[data-pause-cases]")
        crow = cases.locator("tr", has_text="510300")
        expect(crow).to_contain_text("206,093 元")
        expect(crow).to_contain_text("178,336 元")
        expect(page.locator("[data-pause-exp]")).to_contain_text(
            "不构成任何操作建议")

    def test_pause_exp_mode_switch(self, servers, page, mock_dca,
                                   mock_pause_exp, mock_bear_pause):
        """E2E-451：口径切换止盈 20% → 无止盈（躺平），KPI 与分层表跟随。"""
        _, frontend_port = servers
        open_dca(page, frontend_port)
        expect(page.get_by_role("heading", name="熊市断缴实验")).to_be_visible()
        kpis = page.locator("[data-pause-kpis] .lm")
        expect(kpis.nth(1)).to_contain_text("-6,229 元")
        page.locator("[data-pause-mode] button", has_text="无止盈").click()
        expect(kpis.nth(1)).to_contain_text("-2,419 元")
        expect(kpis.nth(2)).to_contain_text("-4,584 元")
        # 分层表 all 行 A 年化中位 2.6%（躺平口径）
        strata = page.locator("[data-pause-strata]")
        expect(strata.locator("tr", has_text="全体基金")
               ).to_contain_text("2.6%")

    def test_pause_dialog_section(self, servers, page, mock_dca,
                                  mock_pause_exp, mock_bear_pause):
        """E2E-452：排行行打开弹窗 → 断缴对照四卡 + 断缴月数 note。"""
        _, frontend_port = servers
        open_dca(page, frontend_port)
        page.locator(".browse-pv tbody tr", has_text="中欧时代共赢").click()
        expect(page.locator(".p-dialog")).to_be_visible()
        dlg = page.locator("[data-pause-dialog]")
        expect(dlg).to_be_visible()
        expect(dlg).to_contain_text("A 坚持定投")
        expect(dlg).to_contain_text("B 断缴闲置")
        expect(dlg).to_contain_text("C 断缴补投")
        expect(dlg).to_contain_text("D 熊市加倍")
        expect(dlg).to_contain_text("60,000 元")
        expect(dlg).to_contain_text("71,000 元")
        # note：断缴 18 / 共 48 月（2 次）
        expect(page.locator(".p-dialog")).to_contain_text(
            "断缴 18 / 共 48 月（2 次）")

    def test_pause_dialog_hidden_when_not_in_experiment(
            self, servers, page, mock_dca, mock_pause_exp,
            mock_bear_pause_none):
        """E2E-452b：未参与实验（fund=null）→ 断缴对照整块隐藏。"""
        _, frontend_port = servers
        open_dca(page, frontend_port)
        page.locator(".browse-pv tbody tr", has_text="中欧时代共赢").click()
        expect(page.locator(".p-dialog")).to_be_visible()
        expect(page.locator("[data-pause-dialog]")).to_have_count(0)

    def test_pause_exp_down_degrades(self, servers, page, mock_dca,
                                     mock_pause_exp_down, mock_bear_pause):
        """E2E-453：pause-experiment status=down → 区块降级提示。"""
        _, frontend_port = servers
        open_dca(page, frontend_port)
        block = page.locator("[data-pause-exp]")
        expect(block).to_contain_text("断缴实验数据不可用")
