"""量化教学页（六大经典回测策略）E2E：mock /api/backtest 与个股详情接口。

覆盖：策略导航与原理卡、参数面板、回测结果（净值图/指标/交易表）、撮合模式
切换入参、轮动股票池输入、详情页联动入口、空数据空态。
"""

import json
import re

import pytest
from playwright.sync_api import expect

def _bt_result(code, total_return):
    return {
        "status": "up", "codes": [code], "strategy": "donchian",
        "range": "3y", "exec_mode": "next_open", "adjust_applied": True,
        "metrics": {"total_return": total_return, "annual_return": 0.08,
                    "max_drawdown": 0.18, "sharpe": 0.85, "win_rate": 0.5,
                    "profit_loss_ratio": 1.8, "trades": 3, "vol_days": 750},
        "equity": [[f"2026-0{i}-0{j}", 1.0 + total_return * (i / 3.0),
                    1.0] for i in range(1, 4) for j in range(1, 6)],
        "drawdown": [[f"2026-0{i}-0{j}", -0.01 * i] for i in range(1, 4)
                     for j in range(1, 6)],
        "trades": [
            {"entry_date": "2026-01-06", "exit_date": "2026-02-04",
             "entry_price": 10.5, "exit_price": 11.2, "pnl_pct": 0.0667,
             "direction": "long", "symbol": code},
        ], "gap_dates": [],
    }


# 单股默认（600519 +26%）；批量按 codes[0] 返回不同收益（对比排序断言用）
BT_RESULT = _bt_result("600519", 0.26)
BT_BY_CODE = {"600519": 0.26, "000001": 0.05, "000002": -0.10}

BT_EMPTY = {"status": "up", "metrics": {}, "equity": [], "drawdown": [],
            "trades": [], "gap_dates": [],
            "message": "区间内无行情数据（请检查代码或扩大区间）"}

LIST_BODY = {"status": "up", "total": 1, "limit": 50, "offset": 0, "stocks": [
    {"code": "600519", "name": "贵州茅台", "close": 1500.0, "pct_chg": 1.2,
     "last_date": "2026-10-09", "bars": 250, "first_date": "2001-08-27",
     "sources": "sina"}]}


@pytest.fixture
def api_calls(page):
    """mock /api/backtest（可改返回）与 /api/stocks（详情页联动用）。"""
    calls = []
    holder = {"bt": BT_RESULT}

    def handler(route):
        req = route.request
        url = req.url

        def fulfill_json(obj):
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(obj))

        if re.search(r"/api/backtest/full", url):
            # 全量回测端点（教学页进入时会拉历史列表）：与单股回测分开 mock，
            # 避免误入 calls 计数与单股响应分支
            if re.search(r"/runs$", url):
                fulfill_json({"runs": []})
            else:
                route.fulfill(status=404, content_type="application/json",
                              body=json.dumps({"detail": "任务不存在"}))
            return
        if re.search(r"/api/backtest", url):
            calls.append({"url": url, "body": req.post_data})
            if callable(holder["bt"]):
                fulfill_json(holder["bt"](url, req))
            else:
                fulfill_json(holder["bt"])
            return
        if re.search(r"/api/stocks/basic", url):
            fulfill_json({"status": "up", "total": 3, "stocks": [
                {"code": "600519", "name": "贵州茅台"},
                {"code": "000001", "name": "平安银行"},
                {"code": "000002", "name": "万科A"}]})
            return
        if re.search(r"/api/stocks(\?|$)", url):
            fulfill_json(LIST_BODY)
            return
        route.continue_()

    page.route("**/api/**", handler)
    return calls, holder


def open_learn(page, frontend_port):
    page.goto(f"http://127.0.0.1:{frontend_port}/")
    page.get_by_role("button", name="教学").click()
    expect(page.get_by_role("heading", name="量化教学")).to_be_visible()


class TestLearnPage:
    def test_six_strategies_and_principle_cards(self, servers, page, api_calls):
        """P0：六个策略导航 + 原理卡/教学点随选择切换。"""
        _, frontend_port = servers
        open_learn(page, frontend_port)
        for name in ("买入持有", "双均线交叉", "通道突破（海龟）", "布林带均值回归",
                     "网格交易", "动量轮动"):
            expect(page.locator(".learn-nav", has_text=name)).to_be_visible()
        page.locator(".learn-nav", has_text="布林带均值回归").click()
        expect(page.locator(".learn-brief")).to_contain_text("赌偏离修复")
        expect(page.locator(".learn-lessons")).to_contain_text("单边下跌")

    def test_run_backtest_renders_results(self, servers, page, api_calls):
        """P0：跑回测 → 净值图 canvas + 七项指标 + 交易明细表。"""
        calls, _ = api_calls
        _, frontend_port = servers
        open_learn(page, frontend_port)
        page.locator(".learn-nav", has_text="通道突破").click()
        self._pick(page, "600519")
        resp_body = {}

        def _capture(resp):
            if "/api/backtest" in resp.url:
                resp_body["text"] = resp.text()

        page.on("response", _capture)
        page.get_by_role("button", name="▶ 跑回测").click()

        expect(page.locator(".learn-chart canvas").first).to_be_visible()
        # 回归：LineChart 未注册时 canvas 只有轴网格、曲线全空（用户截图事件）
        diag = page.evaluate(
            "() => { const c = window.__learnChart; if (!c) return {hook: false};"
            " const o = c.getOption();"
            " return {hook: true, n: o.series.length,"
            " lens: o.series.map(x => (x.data || []).length)}; }")
        print("CHART DIAG:", diag)
        assert diag.get("hook") and diag.get("n") == 3 and all(
            n > 0 for n in diag.get("lens", [])), f"series 未渲染: {diag}"
        for k in ("总收益", "年化收益", "最大回撤", "夏普比率", "胜率", "盈亏比", "交易次数"):
            expect(page.locator(".lm-k", has_text=k)).to_be_visible()
        print("DIAG table-cards:", page.locator(".detail-table-card").count(),
              "runs rows:", page.locator(".runs-table tbody tr").count())
        expect(page.locator(".runs-table tbody tr")).to_have_count(1)
        expect(page.locator(".runs-table tbody tr").first).to_contain_text("+6.67%")
        # 请求体契约
        body = json.loads(calls[-1]["body"])
        assert body["strategy"] == "donchian"
        assert body["codes"] == ["600519"]
        assert body["exec_mode"] == "next_open"
        assert body["params"] == {"entry_n": 20, "exit_n": 10}

    def test_exec_mode_and_momentum_pool(self, servers, page, api_calls):
        """P1：撮合模式入参切换；动量策略展示股票池输入。"""
        calls, _ = api_calls
        _, frontend_port = servers
        open_learn(page, frontend_port)
        page.locator(".learn-nav", has_text="动量轮动").click()
        pool = page.locator(".learn-field", has_text="轮动股票池")
        expect(pool).to_be_visible()
        self._pick(page, "600519")                           # 池输入同源多选控件
        page.locator(".learn-select").nth(1).select_option("close")   # 撮合模式
        page.get_by_role("button", name="▶ 跑回测").click()
        body = json.loads(calls[-1]["body"])
        assert body["strategy"] == "momentum"
        assert body["codes"] == ["600519"]                # UI 层验证发送选中集合
        assert body["exec_mode"] == "close"

    def test_detail_page_link_to_learn(self, servers, page, api_calls):
        """P1：详情页「用该股教学回测」→ 跳教学页且代码带入。"""
        _, frontend_port = servers
        page.goto(f"http://127.0.0.1:{frontend_port}/")
        page.get_by_role("button", name="数据", exact=True).click()
        page.locator(".browse-pv tbody tr", has_text="贵州茅台").click()
        expect(page.get_by_role("heading", name="600519")).to_be_visible()
        page.get_by_role("button", name="用该股教学回测").click()
        expect(page.get_by_role("heading", name="量化教学")).to_be_visible()
        expect(page.locator(".learn-ac")).to_contain_text("600519")

    def test_empty_history_message(self, servers, page, api_calls):
        """P2：区间无数据 → 空态文案而非报错。"""
        _, holder = api_calls
        holder["bt"] = BT_EMPTY
        _, frontend_port = servers
        open_learn(page, frontend_port)
        self._pick(page, "600519")
        page.get_by_role("button", name="▶ 跑回测").click()
        expect(page.locator(".learn-empty .state")
               ).to_contain_text("区间内无行情数据")

    def test_gateway_html_response_friendly_error(self, servers, page, api_calls):
        """回归（生产 504 事件）：网关超时返回 HTML 错误页时给出友好提示，
        而不是 json() 解析崩出 SyntaxError。"""
        _, frontend_port = servers
        page.route("**/api/backtest", lambda route: route.fulfill(
            status=504, content_type="text/html",
            body="<html><head><title>504</title></head><body>nginx</body></html>"))
        open_learn(page, frontend_port)
        self._pick(page, "600519")
        page.get_by_role("button", name="▶ 跑回测").click()
        expect(page.locator(".error-banner")).to_contain_text("服务返回异常")
        expect(page.locator(".error-banner")).to_contain_text("504")

    def test_code_dropdown_search_and_select(self, servers, page, api_calls):
        """教学页代码控件为可搜索下拉：输入名称过滤、选中后带码回测。"""
        calls, _ = api_calls
        _, frontend_port = servers
        open_learn(page, frontend_port)
        box = page.locator(".learn-ac input")
        box.click()
        box.fill("茅台")                                  # 输入触发本地过滤建议
        option = page.locator(".p-autocomplete-option", has_text="贵州茅台")
        expect(option.first).to_be_visible()
        option.first.click()
        page.get_by_role("button", name="▶ 跑回测").click()
        body = json.loads(calls[-1]["body"])
        assert body["codes"] == ["600519"]

    def test_code_dropdown_lists_all_on_dropdown_click(self, servers, page, api_calls):
        """下拉按钮：不输入时列出清单前若干条。"""
        _, frontend_port = servers
        open_learn(page, frontend_port)
        page.locator(".learn-ac .p-autocomplete-dropdown").click()
        expect(page.locator(".p-autocomplete-option", has_text="平安银行")
               .first).to_be_visible()


    def test_adjust_badge_on_result(self, servers, page, api_calls):
        """复权徽标：因子可用显示「已按后复权口径」，不可用显示失真警示。"""
        _, holder = api_calls
        _, frontend_port = servers
        open_learn(page, frontend_port)
        self._pick(page, "600519")
        page.get_by_role("button", name="▶ 跑回测").click()
        expect(page.locator(".adj-ok", has_text="后复权口径")).to_be_visible()
        holder["bt"] = {**BT_RESULT, "adjust_applied": False}
        page.get_by_role("button", name="▶ 跑回测").click()
        expect(page.locator(".task-risk", has_text="不复权口径")).to_be_visible()


    def _pick(self, page, *codes):
        """调试钩子直设多选（绕过 AutoComplete 下拉交互——高负载下
        点击/键盘选码有渲染竞态，偶发丢选）。"""
        page.evaluate("codes => window.__learnSetCodes(codes)", list(codes))
        for code in codes:
            expect(page.locator(".learn-ac")).to_contain_text(code)

    def test_batch_compare_table_and_overlay(self, servers, page, api_calls):
        """P0 批量：选 3 只 → 对比表按收益降序 3 行 + 归一化叠加净值图。"""
        calls, holder = api_calls
        holder["bt"] = lambda url, req: _bt_result(
            json.loads(req.post_data)["codes"][0],
            BT_BY_CODE.get(json.loads(req.post_data)["codes"][0], 0.0))
        _, frontend_port = servers
        open_learn(page, frontend_port)
        page.locator(".learn-nav", has_text="双均线交叉").click()
        self._pick(page, "600519", "000001", "000002")
        expect(page.locator(".learn-ac")).to_contain_text("600519")
        expect(page.locator(".learn-ac")).to_contain_text("000001")
        expect(page.locator(".learn-ac")).to_contain_text("000002")
        page.get_by_role("button", name="▶ 跑回测").click()

        rows = page.locator(".learn-cmp tbody tr")
        expect(rows).to_have_count(3)
        # 按收益降序：600519(+26) > 000001(+5) > 000002(-10)
        expect(rows.nth(0)).to_contain_text("600519")
        expect(rows.nth(0)).to_contain_text("+26.0%")
        expect(rows.nth(1)).to_contain_text("000001")
        expect(rows.nth(2)).to_contain_text("-10.0%")
        # 叠加净值图：3 条 series（LineChart 注册回归）
        series_ok = page.evaluate(
            "() => { const c = window.__learnChart; if (!c) return {hook: false};"
            " return {hook: true, n: c.getOption().series.length}; }")
        assert series_ok == {"hook": True, "n": 3}
        # 3 个独立请求（每股一次）
        assert len(calls) == 3

    def test_batch_skips_failed_with_reason(self, servers, page, api_calls):
        """P1：某只失败（区间无数据）→ 跳过并标原因，其余正常对比。"""
        _, holder = api_calls

        def by_code(url, req):
            code = json.loads(req.post_data)["codes"][0]
            if code == "000002":
                return {**BT_RESULT, "metrics": {}, "equity": [], "drawdown": [],
                        "trades": [], "gap_dates": [],
                        "message": "区间内无行情数据"}
            return _bt_result(code, BT_BY_CODE.get(code, 0.0))

        holder["bt"] = by_code
        _, frontend_port = servers
        open_learn(page, frontend_port)
        self._pick(page, "600519", "000002")
        page.get_by_role("button", name="▶ 跑回测").click()

        rows = page.locator(".learn-cmp tbody tr")
        expect(rows).to_have_count(2)                  # 1 成功 + 1 失败行
        fail_row = page.locator(".learn-cmp tbody tr", has_text="000002")
        expect(fail_row.locator(".learn-fail-reason")
               ).to_contain_text("区间内无行情数据")
        expect(rows.nth(0)).to_contain_text("600519")
