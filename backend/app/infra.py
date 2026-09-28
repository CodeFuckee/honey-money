"""基础设施连通性探测：PostgreSQL 与代理池，各自独立、快速失败。"""

import httpx
import psycopg


def check_postgres(dsn: str, timeout: float = 1.5) -> dict:
    """SELECT 1 探活。返回 {"status": "up"/"down", "detail": ...}。

    连接串非法/服务不可达都归为 down 并带原因——健康检查接口不允许抛异常。
    """
    try:
        with psycopg.connect(dsn, connect_timeout=int(timeout)) as conn:
            conn.execute("SELECT 1")
        return {"status": "up", "detail": "SELECT 1 ok"}
    except Exception as exc:  # 连不上属于"状态"而非"错误"
        return {"status": "down", "detail": f"{type(exc).__name__}: {str(exc)[:120]}"}


def check_proxy_pool(base_url: str, timeout: float = 1.5) -> dict:
    """GET {base_url}/get/ 探活（proxy_pool 的取代理接口；Flask 路由带尾斜杠，
    请求 /get 会得到 308 重定向而非 200）。"""
    try:
        resp = httpx.get(f"{base_url.rstrip('/')}/get/", timeout=timeout)
        if resp.status_code == 200:
            return {"status": "up", "detail": resp.text[:120]}
        return {"status": "down", "detail": f"HTTP {resp.status_code}"}
    except Exception as exc:
        return {"status": "down", "detail": f"{type(exc).__name__}: {str(exc)[:120]}"}
