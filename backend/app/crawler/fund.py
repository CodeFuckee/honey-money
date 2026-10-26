"""基金历史净值数据源（东财天天基金，任务 source=fund）。

清单与净值各一个接口（均免 Cookie，带 Referer 即可；与 push2his 股票接口
不同子系统，2026-10-26 实测可用——见知识库 fund-nav-data-sources.md）：
- 清单：fund.eastmoney.com/js/fundcode_search.js —— 全部基金代码/名称/类型
  一次返回（含已清盘与 A/C 份额，全量爬取避免回测幸存者偏差）；
- 净值：fund.eastmoney.com/pingzhongdata/{code}.js —— 单请求全史
  （单位净值 Data_netWorthTrend + 累计净值 Data_ACWorthTrend）。lsjz 分页
  接口单页上限 20 条、全史几百页/只，不可用作爬虫通道。

x 时间戳为北京时间零点锚点（毫秒），涨跌幅 equityReturn 可能是 "-"（缺省）。
货币基金无净值走势（空数组）→ 0 行正常返回，由上层记成功（跳过语义）。

限速通道：按域名 channel_key 归并到 fund.eastmoney.com，默认 1 QPS AIMD
（调度器 _SOURCE_DEFAULTS，失败自动降速 + 连续失败熔停），宁慢勿封。
"""

import datetime as dt
import json
import re
from zoneinfo import ZoneInfo

import httpx

from .eastmoney import DailyBars, UA
from .rate import RateLimiter

SH_TZ = ZoneInfo("Asia/Shanghai")

FUND_LIST_URL = "https://fund.eastmoney.com/js/fundcode_search.js"
PINGZHONG_URL = "https://fund.eastmoney.com/pingzhongdata/{code}.js"

_NET_WORTH_RE = re.compile(r"Data_netWorthTrend\s*=\s*(\[.*?\])\s*;", re.S)
_ACC_WORTH_RE = re.compile(r"Data_ACWorthTrend\s*=\s*(\[.*?\])\s*;", re.S)
_NAME_RE = re.compile(r'fS_name\s*=\s*"([^"]*)"')


class FundError(Exception):
    """基金接口最终失败（传输/解析）。"""


def _ms_to_date(x_ms) -> str:
    """北京零点锚点 epoch 毫秒 → ISO 日期。"""
    return dt.datetime.fromtimestamp(x_ms / 1000, SH_TZ).date().isoformat()


def _num_or_none(v):
    """equityReturn 等数值字段："-" 或缺失 → None。"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_fund_list(text: str) -> list[dict]:
    """fundcode_search.js → [{code, market=0, name, fund_type}]。

    行形如 ["000001","HXCZHH","华夏成长混合","混合型-灵活","HXCZHH"]；
    market 占位 0（基金无沪深市场之分，客户端不使用该字段）。
    """
    payload = text.lstrip("\ufeff")
    match = re.search(r"=\s*(\[.*\])\s*;?\s*$", payload, re.S)
    if not match:
        raise FundError("基金清单响应缺少数组（可能被拦截）")
    try:
        rows = json.loads(match.group(1))
    except ValueError as exc:
        raise FundError(f"基金清单 JSON 解析失败: {exc}") from exc
    funds = []
    for row in rows or []:
        try:
            code, _, name, fund_type = row[0], row[1], row[2], row[3]
        except (IndexError, TypeError) as exc:
            raise FundError(f"基金清单行字段异常: {row!r}") from exc
        funds.append({"code": str(code), "market": 0, "name": str(name),
                      "fund_type": str(fund_type)})
    return funds


def parse_pingzhongdata(text: str, code: str) -> DailyBars:
    """pingzhongdata/{code}.js → DailyBars，行 (nav_date, unit_nav,
    accum_nav, pct_chg, unit_money)。

    累计净值按时间戳对齐（两序列等长但容错错位），缺失置 None；
    unitMoney 保留原始字符串（分红信息，复权计算后续处理），空串置 None。
    """
    net_match = _NET_WORTH_RE.search(text)
    if not net_match:
        raise FundError(f"pingzhongdata 缺少 Data_netWorthTrend({code})")
    try:
        net = json.loads(net_match.group(1))
    except ValueError as exc:
        raise FundError(f"净值走势 JSON 解析失败({code}): {exc}") from exc

    accum_by_x: dict = {}
    acc_match = _ACC_WORTH_RE.search(text)
    if acc_match:
        try:
            for x, y in json.loads(acc_match.group(1)) or []:
                accum_by_x[x] = float(y)
        except (ValueError, TypeError):
            accum_by_x = {}                  # 累计净值坏段不拖垮单位净值

    name_match = _NAME_RE.search(text)
    rows = []
    for point in net or []:
        unit_money = point.get("unitMoney") or None
        rows.append((
            _ms_to_date(point["x"]), float(point["y"]),
            accum_by_x.get(point["x"]),
            _num_or_none(point.get("equityReturn")),
            unit_money if isinstance(unit_money, str) else None,
        ))
    return DailyBars(code=code, name=name_match.group(1) if name_match else "",
                     dktotal=len(rows), rows=rows, source="fund_em")


class FundClient:
    """与 SinaClient/DelistedClient 同构：fetch_stock_list / fetch_daily_bars。"""

    def __init__(self, *, rate: RateLimiter, http: httpx.AsyncClient,
                 fetch_timeout: float = 30.0):
        self._rate = rate
        self._http = http
        self._fetch_timeout = fetch_timeout

    async def fetch_stock_list(self) -> list[dict]:
        key = self._rate.channel_key(FUND_LIST_URL)
        await self._rate.acquire(key)
        try:
            resp = await self._http.get(
                FUND_LIST_URL,
                headers={"User-Agent": UA,
                         "Referer": "https://fund.eastmoney.com/"},
                timeout=self._fetch_timeout)
            resp.raise_for_status()
            funds = parse_fund_list(resp.text)
        except FundError:
            self._rate.report(key, False)
            raise
        except Exception as exc:
            self._rate.report(key, False)
            raise FundError(f"基金清单获取失败: {exc}") from exc
        self._rate.report(key, True)
        return funds

    async def fetch_daily_bars(self, market: int, code: str, *,
                               beg: str = "0", end: str = "20500101") -> DailyBars:
        """单只基金全史净值。beg 忽略：接口单请求即全史，upsert 幂等去重
        （同退市股通道的约定）。"""
        key = self._rate.channel_key(PINGZHONG_URL)
        await self._rate.acquire(key)
        try:
            resp = await self._http.get(
                PINGZHONG_URL.format(code=code),
                headers={"User-Agent": UA,
                         "Referer": "https://fund.eastmoney.com/"},
                timeout=self._fetch_timeout)
            resp.raise_for_status()
            bars = parse_pingzhongdata(resp.text, code)
        except FundError:
            self._rate.report(key, False)
            raise
        except Exception as exc:
            self._rate.report(key, False)
            raise FundError(f"基金净值获取失败({code}): {exc}") from exc
        self._rate.report(key, True)
        return bars
