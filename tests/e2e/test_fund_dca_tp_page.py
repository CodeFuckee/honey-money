"""定投止盈回测页·多止盈点对比 E2E：mock /api/funds/dca/tp* 三端点。

用例表（确认门 1：自主开发模式并入最终前端过目，同 test_fund_dca_page 尾注约定）：
- E2E-424 P0 阈值切换主流程：点搜索栏「15%」→ 列表请求 /api/funds/dca/tp?tp=0.15，
  副标题止盈线变 15%，表格渲染 tp 端点行；
- E2E-425 对比区块渲染：八档表格（5%~40%）+ SVG 折线，默认 20% 行高亮；
- E2E-426 对比行点击切换：点「30%」行 → 列表请求 tp=0.30，高亮行转移；
- E2E-427 弹窗阈值跟随：15% 下点行「详情」→ 请求 /api/funds/dca/tp/{code}/rounds
  且带 tp=0.15，弹窗副标题显示止盈线 15%；
- E2E-428 回默认走旧端点：切 15% 再切回 20% → 最后列表请求为 /api/funds/dca
  （旧端点，无 tp 查询参数）；
- E2E-429 深链接：/data/fund-dca?tp=0.25 → 首个列表请求即带 tp=0.25；
- E2E-430 费率口径联动：切「不含费」→ 对比请求带 fee=gross。
- E2E-431 弹窗内档位切换：弹窗按钮组切 15% → tp rounds 请求 + 统计/表跟随，
  页面级阈值不变（弹窗局部状态）；
- E2E-432 全档位止盈点叠加：净值曲线 scatter series 仅含有轮次档位（15%/20%）、
  颜色互不相同（__dcaRoundsChart 钩子断言 option）；
- E2E-433 弹窗总收益率/年化卡：显示当前档位+费率口径的 w10 值，切档位/费率跟随；
- E2E-434 弹窗费率切换：切 gross → rounds 请求带 fee=gross、卡值取原列，
  页面级费率按钮不受波及（弹窗局部状态）。
运行：python -m pytest tests/e2e/test_fund_dca_tp_page.py
"""

import json
import re
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import expect

from test_fund_dca_page import LIST_BODY, mock_dca, open_dca

# 多阈值排行行（结构同旧端点 + tp 一列）；mock tp=0.15，端点回显实际 tp
TP_LIST_BODY = {
    "status": "up", "tp": 0.15, "total": 101, "limit": 50, "offset": 0,
    "rows": [
        {"tp": 0.15, "code": "019387", "name": "中欧时代共赢混合发起A1",
         "fund_type": "混合型-偏股", "alive": True, "scale_yi": 1.2,
         "scale_date": "2026-09-30", "age_years": 2.9,
         "w10_start": "2023-11-28", "w10_end": "2026-10-28", "w10_months": 36,
         "w10_rounds": 11, "w10_invested": 36000, "w10_final_value": 42120,
         "w10_profit_rate": 0.17, "w10_xirr": 0.92, "gap_avg_days": 82.1,
         "full_xirr": 0.92, "full_rounds": 11, "full_profit_rate": 0.17,
         "full_months": 36},
        {"tp": 0.15, "code": "161725", "name": "招商中证白酒指数(LOF)A",
         "fund_type": "指数型-股票", "alive": True, "scale_yi": 209.0,
         "scale_date": "2026-09-30", "age_years": 10.4,
         "w10_start": "2016-10-31", "w10_end": "2026-10-28", "w10_months": 121,
         "w10_rounds": 13, "w10_invested": 121000, "w10_final_value": 114950,
         "w10_profit_rate": -0.05, "w10_xirr": -0.048, "gap_avg_days": None,
         "full_xirr": -0.041, "full_rounds": 13, "full_profit_rate": -0.05,
         "full_months": 138},
    ],
    "stats": {"count": 13557, "p25": 0.03, "median": 0.055, "p75": 0.12,
              "p90": 0.2, "positive_share": 0.86, "take_profit_share": 0.71},
    "meta": {"generated_at": "2026-11-15T02:00:00+00:00", "fund_count": 13557,
             "params": {"amount": 1000, "thresholds": [0.05, 0.1, 0.15, 0.2,
                                                       0.25, 0.3],
                        "window_end": "2026-11-13"}},
}


def _cmp_row(tp: float, med: float) -> dict:
    return {"tp": tp, "count": 8000,
            "w10_p25": round(med * 0.5, 4), "w10_median": med,
            "w10_p75": round(med * 1.5, 4), "w10_p90": round(med * 2.0, 4),
            "w10_mean": round(med * 0.95, 4), "w10_positive_share": 0.8,
            "w10_success_share": round(0.95 - tp, 4), "w10_avg_rounds": 12.5,
            "full_median": round(med * 0.9, 4), "full_mean": round(med * 0.88, 4),
            "full_positive_share": 0.79, "full_success_share": 0.5,
            "avg_gap_days": round(60 + tp * 300, 1)}


# 六档中位年化：非单调形状（5% 费耗拖累、30% 够不着），折线才有可读性
TP_COMPARE_BODY = {
    "status": "up", "fee": "nom", "equity": True,
    "thresholds": [0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.35, 0.4],
    "rows": [_cmp_row(0.05, 0.040), _cmp_row(0.10, 0.055),
             _cmp_row(0.15, 0.065), _cmp_row(0.20, 0.070),
             _cmp_row(0.25, 0.068), _cmp_row(0.30, 0.062),
             _cmp_row(0.35, 0.058), _cmp_row(0.40, 0.050)],
}

TP_ROUNDS_BODY = {
    "status": "up", "code": "019387", "fee": "nom", "tp": 0.15,
    "rounds": [
        {"seq": 1, "tp_date": "2024-03-05", "round_days": 68, "gap_days": 68,
         "profit": 0.163, "tp_nav": 1.163, "invested": 3000.0,
         "sub_fee": 4.43, "redeem_fee": 15.0, "net_profit": 460.2},
    ],
    "totals": {"rounds": 1, "invested": 3000.0, "net_profit": 460.2,
               "sub_fee": 4.43, "redeem_fee": 15.0},
}

# 20% 档独立形状（日期/轮次不同，供多档位叠加与切换断言区分）
TP_ROUNDS_20_BODY = {
    "status": "up", "code": "019387", "fee": "nom", "tp": 0.20,
    "rounds": [
        {"seq": 1, "tp_date": "2024-06-20", "round_days": 171, "gap_days": 171,
         "profit": 0.243, "tp_nav": 1.243, "invested": 6000.0,
         "sub_fee": 8.86, "redeem_fee": 30.0, "net_profit": 1420.4},
    ],
    "totals": {"rounds": 1, "invested": 6000.0, "net_profit": 1420.4,
               "sub_fee": 8.86, "redeem_fee": 30.0},
}

_EMPTY_ROUNDS = {"rounds": 0, "invested": 0, "net_profit": 0,
                 "sub_fee": 0, "redeem_fee": 0}


def _rounds_body_for(tp: float) -> dict:
    """按 tp 返回独立形状：15%/20% 各有 1 轮（多档位叠加断言），其余档空。"""
    if tp == 0.15:
        return TP_ROUNDS_BODY
    if tp == 0.20:
        return TP_ROUNDS_20_BODY
    return {"status": "up", "code": "019387", "fee": "nom", "tp": tp,
            "rounds": [], "totals": dict(_EMPTY_ROUNDS)}


# 单基金全阈值总收益率/年化汇总（gross 原列 / _disc / _nom 后缀，w10 窗口）；
# 15%/20% 取不同值供切换断言
TP_SUMMARY_BODY = {
    "status": "up", "code": "019387",
    "rows": [
        {"tp": 0.05, "w10_profit_rate": 0.30, "w10_xirr": 0.40,
         "w10_profit_rate_disc": 0.29, "w10_xirr_disc": 0.38,
         "w10_profit_rate_nom": 0.28, "w10_xirr_nom": 0.36},
        {"tp": 0.10, "w10_profit_rate": 0.22, "w10_xirr": 0.60,
         "w10_profit_rate_disc": 0.21, "w10_xirr_disc": 0.57,
         "w10_profit_rate_nom": 0.20, "w10_xirr_nom": 0.55},
        {"tp": 0.15, "w10_profit_rate": 0.16, "w10_xirr": 0.90,
         "w10_profit_rate_disc": 0.158, "w10_xirr_disc": 0.885,
         "w10_profit_rate_nom": 0.155, "w10_xirr_nom": 0.87},
        {"tp": 0.20, "w10_profit_rate": 0.18, "w10_xirr": 1.003,
         "w10_profit_rate_disc": 0.175, "w10_xirr_disc": 0.96,
         "w10_profit_rate_nom": 0.17, "w10_xirr_nom": 0.92},
        {"tp": 0.25, "w10_profit_rate": 0.12, "w10_xirr": 0.70,
         "w10_profit_rate_disc": 0.115, "w10_xirr_disc": 0.68,
         "w10_profit_rate_nom": 0.11, "w10_xirr_nom": 0.65},
        {"tp": 0.30, "w10_profit_rate": None, "w10_xirr": None,
         "w10_profit_rate_disc": None, "w10_xirr_disc": None,
         "w10_profit_rate_nom": None, "w10_xirr_nom": None},
        {"tp": 0.35, "w10_profit_rate": None, "w10_xirr": None,
         "w10_profit_rate_disc": None, "w10_xirr_disc": None,
         "w10_profit_rate_nom": None, "w10_xirr_nom": None},
        {"tp": 0.40, "w10_profit_rate": None, "w10_xirr": None,
         "w10_profit_rate_disc": None, "w10_xirr_disc": None,
         "w10_profit_rate_nom": None, "w10_xirr_nom": None},
    ],
}


@pytest.fixture
def mock_tp(page):
    """mock /api/funds/dca/tp*（排行/对比/轮次三端点，记录请求 URL 供参数断言）。

    需在 mock_dca 之后注册：Playwright 后注册者优先匹配，tp URL 不会漏进
    旧端点桩（stub 的 dca* 通配同样盖到 tp 路径）。"""
    calls = []

    def handler(route):
        url = route.request.url
        calls.append(url)
        qs = parse_qs(urlparse(url).query)
        if re.search(r"/api/funds/dca/tp/[^/?]+/summary", url):
            body = {**TP_SUMMARY_BODY,
                    "rows": [{**r, "age_years": 10.4}
                             for r in TP_SUMMARY_BODY["rows"]]}
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(body))
        elif "/api/funds/dca/tp/compare" in url:
            body = {**TP_COMPARE_BODY,
                    "fee": (qs.get("fee") or ["nom"])[0]}
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(body))
        elif re.search(r"/api/funds/dca/tp/[^/?]+/rounds", url):
            body = _rounds_body_for(float((qs.get("tp") or ["0.2"])[0]))
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(body))
        else:
            body = {**TP_LIST_BODY,
                    "tp": float((qs.get("tp") or ["0.2"])[0])}
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(body))

    page.route("**/api/funds/dca/tp**", handler)
    return calls


@pytest.fixture
def mock_rounds_navs(page):
    """弹窗依赖：轮次明细旧端点放行（mock_tp 已盖 tp 轮次）+ 净值曲线两桩。"""
    page.route("**/api/funds/dca/019387/rounds*", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps({
            "status": "up", "code": "019387", "fee": "nom", "rounds": [],
            "totals": {"rounds": 0, "invested": 0, "net_profit": 0,
                       "sub_fee": 0, "redeem_fee": 0}})))
    page.route("**/api/funds/019387/navs*", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps({
            "status": "up", "code": "019387", "navs": [
                {"date": "2023-11-28", "unit_nav": 1.0, "accum_nav": 1.0,
                 "pct_chg": 0, "unit_money": None, "source": "fund_em"},
                {"date": "2024-03-05", "unit_nav": 1.163, "accum_nav": 1.163,
                 "pct_chg": 1.0, "unit_money": None, "source": "fund_em"},
                {"date": "2024-06-20", "unit_nav": 1.243, "accum_nav": 1.243,
                 "pct_chg": 1.0, "unit_money": None, "source": "fund_em"},
                {"date": "2026-10-28", "unit_nav": 1.1, "accum_nav": 1.1,
                 "pct_chg": 0.2, "unit_money": None, "source": "fund_em"}]})))


class TestDcaTp:
    def test_tp_switcher_switch_and_request(self, servers, page, mock_dca,
                                            mock_tp, mock_rounds_navs):
        """E2E-424 P0：点「15%」→ tp 列表请求 + 副标题止盈线 15% + tp 行渲染。"""
        _, frontend_port = servers
        open_dca(page, frontend_port)
        page.locator('button[data-tp="0.15"]').click()
        expect(page.locator(".tp-compare tr.tp-row-active")
              ).to_contain_text("15%", timeout=10_000)
        assert "tp=0.15" in mock_tp[-1], mock_tp[-1]
        row = page.locator(".browse-pv tbody tr", has_text="019387")
        expect(row).to_contain_text("+92.0%")     # tp 行自己的年化
        expect(row).to_contain_text("11")          # 15% 阈值止盈次数

    def test_compare_block_renders(self, servers, page, mock_dca, mock_tp):
        """E2E-425：六档表格 + SVG 折线渲染，默认 20% 行高亮。"""
        _, frontend_port = servers
        open_dca(page, frontend_port)
        expect(page.locator(".tp-compare tbody tr")).to_have_count(8)
        for label in ("5%", "10%", "15%", "20%", "25%", "30%", "35%", "40%"):
            expect(page.locator(".tp-compare tbody tr td:first-child")
                  .filter(has_text=re.compile(rf"^{re.escape(label)}"))
                  ).to_be_visible()
        expect(page.locator(".tp-chart polyline")).to_have_count(1)
        expect(page.locator(".tp-chart circle")).to_have_count(8)
        expect(page.locator(".tp-compare tr.tp-row-active")
              ).to_contain_text("20%")
        expect(page.locator(".tp-compare")).to_contain_text("7.0%")

    def test_compare_row_click_switches(self, servers, page, mock_dca,
                                        mock_tp, mock_rounds_navs):
        """E2E-426：点对比表「30%」行 → 列表请求 tp=0.30，高亮转移。"""
        _, frontend_port = servers
        open_dca(page, frontend_port)
        page.locator(".tp-compare tbody tr", has_text="30%").click()
        expect(page.locator(".tp-compare tr.tp-row-active")
              ).to_contain_text("30%", timeout=10_000)
        assert "tp=0.3" in mock_tp[-1], mock_tp[-1]

    def test_rounds_modal_follows_tp(self, servers, page, mock_dca, mock_tp,
                                     mock_rounds_navs):
        """E2E-427：15% 下点「详情」→ tp 轮次请求带 tp=0.15，弹窗副标题 15%。"""
        _, frontend_port = servers
        open_dca(page, frontend_port)
        page.locator('button[data-tp="0.15"]').click()
        expect(page.locator(".tp-compare tr.tp-row-active")
              ).to_contain_text("15%", timeout=10_000)
        page.locator(".browse-pv tbody tr", has_text="019387") \
            .locator(".detail-btn").click()
        dlg = page.locator(".p-dialog", has_text="每次止盈明细")
        expect(dlg).to_be_visible()
        # 副标题口径文字已删（2026-11-16 用户要求），状态以按钮 active 表达
        expect(dlg.locator('button[data-modal-tp="0.15"]')
              ).to_have_class(re.compile("active"))
        expect(dlg).to_contain_text("运作 10.4 年")  # 弹窗 header 运作年限
        expect(dlg).to_contain_text("16.3%")       # 轮次市值口径收益
        rounds_calls = [u for u in mock_tp
                        if re.search(r"/api/funds/dca/tp/[^/?]+/rounds", u)]
        # 弹窗打开会并发拉全档位 rounds；只断言“存在当前档位请求”
        assert any("tp=0.15" in u for u in rounds_calls), mock_tp

    def test_switch_back_uses_legacy_endpoint(self, servers, page, mock_dca,
                                              mock_tp, mock_rounds_navs):
        """E2E-428：15% → 20% 后，最后列表请求为旧端点（无 tp 参数）。"""
        _, frontend_port = servers
        open_dca(page, frontend_port)
        page.locator('button[data-tp="0.15"]').click()
        expect(page.locator(".tp-compare tr.tp-row-active")
              ).to_contain_text("15%", timeout=10_000)
        page.locator('button[data-tp="0.2"]').click()
        expect(page.locator(".tp-compare tr.tp-row-active")
              ).to_contain_text("20%", timeout=10_000)
        list_calls = [u for u in mock_dca
                      if "/api/funds/dca?" in u or
                      re.search(r"/api/funds/dca\?", u)]
        assert list_calls, "应回落旧端点"
        assert "tp=" not in list_calls[-1], list_calls[-1]

    def test_tp_deeplink(self, servers, page, mock_dca, mock_tp):
        """E2E-429：?tp=0.25 深链接 → 首个列表请求即带 tp=0.25。"""
        _, frontend_port = servers
        page.goto(f"http://127.0.0.1:{frontend_port}/data/fund-dca?tp=0.25")
        expect(page.get_by_role("heading", name="定投止盈回测")).to_be_visible()
        expect(page.locator(".tp-compare tr.tp-row-active")
              ).to_contain_text("25%", timeout=10_000)
        list_calls = [u for u in mock_tp
                      if "/rounds" not in u and "/compare" not in u]
        assert list_calls and "tp=0.25" in list_calls[0], mock_tp

    def test_fee_toggle_hits_compare(self, servers, page, mock_dca, mock_tp):
        """E2E-430：切「不含费」→ 对比请求带 fee=gross（口径联动对比区块）。"""
        _, frontend_port = servers
        open_dca(page, frontend_port)
        expect(page.locator(".tp-compare tbody tr")).to_have_count(8)
        page.locator('button[data-table-fee="gross"]').click()
        expect(page.locator(".tp-compare tr.tp-row-active")
              ).to_contain_text("20%", timeout=10_000)
        compare_calls = [u for u in mock_tp if "/compare" in u]
        assert compare_calls and "fee=gross" in compare_calls[-1], mock_tp

    def test_modal_tp_switcher_local(self, servers, page, mock_dca, mock_tp,
                                     mock_rounds_navs):
        """E2E-431：弹窗内按钮组切 15% → tp rounds 请求 + 表跟随；
        页面级阈值仍为 20%（弹窗局部状态独立）。"""
        _, frontend_port = servers
        open_dca(page, frontend_port)
        page.locator(".browse-pv tbody tr", has_text="019387") \
            .locator(".detail-btn").click()
        dlg = page.locator(".p-dialog", has_text="每次止盈明细")
        expect(dlg).to_be_visible()
        expect(dlg.locator('button[data-modal-tp="0.15"]')).to_be_visible()
        expect(dlg.locator('button[data-modal-tp="0.2"]')
              ).to_have_class(re.compile("active"))   # 初始 = 页面阈值
        dlg.locator('button[data-modal-tp="0.15"]').click()
        expect(dlg.locator('button[data-modal-tp="0.15"]')
              ).to_have_class(re.compile("active"), timeout=10_000)
        expect(dlg).to_contain_text("第 1 次")          # 15% 轮次表有数据
        rounds_calls = [u for u in mock_tp
                        if re.search(r"/api/funds/dca/tp/[^/?]+/rounds", u)]
        assert any("tp=0.15" in u for u in rounds_calls), mock_tp
        # 页面级阈值未被弹窗切换波及
        page_btn = page.locator('button[data-tp="0.2"]')
        expect(page_btn).to_have_class(re.compile("active"))

    def test_modal_chart_overlays_all_thresholds(self, servers, page,
                                                 mock_dca, mock_tp,
                                                 mock_rounds_navs):
        """E2E-432：净值曲线同时叠加全部有轮次档位的止盈三角且颜色互不相同
        （15%/20% mock 有数据 → scatter series = 2）。"""
        _, frontend_port = servers
        open_dca(page, frontend_port)
        page.locator(".browse-pv tbody tr", has_text="019387") \
            .locator(".detail-btn").click()
        dlg = page.locator(".p-dialog", has_text="每次止盈明细")
        expect(dlg).to_be_visible()
        # 全档位 rounds（8 并发）到达后重渲染：scatter series = 15%/20% 两档
        page.wait_for_function(
            "window.__dcaRoundsChart && "
            "window.__dcaRoundsChart.getOption().series"
            ".filter(s => s.type === 'scatter').length >= 2",
            timeout=15_000)
        info = page.evaluate(
            "(() => { const s = window.__dcaRoundsChart.getOption().series"
            ".filter(x => x.type === 'scatter');"
            "return { names: s.map(x => x.name),"
            "colors: s.map(x => x.itemStyle && x.itemStyle.color) }; })()")
        assert set(info["names"]) == {"15%", "20%"}, info
        assert len(info["colors"]) == 2 and info["colors"][0] != info["colors"][1], info

    def test_modal_summary_cards_follow_tp_and_fee(self, servers, page,
                                                   mock_dca, mock_tp,
                                                   mock_rounds_navs):
        """E2E-433：弹窗总收益率/年化卡显示当前档位+费率口径的 w10 值，
        切档位、切费率均跟随。"""
        _, frontend_port = servers
        open_dca(page, frontend_port)
        page.locator(".browse-pv tbody tr", has_text="019387") \
            .locator(".detail-btn").click()
        dlg = page.locator(".p-dialog", has_text="每次止盈明细")
        expect(dlg).to_be_visible()
        grid = dlg.locator(".dc-grid").first
        # 初始：页面 tp=20% + 弹窗费率 nom → 0.20 行 nom 后缀
        expect(grid).to_contain_text("总收益率·近10年")
        expect(grid).to_contain_text("+17.0%")
        expect(grid).to_contain_text("年化 XIRR·近10年")
        expect(grid).to_contain_text("+92.0%")
        # 切弹窗档位 15% → 0.15 行 nom
        dlg.locator('button[data-modal-tp="0.15"]').click()
        expect(grid).to_contain_text("+15.5%", timeout=10_000)
        expect(grid).to_contain_text("+87.0%")
        # 切弹窗费率 gross → 0.15 行原列
        dlg.locator('button[data-modal-fee="gross"]').click()
        expect(grid).to_contain_text("+16.0%", timeout=10_000)
        expect(grid).to_contain_text("+90.0%")

    def test_modal_fee_switcher_local(self, servers, page, mock_dca, mock_tp,
                                      mock_rounds_navs):
        """E2E-434：弹窗内切 gross → rounds 请求带 fee=gross；
        页面级费率按钮（data-table-fee=nom）active 不变。"""
        _, frontend_port = servers
        open_dca(page, frontend_port)
        page.locator(".browse-pv tbody tr", has_text="019387") \
            .locator(".detail-btn").click()
        dlg = page.locator(".p-dialog", has_text="每次止盈明细")
        expect(dlg).to_be_visible()
        expect(dlg.locator('button[data-modal-fee="nom"]')
              ).to_have_class(re.compile("active"))
        dlg.locator('button[data-modal-fee="gross"]').click()
        expect(dlg.locator('button[data-modal-fee="gross"]')
              ).to_have_class(re.compile("active"), timeout=10_000)
        rounds_calls = [u for u in mock_tp
                        if re.search(r"/api/funds/dca/tp/[^/?]+/rounds", u)
                        and "fee=gross" in u]
        assert rounds_calls, mock_tp
        page_fee_btn = page.locator('button[data-table-fee="nom"]')
        expect(page_fee_btn).to_have_class(re.compile("active"))
