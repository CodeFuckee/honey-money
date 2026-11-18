"""数据浏览页退市列 + 退市过滤 E2E：mock /api/stocks。

用例表（确认门 1 已确认）：
- E2E-301 退市股行显示「已退市」标记；
- E2E-302 在市股（delisted=False 或老接口无该键）该列显示「—」占位；
- E2E-303 默认加载不过滤（请求无 delisted 谓词）；
- E2E-304 过滤下拉切「仅在市」/「仅退市」→ 请求带 delisted=false/true，
  且过滤态下翻页请求仍携带（分页不丢过滤）；
- E2E-317 基金列表波动指标五列渲染（格式化 + 无指标行 — 占位）；
- E2E-318 点波动列表头 → sort=vol_all&order=asc/desc 服务端排序；
- E2E-319 详情页风险指标区（指标卡 + 多窗口对照表 + 快照截至 note）；
- E2E-320 无指标行 → 指标区不渲染、待生成占位。
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


# ---------------------------------------------------------- 基金净值子页 ----

FUNDS_BODY = {"status": "up", "total": 2, "limit": 50, "offset": 0, "funds": [
    {"code": "000001", "name": "华夏成长混合", "fund_type": "混合型-灵活",
     "unit_nav": 1.046, "pct_chg": 2.05, "navs": 6029,
     "last_date": "2026-10-23", "first_date": "2001-12-18",
     "vol_all": 0.185, "vol_down_all": 0.1201, "mdd_all": 0.2533,
     "sharpe_all": 1.4233, "roll_vol": 0.0812, "vol_pctile": 0.6666},
    {"code": "005827", "name": "易方达蓝筹精选", "fund_type": "混合型-偏股",
     "unit_nav": 1.8721, "pct_chg": -0.31, "navs": 3200,
     "last_date": "2026-10-23", "first_date": "2018-09-05",
     "vol_all": None, "vol_down_all": None, "mdd_all": None,
     "sharpe_all": None, "roll_vol": None, "vol_pctile": None},
]}
FUND_NAV_BODY = {"status": "up", "code": "000001", "count": 3, "navs": [
    {"date": "2026-10-21", "unit_nav": 1.064, "accum_nav": 3.637,
     "pct_chg": -0.37, "unit_money": None, "source": "fund_em"},
    {"date": "2026-10-22", "unit_nav": 1.025, "accum_nav": 3.598,
     "pct_chg": -3.67, "unit_money": None, "source": "fund_em"},
    {"date": "2026-10-23", "unit_nav": 1.046, "accum_nav": 3.619,
     "pct_chg": 2.05, "unit_money": "0.018", "source": "fund_em"},
]}
# 风险/波动指标快照（fund_risk_metrics 日更行；详情页指标区数据源）。
# 四窗口全给：卡用 *_all（全史口径），窗口对照表四行各取 *_1y/3y/5y/all
FUND_RISK = {
    "vol_1y": 0.185, "vol_down_1y": 0.1201, "mdd_1y": 0.2533, "sharpe_1y": 1.4233,
    "vol_3y": 0.201, "vol_down_3y": 0.135, "mdd_3y": 0.301, "sharpe_3y": 1.1,
    "vol_5y": 0.22, "vol_down_5y": 0.14, "mdd_5y": 0.35, "sharpe_5y": 0.9,
    "vol_all": 0.23, "vol_down_all": 0.15, "mdd_all": 0.42, "sharpe_all": 0.8,
    "roll_vol": 0.0812, "vol_pctile": 0.6666, "risk_score": 0.4123,
    "as_of": "2026-10-23", "computed_at": "2026-11-17T06:00:00",
}
FUND_NAV_BODY_WITH_RISK = {**FUND_NAV_BODY, "risk": FUND_RISK}


class TestFundBrowse:
    def test_fund_list_renders(self, servers, page):
        """E2E-311：侧栏「基金净值」子页列表渲染（类型/净值条数/最早净值日）。"""
        _, frontend_port = servers
        page.route("**/api/funds?**", lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps(FUNDS_BODY)))
        page.goto(f"http://127.0.0.1:{frontend_port}/")
        page.get_by_role("button", name="基金净值").click()
        expect(page.get_by_role("heading", name="基金净值")).to_be_visible()
        row = page.locator(".browse-pv tbody tr", has_text="华夏成长混合")
        expect(row).to_contain_text("混合型-灵活")
        expect(row).to_contain_text("6029")
        expect(row).to_contain_text("2001-12-18")

    def test_fund_detail_chart_and_table_and_back(self, servers, page):
        """E2E-312：点行进净值详情——图表 canvas 渲染、明细最新日期在上、
        分红串展示、返回列表回到基金子页。"""
        _, frontend_port = servers
        page.route("**/api/funds?**", lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps(FUNDS_BODY)))
        page.route("**/api/funds/000001/navs**", lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps(FUND_NAV_BODY)))
        page.goto(f"http://127.0.0.1:{frontend_port}/")
        page.get_by_role("button", name="基金净值").click()
        page.locator(".browse-pv tbody tr", has_text="华夏成长混合").click()
        expect(page.locator("h2.h1")).to_contain_text("000001")
        expect(page.locator(".kchart canvas").first).to_be_visible()
        rows = page.locator(".detail-table-card tbody tr")
        expect(rows.first).to_contain_text("2026-10-23")     # 最新在上
        expect(rows.nth(1)).to_contain_text("2026-10-22")
        expect(page.locator(".detail-table-card")).to_contain_text("0.018")
        page.get_by_role("button", name="← 返回列表").click()
        expect(page.get_by_role("heading", name="基金净值")).to_be_visible()


class TestFundRiskColumns:
    def test_risk_columns_render_and_null_dash(self, servers, page):
        """E2E-317：波动指标五列渲染——数值格式化（18.5% / -25.3% /
        1.42 / 67%），无指标行的基金显示 — 占位。"""
        _, frontend_port = servers
        page.route("**/api/funds?**", lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps(FUNDS_BODY)))
        page.goto(f"http://127.0.0.1:{frontend_port}/")
        page.get_by_role("button", name="基金净值").click()
        expect(page.get_by_role("heading", name="基金净值")).to_be_visible()
        row = page.locator(".browse-pv tbody tr", has_text="华夏成长混合")
        expect(row).to_contain_text("18.5%")          # vol_all 0.185（全史）
        expect(row).to_contain_text("12.0%")          # vol_down_all 0.1201
        expect(row).to_contain_text("-25.3%")         # mdd_all 0.2533 显负号
        expect(row).to_contain_text("1.42")           # sharpe_all 两位裸数
        expect(row).to_contain_text("67%")            # vol_pctile 0.6666 取整
        other = page.locator(".browse-pv tbody tr", has_text="易方达蓝筹精选")
        expect(other).to_contain_text("—")            # 无指标行

    def test_sort_header_sends_sort_param(self, servers, page):
        """E2E-318：点波动列表头 → 请求带 sort=vol_all&order=asc（PrimeVue
        首点升序），再点降序——服务端排序参数正确。"""
        _, frontend_port = servers
        calls = []
        page.route("**/api/funds**", lambda route: (
            calls.append(route.request.url),
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(FUNDS_BODY))) and None)
        page.goto(f"http://127.0.0.1:{frontend_port}/")
        page.get_by_role("button", name="基金净值").click()
        expect(page.get_by_role("heading", name="基金净值")).to_be_visible()
        page.locator(".browse-pv th", has_text="年化波动率").click()
        page.wait_for_timeout(500)
        assert any("sort=vol_all" in u and "order=asc" in u for u in calls), calls
        page.locator(".browse-pv th", has_text="年化波动率").click()
        page.wait_for_timeout(500)
        assert any("sort=vol_all" in u and "order=desc" in u for u in calls), calls


class TestFundRiskDetail:
    def test_risk_cards_and_window_table(self, servers, page):
        """E2E-319：详情页指标区——五张指标卡（近1年+当前滚动/分位）、
        多窗口对照表（近1年/近3年/近5年/全史）、口径 note 含快照截至。"""
        _, frontend_port = servers
        page.route("**/api/funds?**", lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps(FUNDS_BODY)))
        page.route("**/api/funds/000001/navs**", lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps(FUND_NAV_BODY_WITH_RISK)))
        page.goto(f"http://127.0.0.1:{frontend_port}/")
        page.get_by_role("button", name="基金净值").click()
        page.locator(".browse-pv tbody tr", has_text="华夏成长混合").click()
        expect(page.locator("h2.h1")).to_contain_text("000001")
        card = page.locator(".risk-card")
        expect(card).to_be_visible()
        expect(card).to_contain_text("23.0%")          # 年化波动率·全史卡 vol_all 0.23
        expect(card).to_contain_text("15.0%")          # 下行波动率·全史 0.15
        expect(card).to_contain_text("-42.0%")         # 最大回撤·全史 0.42 显负号
        expect(card).to_contain_text("0.80")           # 夏普·全史
        expect(card).to_contain_text("8.1%")           # 当前滚动波动率 0.0812
        expect(card).to_contain_text("67%")            # 分位
        expect(card).to_contain_text("41%")            # 综合波动分位 risk_score 0.4123
        table = card.locator(".risk-table")
        for label in ("近1年", "近3年", "近5年", "全史"):
            expect(table).to_contain_text(label)
        expect(table).to_contain_text("18.5%")         # 近1年行 vol_1y 0.185
        expect(table).to_contain_text("22.0%")         # vol_5y 0.22
        expect(table).to_contain_text("23.0%")         # vol_all 0.23
        expect(card).to_contain_text("快照截至 2026-10-23")

    def test_risk_missing_shows_placeholder(self, servers, page):
        """E2E-320：无指标行（risk=null）→ 指标区不渲染、显示待生成占位。"""
        _, frontend_port = servers
        page.route("**/api/funds?**", lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps(FUNDS_BODY)))
        page.route("**/api/funds/000001/navs**", lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps(FUND_NAV_BODY)))
        page.goto(f"http://127.0.0.1:{frontend_port}/")
        page.get_by_role("button", name="基金净值").click()
        page.locator(".browse-pv tbody tr", has_text="华夏成长混合").click()
        expect(page.locator("h2.h1")).to_contain_text("000001")
        expect(page.locator(".risk-card")).to_have_count(0)
        expect(page.get_by_text("波动指标尚未生成", exact=False)
               ).to_be_visible()


class TestFundDelistedFilter:
    def test_filter_switch_sends_param(self, servers, page):
        """E2E-313：基金子页退市过滤切「仅在市」→ delisted=false、
        「仅退市」→ delisted=true（请求参数与股票页同款三态）。"""
        _, frontend_port = servers
        calls = []
        page.route("**/api/funds**", lambda route: (
            calls.append(route.request.url),
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(FUNDS_BODY))) and None)
        page.goto(f"http://127.0.0.1:{frontend_port}/")
        page.get_by_role("button", name="基金净值").click()
        expect(page.get_by_role("heading", name="基金净值")).to_be_visible()
        assert calls and all("delisted=" not in u for u in calls)

        page.locator(".delisted-select").click()
        page.locator(".p-select-overlay .p-select-option",
                     has_text="仅在市").click()
        page.wait_for_timeout(500)
        assert any("delisted=false" in u for u in calls), calls

        page.locator(".delisted-select").click()
        page.locator(".p-select-overlay .p-select-option",
                     has_text="仅退市").click()
        page.wait_for_timeout(500)
        assert any("delisted=true" in u for u in calls), calls


class TestFundBackendToggle:
    def test_backend_toggle_sends_param(self, servers, page):
        """E2E-314：后端收费份额默认隐藏（请求无 show_backend），按钮打开
        → show_backend=1；再点关闭 → 参数消失。"""
        _, frontend_port = servers
        calls = []
        page.route("**/api/funds**", lambda route: (
            calls.append(route.request.url),
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(FUNDS_BODY))) and None)
        page.goto(f"http://127.0.0.1:{frontend_port}/")
        page.get_by_role("button", name="基金净值").click()
        expect(page.get_by_role("heading", name="基金净值")).to_be_visible()
        # 退市下拉默认显示占位文案（空串值曾被 PrimeVue 渲染成空白框）
        expect(page.locator(".funds-search .p-select-label")
               ).to_have_text("退市：全部")
        assert calls and all("show_backend" not in u for u in calls)

        btn = page.get_by_role("button", name="后端份额")
        assert btn.get_attribute("aria-pressed") == "false"
        btn.click()
        page.wait_for_timeout(500)
        assert any("show_backend=1" in u for u in calls), calls
        assert btn.get_attribute("aria-pressed") == "true"

        btn.click()
        page.wait_for_timeout(500)
        assert calls[-1].count("show_backend") == 0, calls[-1]
        assert btn.get_attribute("aria-pressed") == "false"


class TestFundMoneyToggle:
    def test_money_toggle_sends_param(self, servers, page):
        """E2E-315：货币基金默认隐藏（请求无 show_money），按钮打开
        → show_money=1；再点关闭 → 参数消失。"""
        _, frontend_port = servers
        calls = []
        page.route("**/api/funds**", lambda route: (
            calls.append(route.request.url),
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(FUNDS_BODY))) and None)
        page.goto(f"http://127.0.0.1:{frontend_port}/")
        page.get_by_role("button", name="基金净值").click()
        expect(page.get_by_role("heading", name="基金净值")).to_be_visible()
        assert calls and all("show_money" not in u for u in calls)

        btn = page.get_by_role("button", name="货币基金")
        assert btn.get_attribute("aria-pressed") == "false"
        btn.click()
        page.wait_for_timeout(500)
        assert any("show_money=1" in u for u in calls), calls
        assert btn.get_attribute("aria-pressed") == "true"

        btn.click()
        page.wait_for_timeout(500)
        assert calls[-1].count("show_money") == 0, calls[-1]
        assert btn.get_attribute("aria-pressed") == "false"


class TestFundQdiiToggle:
    def test_qdii_toggle_sends_param(self, servers, page):
        """E2E-316：QDII 默认隐藏（请求无 show_qdii），按钮打开
        → show_qdii=1；再点关闭 → 参数消失。"""
        _, frontend_port = servers
        calls = []
        page.route("**/api/funds**", lambda route: (
            calls.append(route.request.url),
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(FUNDS_BODY))) and None)
        page.goto(f"http://127.0.0.1:{frontend_port}/")
        page.get_by_role("button", name="基金净值").click()
        expect(page.get_by_role("heading", name="基金净值")).to_be_visible()
        assert calls and all("show_qdii" not in u for u in calls)

        btn = page.get_by_role("button", name="QDII")
        assert btn.get_attribute("aria-pressed") == "false"
        btn.click()
        page.wait_for_timeout(500)
        assert any("show_qdii=1" in u for u in calls), calls
        assert btn.get_attribute("aria-pressed") == "true"

        btn.click()
        page.wait_for_timeout(500)
        assert calls[-1].count("show_qdii") == 0, calls[-1]
        assert btn.get_attribute("aria-pressed") == "false"
