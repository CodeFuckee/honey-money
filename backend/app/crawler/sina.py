"""新浪数据源客户端（主力源，替代被封的东财直连）。

- 清单：自研直调新浪 getHQNodeData 分页（node=hs_a 沪深京全 A，过滤北交所）；
- 日线：经 akshare 的 stock_zh_a_daily（新浪 kjs 全史接口，一次全史含
  流通股本/换手率），同步库用 to_thread 包裹，节奏由限速通道控制。

防封设计：新浪域名独立限速通道（初始 1 QPS + AIMD + jitter），每次取数前
必须 acquire；不做代理（新浪对家宽直连友好，免费代理已被实测证明无效）。
"""

import asyncio
from dataclasses import dataclass

import httpx

from .eastmoney import DailyBars
from .rate import RateLimiter

SINA_LIST_URL = ("http://vip.stock.finance.sina.com.cn/quotes_service/api/"
                 "json_v2.php/Market_Center.getHQNodeData")
SINA_COUNT_URL = ("http://vip.stock.finance.sina.com.cn/quotes_service/api/"
                  "json_v2.php/Market_Center.getHQNodeStockCount")
SINA_CHANNEL = "sina"          # 清单与日线共用一个新浪通道（同一服务商统一限速）
SINA_REFERER = "https://finance.sina.com.cn"
MIN_LIST_RATIO = 0.85          # 清单完整性下限（hs_a 含北交所 ~6%，过滤后应 ≥85%）


class SinaError(Exception):
    """新浪源最终失败。"""


def _default_daily_fetcher(symbol: str, *, start_date=None, end_date=None):
    import akshare as ak   # 延迟导入：重依赖，仅新浪源需要
    return ak.stock_zh_a_daily(symbol=symbol, start_date=start_date, end_date=end_date)


def df_to_rows(df, beg: str = "0") -> list[tuple]:
    """akshare 新浪日线 DataFrame → 统一 12 元组行。

    单位对齐东财口径：volume 股 → 手；衍生字段（振幅/涨跌幅/涨跌额）新浪
    不提供，基于前收盘现算（首根无前收为 None）——行结构跨源一致，便于对比。
    """
    if df is None or len(df) == 0:
        return []
    beg_iso = f"{beg[:4]}-{beg[4:6]}-{beg[6:8]}" if beg and beg != "0" else None
    n = len(df["date"])
    rows: list[tuple] = []
    prev_close = None
    for i in range(n):
        d = df["date"][i]
        ds = d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)
        close = float(df["close"][i])
        if beg_iso and ds < beg_iso:      # beg 之前的行不输出，但保留前收供首根现算
            prev_close = close
            continue
        high, low = float(df["high"][i]), float(df["low"][i])
        volume_hand = int(float(df["volume"][i]) // 100)   # 股 → 手
        amplitude = pct_chg = change = None
        if prev_close:
            amplitude = (high - low) / prev_close * 100
            pct_chg = (close - prev_close) / prev_close * 100
            change = close - prev_close
        rows.append((
            ds, float(df["open"][i]), close, high, low, volume_hand,
            float(df["amount"][i]),
            amplitude, pct_chg, change,
            float(df["turnover"][i]) * 100,   # 新浪为小数，统一百分数口径（对齐东财）
            float(df["outstanding_share"][i]),
        ))
        prev_close = close
    return rows


class SinaClient:
    """与 EastmoneyClient 同构：fetch_stock_list / fetch_daily_bars。"""

    def __init__(self, *, rate: RateLimiter, http: httpx.AsyncClient,
                 daily_fetcher=None, fetch_timeout: float = 60.0):
        self._rate = rate
        self._http = http
        self._daily = daily_fetcher or _default_daily_fetcher
        self._fetch_timeout = fetch_timeout

    async def _fetch_list_total(self) -> int:
        """新浪官方股票总数（hs_a 含北交所）——清单完整性校验基准。"""
        await self._rate.acquire(SINA_CHANNEL)
        try:
            resp = await self._http.get(SINA_COUNT_URL, params={"node": "hs_a"},
                                        headers={"Referer": SINA_REFERER})
            total = int(resp.json())
        except Exception as exc:
            self._rate.report(SINA_CHANNEL, False)
            raise SinaError(f"新浪清单总数获取失败: {exc}") from exc
        self._rate.report(SINA_CHANNEL, True)
        return total

    async def fetch_stock_list(self) -> list[dict]:
        """沪深 A 股清单（sh/sz 前缀转 market，北交所等其余前缀过滤）。

        完整性防护：新浪分页在连续请求下会返回空页截断（实测软限流）——
        空页重试同页（最多 2 次，冷却递增）；收尾与官方总数校验，差超
        15% 直接报错，宁可失败重跑也不静默用残缺清单漏爬。
        """
        total = await self._fetch_list_total()
        stocks, page, empty_retries = [], 1, 0
        max_page = total // 100 + 10   # 分页上限防御：异常场景下绝不无限翻页
        while page <= max_page:
            await self._rate.acquire(SINA_CHANNEL)
            try:
                resp = await self._http.get(SINA_LIST_URL, params={
                    "page": page, "num": 100, "sort": "symbol", "asc": 1,
                    "node": "hs_a",
                }, headers={"Referer": SINA_REFERER})
                page_rows = resp.json()
                if not isinstance(page_rows, list):
                    raise ValueError(f"非预期响应: {str(page_rows)[:60]}")
            except Exception as exc:
                self._rate.report(SINA_CHANNEL, False)
                raise SinaError(f"新浪清单接口失败(page={page}): {exc}") from exc
            self._rate.report(SINA_CHANNEL, True)

            if not page_rows:      # 空页：重试同页，疑似软限流截断
                empty_retries += 1
                if empty_retries > 2:
                    break
                await asyncio.sleep(1.5 * empty_retries)
                continue
            empty_retries = 0
            for r in page_rows:
                sym = r.get("symbol", "")
                if sym.startswith("sh"):
                    market = 1
                elif sym.startswith("sz"):
                    market = 0
                else:
                    continue   # 北交所(bj)等暂不采集
                stocks.append({"code": r["code"], "market": market, "name": r["name"]})
            page += 1

        if len(stocks) < total * MIN_LIST_RATIO:
            raise SinaError(
                f"清单不完整：仅拿到 {len(stocks)}/{total} 只"
                f"（低于 {MIN_LIST_RATIO:.0%}，新浪分页疑似被软限流截断），请稍后重跑")
        return stocks

    async def fetch_daily_bars(self, market: int, code: str, *,
                               beg: str = "0", end: str = "20500101") -> DailyBars:
        """单只股票日线（akshare 全史 + beg 增量过滤），不复权口径。"""
        symbol = ("sh" if market == 1 else "sz") + code
        start = None if beg in ("", "0") else beg
        await self._rate.acquire(SINA_CHANNEL)
        try:
            # akshare 同步库内部 requests 不带超时，NAS 网络下偶发永久挂起
            # （实测：事件循环停摆 + PG 事务悬空锁表）——外层必须加超时兜底，
            # 挂起降级为普通单股失败，交给连续失败熔停机制处置。
            df = await asyncio.wait_for(
                asyncio.to_thread(self._daily, symbol, start_date=start),
                timeout=self._fetch_timeout)
            rows = df_to_rows(df, beg=beg)
        except asyncio.TimeoutError as exc:
            self._rate.report(SINA_CHANNEL, False)
            raise SinaError(
                f"新浪日线获取超时({symbol}, {self._fetch_timeout}s, "
                f"akshare 内部挂起)") from exc
        except Exception as exc:
            self._rate.report(SINA_CHANNEL, False)
            raise SinaError(f"新浪日线获取失败({symbol}): {exc}") from exc
        self._rate.report(SINA_CHANNEL, True)
        return DailyBars(code=code, name="", dktotal=len(rows), rows=rows,
                         source="sina")
