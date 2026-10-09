"""个股历史行情详情页 E2E：mock /api/stocks 与 /api/stocks/*/bars。

用例表（确认门 1 已确认）：E2E-201~206，覆盖行点击主流程/返回状态保留/
来源切换/明细分页/无 PG 降级/列表回归。运行：
python -m pytest tests/e2e/test_stock_detail.py
"""

import json
import re

import pytest
from playwright.sync_api import expect

# 列表 mock：2 只股票（total 101 触发分页可翻页）
LIST_BODY = {"status": "up", "total": 101, "limit": 50, "offset": 0, "stocks": [
    {"code": "600519", "name": "贵州茅台", "close": 1500.0, "pct_chg": 1.2,
     "last_date": "2026-10-09", "bars": 250, "first_date": "2001-08-27",
     "sources": "sina"},
    {"code": "600000", "name": "浦发银行", "close": 9.18, "pct_chg": -0.5,
     "last_date": "2026-10-09", "bars": 250, "first_date": "1999-11-10",
     "sources": "sina"},
]}


def bar(date, open_, close, high, low):
    return {"date": date, "open": open_, "close": close, "high": high,
            "low": low, "volume": 805097, "amount": 739740988.0,
            "amplitude": 1.75, "pct_chg": 0.22, "change": 0.02,
            "turnover": 0.24, "source": "sina", "outstanding_share": 3.33e10}


BARS_BODY = {"status": "up", "code": "600519", "count": 3, "bars": [
    bar("2026-10-07", 9.10, 9.16, 9.20, 9.05),
    bar("2026-10-08", 9.16, 9.15, 9.25, 9.09),
    bar("2026-10-09", 9.15, 9.18, 9.25, 9.09),
]}
BARS_EMPTY = {"status": "up", "code": "600519", "count": 0, "bars": []}


@pytest.fixture
def api_calls(page):
    """mock 列表与个股 bars；bars 请求记录 (url) 供断言。

    bars_body["value"] 可换 dict 或 callable(url) -> dict（按请求动态响应，
    分页用例据此按 offset 返回不同页数据）。"""
    calls = []
    bars_body = {"value": BARS_BODY}

    def handler(route):
        req = route.request
        url = req.url
        if re.search(r"/api/stocks/[^/]+/bars", url):
            calls.append(url)
            body = bars_body["value"](url) if callable(bars_body["value"]) \
                else bars_body["value"]
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(body))
        elif re.search(r"/api/stocks(\?|$)", url):
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(LIST_BODY))
        else:
            route.continue_()

    page.route("**/api/stocks**", handler)
    return calls, bars_body


def open_data(page, frontend_port):
    page.goto(f"http://127.0.0.1:{frontend_port}/")
    page.get_by_role("button", name="数据", exact=True).click()
    expect(page.get_by_role("heading", name="数据浏览")).to_be_visible()


class TestStockDetail:
    def test_row_click_opens_detail(self, servers, page, api_calls):
        """E2E-201：点行 → 详情子页（标题/K线 canvas/明细表字段与数据）。"""
        calls, _ = api_calls
        _, frontend_port = servers
        open_data(page, frontend_port)
        page.locator(".browse-pv tbody tr", has_text="贵州茅台").click()

        expect(page.get_by_role("heading", name="600519")).to_be_visible()
        expect(page.get_by_role("heading", name="贵州茅台")).to_be_visible()
        # K 线 canvas（ECharts）渲染
        expect(page.locator(".kchart canvas").first).to_be_visible()
        # 明细表：全字段列头 + 数据行
        for col in ("日期", "开盘", "收盘", "最高", "最低", "涨跌额", "涨跌幅",
                    "振幅", "成交量(手)", "成交额", "换手率", "流通股本"):
            expect(page.locator(".detail-table-card th", has_text=col)).to_be_visible()
        expect(page.locator(".detail-table-card tbody tr")).to_have_count(3)
        expect(page.locator(".detail-table-card tbody tr").first).to_contain_text("2026-10-09")
        # 图表请求带默认来源
        assert any("source=sina" in u for u in calls)

    def test_back_preserves_list_state(self, servers, page, api_calls):
        """E2E-202：搜索词 + 翻页后进详情，返回后列表状态保留。"""
        _, frontend_port = servers
        open_data(page, frontend_port)
        page.get_by_placeholder(re.compile("搜索代码")).fill("600519")
        page.locator(".ph-search").get_by_role("button", name="搜索").click()
        page.locator(".browse-pv .p-paginator-next").click()
        page.locator(".browse-pv tbody tr", has_text="贵州茅台").click()
        expect(page.get_by_role("heading", name="600519")).to_be_visible()

        page.get_by_role("button", name="← 返回列表").click()
        expect(page.get_by_role("heading", name="数据浏览", exact=True)).to_be_visible()
        expect(page.get_by_placeholder(re.compile("搜索代码"))).to_have_value("600519")
        # 页码停在第 2 页（paginator 选中项为 2）
        expect(page.locator(".browse-pv .p-paginator-page.p-paginator-page-selected")
               ).to_have_text("2")

    def test_source_switch_and_empty_state(self, servers, page, api_calls):
        """E2E-203：切东财 → bars 请求带 source=eastmoney；空数据 → 空态文案。"""
        calls, bars_body = api_calls
        _, frontend_port = servers
        open_data(page, frontend_port)
        page.locator(".browse-pv tbody tr", has_text="贵州茅台").click()
        expect(page.locator(".kchart canvas").first).to_be_visible()

        bars_body["value"] = BARS_EMPTY
        page.locator(".src-seg-btn", has_text="东方财富").click()
        assert any("source=eastmoney" in u for u in calls), \
            f"切源后应带 source=eastmoney: {calls}"
        expect(page.locator(".chart-note")).to_contain_text("东方财富源暂无该股K线数据")
        # PrimeVue 空数据渲染一行 empty message（非数据行）
        expect(page.locator(".detail-table-card .browse-empty")
               ).to_contain_text("该源暂无该股行情数据")

    def test_detail_pagination_offset(self, servers, page, api_calls):
        """E2E-204：明细表下一页 → bars 请求 offset=50（mock 按 offset 返回不同页）。"""
        calls, bars_body = api_calls

        def by_offset(url):
            offset = re.search(r"offset=(\d+)", url)
            if offset and offset.group(1) == "50":
                return {**BARS_BODY, "count": 3,
                        "bars": [bar("2026-07-01", 9.0, 9.1, 9.2, 8.9) for _ in range(3)]}
            return {**BARS_BODY, "count": 50,
                    "bars": [bar(f"2026-09-{d:02d}", 9.0, 9.1, 9.2, 8.9)
                             for d in range(1, 51)]}

        bars_body["value"] = by_offset
        _, frontend_port = servers
        open_data(page, frontend_port)
        page.locator(".browse-pv tbody tr", has_text="贵州茅台").click()
        expect(page.locator(".detail-table-card tbody tr")).to_have_count(50)

        page.locator(".detail-table-card .p-paginator-next").click()
        expect(page.locator(".detail-table-card tbody tr")).to_have_count(3)
        assert any("offset=50" in u for u in calls), f"应发起 offset=50 请求: {calls}"

    def test_down_degrades_without_mock(self, servers, page):
        """E2E-205：列表 mock 但 bars 放行到真实后端（本地无 PG）→ 降级文案不崩溃。"""
        _, frontend_port = servers

        def list_only(route):
            url = route.request.url
            if re.search(r"/api/stocks(\?|$)", url):
                route.fulfill(status=200, content_type="application/json",
                              body=json.dumps(LIST_BODY))
            else:
                route.continue_()

        page.route("**/api/stocks**", list_only)
        open_data(page, frontend_port)
        page.locator(".browse-pv tbody tr", has_text="贵州茅台").click()
        expect(page.get_by_role("heading", name="600519")).to_be_visible()
        expect(page.locator(".chart-note")).to_contain_text("行情数据不可用")
        expect(page.locator(".detail-table-card .browse-empty")
               ).to_contain_text("数据库不可达")
