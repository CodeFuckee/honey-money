"""数据浏览页退市列 + 退市过滤 E2E：mock /api/stocks。

用例表（确认门 1 已确认）：
- E2E-301 退市股行显示「已退市」标记；
- E2E-302 在市股（delisted=False 或老接口无该键）该列显示「—」占位；
- E2E-303 默认加载不过滤（请求无 delisted 谓词）；
- E2E-304 过滤下拉切「仅在市」/「仅退市」→ 请求带 delisted=false/true，
  且过滤态下翻页请求仍携带（分页不丢过滤）。
运行：python -m pytest tests/e2e/test_data_page.py
"""

import json
import re

import pytest
from playwright.sync_api import expect

# 3 只股票：在市（False）/ 退市（True）/ 无 delisted 键（老接口兼容）；
# total=101 触发分页（过滤态翻页用例需要下一页可点）
LIST_BODY = {"status": "up", "total": 101, "limit": 50, "offset": 0, "stocks": [
    {"code": "600519", "name": "贵州茅台", "close": 1500.0, "pct_chg": 1.2,
     "last_date": "2026-10-09", "bars": 250, "first_date": "2001-08-27",
     "sources": "sina", "delisted": False},
    {"code": "400001", "name": "退市博信", "close": None, "pct_chg": None,
     "last_date": None, "bars": 5120, "first_date": "1996-01-30",
     "sources": "sina", "delisted": True},
    {"code": "000001", "name": "平安银行", "close": 12.5, "pct_chg": -0.4,
     "last_date": "2026-10-09", "bars": 300, "first_date": "1991-04-03",
     "sources": "sina"},
]}


@pytest.fixture
def mock_list(page):
    """mock /api/stocks；记录列表请求 URL 供过滤参数断言。"""
    calls = []

    def handler(route):
        url = route.request.url
        if re.search(r"/api/stocks(\?|$)", url):
            calls.append(url)
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(LIST_BODY))
        else:
            route.continue_()

    page.route("**/api/stocks**", handler)
    return calls


def open_data(page, frontend_port):
    page.goto(f"http://127.0.0.1:{frontend_port}/")
    page.get_by_role("button", name="数据", exact=True).click()
    expect(page.get_by_role("heading", name="数据浏览")).to_be_visible()


def pick_filter(page, label):
    """打开退市过滤下拉并选中文案为 label 的选项。"""
    page.locator(".delisted-select").click()
    page.locator(".p-select-overlay .p-select-option", has_text=label).click()


class TestDataPageDelistedColumn:
    def test_delisted_row_shows_tag(self, servers, page, mock_list):
        """E2E-301：退市股行「退市」列显示「已退市」标记。"""
        _, frontend_port = servers
        open_data(page, frontend_port)
        row = page.locator(".browse-pv tbody tr", has_text="退市博信")
        expect(row.locator(".delisted-tag")).to_have_text("已退市")

    def test_live_rows_show_placeholder(self, servers, page, mock_list):
        """E2E-302：在市股（False 或无键）该列显示「—」，无标记。"""
        _, frontend_port = servers
        open_data(page, frontend_port)
        for name in ("贵州茅台", "平安银行"):
            row = page.locator(".browse-pv tbody tr", has_text=name)
            expect(row.locator(".delisted-tag")).to_have_count(0)
            expect(row).to_contain_text("—")


class TestDataPageDelistedFilter:
    def test_default_load_no_filter_param(self, servers, page, mock_list):
        """E2E-303：默认（全部）加载请求不带 delisted 参数（兼容旧行为）。"""
        _, frontend_port = servers
        open_data(page, frontend_port)
        assert mock_list, "应发起 /api/stocks 请求"
        assert all("delisted=" not in u for u in mock_list)

    def test_filter_switch_sends_param_and_kept_on_paging(
            self, servers, page, mock_list):
        """E2E-304：切「仅在市」→ delisted=false；再切「仅退市」→ true；
        过滤态翻页，下一页请求仍带 delisted=true（过滤不丢）。"""
        _, frontend_port = servers
        open_data(page, frontend_port)

        pick_filter(page, "仅在市")
        page.wait_for_timeout(500)
        assert any("delisted=false" in u for u in mock_list), mock_list

        pick_filter(page, "仅退市")
        page.wait_for_timeout(500)
        assert any("delisted=true" in u for u in mock_list), mock_list

        # 过滤态翻页：下一页请求仍带过滤参数
        n0 = len(mock_list)
        page.locator(".browse-pv .p-paginator-next").click()
        page.wait_for_timeout(500)
        assert any("delisted=true" in u and "offset=50" in u
                   for u in mock_list[n0:]), mock_list[n0:]
