"""东方财富 Cookie：存储 / 读取 / 有效性验证。

Cookie 由用户从浏览器复制（登录或匿名访问东财均可获得），存于数据卷文件
（EM_COOKIE_PATH，默认 /srv/data/em_cookie.txt；本地默认 ./data/em_cookie.txt）。
爬虫启动东财源时前置校验：无 Cookie / 验证失败直接拒绝启动（裸请求是上次
封禁的触发器，不允许再裸奔）。
"""

import os
import time

import httpx

# 默认路径：容器内挂载卷优先；本地开发落到仓库 ./data/（已 gitignore）
_DEFAULT = "./data/em_cookie.txt"
EM_COOKIE_PATH = os.environ.get("EM_COOKIE_PATH", _DEFAULT)

# 验证用：轻量接口（探测存活而非抓全量数据）
_VERIFY_URL = ("https://push2.eastmoney.com/api/qt/clist/get"
               "?pn=1&pz=3&po=1&np=1&fltt=2&invt=2&fid=f12"
               "&fs=m:0+t:6&fields=f12,f13,f14")
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def load_cookie(path: str | None = None) -> str:
    """读已存 Cookie；不存在/为空返回空串。"""
    path = path or EM_COOKIE_PATH
    try:
        with open(path, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def save_cookie(cookie: str, path: str | None = None) -> str:
    """存 Cookie（去首尾空白与引号包裹），返回落盘路径。"""
    path = path or EM_COOKIE_PATH
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    clean = cookie.strip().strip('"').strip("'")
    with open(path, "w", encoding="utf-8") as f:
        f.write(clean)
    return path


def clear_cookie(path: str | None = None) -> None:
    path = path or EM_COOKIE_PATH
    try:
        os.remove(path)
    except OSError:
        pass


def verify_cookie(cookie: str, timeout: float = 10.0) -> dict:
    """带 Cookie 实测东财接口，判有效性。

    返回 {valid, http_status, reason, checked_at}：
    - 200 且业务 rc=0 → valid=True
    - 200 但 rc!=0/空数据 → Cookie 被接受但接口异常（视无效，附原因）
    - 连接层拒绝（RemoteDisconnected 等）→ IP 风控仍在（与 Cookie 无关也要告知）
    """
    checked_at = time.strftime("%Y-%m-%d %H:%M:%S")
    if not cookie:
        return {"valid": False, "http_status": None,
                "reason": "cookie 为空", "checked_at": checked_at}
    try:
        resp = httpx.get(_VERIFY_URL, headers={
            "User-Agent": _UA, "Cookie": cookie,
            "Referer": "https://quote.eastmoney.com/",
        }, timeout=timeout)
    except Exception as exc:
        return {"valid": False, "http_status": None,
                "reason": f"连接失败（可能 IP 仍被风控）: {type(exc).__name__}: {str(exc)[:80]}",
                "checked_at": checked_at}
    if resp.status_code != 200:
        return {"valid": False, "http_status": resp.status_code,
                "reason": f"HTTP {resp.status_code}", "checked_at": checked_at}
    try:
        payload = resp.json()
    except ValueError:
        return {"valid": False, "http_status": 200,
                "reason": "响应非 JSON（可能被风控页拦截）", "checked_at": checked_at}
    data = payload.get("data") or {}
    if payload.get("rc") == 0 and data.get("diff"):
        return {"valid": True, "http_status": 200,
                "reason": "验证通过", "checked_at": checked_at}
    return {"valid": False, "http_status": 200,
            "reason": f"业务码 rc={payload.get('rc')} 或数据为空", "checked_at": checked_at}
