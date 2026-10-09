"""量化教学页（六大经典回测策略）E2E：mock /api/backtest 与个股详情接口。

覆盖：策略导航与原理卡、参数面板、回测结果（净值图/指标/交易表）、撮合模式
切换入参、轮动股票池输入、详情页联动入口、空数据空态。
"""

import json
import re

import pytest
from playwright.sync_api import expect

BT_RESULT = {
    "status": "up", "codes": ["600519"], "strategy": "donchian",
    "range": "3y", "exec_mode": "next_open",
    "metrics": {"total_return": 0.26, "annual_return": 0.08,
                "max_drawdown": 0.18, "sharpe": 0.85, "win_rate": 0.5,
                "profit_loss_ratio": 1.8, "trades": 3, "vol_days": 750},
    "equity": [[f"2026-0{i}-0{j}", 1.0 + 0.01 * i + 0.001 * j,
                1.0 + 0.005 * i] for i in range(1, 4) for j in range(1, 6)],
    "drawdown": [[f"2026-0{i}-0{j}", -0.01 * i] for i in range(1, 4)
                 for j in range(1, 6)],
    "trades": [
        {"entry_date": "2026-01-06", "exit_date": "2026-02-04",
         "entry_price": 10.5, "exit_price": 11.2, "pnl_pct": 0.0667,
         "direction": "long", "symbol": "600519"},
        {"entry_date": "2026-02-10", "exit_date": "2026-03-03",
         "entry_price": 11.0, "exit_price": 10.8, "pnl_pct": -0.0182,
         "direction": "long", "symbol": "600519"},
    ],
    "gap_dates": [],
}

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
        if re.search(r"/api/backtest", url):
            calls.append({"url": url, "body": req.post_data})
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(holder["bt"]))
        elif re.search(r"/api/stocks(\?|$)", url):
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps(LIST_BODY))
        else:
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
        page.get_by_role("button", name="▶ 跑回测").click()

        expect(page.locator(".learn-chart canvas").first).to_be_visible()
        for k in ("总收益", "年化收益", "最大回撤", "夏普比率", "胜率", "盈亏比", "交易次数"):
            expect(page.locator(".lm-k", has_text=k)).to_be_visible()
        expect(page.locator(".runs-table tbody tr")).to_have_count(2)
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
        expect(pool.locator("input")).to_have_value(
            re.compile("600519,000001"))                     # 默认预填 5 只
        page.locator(".learn-select").nth(1).select_option("close")   # 撮合模式
        page.get_by_role("button", name="▶ 跑回测").click()
        body = json.loads(calls[-1]["body"])
        assert body["strategy"] == "momentum"
        assert len(body["codes"]) == 5
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
        expect(page.locator(".learn-field", has_text="股票代码")
               .locator("input")).to_have_value("600519")

    def test_empty_history_message(self, servers, page, api_calls):
        """P2：区间无数据 → 空态文案而非报错。"""
        _, holder = api_calls
        holder["bt"] = BT_EMPTY
        _, frontend_port = servers
        open_learn(page, frontend_port)
        page.get_by_role("button", name="▶ 跑回测").click()
        expect(page.locator(".learn-empty .state")
               ).to_contain_text("区间内无行情数据")
