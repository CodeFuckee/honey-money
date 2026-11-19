"""定投止盈回测页「最近一次止盈后」当前轮统计 E2E（mock /api/funds/dca*）。

用例表（确认门 1：2026-11-19 用户确认 6 条；确认门 2 与前端过目合并）：
- E2E-444 P0 表格两列渲染：排行表新增「止盈后收益率/止盈后年化」列，
  按当前费率口径（默认 nom）取后缀列（+11.0% / +43.0%）；
- E2E-445 P1 未止盈基金：全史从未止盈（cur 字段 null/缺失）→ 两列显示 —；
- E2E-446 P1 费率口径切换：切「不含费」→ 两列取原列（+12.0% / +45.0%）；
- E2E-447 P1 列显隐：MultiSelect 取消勾选「止盈后年化」→ 表头消失，
  重新勾选恢复；
- E2E-448 P1 弹窗四卡：详情弹窗「止盈后当前轮」四卡（上次止盈日·全史 /
  本轮已投入 / 止盈后收益率 / 止盈后年化）按弹窗费率口径（nom）渲染
  summary 当前档位行（2025-03-17 / 21,000 元 / +11.0% / +43.0%）；
- E2E-449 P1 弹窗费率切换：切 gross → 四卡取原列（2025-03-14 /
  +12.0% / +45.0%，已投入不变）。
运行：python -m pytest tests/e2e/test_dca_current_round_page.py
"""

import copy
import json
import re
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import expect

from test_fund_dca_page import LIST_BODY, open_dca
from test_fund_dca_tp_page import TP_COMPARE_BODY

# 019387（止盈过）当前轮三口径字段：gross 原列 / disc / nom 后缀；
# 止盈日随费用口径位移（nom 晚 3 天）供 E2E-449 观察取列切换
CUR_FIELDS = {
    "full_last_tp": "2025-03-14", "full_last_tp_disc": "2025-03-14",
    "full_last_tp_nom": "2025-03-17",
    "full_cur_invested": 21000,
    "full_cur_profit_rate": 0.12, "full_cur_profit_rate_disc": 0.115,
    "full_cur_profit_rate_nom": 0.11,
    "full_cur_xirr": 0.45, "full_cur_xirr_disc": 0.44,
    "full_cur_xirr_nom": 0.43,
}
# 从未止盈行：当前轮十字段全 None（E2E-445 显示 —）
_NO_CUR_ROW = {
    "code": "006327", "name": "中银丰和定期开放债券",
    "fund_type": "债券型-长债", "alive": True, "scale_yi": 12.0,
    "scale_date": "2026-09-30", "age_years": 6.2,
    "w10_start": "2016-10-31", "w10_end": "2026-10-28", "w10_months": 121,
    "w10_rounds": 0, "w10_invested": 121000, "w10_final_value": 128000,
    "w10_profit_rate": 0.058, "w10_xirr": 0.032, "gap_avg_days": None,
    "full_xirr": 0.031, "full_rounds": 0, "full_profit_rate": 0.056,
    "full_months": 121,
    **{k: None for k in CUR_FIELDS},
}


def _cur_rows() -> list:
    rows = copy.deepcopy(LIST_BODY["rows"])
    rows[0] = {**rows[0], **CUR_FIELDS}          # 019387 止盈过
    rows[1] = {**rows[1], **{k: None for k in CUR_FIELDS}}   # 161725 未止盈口径
    return rows + [dict(_NO_CUR_ROW)]


CUR_LIST_BODY = {**LIST_BODY, "rows": _cur_rows()}
CUR_TP_LIST_BODY = {**CUR_LIST_BODY, "tp": 0.2, "total": 102}


def _cur_summary_body() -> dict:
    """summary 八档行：tp=0.20（页面默认档）带当前轮字段，其余档 None。"""
    rows = []
    for tp in (0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40):
        rows.append({"tp": tp, "w10_profit_rate": None, "w10_xirr": None,
                     "w10_profit_rate_disc": None, "w10_xirr_disc": None,
                     "w10_profit_rate_nom": None, "w10_xirr_nom": None,
                     **({**CUR_FIELDS} if tp == 0.20
                        else {k: None for k in CUR_FIELDS})})
    return {"status": "up", "code": "019387", "rows": rows}


_EMPTY_ROUNDS = {"status": "up", "rounds": [],
                 "totals": {"rounds": 0, "invested": 0, "net_profit": 0,
                            "sub_fee": 0, "redeem_fee": 0}}
_NAVS_BODY = {"status": "up", "code": "019387", "navs": [
    {"date": "2023-11-28", "unit_nav": 1.0, "accum_nav": 1.0, "pct_chg": 0,
     "unit_money": None, "source": "fund_em"},
    {"date": "2025-03-14", "unit_nav": 1.4, "accum_nav": 1.4, "pct_chg": 1.0,
     "unit_money": None, "source": "fund_em"},
    {"date": "2026-10-28", "unit_nav": 1.2, "accum_nav": 1.2, "pct_chg": 0.2,
     "unit_money": None, "source": "fund_em"}]}


@pytest.fixture
def mock_cur(page):
    """mock /api/funds/dca* 全家桶（旧列表/tp 列表/summary/轮次/对比）+
    净值曲线桩；记录请求 URL 供断言。scatter/full-backtest grid 放行
    （死 DSN → down，不污染列表断言）。"""
    calls = []

    def handler(route):
        url = route.request.url
        if "/api/funds/dca/scatter" in url or "/api/funds/dca/grid" in url:
            route.continue_()
            return
        calls.append(url)
        qs = parse_qs(urlparse(url).query)
        fee = (qs.get("fee") or ["nom"])[0]
        if re.search(r"/api/funds/dca/tp/[^/?]+/summary", url):
            body = _cur_summary_body()
        elif re.search(r"/api/funds/dca/tp/[^/?]+/rounds", url):
            body = {**_EMPTY_ROUNDS, "tp": float((qs.get("tp") or ["0.2"])[0])}
        elif "/api/funds/dca/tp/compare" in url:
            body = {**TP_COMPARE_BODY, "fee": fee}
        elif urlparse(url).path.endswith("/api/funds/dca/tp"):
            body = {**CUR_TP_LIST_BODY, "fee": fee}
        elif re.search(r"/api/funds/dca/[^/?]+/rounds", url):
            body = {**_EMPTY_ROUNDS, "fee": fee}
        else:
            body = {**CUR_LIST_BODY, "fee": fee}
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(body))

    # 注意 glob 用 dca**（** 跨 /）：summary/rounds 子路径也要拦截，
    # dca* 只匹配旧列表一层路径（2026-11-19 实测漏拦 summary 落到死后端）
    page.route("**/api/funds/dca**", handler)
    page.route("**/api/funds/019387/navs*", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body=json.dumps(_NAVS_BODY)))
    return calls


def _open_detail(page, frontend_port):
    open_dca(page, frontend_port)
    page.locator(".browse-pv tbody tr", has_text="019387") \
        .locator(".detail-btn").click()
    dlg = page.locator(".p-dialog", has_text="每次止盈明细")
    expect(dlg).to_be_visible()
    return dlg


class TestDcaCurrentRound:
    def test_table_columns_render_by_fee(self, servers, page, mock_cur):
        """E2E-444：排行表新增「止盈后收益率/止盈后年化」列，默认 nom 口径
        取后缀列（+11.0% / +43.0%）。"""
        _, frontend_port = servers
        open_dca(page, frontend_port)
        expect(page.locator(".browse-pv th", has_text="止盈后收益率")
              ).to_be_visible()
        expect(page.locator(".browse-pv th", has_text="止盈后年化")
              ).to_be_visible()
        row = page.locator(".browse-pv tbody tr", has_text="019387")
        expect(row).to_contain_text("+11.0%")
        expect(row).to_contain_text("+43.0%")

    def test_never_took_profit_shows_dash(self, servers, page, mock_cur):
        """E2E-445：全史从未止盈（cur 字段 null）→ 两列显示 —。"""
        _, frontend_port = servers
        open_dca(page, frontend_port)
        row = page.locator(".browse-pv tbody tr", has_text="006327")
        expect(row).to_be_visible()
        expect(row).to_contain_text("—")
        assert "+12.0%" not in row.inner_text()
        assert "+45.0%" not in row.inner_text()

    def test_fee_switch_takes_raw_columns(self, servers, page, mock_cur):
        """E2E-446：切「不含费」→ 两列取原列（+12.0% / +45.0%）。"""
        _, frontend_port = servers
        open_dca(page, frontend_port)
        page.locator("[data-fee='gross']").first.click()
        row = page.locator(".browse-pv tbody tr", has_text="019387")
        expect(row).to_contain_text("+12.0%")
        expect(row).to_contain_text("+45.0%")

    def test_column_visibility_toggle(self, servers, page, mock_cur):
        """E2E-447：列显隐 MultiSelect——取消勾选「止盈后年化」→ 表头
        消失；重新勾选恢复（E2E-437 同款交互防 detach 竞态）。"""
        _, frontend_port = servers
        open_dca(page, frontend_port)
        expect(page.locator(".browse-pv th", has_text="止盈后年化")
              ).to_be_visible()
        sel = page.locator(".funds-search .dca-col-select")
        sel.click()
        page.locator(".p-multiselect-option", has_text="止盈后年化").click()
        expect(page.locator(".browse-pv th", has_text="止盈后年化")
              ).to_have_count(0)
        # 其余新列不受影响
        expect(page.locator(".browse-pv th", has_text="止盈后收益率")
              ).to_be_visible()
        page.get_by_role("heading", name="定投止盈回测").click()
        sel.click()
        page.locator(".p-multiselect-option", has_text="止盈后年化").click()
        expect(page.locator(".browse-pv th", has_text="止盈后年化")
              ).to_be_visible()

    def test_modal_cards_render(self, servers, page, mock_cur):
        """E2E-448：详情弹窗「止盈后当前轮」四卡按弹窗费率（nom）渲染
        summary 当前档位行：2025-03-17 / 21,000 元 / +11.0% / +43.0%。"""
        _, frontend_port = servers
        dlg = _open_detail(page, frontend_port)
        grid = dlg.locator(".dc-grid").nth(1)
        expect(grid).to_contain_text("上次止盈日")
        expect(grid).to_contain_text("2025-03-17")
        expect(grid).to_contain_text("本轮已投入")
        expect(grid).to_contain_text("21,000 元")
        expect(grid).to_contain_text("止盈后收益率")
        expect(grid).to_contain_text("+11.0%")
        expect(grid).to_contain_text("止盈后年化")
        expect(grid).to_contain_text("+43.0%")

    def test_modal_fee_switch_follows(self, servers, page, mock_cur):
        """E2E-449：弹窗内切 gross → 四卡取原列（2025-03-14 / +12.0% /
        +45.0%，本轮已投入与费用无关保持 21,000 元）。"""
        _, frontend_port = servers
        dlg = _open_detail(page, frontend_port)
        dlg.locator("[data-modal-fee='gross']").click()
        grid = dlg.locator(".dc-grid").nth(1)
        expect(grid).to_contain_text("2025-03-14")
        expect(grid).to_contain_text("21,000 元")
        expect(grid).to_contain_text("+12.0%")
        expect(grid).to_contain_text("+45.0%")
