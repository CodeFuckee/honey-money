"""E2E 冒烟：本地拉起后端(uvicorn)与前端(vite preview)，验证健康仪表盘页面。

运行：.venv/Scripts/python.exe -m pytest tests/e2e --headed  # 人工观看
      .venv/Scripts/python.exe -m pytest tests/e2e            # 无头
"""

import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
from playwright.sync_api import expect

ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    """让 OS 分配一个当前空闲端口（本机开发端口常被占，写死不可靠）。"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait(url: str, timeout: float = 30) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.5)
    return False


@pytest.fixture(scope="session")
def servers():
    backend_port, frontend_port = _free_port(), _free_port()
    import os

    procs = []
    try:
        # 注入本地必失败地址：PG/代理池探测秒级返回 down（默认 DSN 的主机名
        # postgres 在无 DNS 的本机要等十几秒才超时，页面会长时间停在 1 张卡）
        backend_env = {**os.environ,
                       "DATABASE_URL": "postgresql://x:x@127.0.0.1:59999/none",
                       "PROXY_POOL_URL": "http://127.0.0.1:59998"}
        procs.append(subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app",
             "--port", str(backend_port), "--log-level", "warning"],
            cwd=ROOT / "backend", env=backend_env,
        ))
        env = {**os.environ, "BACKEND_PORT": str(backend_port)}
        # Windows 的 npm 是 npm.cmd 需要 shell；POSIX 直接 exec（shell=True+数组
        # 在 POSIX 下只有首元素生效，preview 参数会全部丢失）
        procs.append(subprocess.Popen(
            ["npm", "run", "preview", "--", "--port", str(frontend_port),
             "--strictPort", "--host", "127.0.0.1"],  # 默认只绑 IPv6 的 ::1
            cwd=ROOT / "frontend", shell=(sys.platform == "win32"), env=env,
        ))
        assert _wait(f"http://127.0.0.1:{backend_port}/healthz"), "后端 30s 内未就绪"
        assert _wait(f"http://127.0.0.1:{frontend_port}/"), "前端 preview 30s 内未就绪"
        yield backend_port, frontend_port
    finally:
        for p in procs:
            p.terminate()
            try:
                p.wait(timeout=10)
            except subprocess.TimeoutExpired:
                p.kill()


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

    # 回归：苹果主题徽章文字不得与大字状态重复（fbadge 文本节点曾双渲染）
    for name in ("PostgreSQL", "代理池", "爬虫数据"):
        txt = page.locator(".card", has_text=name).first.inner_text()
        assert txt.count("未连接") == 1, f"{name} 卡状态文字重复：{txt!r}"


def test_macos_shell_and_nav(servers, page):
    """Web 全幅外壳：标题栏（无红绿灯）+ 侧栏导航四项 + 占位页切换。"""
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

    # 侧栏导航：四项，「仪表盘」默认激活
    nav = page.locator(".sidebar .nav-item")
    expect(nav).to_have_count(4)
    expect(page.locator(".sidebar .nav-item.active")).to_have_text("仪表盘")

    # 切到「数据」占位页：标题 + Placeholder 标记可见
    page.get_by_role("button", name="数据").click()
    expect(page.get_by_role("heading", name="数据浏览")).to_be_visible()
    expect(page.locator(".ph-tag").first).to_contain_text("Placeholder")

    # 回归：搜索框放大镜 SVG 曾无显式尺寸被撑成千像素巨图（整框跟着撑爆）
    mag = page.locator(".mag").bounding_box()
    assert mag is not None and mag["width"] <= 20 and mag["height"] <= 20, \
        f"放大镜图标被撑大: {mag}"
    sb = page.locator(".ph-search").bounding_box()
    assert sb is not None and sb["height"] <= 60, f"搜索框高度异常: {sb}"


def test_source_pages(servers, page):
    """数据源子页（Carbon 原型 ⑥⑦ 屏）：子项选中灰底+右箭头、父项「数据」保留
    高亮；标题/副题/来源 Tag 随源切换；行高 32px 文字垂直居中（曾 block 贴顶）。"""
    _, frontend_port = servers
    page.goto(f"http://127.0.0.1:{frontend_port}/")
    page.locator(".theme-toggle:visible").get_by_role("button", name="Carbon").click()
    expect(page.locator("html")).to_have_attribute("data-theme", "carbon")

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


def test_theme_toggle_and_persistence(servers, page):
    """风格切换：默认 apple；切 Fluent 2（按原型无标题栏/白侧栏/徽章）；刷新保持；可切回。"""
    _, frontend_port = servers
    page.goto(f"http://127.0.0.1:{frontend_port}/")

    # 默认苹果风格：标题栏可见（双主题各有一份切换控件，只操作当前可见的）
    expect(page.locator("html")).to_have_attribute("data-theme", "apple")
    expect(page.locator(".titlebar")).to_be_visible()

    # 切到 Fluent 2：无标题栏；侧栏 260px 白底；卡片 8px 圆角；状态徽章带文字
    page.locator(".theme-toggle:visible").get_by_role("button", name="Fluent 2").click()
    expect(page.locator("html")).to_have_attribute("data-theme", "fluent")
    expect(page.locator(".titlebar")).to_be_hidden()
    page.wait_for_function(
        "getComputedStyle(document.querySelector('.sidebar')).backgroundColor === 'rgb(255, 255, 255)'")
    assert page.locator(".sidebar").evaluate(
        "el => getComputedStyle(el).width") == "260px", "fluent 侧栏应为 260px"
    assert page.locator(".card").first.evaluate(
        "el => getComputedStyle(el).borderRadius") == "8px", "fluent 卡片圆角应为 8px"
    expect(page.locator(".fbadge", has_text="运行中").first).to_be_visible()

    # 刷新后保持 fluent（localStorage 持久化 + 首帧预处理）
    page.reload()
    expect(page.locator("html")).to_have_attribute("data-theme", "fluent")

    # 切回苹果风格：标题栏回归 40px
    page.locator(".theme-toggle:visible").get_by_role("button", name="苹果风格").click()
    expect(page.locator("html")).to_have_attribute("data-theme", "apple")
    expect(page.locator(".titlebar")).to_be_visible()
    assert page.locator(".titlebar").evaluate(
        "el => getComputedStyle(el).height") == "40px", "apple 标题栏应为 40px"


def test_carbon_theme_shell(servers, page):
    """Carbon 主题（按 Penpot 原型）：深色 UI Shell 头 48px + 256px 白侧栏 +
    方角 1px 边框卡 + 蓝主按钮 #0F62FE；刷新持久化；可切回。"""
    _, frontend_port = servers
    page.goto(f"http://127.0.0.1:{frontend_port}/")

    page.locator(".theme-toggle:visible").get_by_role("button", name="Carbon").click()
    expect(page.locator("html")).to_have_attribute("data-theme", "carbon")

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
    assert page.locator(".card").first.evaluate(
        "el => getComputedStyle(el).borderRadius") == "0px", "carbon 卡片应为方角"
    btn = page.get_by_role("button", name="重新检测")
    expect(btn).to_be_visible()
    assert btn.evaluate("el => getComputedStyle(el).backgroundColor") == "rgb(15, 98, 254)", \
        "carbon 主按钮应为 #0F62FE"
    assert btn.evaluate("el => getComputedStyle(el).color") == "rgb(255, 255, 255)", \
        "carbon 主按钮文字应为白色"

    # 状态徽章：dot + 12px 文字（后端 API 运行中）
    expect(page.locator(".fbadge", has_text="运行中").first).to_be_visible()

    # 数据页：DataTable 表头 48px 灰字 + 蓝色 Placeholder Tag
    page.get_by_role("button", name="数据").click()
    head = page.locator(".ph-table .ph-head")
    expect(head).to_contain_text("代码")
    assert head.evaluate("el => getComputedStyle(el).height") == "48px", "carbon 表头行应为 48px"
    assert head.evaluate("el => getComputedStyle(el).color") == "rgb(82, 82, 82)", \
        "carbon 表头文字应为 #525252"
    tag = page.locator(".ph-tag").first
    assert tag.evaluate("el => getComputedStyle(el).backgroundColor") == "rgb(15, 98, 254)", \
        "carbon Placeholder Tag 应为蓝底"

    # 刷新持久化；切回苹果风格
    page.reload()
    expect(page.locator("html")).to_have_attribute("data-theme", "carbon")
    page.locator(".theme-toggle:visible").get_by_role("button", name="苹果风格").click()
    expect(page.locator("html")).to_have_attribute("data-theme", "apple")


def test_error_banner_icon_sizing(servers, page):
    """回归：错误横幅 SVG 警告图标曾无显式尺寸被撑成千像素巨图（三主题共用）。
    拦截 /healthz 制造后端不可用，断言图标不大于 20px 且横幅高度正常。"""
    _, frontend_port = servers
    page.route("**/healthz", lambda r: r.fulfill(status=500, body="x"))
    page.goto(f"http://127.0.0.1:{frontend_port}/")

    expect(page.locator(".error-banner")).to_be_visible()
    box = page.locator(".error-banner .warn-icon").first.bounding_box()
    assert box is not None and box["width"] <= 20 and box["height"] <= 20, \
        f"警告图标被撑大: {box}"
    bh = page.locator(".error-banner").evaluate("el => el.getBoundingClientRect().height")
    assert bh <= 80, f"错误横幅高度异常: {bh}"
