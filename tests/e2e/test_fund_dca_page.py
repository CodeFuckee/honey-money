"""定投止盈回测页 E2E：mock /api/funds/dca。

用例表（确认门 1：自主开发模式并入最终前端过目，见 test 文件尾注）：
- E2E-401 P0 主流程：侧栏进入 → 标题/统计卡/表格行渲染，年化带红涨绿跌类；
- E2E-402 搜索：输入回车 → 请求带 q；翻页请求保留 q（过滤不丢）；
- E2E-403 在市过滤：切「仅清盘/退市」→ alive=false，切「全部」→ alive=（空值）；
- E2E-404 排序：点「年化·全史」列头 → sort=full_xirr&order=asc，且翻页保留；
- E2E-405 深链接：直接打开 /data/fund-dca?alive=false → 首个请求即带 alive=false。
运行：python -m pytest tests/e2e/test_fund_dca_page.py
"""

import json
import re

import pytest
from playwright.sync_api import expect

LIST_BODY = {
    "status": "up", "total": 101, "limit": 50, "offset": 0,
    "rows": [
        {"code": "019387", "name": "中欧时代共赢混合发起A1", "fund_type": "混合型-偏股",
         "alive": True, "scale_yi": 1.2, "scale_date": "2026-09-30",
         "age_years": 2.9,
         "w10_start": "2023-11-28", "w10_end": "2026-10-28", "w10_months": 36,
         "w10_rounds": 6, "w10_invested": 36000, "w10_final_value": 42480,
         "w10_profit_rate": 0.18, "w10_xirr": 1.003,
         "full_xirr": 1.003, "full_rounds": 6, "full_profit_rate": 0.18,
         "full_months": 36},
        {"code": "161725", "name": "招商中证白酒指数(LOF)A", "fund_type": "指数型-股票",
         "alive": True, "scale_yi": 209.0, "scale_date": "2026-09-30",
         "age_years": 10.4,
         "w10_start": "2016-10-31", "w10_end": "2026-10-28", "w10_months": 121,
         "w10_rounds": 7, "w10_invested": 121000, "w10_final_value": 102850,
         "w10_profit_rate": -0.15, "w10_xirr": -0.147,
         "full_xirr": -0.133, "full_rounds": 7, "full_profit_rate": -0.15,
         "full_months": 138},
    ],
    "stats": {"count": 13557, "p25": 0.022, "median": 0.041, "p75": 0.114,
              "p90": 0.192, "positive_share": 0.853, "take_profit_share": 0.54},
    "meta": {"generated_at": "2026-10-29T14:39:32+00:00", "fund_count": 13981,
             "params": {"amount": 1000, "take_profit": 0.2, "min_age_years": 3,
                        "window_end": "2026-10-28"}},
}


SCATTER_BODY = {
    "status": "up", "ref_date": "2026-10-29",
    "points": [
        [10.4, 209.0, -0.147, "161725", "招商中证白酒指数(LOF)A", "指数型-股票"],
        [2.9, 1.2, 1.003, "019387", "中欧时代共赢混合发起A1", "混合型-偏股"],
        [12.8, 45.0, 0.086, "000001", "华夏成长混合", "混合型-灵活"],
    ],
    "age_bands": ["0-1 年", "1-3 年", "3-5 年", "5-10 年", "≥10 年"],
    "scale_bands": ["<1 亿", "1-10 亿", "10-100 亿", "≥100 亿"],
    "matrix": [
        [{"age_band": "0-1 年", "scale_band": "<1 亿", "count": 0, "mean_xirr": None,
          "median_xirr": None, "positive_share": None}] * 4,
        [{"age_band": "1-3 年", "scale_band": "<1 亿", "count": 120, "mean_xirr": 0.09,
          "median_xirr": 0.07, "positive_share": 0.7}] * 4,
        [{"age_band": "3-5 年", "scale_band": "<1 亿", "count": 200, "mean_xirr": 0.06,
          "median_xirr": 0.05, "positive_share": 0.8}] * 4,
        [{"age_band": "5-10 年", "scale_band": "<1 亿", "count": 400, "mean_xirr": 0.07,
          "median_xirr": 0.06, "positive_share": 0.85}] * 4,
        [{"age_band": "≥10 年", "scale_band": "<1 亿", "count": 900, "mean_xirr": 0.076,
          "median_xirr": 0.065, "positive_share": 0.9},
         {"age_band": "≥10 年", "scale_band": "1-10 亿", "count": 800, "mean_xirr": 0.075,
          "median_xirr": 0.062, "positive_share": 0.9},
         {"age_band": "≥10 年", "scale_band": "10-100 亿", "count": 300, "mean_xirr": 0.07,
          "median_xirr": 0.06, "positive_share": 0.89},
         {"age_band": "≥10 年", "scale_band": "≥100 亿", "count": 50, "mean_xirr": 0.06,
          "median_xirr": 0.05, "positive_share": 0.88}],
    ],
}


@pytest.fixture
def mock_dca(page):
    """mock /api/funds/dca（列表）；记录请求 URL 供参数断言。grid 端点放行走
    E2E 后端（死 DSN → down），避免污染列表参数断言。"""
    calls = []

    def handler(route):
        url = route.request.url
        if "/api/funds/dca/scatter" in url:
            route.continue_()
            return
        calls.append(url)
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(LIST_BODY))

    page.route("**/api/funds/dca*", handler)
    return calls


def mock_scatter(page):
    """mock /api/funds/dca/scatter（三维散点）。"""
    page.route("**/api/funds/dca/scatter", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps(SCATTER_BODY)))


def open_dca(page, frontend_port):
    page.goto(f"http://127.0.0.1:{frontend_port}/")
    page.get_by_role("button", name="定投止盈回测").click()
    expect(page.get_by_role("heading", name="定投止盈回测")).to_be_visible()


class TestFundDcaPage:
    def test_renders_stats_and_rows(self, servers, page, mock_dca):
        """E2E-401：统计卡与表格行渲染，正负年化分别带涨/跌配色类。"""
        _, frontend_port = servers
        open_dca(page, frontend_port)
        expect(page.locator(".learn-metrics .lm")).to_have_count(7)
        expect(page.locator(".learn-metrics")).to_contain_text("4.1%")
        row = page.locator(".browse-pv tbody tr", has_text="中欧时代共赢")
        expect(row).to_contain_text("+100.3%")
        expect(row.locator(".chg-up")).to_have_count(3)   # 总收益/两个年化
        row2 = page.locator(".browse-pv tbody tr", has_text="白酒")
        expect(row2.locator(".chg-down")).to_have_count(3)
        expect(row2).to_contain_text("-14.7%")
        # 运作年限列渲染（服务端按首条净值计算）
        expect(row).to_contain_text("2.9 年")
        expect(row2).to_contain_text("10.4 年")
        # 口径副标题含运作年限门槛（meta.params.min_age_years 驱动，无则不显示）
        expect(page.locator(".toolbar .sub").first).to_contain_text("运作 ≥3 年")

    def test_search_sends_q_and_kept_on_paging(self, servers, page, mock_dca):
        """E2E-402：搜索带 q，翻页请求保留 q。"""
        _, frontend_port = servers
        open_dca(page, frontend_port)
        page.locator(".funds-search .browse-input").fill("白酒")
        page.locator(".funds-search").get_by_role("button", name="搜索").click()
        page.wait_for_timeout(400)
        assert any("q=%E7%99%BD%E9%85%92" in u for u in mock_dca), mock_dca
        page.locator(".browse-pv .p-paginator-next").click()
        page.wait_for_timeout(400)
        last = mock_dca[-1]
        assert "q=%E7%99%BD%E9%85%92" in last and "offset=50" in last, last

    def test_alive_filter_param(self, servers, page, mock_dca):
        """E2E-403：默认仅在市；切「仅清盘/退市」→ alive=false；「全部」→ alive=。"""
        _, frontend_port = servers
        open_dca(page, frontend_port)
        assert mock_dca and "alive=true" in mock_dca[0]
        page.locator(".delisted-select").first.click()
        page.locator(".p-select-overlay .p-select-option",
                     has_text="仅清盘/退市").click()
        page.wait_for_timeout(400)
        assert any("alive=false" in u for u in mock_dca), mock_dca
        page.locator(".delisted-select").first.click()
        page.locator(".p-select-overlay .p-select-option", has_text="全部").click()
        page.wait_for_timeout(400)
        assert any(re.search(r"alive=&", u) for u in mock_dca), mock_dca

    def test_age_input_filter_param(self, servers, page, mock_dca):
        """E2E-407：年限输入——填 3 回车 → min_age=3；清成 0 → 不带年限参数；
        深链接 ?age=5 → 首请求即 min_age=5。"""
        _, frontend_port = servers
        open_dca(page, frontend_port)
        assert mock_dca and all("min_age" not in u for u in mock_dca)
        box = page.get_by_role("spinbutton", name="按运作年限过滤，大于该年数")
        box.fill("3")
        box.press("Enter")
        page.wait_for_timeout(900)          # 年限输入走 400ms 防抖搜索
        assert any("min_age=3" in u for u in mock_dca), mock_dca
        box.fill("0")
        box.press("Enter")
        page.wait_for_timeout(900)
        assert not any("min_age" in u for u in mock_dca[-1:]), mock_dca

    def test_age_deeplink(self, servers, page, mock_dca):
        """E2E-408：深链接 /data/fund-dca?age=5 → 首请求带 min_age=5。"""
        _, frontend_port = servers
        page.goto(f"http://127.0.0.1:{frontend_port}/data/fund-dca?age=5")
        expect(page.get_by_role("heading", name="定投止盈回测")).to_be_visible()
        assert mock_dca and any("min_age=5" in u for u in mock_dca), mock_dca

    def test_scale_input_filter_param(self, servers, page, mock_dca):
        """E2E-409：规模输入——填 2 回车 → min_scale=2；填 0 → 不带规模参数。"""
        _, frontend_port = servers
        open_dca(page, frontend_port)
        box = page.get_by_role("spinbutton", name="按资产规模过滤，大于该数值（亿元）")
        box.fill("2")
        box.press("Enter")
        page.wait_for_timeout(900)          # 输入走 400ms 防抖搜索
        assert any("min_scale=2" in u for u in mock_dca), mock_dca
        box.fill("0")
        box.press("Enter")
        page.wait_for_timeout(900)
        assert not any("min_scale" in u for u in mock_dca[-1:]), mock_dca

    def test_scale_deeplink(self, servers, page, mock_dca):
        """E2E-410：深链接 ?scale=0.5 → 首请求带 min_scale=0.5。"""
        _, frontend_port = servers
        page.goto(f"http://127.0.0.1:{frontend_port}/data/fund-dca?scale=0.5")
        expect(page.get_by_role("heading", name="定投止盈回测")).to_be_visible()
        assert any("min_scale=0.5" in u for u in mock_dca), mock_dca

    def test_scatter_section_renders(self, servers, page, mock_dca):
        """E2E-411：三维散点区块——结论卡、canvas、分箱矩阵表渲染。"""
        mock_scatter(page)
        _, frontend_port = servers
        open_dca(page, frontend_port)
        expect(page.get_by_role("heading",
              name="年限 × 规模 × 年化 三维散点")).to_be_visible()
        # 结论卡（分箱口径：每基金只计一次；最优格样本 ≥300）
        expect(page.locator(".dca-conclusion")).to_contain_text(
            "每基金只计一次")
        expect(page.locator(".dca-conclusion")).to_contain_text(
            "≥10 年 × <1 亿")
        # 伪 3D 散点 canvas 渲染
        expect(page.locator(".dca-scatter-chart canvas")).to_have_count(1)
        # 分箱矩阵表：≥10 年表头（行首+列首）与 900 只格子
        expect(page.locator(".dca-matrix th", has_text="≥10 年")).to_have_count(1)
        expect(page.locator(".dca-matrix td", has_text="900 只")).to_have_count(1)

    def test_sort_column_sends_params(self, servers, page, mock_dca):
        """E2E-404：点「年化·全史」列头 → sort=full_xirr&order=asc。"""
        _, frontend_port = servers
        open_dca(page, frontend_port)
        page.locator(".browse-pv th", has_text="年化·全史").click()
        page.wait_for_timeout(400)
        assert any("sort=full_xirr" in u and "order=asc" in u
                   for u in mock_dca), mock_dca

    def test_deeplink_restores_params(self, servers, page, mock_dca):
        """E2E-405：深链接 /data/fund-dca?alive=false 直接带参加载。"""
        _, frontend_port = servers
        page.goto(f"http://127.0.0.1:{frontend_port}/data/fund-dca?alive=false")
        expect(page.get_by_role("heading", name="定投止盈回测")).to_be_visible()
        assert mock_dca and "alive=false" in mock_dca[0], mock_dca

    def test_empty_state_when_no_data(self, servers, page):
        """E2E-406：未导入（total=0）显示导入指引空态，而非空白表格。"""
        def handler(route):
            body = {**LIST_BODY, "total": 0, "rows": [], "stats": None,
                    "meta": None}
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(body))
        page.route("**/api/funds/dca*", handler)
        _, frontend_port = servers
        page.goto(f"http://127.0.0.1:{frontend_port}/data/fund-dca")
        expect(page.locator(".browse-pv .browse-empty")).to_contain_text("暂无回测数据")

# 用例表确认门说明：本功能在自主开发模式下进行，测试计划与用例表未单独
# 走 e2e-test-writer 确认门 1，与前端过目（确认门 2）合并由用户一并确认。
