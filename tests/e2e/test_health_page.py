"""E2E 冒烟：本地拉起后端(uvicorn)与前端(vite preview)，验证健康仪表盘页面。

运行：.venv/Scripts/python.exe -m pytest tests/e2e --headed  # 人工观看
      .venv/Scripts/python -m pytest tests/e2e            # 无头
servers fixture 见同目录 conftest.py（本文件只写用例）。
"""

import json

import pytest
from playwright.sync_api import expect


def test_health_dashboard_renders(servers, page):
    """页面加载：标题可见，四张卡片渲染，本地无 PG/代理池时优雅降级为未连接。"""
    _, frontend_port = servers
    page.goto(f"http://127.0.0.1:{frontend_port}/")

    expect(page.get_by_role("heading", name="honey-money")).to_be_visible()

    cards = page.locator(".card")
    expect(cards).to_have_count(4, timeout=20000)

    expect(page.locator(".card", has_text="后端 API")
           .locator(".state")).to_have_text("运行中")

    # 本地无 PG/代理池：三卡应渲染"未连接"且页面不崩溃（优雅降级路径）
    expect(page.locator(".card", has_text="PostgreSQL")
           .locator(".state")).to_have_text("未连接")
    expect(page.locator(".card", has_text="代理池")
           .locator(".state")).to_have_text("未连接")
    # 爬虫数据卡：PG 不可达 → 未连接（crawl/stats 接口同样优雅降级）
    expect(page.locator(".card", has_text="爬虫数据")
           .locator(".state")).to_have_text("未连接")

    # favicon / PWA 图标资产：链接存在且资源可达
    icon_svg = page.get_attribute('link[rel="icon"]', "href")
    assert icon_svg == "/favicon.svg", f"SVG favicon 缺失: {icon_svg}"
    for href in ("/favicon.svg", "/favicon-32.png", "/apple-touch-icon.png",
                 "/icon-192.png", "/icon-512.png", "/manifest.webmanifest"):
        r = page.request.get(f"http://127.0.0.1:{frontend_port}{href}")
        assert r.status == 200, f"图标资产 {href} 返回 {r.status}"
    assert page.get_attribute('link[rel="manifest"]', "href") == "/manifest.webmanifest"

    # 回归：徽章文字不得与大字状态重复（fbadge 文本节点曾双渲染）
    expect(page.locator(".card", has_text="PostgreSQL").first).to_contain_text("未连接")
    expect(page.locator(".card", has_text="代理池").first).to_contain_text("未连接")
    for name, word in (("PostgreSQL", "未连接"), ("代理池", "未连接"), ("爬虫数据", "数据不可用")):
        txt = page.locator(".card", has_text=name).first.inner_text()
        assert txt.count(word) == 1, f"{name} 卡状态文字重复：{txt!r}"


def test_dashboard_progressive_render(servers, page):
    """渐进上屏：/healthz 先回先渲染后端卡，/api/infra 悬挂时 PG/代理池保持骨架，
    放行后补齐——三路探测互不等待（曾经 Promise.all 全等导致长时「正在检查各服务…」）。"""
    _, frontend_port = servers
    held = []

    def hold_infra(route):
        held.append(route)               # 只挂起不响应，模拟 PG/代理池探测慢

    page.route("**/api/infra", hold_infra)
    page.goto(f"http://127.0.0.1:{frontend_port}/")

    # 后端卡不等 infra，先上屏
    expect(page.locator(".card", has_text="后端 API")
           .locator(".state")).to_have_text("运行中", timeout=10000)
    expect(page.locator(".card", has_text="PostgreSQL")).to_have_count(0)
    expect(page.locator(".skeleton-card").first).to_be_visible()

    # 放行 infra → PG/代理池卡补齐，骨架清空
    held[0].fulfill(status=200, content_type="application/json", body=json.dumps(
        {"postgres": {"status": "up", "detail": "ok"},
         "proxy_pool": {"status": "up", "detail": "ok"}}))
    expect(page.locator(".card", has_text="PostgreSQL")
           .locator(".state")).to_have_text("已连接")
    expect(page.locator(".card", has_text="代理池")
           .locator(".state")).to_contain_text("已连接")
    expect(page.locator(".skeleton-card")).to_have_count(0)


def test_shell_and_nav(servers, page):
    """Web 全幅外壳：标题栏（无红绿灯）+ 侧栏导航五项 + 占位页切换。"""
    _, frontend_port = servers
    page.goto(f"http://127.0.0.1:{frontend_port}/")

    # 标题栏：web 端不保留 macOS 红绿灯，仅标题
    expect(page.locator(".titlebar .tl")).to_have_count(0)
    expect(page.locator(".titlebar .title")).to_contain_text("honey-money")

    # 全幅铺满视口：四周无留白（窗口盒 == 视口）
    vp = page.viewport_size
    box = page.locator(".window").bounding_box()
    assert box is not None, ".window 未渲染"
    assert box["x"] == 0 and box["y"] == 0, f"窗口未贴边: {box['x']},{box['y']}"
    assert box["width"] == vp["width"] and box["height"] == vp["height"], \
        f"窗口未铺满视口: {box['width']}x{box['height']} != {vp['width']}x{vp['height']}"

    # 侧栏导航：五项（仪表盘/数据/任务/回测/教学），「仪表盘」默认激活
    nav = page.locator(".sidebar .nav-item")
    expect(nav).to_have_count(5)
    expect(page.locator(".sidebar .nav-item.active")).to_have_text("仪表盘")

    # 切到「数据」页：标题 + 真数据表（本地无 PG → 降级空表）可见
    page.get_by_role("button", name="数据").click()
    expect(page.get_by_role("heading", name="数据浏览")).to_be_visible()
    expect(page.locator(".browse-pv")).to_be_visible()
    expect(page.locator(".p-datatable .p-datatable-thead")).to_contain_text("代码")

    # 回归：搜索框曾因图标无尺寸被撑爆（现为 PrimeVue InputText + primeicons，高度有界）
    sb = page.locator(".ph-search").bounding_box()
    assert sb is not None and sb["height"] <= 60, f"搜索框高度异常: {sb}"

    # 数据源 Tab：点「东方财富」→ 走 source=eastmoney 过滤（本地无 PG 时为空表但接口仍 200）
    page.locator(".sidebar .src-item", has_text="东方财富").click()
    expect(page.get_by_role("heading", name="数据浏览 · 东方财富")).to_be_visible()
    expect(page.locator(".p-datatable")).to_be_visible()
    # 回数据页
    page.get_by_role("button", name="数据").click()
    expect(page.get_by_role("heading", name="数据浏览", exact=True)).to_be_visible()


def test_source_pages(servers, page):
    """数据源子页（Carbon 原型 ⑥⑦ 屏）：子项选中灰底+右箭头、父项「数据」保留
    高亮；标题/副题/来源 Tag 随源切换；行高 32px 文字垂直居中（曾 block 贴顶）。"""
    _, frontend_port = servers
    page.goto(f"http://127.0.0.1:{frontend_port}/")

    # 新浪财经子页：标题/选中态/父项高亮/蓝 Tag
    page.get_by_role("button", name="新浪财经").click()
    expect(page.get_by_role("heading", name="数据浏览 · 新浪财经")).to_be_visible()
    sina = page.locator(".src-item.active")
    expect(sina).to_have_text("新浪财经")
    assert sina.evaluate("el => getComputedStyle(el).backgroundColor") == "rgb(224, 224, 224)", \
        "数据源选中行应为 #E0E0E0 灰底"
    box = sina.bounding_box()
    assert box is not None and box["height"] == 32, f"数据源行高应为 32px: {box}"
    off = sina.evaluate("""el => {
        const r = el.getBoundingClientRect();
        const range = document.createRange(); range.selectNodeContents(el);
        const t = range.getBoundingClientRect();
        return Math.abs((t.top + t.bottom) / 2 - (r.top + r.bottom) / 2);
    }""")
    assert off <= 2, f"数据源文字未垂直居中，偏差 {off}px"
    parent = page.locator(".nav-item.sub-active")
    expect(parent).to_have_text("数据")
    assert parent.evaluate("el => getComputedStyle(el).backgroundColor") == "rgb(224, 224, 224)", \
        "父项「数据」应保留选中灰底"
    assert page.locator(".ph-tag.src-sina").evaluate(
        "el => getComputedStyle(el).backgroundColor") == "rgb(15, 98, 254)", "新浪 Tag 应蓝底"

    # 东方财富子页：绿 Tag、选中切换
    page.get_by_role("button", name="东方财富").click()
    expect(page.get_by_role("heading", name="数据浏览 · 东方财富")).to_be_visible()
    assert page.locator(".ph-tag.src-ef").evaluate(
        "el => getComputedStyle(el).backgroundColor") == "rgb(36, 161, 72)", "东财 Tag 应绿底"
    expect(page.locator(".src-item.active")).to_have_text("东方财富")

    # 回到「数据」父页：占位 Tag 回归、子项无选中
    page.get_by_role("button", name="数据", exact=True).click()
    expect(page.get_by_role("heading", name="数据浏览", exact=True)).to_be_visible()
    expect(page.locator(".src-item.active")).to_have_count(0)

    # 切回仪表盘：四卡仍在
    page.get_by_role("button", name="仪表盘").click()
    expect(page.locator(".card")).to_have_count(4, timeout=20000)


def test_carbon_theme_shell(servers, page):
    """Carbon 主题（按 Penpot 原型）：深色 UI Shell 头 48px + 256px 白侧栏 +
    方角 1px 边框卡 + 蓝主按钮 #0F62FE；刷新持久化；可切回。"""
    _, frontend_port = servers
    page.goto(f"http://127.0.0.1:{frontend_port}/")


    # 深色头部：48px #161616（有 0.15s 背景过渡，等终值），菜单/设置图标可见
    expect(page.locator(".titlebar")).to_be_visible()
    page.wait_for_function(
        "getComputedStyle(document.querySelector('.titlebar')).backgroundColor === 'rgb(22, 22, 22)'")
    assert page.locator(".titlebar").evaluate(
        "el => getComputedStyle(el).height") == "48px", "carbon 头部应为 48px"
    expect(page.locator(".titlebar .shell-menu")).to_be_visible()
    expect(page.locator(".titlebar .shell-set")).to_be_visible()
    expect(page.locator(".titlebar .title")).to_contain_text("A 股量化研究与交易系统")

    # 侧栏 256px 白底；导航选中项灰底（#E0E0E0）
    assert page.locator(".sidebar").evaluate(
        "el => getComputedStyle(el).width") == "256px", "carbon 侧栏应为 256px"
    page.wait_for_function(
        "getComputedStyle(document.querySelector('.sidebar')).backgroundColor === 'rgb(255, 255, 255)'")
    assert page.locator(".sidebar .nav-item.active").evaluate(
        "el => getComputedStyle(el).backgroundColor") == "rgb(224, 224, 224)", \
        "carbon 选中导航应为 #E0E0E0 灰底"

    # 卡片方角；主按钮蓝底白字（#0F62FE）
    radius = page.locator(".card").first.evaluate(
        "el => getComputedStyle(el).borderRadius")
    assert radius in ("0px", "2px"), f"carbon 卡片应为方角/微圆角: {radius}"
    btn = page.get_by_role("button", name="重新检测")
    expect(btn).to_be_visible()
    assert btn.evaluate("el => getComputedStyle(el).backgroundColor") == "rgb(15, 98, 254)", \
        "carbon 主按钮应为 #0F62FE"
    assert btn.evaluate("el => getComputedStyle(el).color") == "rgb(255, 255, 255)", \
        "carbon 主按钮文字应为白色"

    # 状态徽章：dot + 12px 文字（后端 API 运行中）
    expect(page.locator(".fbadge", has_text="运行中").first).to_be_visible()

    # 数据页：真数据表（browse）表头 48px 灰字；本地无 PG → 降级空表仍渲染表头
    page.get_by_role("button", name="数据").click()
    head = page.locator(".p-datatable-thead th").first
    expect(head).to_contain_text("代码")
    assert head.evaluate("el => getComputedStyle(el).backgroundColor") == "rgb(244, 244, 244)", \
        "carbon 表头应 #F4F4F4 底"
    expect(page.locator(".p-datatable")).to_be_visible()

    # 刷新后仍正常（单主题 Carbon）
    page.reload()


def test_error_banner_icon_sizing(servers, page):
    """回归：错误横幅 SVG 警告图标曾无显式尺寸被撑成千像素巨图（三主题共用）。
    拦截 /healthz 制造后端不可用，断言图标不大于 20px 且横幅高度正常。"""
    _, frontend_port = servers
    page.route("**/healthz", lambda r: r.fulfill(status=500, body="x"))
    page.goto(f"http://127.0.0.1:{frontend_port}/")

    expect(page.locator(".error-banner")).to_be_visible()
    icon = page.locator(".error-banner svg.warn-icon").last   # carbon 实心图标（首个线条图标已隐藏）
    expect(icon).to_be_visible()
    box = icon.bounding_box()
    assert box is not None and box["width"] <= 20 and box["height"] <= 20, \
        f"警告图标被撑大: {box}"
    bh = page.locator(".error-banner").evaluate("el => el.getBoundingClientRect().height")
    assert bh <= 80, f"错误横幅高度异常: {bh}"


def test_dashboard_auto_retry_recovers(servers, page):
    """部署窗口自愈：healthz 先 502（nginx 拿不到重启中的 upstream）后 200，
    页面按 2/5s 退避自动重试恢复后端卡——曾经失败即死、停在「不可用」
    直到手动刷新（2026-10-14 部署窗口仪表盘全卡数据不可用的根因之一）。"""
    _, frontend_port = servers
    calls = {"n": 0}

    def flaky_healthz(route):
        calls["n"] += 1
        if calls["n"] <= 2:
            route.fulfill(status=502, content_type="text/html",
                          body="<html><body>502 Bad Gateway</body></html>")
            return
        route.fulfill(status=200, content_type="application/json", body=json.dumps(
            {"status": "ok", "service": "honey-money-backend",
             "version": "test"}))

    page.route("**/healthz", flaky_healthz)
    page.goto(f"http://127.0.0.1:{frontend_port}/")

    # 首次 502：后端卡不可用
    expect(page.locator(".card", has_text="后端 API")
           .locator(".state")).to_have_text("不可用", timeout=10000)
    # 自动退避重试（2s→5s）：第 3 次探测 200 → 卡片自愈为运行中
    expect(page.locator(".card", has_text="后端 API")
           .locator(".state")).to_have_text("运行中", timeout=15000)
    assert calls["n"] >= 3, f"应有多次自动重试，实际探测 {calls['n']} 次"
