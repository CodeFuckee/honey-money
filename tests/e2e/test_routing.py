"""前端路由 E2E：URL 路径 ↔ 页面、查询串 ↔ 参数（刷新/分享恢复）。

history 路径路由（手写 History API）：/learn /stock /data/... 深链接刷新
恢复页面与参数；导航点击 pushState、参数变化 replaceState。
"""

import re

import pytest
from playwright.sync_api import expect

STRATS_NAV = {"buy_hold": "买入持有", "ma_cross": "双均线交叉",
              "donchian": "通道突破（海龟）", "bollinger": "布林带均值回归",
              "grid": "网格交易", "momentum": "动量轮动"}


def open_learn(page, frontend_port):
    page.goto(f"http://127.0.0.1:{frontend_port}/")
    page.get_by_role("button", name="教学").click()
    expect(page.get_by_role("heading", name="量化教学")).to_be_visible()


class TestRouting:
    def test_deep_link_restores_learn_params(self, servers, page):
        """P0 深链接：/learn?strategy&mode&codes&range&exec 刷新后页面与参数全恢复。"""
        _, frontend_port = servers
        page.goto(
            f"http://127.0.0.1:{frontend_port}/learn"
            "?strategy=donchian&mode=batch&codes=600519,000001&range=5y&exec=close")
        expect(page.get_by_role("heading", name="量化教学")).to_be_visible()
        # 策略 active
        expect(page.locator(".learn-nav", has_text="通道突破（海龟）")
               ).to_have_class("learn-nav active")
        # 模式段 + chips + 区间/撮合
        expect(page.locator(".learn-mode-btn", has_text="批量对比")
               ).to_have_class(re.compile("active"))
        expect(page.locator(".learn-ac")).to_contain_text("600519")
        expect(page.locator(".learn-ac")).to_contain_text("000001")
        expect(page.locator(".learn-field", has_text="回测区间")
               .locator("select")).to_have_value("5y")
        expect(page.locator(".learn-field", has_text="撮合模式")
               .locator("select")).to_have_value("close")

    def test_url_updates_on_param_change(self, servers, page):
        """P1 参数变化 → URL 查询串同步（replace，不灌历史）。"""
        _, frontend_port = servers
        open_learn(page, frontend_port)
        page.locator(".learn-nav", has_text="布林带均值回归").click()
        expect(page.locator(".learn-mode-btn", has_text="单股")
               ).to_have_class(re.compile("active"))
        page.wait_for_timeout(500)                     # 防抖 250ms
        assert "strategy=bollinger" in page.url

    def test_nav_click_pushes_path(self, servers, page):
        """P1 导航点击 → 路径切换（pushState）。"""
        _, frontend_port = servers
        page.goto(f"http://127.0.0.1:{frontend_port}/")
        page.get_by_role("button", name="教学").click()
        expect(page.get_by_role("heading", name="量化教学")).to_be_visible()
        page.wait_for_timeout(400)
        assert page.url.split("?")[0].endswith("/learn")

    def test_unknown_path_falls_back_to_dash(self, servers, page):
        """P2 未知路径回仪表盘（不白屏）。"""
        _, frontend_port = servers
        page.goto(f"http://127.0.0.1:{frontend_port}/no-such-page")
        expect(page.get_by_role("heading", name="服务状态")).to_be_visible()

    def test_stock_deep_link(self, servers, page):
        """P1 个股详情深链接：/stock?code=600519 刷新恢复详情页并加载数据。"""
        _, frontend_port = servers
        page.goto(
            f"http://127.0.0.1:{frontend_port}/stock?code=600519&source=sina")
        expect(page.get_by_role("heading", name="600519")).to_be_visible()
