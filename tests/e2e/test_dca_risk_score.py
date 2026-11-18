"""定投页波动指标回归 + 综合波动分位 E2E。

- 回归（2026-11-18 用户报告表头文字显示两次）：波动指标五列表头文字只出现
  一次——根因是 <Column> 同时带 header 属性与 #header 插槽被双重渲染，
  修复后仅保留插槽（与数据页基金浏览表同款写法）；
- 综合波动分位（risk_score，三指标横截面百分位等权，越低越稳）：排行列
  渲染 / 服务端排序契约 / 详情弹窗指标卡；
- 内容区左右边距收窄（用户反馈折叠侧栏后仍觉边距大）：--content-pad
  左右 40px → 20px。

运行：python -m pytest tests/e2e/test_dca_risk_score.py
"""

import json

import pytest
from playwright.sync_api import expect

from test_fund_dca_page import mock_dca, open_dca   # noqa: F401  (pytest fixture)


class TestHeaderNotDuplicated:
    def test_risk_header_labels_appear_once(self, servers, page, mock_dca):
        """P0 回归：波动指标五列表头文字各只出现一次（修复前双重渲染）。"""
        _, frontend_port = servers
        open_dca(page, frontend_port)
        for label in ("年化波动率", "下行波动率", "最大回撤", "夏普",
                      "波动率分位"):
            th = page.locator(".browse-pv th", has_text=label).first
            expect(th).to_be_visible()
            assert th.inner_text().count(label) == 1, \
                f"表头「{label}」文字重复：{th.inner_text()!r}"


class TestRiskScoreColumn:
    def test_risk_score_renders_pctile(self, servers, page, mock_dca):
        """P0 综合波动分位列：0.4123 → 41%；无指标行 — 占位。"""
        _, frontend_port = servers
        open_dca(page, frontend_port)
        row = page.locator(".browse-pv tbody tr", has_text="中欧时代共赢")
        expect(row).to_contain_text("41%")
        other = page.locator(".browse-pv tbody tr", has_text="招商中证白酒")
        expect(other).to_contain_text("—")

    def test_risk_score_sort_sends_param(self, servers, page, mock_dca):
        """P1 点「综合波动分位」列头 → 服务端排序请求带 sort=risk_score。"""
        _, frontend_port = servers
        open_dca(page, frontend_port)
        page.locator(".browse-pv th", has_text="综合波动分位").click()
        page.wait_for_timeout(400)
        assert any("sort=risk_score" in u for u in mock_dca), mock_dca


class TestRiskScoreDetailCard:
    def test_detail_dialog_shows_risk_score_card(self, servers, page, mock_dca):
        """P0 详情弹窗风险指标区新增「综合波动分位」卡（navs risk 附带）。"""
        _, frontend_port = servers
        page.route("**/api/funds/019387/navs*", lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps({
                "status": "up", "code": "019387", "navs": [],
                "risk": {"vol_1y": 0.185, "vol_down_1y": 0.1201,
                         "mdd_1y": 0.2533, "sharpe_1y": 1.4233,
                         "vol_3y": None, "vol_down_3y": None, "mdd_3y": None,
                         "sharpe_3y": None, "vol_5y": None, "vol_down_5y": None,
                         "mdd_5y": None, "sharpe_5y": None, "vol_all": 0.23,
                         "vol_down_all": 0.15, "mdd_all": 0.42,
                         "sharpe_all": 0.8, "roll_vol": 0.0812,
                         "vol_pctile": 0.6666, "risk_score": 0.4123,
                         "as_of": "2026-10-23",
                         "computed_at": "2026-11-17T06:00:00"}})))
        open_dca(page, frontend_port)
        page.locator(".browse-pv tbody tr", has_text="中欧时代共赢").click()
        expect(page.locator(".p-dialog")).to_be_visible()
        card = page.locator(".p-dialog .dc-item", has_text="综合波动分位")
        expect(card).to_be_visible()
        expect(card).to_contain_text("41%")


class TestOscPctileColumn:
    def test_osc_pctile_renders(self, servers, page, mock_dca):
        """P0 震荡分位列：0.7777 → 78%（越高净值越规律地上上下下）；无指标行 —。"""
        _, frontend_port = servers
        open_dca(page, frontend_port)
        row = page.locator(".browse-pv tbody tr", has_text="中欧时代共赢")
        expect(row).to_contain_text("78%")
        other = page.locator(".browse-pv tbody tr", has_text="招商中证白酒")
        expect(other).to_contain_text("—")

    def test_osc_pctile_sort_sends_param(self, servers, page, mock_dca):
        """P1 点「震荡分位」列头 → 服务端排序请求带 sort=osc_pctile。"""
        _, frontend_port = servers
        open_dca(page, frontend_port)
        page.locator(".browse-pv th", has_text="震荡分位").click()
        page.wait_for_timeout(400)
        assert any("sort=osc_pctile" in u for u in mock_dca), mock_dca
