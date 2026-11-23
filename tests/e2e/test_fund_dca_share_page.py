"""定投止盈回测页·定投方式切换 E2E（等额定投 amount / 等份额定投 share）。

用例表（确认门 1：自主开发模式并入最终前端过目，同 test_fund_dca_tp_page 约定）：
- E2E-444 P0 方式切换主流程：点搜索栏「等份额定投」→ 列表请求
  /api/funds/dca/tp?tp=0.2&method=share（等份额即便 20% 也走 tp 端点），
  副标题出现「固定份额」口径文案，表格渲染等份额行；
- E2E-445 对比卡跟随：切换后 compare 请求带 method=share，对比卡标题
  显示当前口径「等份额定投」；
- E2E-446 弹窗跟随：等份额下点「详情」→ summary/rounds 请求带
  method=share，弹窗标题含口径、轮次表有数据；
- E2E-447 回默认走旧端点：share → amount 后，最后列表请求为
  /api/funds/dca?（旧端点，无 method/tp 参数）；
- E2E-448 深链接：/data/fund-dca?mode=share → 首个列表请求即带 method=share；
- E2E-449 非法 mode 深链接忽略：?mode=dca → 回落等额走旧端点；
- E2E-450 方式×止盈点组合：share + 15% → 请求同时带 tp=0.15 与 method=share；
- E2E-451 弹窗内档位/费率切换保留方式：弹窗切 15%/gross 后 rounds 请求
  仍带 method=share（弹窗跟随页面级方式）；
- E2E-460 深回撤加码切换：切「深回撤加码」→ 列表/散点请求带
  method=dd_ladder_deep，悬停 title 携带深梯度口径；
- E2E-461 深回撤加码深链接：?mode=dd_ladder_deep → tab 激活且首请求即带参。
运行：python -m pytest tests/e2e/test_fund_dca_share_page.py
"""

import json
import re
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import expect

from test_fund_dca_page import LIST_BODY, mock_dca, open_dca

# 等份额排行行（数值与等额 mock 不同，证明表格确实切换了数据源）
SHARE_LIST_BODY = {
    "status": "up", "tp": 0.2, "method": "share", "total": 101,
    "limit": 50, "offset": 0,
    "rows": [
        {"tp": 0.2, "code": "019387", "name": "中欧时代共赢混合发起A1",
         "fund_type": "混合型-偏股", "alive": True, "scale_yi": 1.2,
         "scale_date": "2026-09-30", "age_years": 2.9,
         "w10_start": "2023-11-28", "w10_end": "2026-10-28", "w10_months": 36,
         "w10_rounds": 9, "w10_invested": 39800, "w10_final_value": 52138,
         "w10_profit_rate": 0.31, "w10_xirr": 0.75, "gap_avg_days": 96.3,
         "full_xirr": 0.74, "full_rounds": 9, "full_profit_rate": 0.30,
         "full_months": 36},
        {"tp": 0.2, "code": "161725", "name": "招商中证白酒指数(LOF)A",
         "fund_type": "指数型-股票", "alive": True, "scale_yi": 209.0,
         "scale_date": "2026-09-30", "age_years": 10.4,
         "w10_start": "2016-10-31", "w10_end": "2026-10-28", "w10_months": 121,
         "w10_rounds": 12, "w10_invested": 128430, "w10_final_value": 119430,
         "w10_profit_rate": -0.07, "w10_xirr": -0.062, "gap_avg_days": None,
         "full_xirr": -0.052, "full_rounds": 12, "full_profit_rate": -0.06,
         "full_months": 138},
    ],
    "stats": {"count": 13557, "p25": 0.018, "median": 0.035, "p75": 0.092,
              "p90": 0.164, "positive_share": 0.78, "take_profit_share": 0.62},
    "meta": {"generated_at": "2026-11-23T02:00:00+00:00", "fund_count": 13557,
             "params": {"amount": 1000, "methods": ["amount", "share"],
                        "window_end": "2026-11-21"}},
}


def _share_cmp_row(tp: float, med: float) -> dict:
    return {"tp": tp, "count": 7900,
            "w10_p25": round(med * 0.5, 4), "w10_median": med,
            "w10_p75": round(med * 1.5, 4), "w10_p90": round(med * 2.0, 4),
            "w10_mean": round(med * 0.95, 4), "w10_positive_share": 0.72,
            "w10_success_share": round(0.93 - tp, 4), "w10_avg_rounds": 11.2,
            "full_median": round(med * 0.9, 4), "full_mean": round(med * 0.88, 4),
            "full_positive_share": 0.70, "full_success_share": 0.47,
            "avg_gap_days": round(58 + tp * 290, 1)}


# 等份额八档中位年化（整体低于等额——成本摊平劣势面的市场级形态）
SHARE_COMPARE_BODY = {
    "status": "up", "fee": "nom", "method": "share", "equity": True,
    "thresholds": [0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.35, 0.4],
    "rows": [_share_cmp_row(0.05, 0.031), _share_cmp_row(0.10, 0.043),
             _share_cmp_row(0.15, 0.051), _share_cmp_row(0.20, 0.054),
             _share_cmp_row(0.25, 0.052), _share_cmp_row(0.30, 0.047),
             _share_cmp_row(0.35, 0.044), _share_cmp_row(0.40, 0.038)],
}

SHARE_ROUNDS_BODY = {
    "status": "up", "code": "019387", "fee": "nom", "tp": 0.2,
    "method": "share",
    "rounds": [
        {"seq": 1, "tp_date": "2024-04-15", "round_days": 109,
         "gap_days": 109, "profit": 0.207, "tp_nav": 1.207,
         "invested": 5150.0, "sub_fee": 7.61, "redeem_fee": 12.4,
         "net_profit": 1049.6},
    ],
    "totals": {"rounds": 1, "invested": 5150.0, "net_profit": 1049.6,
               "sub_fee": 7.61, "redeem_fee": 12.4},
}

SHARE_SUMMARY_BODY = {
    "status": "up", "code": "019387", "method": "share",
    "rows": [
        {"tp": t, "w10_profit_rate": 0.28, "w10_xirr": 0.61,
         "w10_profit_rate_disc": 0.27, "w10_xirr_disc": 0.59,
         "w10_profit_rate_nom": 0.26, "w10_xirr_nom": 0.57}
        for t in (0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40)
    ],
}


@pytest.fixture
def mock_share(page):
    """mock /api/funds/dca/tp*（method 维度版：排行/对比/轮次/汇总四端点，
    回显请求里的 method/tp；记录 URL 供参数断言）。需在 mock_dca 之后注册。"""
    calls = []

    def handler(route):
        url = route.request.url
        calls.append(url)
        qs = parse_qs(urlparse(url).query)
        method = (qs.get("method") or ["amount"])[0]
        tp = float((qs.get("tp") or ["0.2"])[0])
        if re.search(r"/api/funds/dca/tp/[^/?]+/summary", url):
            body = {**SHARE_SUMMARY_BODY, "method": method,
                    "rows": [{**r, "age_years": 10.4}
                             for r in SHARE_SUMMARY_BODY["rows"]]}
        elif "/api/funds/dca/tp/compare" in url:
            body = {**SHARE_COMPARE_BODY, "method": method,
                    "fee": (qs.get("fee") or ["nom"])[0]}
        elif re.search(r"/api/funds/dca/tp/[^/?]+/rounds", url):
            body = {**SHARE_ROUNDS_BODY, "method": method, "tp": tp}
        else:
            body = {**SHARE_LIST_BODY, "method": method, "tp": tp}
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(body))

    page.route("**/api/funds/dca/tp**", handler)
    return calls


@pytest.fixture
def mock_navs(page):
    """弹窗净值曲线桩（等份额轮次日期对齐 mock 数据）。"""
    page.route("**/api/funds/019387/navs*", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps({
            "status": "up", "code": "019387", "navs": [
                {"date": "2023-11-28", "unit_nav": 1.0, "accum_nav": 1.0,
                 "pct_chg": 0, "unit_money": None, "source": "fund_em"},
                {"date": "2024-04-15", "unit_nav": 1.207, "accum_nav": 1.207,
                 "pct_chg": 1.0, "unit_money": None, "source": "fund_em"},
                {"date": "2026-10-28", "unit_nav": 1.12, "accum_nav": 1.12,
                 "pct_chg": 0.2, "unit_money": None, "source": "fund_em"}]})))


def _scatter_body_for(method: str, fee: str) -> dict:
    """按定投方式返回可区分的散点/分箱形状：等份额整体年化更低
    （≥10 年格均值 0.076 → 0.046），供切换断言。"""
    factor = 0.55 if method == "share" else 0.9
    from test_fund_dca_page import SCATTER_BODY
    import copy
    b = copy.deepcopy(SCATTER_BODY)
    for row in b["matrix"]:
        for i, cell in enumerate(row):
            if cell["count"] and cell["mean_xirr"] is not None:
                row[i] = {**cell,
                          "mean_xirr": round(cell["mean_xirr"] * factor, 4),
                          "median_xirr": round(cell["median_xirr"] * factor, 4)}
    b["method"] = method
    b["fee"] = fee
    return b


@pytest.fixture
def mock_share_scatter(page):
    """method 感知的散点桩（注册在 mock_dca 之后覆盖其放行路由）。"""
    calls = []

    def handler(route):
        from urllib.parse import urlparse, parse_qs
        url = route.request.url
        calls.append(url)
        qs = parse_qs(urlparse(url).query)
        method = (qs.get("method") or ["amount"])[0]
        fee = (qs.get("fee") or ["nom"])[0]
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(_scatter_body_for(method, fee)))

    page.route("**/api/funds/dca/scatter*", handler)
    return calls


def _switch_share(page):
    page.locator('button[data-mode="share"]').click()
    expect(page.locator('button[data-mode="share"]')
          ).to_have_class(re.compile("active"), timeout=10_000)


class TestDcaShareMode:
    def test_mode_switch_and_request(self, servers, page, mock_dca,
                                     mock_share, mock_navs):
        """E2E-444 P0：点「等份额定投」→ tp 端点 method=share（20% 也是），
        表格渲染等份额行（口径副标题已删，五轮起口径看 ? 面板与悬停 title）。"""
        _, frontend_port = servers
        open_dca(page, frontend_port)
        _switch_share(page)
        list_calls = [u for u in mock_share
                      if "/rounds" not in u and "/compare" not in u
                      and "/summary" not in u]
        assert list_calls and "method=share" in list_calls[-1], mock_share
        assert "tp=0.2" in list_calls[-1], list_calls[-1]
        row = page.locator(".browse-pv tbody tr", has_text="019387")
        expect(row).to_contain_text("+75.0%")   # 等份额行自己的年化
        expect(row).to_contain_text("9")        # 等份额止盈次数

    def test_compare_follows_method(self, servers, page, mock_dca,
                                    mock_share, mock_navs):
        """E2E-445：切换方式后对比请求带 method=share，标题显示当前口径。"""
        _, frontend_port = servers
        open_dca(page, frontend_port)
        _switch_share(page)
        compare_calls = [u for u in mock_share if "/compare" in u]
        assert compare_calls and "method=share" in compare_calls[-1], mock_share
        expect(page.locator(".tp-compare, .card", has_text="止盈点对比")
              ).to_contain_text("等份额定投")

    def test_modal_follows_method(self, servers, page, mock_dca, mock_share,
                                  mock_navs):
        """E2E-446：等份额下点「详情」→ summary/rounds 带 method=share，
        弹窗标题含口径、轮次表有数据。"""
        _, frontend_port = servers
        open_dca(page, frontend_port)
        _switch_share(page)
        page.locator(".browse-pv tbody tr", has_text="019387") \
            .locator(".detail-btn").click()
        dlg = page.locator(".p-dialog", has_text="每次止盈明细")
        expect(dlg).to_be_visible()
        expect(dlg.locator(".p-dialog-title")).to_contain_text("等份额定投")
        expect(dlg).to_contain_text("第 1 次")
        summary_calls = [u for u in mock_share if "/summary" in u]
        assert summary_calls and "method=share" in summary_calls[-1], mock_share
        rounds_calls = [u for u in mock_share
                        if re.search(r"/api/funds/dca/tp/[^/?]+/rounds", u)]
        assert rounds_calls and "method=share" in rounds_calls[0], mock_share

    def test_switch_back_amount_uses_legacy_endpoint(self, servers, page,
                                                     mock_dca, mock_share,
                                                     mock_navs):
        """E2E-447：share → 等额定投后，最后列表请求为旧端点（无 method/tp）。"""
        _, frontend_port = servers
        open_dca(page, frontend_port)
        _switch_share(page)
        page.locator('button[data-mode="amount"]').click()
        expect(page.locator('button[data-mode="amount"]')
              ).to_have_class(re.compile("active"), timeout=10_000)
        list_calls = [u for u in mock_dca if re.search(r"/api/funds/dca\?", u)]
        assert list_calls, "应回落旧端点"
        assert "method=" not in list_calls[-1] and "tp=" not in list_calls[-1], \
            list_calls[-1]

    def test_mode_deeplink(self, servers, page, mock_dca, mock_share,
                           mock_navs):
        """E2E-448：?mode=share 深链接 → 首个列表请求即带 method=share。"""
        _, frontend_port = servers
        page.goto(f"http://127.0.0.1:{frontend_port}/data/fund-dca?mode=share")
        expect(page.get_by_role("heading", name="定投止盈回测")).to_be_visible()
        expect(page.locator('button[data-mode="share"]')
              ).to_have_class(re.compile("active"), timeout=10_000)
        list_calls = [u for u in mock_share
                      if "/rounds" not in u and "/compare" not in u
                      and "/summary" not in u]
        assert list_calls and "method=share" in list_calls[0], mock_share

    def test_mode_deeplink_invalid_ignored(self, servers, page, mock_dca,
                                           mock_share, mock_navs):
        """E2E-449：?mode=dca 非白名单 → 回落等额走旧端点（无 method）。"""
        _, frontend_port = servers
        page.goto(f"http://127.0.0.1:{frontend_port}/data/fund-dca?mode=dca")
        expect(page.get_by_role("heading", name="定投止盈回测")).to_be_visible()
        expect(page.locator('button[data-mode="amount"]')
              ).to_have_class(re.compile("active"), timeout=10_000)
        list_calls = [u for u in mock_dca if re.search(r"/api/funds/dca\?", u)]
        assert list_calls and "method=" not in list_calls[0], mock_dca

    def test_share_with_tp_combined(self, servers, page, mock_dca, mock_share,
                                    mock_navs):
        """E2E-450：等份额 + 15% 止盈点 → 请求同时带 tp=0.15 与 method=share。"""
        _, frontend_port = servers
        open_dca(page, frontend_port)
        _switch_share(page)
        page.locator('button[data-tp="0.15"]').click()
        expect(page.locator(".tp-compare tr.tp-row-active")
              ).to_contain_text("15%", timeout=10_000)
        list_calls = [u for u in mock_share
                      if "/rounds" not in u and "/compare" not in u
                      and "/summary" not in u]
        assert list_calls and "tp=0.15" in list_calls[-1] \
            and "method=share" in list_calls[-1], mock_share

    def test_mode_tab_at_page_top(self, servers, page, mock_dca, mock_share,
                                  mock_navs):
        """E2E-452：定投方式 tab 置于页面最前——位于统计卡之前、紧跟标题，
        筛选面板内不再有第二组（data-mode 唯一）。"""
        _, frontend_port = servers
        open_dca(page, frontend_port)
        tab = page.locator('button[data-mode="share"]')
        expect(tab).to_be_visible()
        metrics = page.locator(".learn-metrics").first
        tab_y = tab.bounding_box()["y"]
        assert tab_y < metrics.bounding_box()["y"], \
            "定投方式 tab 应在统计卡之前"
        # 唯一性：筛选面板里没有第二组 data-mode（已从面板移除）；
        # 五轮起四枚：等额/等份额/回撤加码/深回撤加码
        assert page.locator("button[data-mode]").count() == 4
        # 深链接形态同样置顶
        page.goto(f"http://127.0.0.1:{frontend_port}/data/fund-dca?mode=share")
        expect(page.locator('button[data-mode="share"]')
              ).to_have_class(re.compile("active"), timeout=10_000)
        assert page.locator('button[data-mode="share"]').bounding_box()["y"] \
            < page.locator(".learn-metrics").first.bounding_box()["y"]

    def test_scatter_follows_method(self, servers, page, mock_dca, mock_share,
                                    mock_navs, mock_share_scatter):
        """E2E-453：切换定投方式 → 散点请求带 method=share、结论徽标显示
        口径、分箱年化结论随方式变化（等份额整体更低）。"""
        _, frontend_port = servers
        open_dca(page, frontend_port)
        # 默认等额：结论卡最优格 ≥10 年 × <1 亿 平均年化 = 0.076×0.9 ≈ 6.8%
        concl = page.locator(".dca-conclusion")
        expect(concl).to_contain_text("每基金只计一次")
        expect(concl.locator(".dc-badge")).to_contain_text("等额定投")
        _switch_share(page)
        expect(concl.locator(".dc-badge")
              ).to_contain_text("等份额定投", timeout=10_000)
        # 等份额：最优格平均年化 = 0.076×0.55 ≈ 4.2%（等额同格 ≈ 6.8%）
        expect(concl).to_contain_text("平均年化 4.2%", timeout=10_000)
        scatter_calls = [u for u in mock_share_scatter if "method=share" in u]
        assert scatter_calls, mock_share_scatter
        # 散点卡标题带当前口径提示
        expect(page.locator(".card", has_text="三维散点").first
              ).to_contain_text("等份额定投")

    def test_master_bar_all_groups_at_top(self, servers, page, mock_dca,
                                          mock_share, mock_navs):
        """E2E-454：四组总开关（定投方式/止盈点/基金类型/费用口径）都在
        统计卡之前的最前位置；筛选面板与散点卡不再有重复组。"""
        _, frontend_port = servers
        open_dca(page, frontend_port)
        metrics_y = page.locator(".learn-metrics").first.bounding_box()["y"]
        for sel in ('button[data-mode="share"]', 'button[data-tp="0.2"]',
                    'button[data-table-cat="equity"]',
                    'button[data-table-fee="nom"]'):
            btn = page.locator(sel).first
            expect(btn).to_be_visible()
            assert btn.bounding_box()["y"] < metrics_y, sel
        # 全页唯一：面板/散点卡的重复组已移除
        assert page.locator("button[data-tp]").count() == 8
        assert page.locator("button[data-table-cat]").count() == 2
        assert page.locator("button[data-table-fee]").count() == 3

    def test_scatter_follows_tp(self, servers, page, mock_dca, mock_share,
                                mock_navs, mock_share_scatter):
        """E2E-455：切换止盈点 → 散点请求带 tp=0.15（跟随顶部总开关，
        不再固定 20% 档）；标题口径提示同步。"""
        _, frontend_port = servers
        open_dca(page, frontend_port)
        page.locator('button[data-tp="0.15"]').click()
        expect(page.locator(".tp-compare tr.tp-row-active")
              ).to_contain_text("15%", timeout=10_000)
        scatter_calls = [u for u in mock_share_scatter if "tp=0.15" in u]
        assert scatter_calls, mock_share_scatter
        expect(page.locator(".card", has_text="三维散点").first
              ).to_contain_text("止盈 15%", timeout=10_000)

    def test_compare_follows_equity_toggle(self, servers, page, mock_dca,
                                           mock_share, mock_navs):
        """E2E-456：顶部基金类型切「全市场」→ 对比卡请求带 equity=false、
        副标题池文案切换（权益+商品池 → 全市场池）。"""
        _, frontend_port = servers
        open_dca(page, frontend_port)
        card = page.locator(".card", has_text="止盈点对比").first
        expect(card).to_contain_text("权益+商品池")
        page.locator('button[data-table-cat="all"]').click()
        expect(page.locator('button[data-table-cat="all"]')
              ).to_have_class(re.compile("active"), timeout=10_000)
        compare_calls = [u for u in mock_share if "/compare" in u]
        assert compare_calls and "equity=false" in compare_calls[-1], mock_share
        expect(card).to_contain_text("全市场池", timeout=10_000)

    def test_dd_ladder_switch_and_request(self, servers, page, mock_dca,
                                          mock_share, mock_navs,
                                          mock_share_scatter):
        """E2E-457：切「回撤加码」→ 列表/散点请求带 method=dd_ladder，
        副标题出现回撤梯度加码口径文案，表格行渲染。"""
        _, frontend_port = servers
        open_dca(page, frontend_port)
        page.locator('button[data-mode="dd_ladder"]').click()
        expect(page.locator('button[data-mode="dd_ladder"]')
              ).to_have_class(re.compile("active"), timeout=10_000)
        list_calls = [u for u in mock_share
                      if "/rounds" not in u and "/compare" not in u
                      and "/summary" not in u]
        assert list_calls and "method=dd_ladder" in list_calls[-1], mock_share
        # 悬停 title 携带回撤加码详细口径（副标题已删，五轮起悬停即说明）
        expect(page.locator('button[data-mode="dd_ladder"]')
              ).to_have_attribute("title", re.compile("1\\.5 倍"))
        expect(page.locator(".browse-pv tbody tr", has_text="019387")
              ).to_contain_text("+75.0%")     # mock 回显行
        assert any("method=dd_ladder" in u for u in mock_share_scatter), \
            mock_share_scatter

    def test_dd_ladder_deeplink(self, servers, page, mock_dca, mock_share,
                                mock_navs):
        """E2E-458：?mode=dd_ladder 深链接 → tab 激活且首请求即带参。"""
        _, frontend_port = servers
        page.goto(f"http://127.0.0.1:{frontend_port}/data/fund-dca?mode=dd_ladder")
        expect(page.get_by_role("heading", name="定投止盈回测")).to_be_visible()
        expect(page.locator('button[data-mode="dd_ladder"]')
              ).to_have_class(re.compile("active"), timeout=10_000)
        list_calls = [u for u in mock_share
                      if "/rounds" not in u and "/compare" not in u
                      and "/summary" not in u]
        assert list_calls and "method=dd_ladder" in list_calls[0], mock_share

    def test_dd_ladder_deep_switch_and_request(self, servers, page, mock_dca,
                                               mock_share, mock_navs,
                                               mock_share_scatter):
        """E2E-460：切「深回撤加码」→ 列表/散点请求带
        method=dd_ladder_deep，悬停 title 携带深梯度口径（≥20% 1.5 倍），
        表格行渲染。"""
        _, frontend_port = servers
        open_dca(page, frontend_port)
        page.locator('button[data-mode="dd_ladder_deep"]').click()
        expect(page.locator('button[data-mode="dd_ladder_deep"]')
              ).to_have_class(re.compile("active"), timeout=10_000)
        list_calls = [u for u in mock_share
                      if "/rounds" not in u and "/compare" not in u
                      and "/summary" not in u]
        assert list_calls and "method=dd_ladder_deep" in list_calls[-1], \
            mock_share
        expect(page.locator('button[data-mode="dd_ladder_deep"]')
              ).to_have_attribute("title", re.compile("≥20% 投 1\\.5 倍"))
        expect(page.locator(".browse-pv tbody tr", has_text="019387")
              ).to_contain_text("+75.0%")     # mock 回显行
        assert any("method=dd_ladder_deep" in u
                   for u in mock_share_scatter), mock_share_scatter

    def test_dd_ladder_deep_deeplink(self, servers, page, mock_dca,
                                     mock_share, mock_navs):
        """E2E-461：?mode=dd_ladder_deep 深链接 → tab 激活且首请求即带参。"""
        _, frontend_port = servers
        page.goto(
            f"http://127.0.0.1:{frontend_port}/data/fund-dca?mode=dd_ladder_deep")
        expect(page.get_by_role("heading", name="定投止盈回测")).to_be_visible()
        expect(page.locator('button[data-mode="dd_ladder_deep"]')
              ).to_have_class(re.compile("active"), timeout=10_000)
        list_calls = [u for u in mock_share
                      if "/rounds" not in u and "/compare" not in u
                      and "/summary" not in u]
        assert list_calls and "method=dd_ladder_deep" in list_calls[0], \
            mock_share

    def test_mode_help_popover_and_titles(self, servers, page, mock_dca,
                                          mock_share, mock_navs):
        """E2E-459：点「?」→ 弹出四种方式完整说明（含各自 detail 关键词），
        再点收起；悬停各按钮 title 携带对应方式详情；口径副标题已删。"""
        _, frontend_port = servers
        open_dca(page, frontend_port)
        # 副标题删除锚（头部工具条不再有 .sub）
        expect(page.locator(".toolbar").first.locator(".sub")).to_have_count(0)
        # 悬停 title：每按钮携带对应方式完整说明
        expect(page.locator('button[data-mode="share"]')
              ).to_have_attribute("title", re.compile("现金支出随净值波动"))
        expect(page.locator('button[data-mode="amount"]')
              ).to_have_attribute("title", re.compile("外扣法"))
        # 点 ? 开：面板含四种方式与关键口径词
        page.locator('[data-mode-help]').click()
        pop = page.locator('[data-mode-help-pop]')
        expect(pop).to_be_visible()
        expect(pop).to_contain_text("等额定投")
        expect(pop).to_contain_text("成本摊平效果在各方式中最差")
        expect(pop).to_contain_text("1.5 倍")
        expect(pop).to_contain_text("68% 基金优于坚持等额定投")
        expect(pop).to_contain_text("深回撤加码")
        expect(pop).to_contain_text("≥30% 投 2 倍")
        # 再点 ? 关
        page.locator('[data-mode-help]').click()
        expect(pop).to_have_count(0, timeout=10_000)

    def test_modal_switchers_keep_method(self, servers, page, mock_dca,
                                         mock_share, mock_navs):
        """E2E-451：弹窗内切档位/费率 → rounds 请求仍带 method=share
        （弹窗跟随页面级定投方式，不回退等额）。"""
        _, frontend_port = servers
        open_dca(page, frontend_port)
        _switch_share(page)
        page.locator(".browse-pv tbody tr", has_text="019387") \
            .locator(".detail-btn").click()
        dlg = page.locator(".p-dialog", has_text="每次止盈明细")
        expect(dlg).to_be_visible()
        dlg.locator('button[data-modal-tp="0.15"]').click()
        expect(dlg.locator('button[data-modal-tp="0.15"]')
              ).to_have_class(re.compile("active"), timeout=10_000)
        dlg.locator('button[data-modal-fee="gross"]').click()
        expect(dlg.locator('button[data-modal-fee="gross"]')
              ).to_have_class(re.compile("active"), timeout=10_000)
        rounds_calls = [u for u in mock_share
                        if re.search(r"/api/funds/dca/tp/[^/?]+/rounds", u)
                        and "fee=gross" in u and "tp=0.15" in u]
        assert rounds_calls and all("method=share" in u for u in rounds_calls), \
            mock_share
