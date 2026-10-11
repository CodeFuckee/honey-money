"""退市股补爬数据源（修全量回测幸存者偏差）。

清单：交易所退市名单（akshare stock_info_sh_delist / stock_info_sz_delist），
剔除仍在市的代码（暂停上市后恢复的案例）；
日线：新浪 klc_kl.js 全史接口 + py_mini_racer 跑 akshare 自带的 hk_js_decode
——akshare 的 stock_zh_a_daily 对退市股挂在第二步「流通股本」接口
（demjson No value to decode），K线本体解码其实可用，本模块只取其第一步。

成交额/流通股本/换手率退市股无数据 → 置 NULL（回测引擎只用 OHLC+成交量）。
复权因子同理大概率缺失 → 回测侧降级不复权（现有机制）。

限速：与主爬虫共用新浪通道（SINA_CHANNEL，host 归并后同键），AIMD +
jitter + 批间休眠 + 连续失败熔停全套继承，宁慢勿封。
"""

import asyncio

import httpx
import pandas as pd

from .eastmoney import DailyBars
from .rate import RateLimiter

SINA_CHANNEL = "sina"
KLC_KL_URL = ("https://finance.sina.com.cn/realstock/company/"
              "{symbol}/hisdata/klc_kl.js")


class DelistedError(Exception):
    """退市股日线最终失败。"""


def _delist_lists() -> tuple[pd.DataFrame, pd.DataFrame]:
    """交易所退市名单（沪、深），延迟导入 akshare。"""
    import akshare as ak
    sh = ak.stock_info_sh_delist()
    sz = ak.stock_info_sz_delist()
    return sh, sz


def _live_codes() -> set[str]:
    """在市代码集合（用于剔除暂停上市后恢复的股票）。"""
    import akshare as ak
    spot = ak.stock_zh_a_spot()
    return {str(c).zfill(6) for c in spot["代码"].tolist()}


def parse_delist_lists(sh_df: pd.DataFrame, sz_df: pd.DataFrame,
                       live_codes: set[str]) -> list[dict]:
    """两所退市名单 → [{code, market, name, delisted}]；在市代码剔除。"""
    stocks: list[dict] = []
    for df, market in ((sh_df, 1), (sz_df, 0)):
        if df is None or len(df) == 0:
            continue
        code_col = "公司代码" if market == 1 else "证券代码"
        name_col = "公司简称" if market == 1 else "证券简称"
        for _, row in df.iterrows():
            code = str(row[code_col]).zfill(6)
            if code in live_codes:
                continue                       # 暂停上市后恢复：仍在市，不标退
            stocks.append({"code": code, "market": market,
                           "name": str(row[name_col]), "delisted": True})
    return stocks


def js_rows_to_bars(decoded: list[dict]) -> list[tuple]:
    """hk_js_decode 输出 → 统一 12 元组行（对齐 df_to_rows 口径）。

    前收盘递推现算振幅/涨跌幅/涨跌额（首根 None）；成交额/换手率/流通股本
    退市股无数据 → None。volume 股 → 手。
    """
    rows: list[tuple] = []
    prev_close = None
    for r in decoded or []:
        ds = str(r["date"])[:10]
        close = float(r["close"])
        high, low = float(r["high"]), float(r["low"])
        volume_hand = int(float(r["volume"]) // 100)   # 股 → 手
        amplitude = pct_chg = change = None
        if prev_close:
            amplitude = (high - low) / prev_close * 100
            pct_chg = (close - prev_close) / prev_close * 100
            change = close - prev_close
        rows.append((ds, float(r["open"]), close, high, low, volume_hand,
                     None, amplitude, pct_chg, change, None, None))
        prev_close = close
    return rows


def _decode_kl(text: str) -> list[dict]:
    """klc_kl.js 文本 → 日线 dict 列表（py_mini_racer 跑 akshare 的解码 JS）。"""
    import py_mini_racer
    from akshare.stock.stock_zh_a_sina import hk_js_decode
    payload = text.split("=", 1)[1].split(";")[0].replace('"', "")
    js = py_mini_racer.MiniRacer()
    js.eval(hk_js_decode)
    return js.call("d", payload)


class DelistedClient:
    """与 SinaClient 同构：fetch_stock_list / fetch_daily_bars（async）。"""

    def __init__(self, *, rate: RateLimiter, http: httpx.AsyncClient,
                 fetch_timeout: float = 30.0):
        self._rate = rate
        self._http = http
        self._fetch_timeout = fetch_timeout

    async def fetch_stock_list(self) -> list[dict]:
        sh, sz = await asyncio.to_thread(_delist_lists)
        live = await asyncio.to_thread(_live_codes)
        return parse_delist_lists(sh, sz, live)

    async def fetch_daily_bars(self, market: int, code: str, *,
                               beg: str = "0", end: str = "20500101") -> DailyBars:
        """退市股全史日线（beg 忽略：历史不可变，upsert 幂等去重）。"""
        symbol = ("sh" if market == 1 else "sz") + code
        await self._rate.acquire(SINA_CHANNEL)
        try:
            text = await self._get_text(symbol)
            decoded = await asyncio.to_thread(_decode_kl, text)
            rows = js_rows_to_bars(decoded)
        except asyncio.TimeoutError as exc:
            self._rate.report(SINA_CHANNEL, False)
            raise DelistedError(f"退市股日线获取超时({symbol})") from exc
        except Exception as exc:
            self._rate.report(SINA_CHANNEL, False)
            raise DelistedError(f"退市股日线获取失败({symbol}): {exc}") from exc
        self._rate.report(SINA_CHANNEL, True)
        return DailyBars(code=code, name="", dktotal=len(rows), rows=rows,
                         source="sina")

    async def _get_text(self, symbol: str) -> str:
        r = await self._http.get(
            KLC_KL_URL.format(symbol=symbol),
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                   "Chrome/126.0",
                     "Referer": "https://finance.sina.com.cn"},
            timeout=self._fetch_timeout)
        r.raise_for_status()
        return r.text
