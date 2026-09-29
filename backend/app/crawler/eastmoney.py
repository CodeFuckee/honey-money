"""东方财富行情接口客户端（爬虫数据源）。

两个接口（均免注册、需带公开 ut 参数与浏览器 UA，缺 ut 会拿到空响应）：
- 清单：push2.eastmoney.com /api/qt/clist/get —— 沪深 A 股代码/市场/名称，pn 分页；
- 日线：push2his.eastmoney.com /api/qt/stock/kline/get —— beg=0&lmt=100000 单次全史
  （fqt=0 不复权原始值；涨跌幅等衍生字段由东财按原始价计算好返回）。

失败分类（关键设计）：
- 代理层失败（ProxyError/ConnectError）：免费代理常见病——换代理重试，
  不计入通道限速（不是目标站在拒绝我们）；
- 目标层失败（rc!=0 / data 为空 / 非 JSON）：计入通道 report(False)，驱动降速。
"""

from dataclasses import dataclass

import httpx

from .proxies import ProxyBroker
from .rate import RateLimiter

UT = "fa5fd1943c7b386f172d6893dbfba10b"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

CLIST_URL = "https://push2.eastmoney.com/api/qt/clist/get"
KLINE_URL = "https://push2his.eastmoney.com/api/qt/stock/kline/get"

# 沪深 A 股：深主板 t:6 + 创业板 t:80 + 沪主板 t:2 + 科创板 t:23（实测 5561 只）；
# 北交所（m:0+t:81+s:2048）暂不包含，需要时在此扩展。
FS_HS_A = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23"


class EastmoneyError(Exception):
    """东财接口最终失败（代理重试用尽 / 目标站返回异常）。"""


def secid(market: int, code: str) -> str:
    """东财 secid：1=沪 0=深（market 取自清单接口 f13，不从代码猜测）。"""
    return f"{market}.{code}"


def parse_kline_row(line: str) -> tuple:
    """klines 单行 → 11 元组。

    行格式（fields2=f51..f61）：日期,开,收,高,低,成交量(手),成交额,振幅,涨跌幅,涨跌额,换手率
    """
    parts = line.split(",")
    if len(parts) != 11:
        raise ValueError(f"kline 行字段数异常（{len(parts)}）: {line[:60]!r}")
    date, *nums = parts
    try:
        open_, close, high, low, volume, amount, amplitude, pct_chg, change, turnover = (
            float(x) for x in nums
        )
    except ValueError as exc:
        raise ValueError(f"kline 行含非数值字段: {line[:60]!r}") from exc
    return (date, open_, close, high, low, int(volume), amount,
            amplitude, pct_chg, change, turnover)


@dataclass
class DailyBars:
    code: str
    name: str
    dktotal: int
    rows: list[tuple]


class EastmoneyClient:
    def __init__(self, *, rate: RateLimiter, proxy_broker: ProxyBroker,
                 http: httpx.AsyncClient, max_proxy_retries: int = 2,
                 timeout: float = 15.0, proxy_mode: str = "pool"):
        """proxy_mode: pool=走代理池（空池/不可用自动回退直连）；
        direct=全部直连——实测东财对免费代理的机房 IP 段全部断连，
        家宽/办公网直连 + 低 QPS 才是有效路径（见 docs/ashare-daily-data-sources.md）。"""
        self._rate = rate
        self._broker = proxy_broker
        self._http = http                       # 直连用（代理不可用时兜底）
        self._max_proxy_retries = max_proxy_retries
        self._timeout = timeout
        self._proxy_mode = proxy_mode
        self._proxy_clients: dict[str, httpx.AsyncClient] = {}

    async def _client_for(self, proxy: str | None) -> httpx.AsyncClient:
        """代理 → 专属 AsyncClient（按代理复用连接池）；None → 直连客户端。"""
        if proxy is None:
            return self._http
        client = self._proxy_clients.get(proxy)
        if client is None:
            client = httpx.AsyncClient(proxy=proxy, timeout=self._timeout)
            self._proxy_clients[proxy] = client
        return client

    async def _request(self, url: str, params: dict) -> httpx.Response:
        """限速 → 取代理 → 请求；传输层失败按类型处置后重试，用尽抛错。

        - ProxyError/ConnectError：代理连不上/被拒 → 换代理，不评价目标站通道；
        - RemoteProtocolError/Timeout：对端断连或超时（坏代理截断、目标站限流
          都可能）→ 换代理 + 记通道失败（保守降速，AIMD 会因后续成功自动恢复）。
        """
        key = self._rate.channel_key(url)
        last_exc: Exception | None = None
        for _ in range(self._max_proxy_retries + 1):
            await self._rate.acquire(key)
            proxy = (await self._broker.acquire()
                     if self._proxy_mode == "pool" else None)
            try:
                resp = await (await self._client_for(proxy)).get(
                    url, params=params, headers={"User-Agent": UA},
                )
            except (httpx.ProxyError, httpx.ConnectError) as exc:
                await self._broker.report_failure(proxy)
                last_exc = exc
                continue
            except (httpx.RemoteProtocolError, httpx.TimeoutException) as exc:
                await self._broker.report_failure(proxy)
                self._rate.report(key, False)
                last_exc = exc
                continue
            await self._broker.report_success(proxy)
            return resp
        raise EastmoneyError(
            f"传输层重试用尽: {url} "
            f"({type(last_exc).__name__}: {last_exc})")

    async def fetch_stock_list(self) -> list[dict]:
        """全量沪深 A 股清单，pn 翻页直到 diff 为空。"""
        stocks, pn = [], 1
        while True:
            resp = await self._request(CLIST_URL, {
                "pn": pn, "pz": 100, "po": 1, "np": 1, "fltt": 2, "invt": 2,
                "fid": "f12", "fs": FS_HS_A, "fields": "f12,f13,f14",
            })
            key = self._rate.channel_key(CLIST_URL)
            try:
                payload = resp.json()
            except ValueError as exc:
                self._rate.report(key, False)
                raise EastmoneyError(f"清单接口返回非 JSON(pn={pn})") from exc
            data = payload.get("data")
            if payload.get("rc") != 0 or not data:
                self._rate.report(key, False)
                raise EastmoneyError(f"清单接口 rc={payload.get('rc')} data 为空(pn={pn})")
            diff = data.get("diff") or []
            self._rate.report(key, True)
            if not diff:
                return stocks
            stocks.extend(
                {"code": row["f12"], "market": row["f13"], "name": row["f14"]}
                for row in diff
            )
            pn += 1

    async def fetch_daily_bars(self, market: int, code: str, *,
                               beg: str = "0", end: str = "20500101") -> DailyBars:
        """单只股票全史/区间日线（klt=101 日线，fqt=0 不复权）。"""
        resp = await self._request(KLINE_URL, {
            "secid": secid(market, code), "ut": UT,
            "fields1": "f1,f2,f3,f4,f5,f6",
            "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
            "klt": 101, "fqt": 0, "beg": beg, "end": end, "lmt": 100000,
        })
        key = self._rate.channel_key(KLINE_URL)
        try:
            payload = resp.json()
        except ValueError as exc:
            self._rate.report(key, False)
            raise EastmoneyError(f"K线接口返回非 JSON({code})") from exc
        data = payload.get("data")
        if payload.get("rc") != 0 or data is None:
            self._rate.report(key, False)
            raise EastmoneyError(f"K线接口 rc={payload.get('rc')} data 为空({code})")
        try:
            rows = [parse_kline_row(line) for line in data.get("klines") or []]
        except ValueError as exc:
            # klines 为空是正常业务结果（增量无新数据），行内容坏才是失败
            self._rate.report(key, False)
            raise EastmoneyError(f"K线数据行解析失败({code})") from exc
        self._rate.report(key, True)
        return DailyBars(code=code, name=data.get("name", ""),
                         dktotal=data.get("dktotal", 0), rows=rows)

    async def aclose(self) -> None:
        for client in self._proxy_clients.values():
            await client.aclose()
        self._proxy_clients.clear()
