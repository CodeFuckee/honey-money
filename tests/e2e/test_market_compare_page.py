"""519 vs 924 行情对比页 E2E（本地拉起后端 uvicorn + 前端 vite preview）。

数据路径用 Playwright route 拦截 mock（E2E 环境 PG 必不可达，正常路径
只能 mock）；降级路径走真实坏 DSN 后端的 status=down。运行：
      .venv/bin/python -m pytest tests/e2e/test_market_compare_page.py
"""

import json

from playwright.sync_api import expect

# mock 响应：小窗口数据（对齐契约已由后端单测覆盖，此处只验证页面渲染）
MOCK_POINTS = [
    {"n": n,
     "d1": f"1999-{5 if n < 0 else 6}-{(19 + n) % 28 + 1:02d}",
     "r1": round(n / 100, 4),
     "d2": f"2024-{9 if n < 0 else 10}-{(24 + n) % 30 + 1:02d}",
     "r2": round(n / 120, 4) if n <= 30 else None}
    for n in range(-280, 41, 4)
]
MOCK_BODY = {
    "status": "up",
    "symbol": {"code": "sh000001", "name": "上证指数", "source": "eastmoney"},
    "t1": {"date": "1999-05-19", "label": "5·19 行情"},
    "t2": {"date": "2024-09-24", "label": "9·24 行情"},
    "back": 280, "fwd": 1040,
    "points": MOCK_POINTS,
    "t2_progress": {"n": 28, "date": "2025-11-20", "ret": 0.2333},
}


def _goto(servers, page, path="/data/market-compare", mock=True):
    _, frontend_port = servers
    if mock:
        page.route("**/api/market/519-924*", lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps(MOCK_BODY)))
    page.goto(f"http://127.0.0.1:{frontend_port}{path}")


def test_market_page_renders_chart_and_kpis(servers, page):
    """P0：进入页面 → KPI 四卡 + ECharts 双线画布渲染（mock 数据路径）。"""
    _goto(servers, page)
    expect(page.locator("h2", has_text="519 vs 924")).to_be_visible()
    # KPI 四卡：进度 / 当前累计 / 519 同期 / 峰值
    kpis = page.locator("[data-market-kpis] .lm")
    expect(kpis).to_have_count(4)
    expect(kpis.nth(0)).to_contain_text("T+28")
    expect(kpis.nth(1)).to_contain_text("+23.3%")
    expect(kpis.nth(2)).to_contain_text("519 同期")
    # 图表画布存在且布局稳定后再断言尺寸（E2E-443 教训：过渡动画期测量抖动）
    page.wait_for_selector("[data-market-chart] canvas", state="attached")
    page.wait_for_function(
        "() => { const c = document.querySelector('[data-market-chart] canvas');"
        " return c && c.clientWidth > 300 && c.clientHeight > 300 }")


def test_market_page_shows_calibration_and_progress_date(servers, page):
    """P1：口径行含窗口说明，KPI 进度卡渲染 t2_progress 日期（DOM 可查）。"""
    _goto(servers, page)
    page.wait_for_selector("[data-market-chart] canvas", state="attached")
    expect(page.locator("p.sub").first).to_contain_text("T-280 ~ T+1040")
    expect(page.locator("[data-market-kpis] .lm").first).to_contain_text(
        "2025-11-20")                                    # t2_progress.date


def test_market_page_degrades_without_data(servers, page):
    """P1：E2E 后端 PG 不可达 → /api/market/519-924 status=down → 占位卡。"""
    _goto(servers, page, mock=False)
    expect(page.locator(".browse-empty", has_text="行情对比数据不可用")
           .first).to_be_visible(timeout=30000)


def test_market_route_survives_reload(servers, page):
    """P1 深链回归：/data/market-compare 刷新后仍在本页（SPA 回退 + 路由恢复）。"""
    _goto(servers, page)
    page.reload()
    expect(page.locator("h2", has_text="519 vs 924")).to_be_visible()
    # 侧栏「数据」组保持高亮归属（子页走 sub-active 机制，非 active）
    expect(page.locator(".nav-item.sub-active")).to_have_text("数据")
