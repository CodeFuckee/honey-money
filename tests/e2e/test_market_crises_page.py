"""金融危机史页 E2E（本地拉起后端 uvicorn + 前端 vite preview）。

数据路径用 Playwright route 拦截 mock（E2E 环境 PG 必不可达，正常路径
只能 mock）；降级路径走真实坏 DSN 后端的 status=down。运行：
      .venv/bin/python -m pytest tests/e2e/test_market_crises_page.py
"""

import datetime as dt
import json
import re

from playwright.sync_api import expect

# ---------- mock 数据：七次危机（统计值与结构驱动前端渲染，曲线形状简化） ----------


def _points(start: str, n: int, trough_ret: float, recover: bool):
    """紧凑 mock 点列：0% 起步 → 中点跌到 trough_ret → 尾段修复。
    mock 不求真实日历，只要求日期升序唯一且落在窗口内（图表裁剪用）。"""
    base = dt.date.fromisoformat(start)
    pts = []
    for i in range(n):
        ret = 0.0 if i == 0 else (
            trough_ret * (i / (n // 2)) if i <= n // 2
            else trough_ret + (-trough_ret * 0.6 if recover else -trough_ret * 0.1)
            * ((i - n // 2) / (n - n // 2)))
        pts.append({"date": (base + dt.timedelta(days=2 * i)).isoformat(),
                    "sse": round(ret, 4),
                    "hs300": round(ret * 1.15, 4) if recover or i else None})
    return pts


def _crisis(key, name, start, end, *, trough_ret, hs300, recover, n=50):
    events = [
        {"date": start, "label": f"{name}·爆发"},
        {"date": (dt.date.fromisoformat(start)
                  + dt.timedelta(days=60)).isoformat(), "label": "事件B"},
        {"date": (dt.date.fromisoformat(start)
                  + dt.timedelta(days=120)).isoformat(), "label": "事件C"},
        {"date": (dt.date.fromisoformat(start)
                  + dt.timedelta(days=180)).isoformat(), "label": "事件D"},
    ]
    return {"key": key, "name": name, "summary": f"{name}简介",
            "start": start, "end": end, "events": events,
            "stats": {"base_date": start, "base": 1500.39,
                      "trough_date": (dt.date.fromisoformat(start)
                                      + dt.timedelta(days=n)).isoformat(),
                      "trough": 1000.0, "trough_ret": trough_ret,
                      "trough_days": 25,
                      "recover_date": ((dt.date.fromisoformat(start)
                                        + dt.timedelta(days=n * 2)).isoformat()
                                       if recover else None),
                      "recover_ret": 0.001 if recover else None,
                      "recover_days": 30 if recover else None},
            "points": _points(start, n, trough_ret, recover),
            "hs300_available": hs300}


MOCK_BODY = {
    "status": "up",
    "symbols": {"main": {"code": "sh000001", "name": "上证指数",
                         "source": "eastmoney"},
                "sub": {"code": "sh000300", "name": "沪深300",
                        "source": "eastmoney", "note": "口径注"}},
    "crises": [
        _crisis("crisis-1997", "亚洲金融危机", "1997-05-12", "1999-09-30",
                trough_ret=-0.306, hs300=False, recover=True),
        _crisis("crisis-2001", "国有股减持熊市", "2001-06-13", "2007-03-31",
                trough_ret=-0.549, hs300=False, recover=True),
        _crisis("crisis-2008", "全球金融危机", "2007-10-16", "2009-12-31",
                trough_ret=-0.72, hs300=True, recover=False),
        _crisis("crisis-2015", "股灾（杠杆牛崩盘）", "2015-06-12", "2018-02-28",
                trough_ret=-0.486, hs300=True, recover=False),
        _crisis("crisis-2018", "中美贸易战", "2018-01-29", "2021-06-30",
                trough_ret=-0.30, hs300=True, recover=True),
        _crisis("crisis-2020", "新冠疫情", "2020-01-14", "2020-12-31",
                trough_ret=-0.144, hs300=True, recover=True),
        _crisis("crisis-2021", "地产中概熊市", "2021-09-13", "2025-12-31",
                trough_ret=-0.273, hs300=True, recover=True),
    ],
}


def _goto(servers, page, path="/data/crises", mock=True):
    _, frontend_port = servers
    if mock:
        page.route("**/api/market/crises*", lambda route: route.fulfill(
            status=200, content_type="application/json",
            body=json.dumps(MOCK_BODY)))
    page.goto(f"http://127.0.0.1:{frontend_port}{path}")


def _chart_ready(page):
    page.wait_for_selector("[data-crises-chart] canvas", state="attached")
    page.wait_for_function(
        "() => { const c = document.querySelector('[data-crises-chart] canvas');"
        " return c && c.clientWidth > 300 && c.clientHeight > 300 }")


def test_crises_page_renders_table_kpis_chart(servers, page):
    """E2E-464 P0：进入页面 → 总览表 7 行、KPI 四卡渲染第一次危机
    （1997 基线 1500.39）、图表 canvas 就绪。"""
    _goto(servers, page)
    expect(page.locator("h2", has_text="金融危机史")).to_be_visible()
    rows = page.locator("[data-crises-table] tbody tr")
    expect(rows).to_have_count(7)
    expect(rows.first).to_contain_text("亚洲金融危机")
    kpis = page.locator("[data-crises-kpis] .lm")
    expect(kpis).to_have_count(4)
    expect(kpis.nth(0)).to_contain_text("1500.39")     # 危机基线（mock base）
    expect(kpis.nth(1)).to_contain_text("-30.6%")      # 默认选中第一次
    _chart_ready(page)


def test_crises_page_row_click_switches_crisis(servers, page):
    """E2E-465 P0：点击「全球金融危机」行 → KPI 切为 -72.0%、URL 同步
    ?c=crisis-2008、图表重渲染。"""
    _goto(servers, page)
    page.locator("[data-crises-table] tbody tr",
                 has_text="全球金融危机").click()
    kpis = page.locator("[data-crises-kpis] .lm")
    expect(kpis.nth(1)).to_contain_text("-72.0%")
    expect(kpis.nth(2)).to_contain_text("25 个交易日")  # mock trough_days
    _chart_ready(page)
    expect(page).to_have_url(re.compile(r"c=crisis-2008"))


def test_crises_page_dual_caliber_and_events(servers, page):
    """E2E-466 P1：2008 双口径危机 → 口径行含「双线」、事件时间线 4 条。"""
    _goto(servers, page)
    page.locator("[data-crises-table] tbody tr",
                 has_text="全球金融危机").click()
    expect(page.locator("p.note", has_text="双线 = 上证指数").first) \
        .to_be_visible()
    events = page.locator("[data-crises-events] li")
    expect(events).to_have_count(4)
    expect(events.first).to_contain_text("全球金融危机·爆发")


def test_crises_page_deep_link_selects_crisis(servers, page):
    """E2E-467 P1：?c=crisis-2021 深链接直达 → 默认选中 2021（KPI 跌幅
    与选中行高亮）；刷新后仍选中（URL 同步 + SPA 回退）。"""
    _goto(servers, page, path="/data/crises?c=crisis-2021")
    expect(page.locator("[data-crises-kpis] .lm").nth(1)
           ).to_contain_text("-27.3%")                 # mock 2021 trough_ret
    expect(page.locator("[data-crises-table] tbody tr").nth(6)
           ).to_have_class("active")
    page.reload()
    expect(page.locator("[data-crises-kpis] .lm").nth(1)
           ).to_contain_text("-27.3%")                 # 刷新后仍是 2021


def test_crises_page_degrades_without_data(servers, page):
    """E2E-468 P1：E2E 后端 PG 不可达 → /api/market/crises status=down
    → 占位卡。"""
    _goto(servers, page, mock=False)
    expect(page.locator(".browse-empty", has_text="危机数据不可用")
           .first).to_be_visible(timeout=30000)


def test_crises_route_survives_reload(servers, page):
    """E2E-469 P1 深链回归：/data/crises 刷新后仍在本页，侧栏「数据」
    组保持 sub-active 归属。"""
    _goto(servers, page)
    page.reload()
    expect(page.locator("h2", has_text="金融危机史")).to_be_visible()
    expect(page.locator(".nav-item.sub-active")).to_have_text("数据")


def test_crises_page_single_caliber_note(servers, page):
    """E2E-470 P2：1997 单口径危机（hs300_available=false）→ 口径行显示
    「本危机仅上证口径」。"""
    _goto(servers, page)
    page.locator("[data-crises-table] tbody tr",
                 has_text="亚洲金融危机").click()
    expect(page.locator("p.note", has_text="本危机仅上证口径").first) \
        .to_be_visible()
