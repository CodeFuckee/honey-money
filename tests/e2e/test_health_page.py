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

    # 切回仪表盘：四卡仍在
    page.get_by_role("button", name="仪表盘").click()
    expect(page.locator(".card")).to_have_count(4, timeout=20000)
