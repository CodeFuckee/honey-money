"""代理池经纪：包装 jhao104/proxy_pool 的 Web API。

策略：取到的代理连续成功就复用（免费代理稀缺，且握手成本高）；
失败立即 /delete/ 降分并弃用，下次取新代理。池空 / 池服务不可用时
返回 None，调用方降级为直连——爬虫不许因为代理池故障而停摆。
"""

import httpx


class ProxyBroker:
    def __init__(self, http: httpx.AsyncClient):
        """http: base_url 指向 proxy_pool 服务的 AsyncClient。"""
        self._http = http
        self._cached: str | None = None   # 当前复用中的代理（http://ip:port）

    async def acquire(self) -> str | None:
        """取一个可用代理；复用缓存；池空/不可用返回 None（直连）。"""
        if self._cached:
            return self._cached
        try:
            resp = await self._http.get("/get/")
            data = resp.json()
            proxy = self._parse_get_response(data)
        except Exception:
            return None   # 池服务挂了不算错误状态，直连兜底
        if proxy:
            self._cached = f"http://{proxy}"
        return self._cached

    @staticmethod
    def _parse_get_response(data) -> str | None:
        """jhao104 /get/ 返回 {"proxy": "ip:port", ...}；空池返回无 proxy 键的 dict。"""
        if isinstance(data, dict) and data.get("proxy"):
            return str(data["proxy"])
        return None

    async def report_success(self, proxy: str | None) -> None:
        """成功：保持缓存继续复用（无需调用池子）。"""

    async def report_failure(self, proxy: str | None) -> None:
        """失败：/delete/ 让池子降分；直连（None）无可降分，忽略。"""
        if not proxy:
            return
        self._cached = None
        try:
            await self._http.get("/delete/", params={"proxy": proxy.removeprefix("http://")})
        except Exception:
            pass   # 降分失败不影响主流程
