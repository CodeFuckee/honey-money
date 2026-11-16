"""起点条件回测区块 E2E：mock /api/funds/dca/dip-report（静态验证统计）。

用例表（确认门 1：自主开发模式并入最终前端过目，同 tp 页约定）：
- E2E-437 P0 区块渲染：三段统计表（三组对照/深度梯度/起点年份）+ 关键数值
  （DIP 25-30@20%：582 天 / 63.2% / 32.0%）+ 免责文字。
运行：python -m pytest tests/e2e/test_dip_report_page.py
"""

import json

import pytest
from playwright.sync_api import expect

from test_fund_dca_page import mock_dca, open_dca
from test_fund_dca_tp_page import mock_tp

DIP_REPORT_BODY = {
    "status": "up", "tp_list": [0.15, 0.2],
    "groups": [
        {"grp": "SYS", "bucket": "all", "tp": 0.2, "n": 247584,
         "days_median": 650, "days_p25": 328, "days_p75": 1128,
         "success_rate": 0.588, "xirr_median": 0.286,
         "xirr_p25": 0.1, "xirr_p75": 0.5},
        {"grp": "NODIP", "bucket": "all", "tp": 0.2, "n": 131822,
         "days_median": 797, "days_p25": 346, "days_p75": 1355,
         "success_rate": 0.488, "xirr_median": 0.226,
         "xirr_p25": 0.0, "xirr_p75": 0.4},
        {"grp": "DIP", "bucket": "all", "tp": 0.2, "n": 110255,
         "days_median": 738, "days_p25": 355, "days_p75": 1175,
         "success_rate": 0.587, "xirr_median": 0.256,
         "xirr_p25": 0.1, "xirr_p75": 0.45},
    ],
    "buckets": [
        {"grp": "DIP", "bucket": "20-25", "tp": 0.2, "n": 82108,
         "days_median": 719, "days_p25": 345, "days_p75": 1167,
         "success_rate": 0.586, "xirr_median": 0.261,
         "xirr_p25": 0.1, "xirr_p75": 0.45},
        {"grp": "DIP", "bucket": "25-30", "tp": 0.2, "n": 11511,
         "days_median": 582, "days_p25": 297, "days_p75": 1084,
         "success_rate": 0.632, "xirr_median": 0.32,
         "xirr_p25": 0.15, "xirr_p75": 0.5},
    ],
    "years": [
        {"grp": "SYS", "bucket": "2019", "tp": 0.2, "n": 30000,
         "days_median": 500, "days_p25": 300, "days_p75": 900,
         "success_rate": 0.70, "xirr_median": 0.35,
         "xirr_p25": 0.1, "xirr_p75": 0.6},
    ],
}


@pytest.fixture
def mock_dip_report(page):
    def handler(route):
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(DIP_REPORT_BODY))
    page.route("**/api/funds/dca/dip-report*", handler)


class TestDipReport:
    def test_dip_report_block_renders(self, servers, page, mock_dca, mock_tp,
                                      mock_dip_report):
        """E2E-437 P0：起点条件回测区块渲染三段统计表 + 关键数值 + 免责。"""
        _, frontend_port = servers
        open_dca(page, frontend_port)
        expect(page.get_by_role("heading",
                                name="起点条件回测")).to_be_visible()
        # 表1 三组对照：随机基线 650 天 / 非深回撤 797 天 / 深回撤 738 天
        block = page.locator(".dip-report-block")
        expect(block.locator("tr", has_text="随机起点")).to_contain_text("650")
        expect(block.locator("tr", has_text="非深回撤起点")).to_contain_text("797")
        expect(block.locator("tr", has_text="深回撤起点（≥20%）")
               ).to_contain_text("738")
        # 表2 深度梯度：25~30% 甜蜜点 582 天 / 63.2%
        expect(block.locator("tr", has_text="回撤 -25%~-30%")
               ).to_contain_text("582")
        expect(block.locator("tr", has_text="回撤 -25%~-30%")
               ).to_contain_text("63.2%")
        # 表3 起点年份：2019 行 500 天
        expect(block.locator("tr", has_text="2019 年起").first
               ).to_contain_text("500")
        # 免责
        expect(block).to_contain_text("不构成任何操作建议")
