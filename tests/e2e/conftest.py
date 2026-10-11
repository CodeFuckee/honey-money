"""E2E 共享 fixture：本地拉起后端(uvicorn)与前端(vite preview)。

从 test_health_page.py 提升为目录级共享（任务页等新用例复用同一套服务）。
"""

import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    """让 OS 分配一个当前空闲端口（本机开发端口常被占，写死不可靠）。"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _stop(proc: subprocess.Popen) -> None:
    """终止进程及其子进程组：npm→sh→vite 三层链只 kill 中间层会漏掉
    真正监听端口的 vite 孙进程（实测每轮 E2E 泄漏一个 node 进程）。"""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass


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
        # 先重建前端产物再 preview：vite preview 只服务现成 dist，源码改完
        # 忘了 build 会静默测到旧版本（曾因此误判新功能 E2E 全挂）
        build = subprocess.run(
            ["npm", "run", "build"], cwd=ROOT / "frontend",
            shell=(sys.platform == "win32"), capture_output=True, text=True)
        assert build.returncode == 0, f"前端构建失败:\n{build.stderr[-2000:]}"
        # 注入本地必失败地址：PG/代理池探测秒级返回 down（默认 DSN 的主机名
        # postgres 在无 DNS 的本机要等十几秒才超时，页面会长时间停在 1 张卡）
        backend_env = {**os.environ,
                       "DATABASE_URL": "postgresql://x:x@127.0.0.1:59999/none",
                       "PROXY_POOL_URL": "http://127.0.0.1:59998"}
        # 各进程独立进程组，收尾按组终止（见 _stop 注释）
        procs.append(subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app",
             "--port", str(backend_port), "--log-level", "warning"],
            cwd=ROOT / "backend", env=backend_env, start_new_session=True,
        ))
        env = {**os.environ, "BACKEND_PORT": str(backend_port)}
        # Windows 的 npm 是 npm.cmd 需要 shell；POSIX 直接 exec（shell=True+数组
        # 在 POSIX 下只有首元素生效，preview 参数会全部丢失）
        procs.append(subprocess.Popen(
            ["npm", "run", "preview", "--", "--port", str(frontend_port),
             "--strictPort", "--host", "127.0.0.1"],  # 默认只绑 IPv6 的 ::1
            cwd=ROOT / "frontend", shell=(sys.platform == "win32"), env=env,
            start_new_session=True,
        ))
        assert _wait(f"http://127.0.0.1:{backend_port}/healthz"), "后端 30s 内未就绪"
        assert _wait(f"http://127.0.0.1:{frontend_port}/"), "前端 preview 30s 内未就绪"
        yield backend_port, frontend_port
    finally:
        for p in procs:
            _stop(p)
