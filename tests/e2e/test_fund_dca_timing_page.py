"""定投时机视图 E2E：mock /api/funds/dca/timing（回撤桶 × 历史素质）。

用例表（确认门 1：自主开发模式并入最终前端过目，同 tp 页约定）：
- E2E-435 P0 区块渲染：默认甜蜜点（band=25-30）请求 + 表格行渲染
  （当前回撤 -27.1%、距前高 340 天、年化 +92.0%）；
- E2E-436 桶切换与排序：切「30%+ 警戒」→ 请求 band=30+；点年化列头 →
  sort 参数变化；点行「详情」→ 复用止盈明细弹窗。
运行：python -m pytest tests/e2e/test_fund_dca_timing_page.py
"""

import json
import re
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import expect

from test_fund_dca_page import mock_dca, open_dca
from test_fund_dca_tp_page import mock_tp, mock_rounds_navs

TIMING_BODY = {
    "status": "up", "tp": 0.2, "band": "25-30", "total": 2, "limit": 50,
    "offset": 0, "fee": "nom", "equity": True,
    "rows": [
        {"code": "019387", "name": "中欧时代共赢混合发起A1",
         "fund_type": "混合型-偏股", "alive": True, "dd_current": -0.2712,
         "days_since_peak": 340, "dip_band": "25-30", "scale_yi": 1.2,
         "w10_start": "2016-11-14", "w10_end": "2026-11-13",
         "w10_rounds": 11, "gap_avg_days": 95.4, "w10_xirr": 0.92,
         "full_xirr": 1.003, "w10_profit_rate": 0.18,
         "w10_rounds_disc": 11, "w10_rounds_nom": 11,
         "w10_xirr_disc": 0.90, "w10_xirr_nom": 0.98,
         "full_xirr_disc": 0.98, "full_xirr_nom": 1.05,
         "w10_profit_rate_disc": 0.175, "w10_profit_rate_nom": 0.17,
         "gap_avg_days_disc": 94.0, "gap_avg_days_nom": 96.0},
        {"code": "161725", "name": "招商中证白酒指数(LOF)A",
         "fund_type": "指数型-股票", "alive": True, "dd_current": -0.625,
         "days_since_peak": 1380, "dip_band": "30+", "scale_yi": 209.0,
         "w10_start": "2016-10-31", "w10_end": "2026-10-28",
         "w10_rounds": 13, "gap_avg_days": None, "w10_xirr": -0.048,
         "full_xirr": -0.041, "w10_profit_rate": -0.05,
         "w10_rounds_disc": 13, "w10_rounds_nom": 13,
         "w10_xirr_disc": -0.05, "w10_xirr_nom": -0.055,
         "full_xirr_disc": -0.045, "full_xirr_nom": -0.05,
         "w10_profit_rate_disc": -0.06, "w10_profit_rate_nom": -0.07,
         "gap_avg_days_disc": None, "gap_avg_days_nom": None},
    ],
}


@pytest.fixture
def mock_timing(page):
    """mock /api/funds/dca/timing（回显 band 等参数，记录请求 URL）。"""
    calls = []

    def handler(route):
        url = route.request.url
        calls.append(url)
        qs = parse_qs(urlparse(url).query)
        body = {**TIMING_BODY, "band": (qs.get("band") or [""])[0]}
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(body))

    page.route("**/api/funds/dca/timing*", handler)
    return calls


class TestDcaTiming:
    def test_timing_block_renders_with_default_band(self, servers, page,
                                                   mock_dca, mock_tp,
                                                   mock_timing):
        """E2E-435 P0：区块默认甜蜜点请求（band=25-30，tp/fee 随页面口径），
        表格渲染当前回撤/距前高/年化。"""
        _, frontend_port = servers
        open_dca(page, frontend_port)
        expect(page.get_by_role("heading",
                                name="定投时机")).to_be_visible()
        timing_calls = [u for u in mock_timing if "band=25-30" in u]
        assert timing_calls, mock_timing
        assert "tp=0.2" in timing_calls[-1] and "fee=nom" in timing_calls[-1]
        row = page.locator(".timing-block tbody tr", has_text="019387")
        expect(row).to_contain_text("-27.1%")      # 当前回撤
        expect(row).to_contain_text("340 天")       # 距前高
        expect(row).to_contain_text("+98.0%")       # 年化 nom 后缀

    def test_timing_band_switch_and_sort_and_detail(self, servers, page,
                                                    mock_dca, mock_tp,
                                                    mock_timing,
                                                    mock_rounds_navs):
        """E2E-436：切「30%+ 警戒」→ band=30+ 请求；点年化列头 → sort 变化；
        点行「详情」→ 复用止盈明细弹窗。"""
        _, frontend_port = servers
        open_dca(page, frontend_port)
        expect(page.locator(".timing-block tbody tr",
                            has_text="019387")).to_be_visible()
        page.locator('button[data-timing-band="30+"]').click()
        expect(page.locator(".timing-block tbody tr",
                            has_text="161725")
               ).to_be_visible(timeout=10_000)
        assert any("band=30%2B" in u or "band=30+" in u
                   for u in mock_timing), mock_timing
        page.locator(".timing-block th", has_text="年化·近10年").click()
        expect(page.locator(".timing-block th",
                            has_text="年化·近10年")
               ).to_have_class(re.compile("p-datatable-column-sorted"),
                               timeout=10_000)
        assert any("sort=w10_xirr" in u and "order=asc" in u
                   for u in mock_timing), mock_timing
        page.locator(".timing-block tbody tr", has_text="161725") \
            .locator(".detail-btn").click()
        dlg = page.locator(".p-dialog", has_text="每次止盈明细")
        expect(dlg).to_be_visible()
