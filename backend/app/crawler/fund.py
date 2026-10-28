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
import time
from zoneinfo import ZoneInfo

import httpx

from .eastmoney import DailyBars, UA
from .rate import RateLimiter

SH_TZ = ZoneInfo("Asia/Shanghai")

FUND_LIST_URL = "https://fund.eastmoney.com/js/fundcode_search.js"
PINGZHONG_URL = "https://fund.eastmoney.com/pingzhongdata/{code}.js"
_JJJZ_URL = "https://fund.eastmoney.com/Data/Fund_JJJZ_Data.aspx"

# 在市集合接口（Fund_JJJZ_Data 当前列表）：出现在列表=仍在披露（在市），
# 全集差集=清盘/终止/不再披露（退市）。**必须两列表取并集**——t=1 净值
# 列表只含发布单位净值的基金，传统摊余成本法货币基金（发布万份收益）
# 只在 t=7 货币列表（000009 误标教训 2026-10-27）；l 单页实测上限 100，
# 按 pages 翻页。
_JJJZ_LIVE_TYPES = (1, 7)
_JJJZ_PARAMS = {"l": 100, "st": "dq", "isdesc": 0}
_JJJZ_CODE_RE = re.compile(r'\[\s*"(\d{6})",')
_JJJZ_PAGES_RE = re.compile(r'pages:"(\d+)"')
_JJJZ_MAX_PAGES_PER_TYPE = 500              # 安全阀（24k 量级约 243 页）

# 快照（增量模式）：行前 9 字段 = 代码/名称/拼音/今日单位/今日累计/
# 昨日单位/昨日累计/今日增长值/今日增长率%；showday 与槽位日历对齐
_SNAP_ROW_RE = re.compile(
    r'\["(\d{6})","([^"]*)","[^"]*",'
    r'"([^"]*)","([^"]*)","([^"]*)","([^"]*)",'
    r'"([^"]*)","([^"]*)"')
_SNAP_SHOWDAY_RE = re.compile(r'showday:\["([^"]+)","([^"]+)"\]')
_SNAP_RECORD_RE = re.compile(r'record:"(\d+)"')

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


def parse_jjjz_codes(text: str) -> set:
    """Fund_JJJZ_Data 单页响应 → 代码集合。"""
    return set(_JJJZ_CODE_RE.findall(text))


def parse_jjjz_snapshot(text: str) -> tuple:
    """当前净值列表页 → (nav_rows, pages, record)。

    日期规则（槽位与日历对齐，实测未发布时段全市场今日槽为空、QDII 滞后
    基金双槽皆空）：今日槽有值 → showday[0]；否则昨日槽 → showday[1]；
    双空跳过——绝不落错误日期的行。日增长率字段只随今日槽。
    """
    m = _SNAP_SHOWDAY_RE.search(text)
    if not m:
        raise FundError("快照响应缺 showday（可能被拦截）")
    d_today, d_prev = m.group(1), m.group(2)
    rows = []
    for found in _SNAP_ROW_RE.findall(text):
        code, name, u_t, a_t, u_p, a_p, _g, pct_t = found
        try:
            if u_t:
                rows.append({"code": code, "name": name,
                             "unit_nav": float(u_t), "accum_nav": float(a_t),
                             "pct_chg": _num_or_none(pct_t),
                             "nav_date": d_today})
            elif u_p:
                rows.append({"code": code, "name": name,
                             "unit_nav": float(u_p), "accum_nav": float(a_p),
                             "pct_chg": None, "nav_date": d_prev})
        except (TypeError, ValueError):
            continue                        # 坏数值行跳过，不拖垮整页
    pages = _JJJZ_PAGES_RE.search(text)
    record = _SNAP_RECORD_RE.search(text)
    return rows, (int(pages.group(1)) if pages else 0), \
        (int(record.group(1)) if record else 0)


class FundClient:
    """与 SinaClient/DelistedClient 同构：fetch_stock_list / fetch_daily_bars。"""

    def __init__(self, *, rate: RateLimiter, http: httpx.AsyncClient,
                 fetch_timeout: float = 30.0):
        self._rate = rate
        self._http = http
        self._fetch_timeout = fetch_timeout

    async def fetch_live_codes(self) -> set | None:
        """在市基金代码集合（Fund_JJJZ_Data t=1 净值列表 ∪ t=7 货币列表，
        按 pages 翻页收齐）。

        任一页失败 → 返回 None（调用方跳过退市标记，保守不误杀）；
        该接口仅辅助清单标记，绝不因它阻塞爬取主流程。
        """
        key = self._rate.channel_key(_JJJZ_URL)
        codes: set = set()
        try:
            for t in _JJJZ_LIVE_TYPES:
                for page in range(1, _JJJZ_MAX_PAGES_PER_TYPE + 1):
                    await self._rate.acquire(key)
                    try:
                        resp = await self._http.get(
                            _JJJZ_URL,
                            params={**_JJJZ_PARAMS, "t": t, "page": page},
                            headers={"User-Agent": UA,
                                     "Referer": "https://fund.eastmoney.com/"},
                            timeout=self._fetch_timeout)
                        resp.raise_for_status()
                        new = parse_jjjz_codes(resp.text)
                    except Exception as exc:
                        self._rate.report(key, False)
                        raise FundError(f"在市清单获取失败(t={t}, "
                                        f"page={page}): {exc}") from exc
                    self._rate.report(key, True)
                    if page == 1:
                        m = _JJJZ_PAGES_RE.search(resp.text)
                        pages = int(m.group(1)) if m else 0
                    if not new:
                        break
                    codes |= new
                    if pages and page >= pages:
                        break
            return codes
        except FundError:
            return None

    async def iter_nav_snapshot(self):
        """当日净值快照（t=1 当前净值列表）分页迭代。

        yield {"rows": 解析后净值行, "page", "pages", "record"}；
        空页即止。FundError 最终失败由调用方处置。
        """
        key = self._rate.channel_key(_JJJZ_URL)
        page = 1
        pages = None
        while page <= _JJJZ_MAX_PAGES_PER_TYPE:
            await self._rate.acquire(key)
            try:
                resp = await self._http.get(
                    _JJJZ_URL,
                    params={**_JJJZ_PARAMS, "t": 1, "page": page},
                    headers={"User-Agent": UA,
                             "Referer": "https://fund.eastmoney.com/"},
                    timeout=self._fetch_timeout)
                resp.raise_for_status()
                rows, pages_p, record = parse_jjjz_snapshot(resp.text)
            except Exception as exc:
                self._rate.report(key, False)
                raise FundError(f"净值快照获取失败(page={page}): {exc}") \
                    from exc
            self._rate.report(key, True)
            if pages is None:
                pages = pages_p
            yield {"rows": rows, "page": page, "pages": pages,
                   "record": record}
            if not rows or (pages and page >= pages):
                return
            page += 1

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
        # 退市标记：在市集合（当前净值列表）与全集差集=清盘/退市。
        # 标记失败跳过（保持 FALSE），清单照常返回。
        try:
            live = await self.fetch_live_codes()
        except Exception:
            live = None
        if live:
            for f in funds:
                f["delisted"] = f["code"] not in live
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
            if resp.status_code in (301, 302, 404):
                # 源上无该基金数据文件（后端收费份额等从未披露净值的代码，
                # 抽样实锤：301 且 lsjz TotalCount=0）——0 行记成功跳过，
                # 不算失败（真实失败与"源上无数据"在汇总里保持可分）
                self._rate.report(key, True)
                return DailyBars(code=code, name="", dktotal=0, rows=[],
                                 source="fund_em")
            bars = parse_pingzhongdata(resp.text, code)
        except FundError:
            self._rate.report(key, False)
            raise
        except Exception as exc:
            self._rate.report(key, False)
            raise FundError(f"基金净值获取失败({code}): {exc}") from exc
        self._rate.report(key, True)
        return bars


async def crawl_nav_snapshot(client, store, conn, *, mode, source,
                             task_id=None, trigger="manual", progress=None,
                             on_batch=None) -> dict:
    """基金当日净值快照增量编排（fund 源 + incremental 模式专用执行体）。

    t=1 当前净值列表分页拉取 → 逐页批量 upsert（fund_basic 保全型：快照
    无类型信息传 None 由 COALESCE 保留库内值、快照码必在市 delisted=False；
    fund_nav 新增行，冲突时 COALESCE 保留库内非空值）→ 批级提交。
    清盘/滞后基金当日无行自动跳过（由全量任务补齐）；退市标记不在此刷新
    （快照不含货币列表，差集会误杀货基——全量任务负责）。
    返回与 CrawlSummary 字段同构的 dict。
    """
    run_id = store.start_run(conn, mode=mode, source=source,
                             task_id=task_id, trigger=trigger)
    started = time.perf_counter()
    ok = bars = done_rows = 0

    async def _run_pages():
        nonlocal ok, bars, done_rows
        async for batch in client.iter_nav_snapshot():
            rows = batch["rows"]
            if not rows:
                continue
            store.upsert_fund_basic(conn, [
                {"code": r["code"], "name": r["name"], "delisted": False}
                for r in rows])
            bars += store.upsert_fund_nav_rows(conn, rows, source="fund_em")
            ok += len(rows)
            done_rows += len(rows)
            if progress:
                record = batch.get("record") or done_rows
                progress(min(done_rows, record), record)
            if on_batch:
                on_batch(done_rows)

    try:
        await _run_pages()
    except Exception as exc:
        store.finish_run(conn, run_id, total_stocks=ok, ok_stocks=ok,
                         failed_stocks=0, bars_upserted=bars,
                         failures=[f"{type(exc).__name__}: {exc}"[:80]],
                         aborted=False)
        raise
    if progress:
        progress(done_rows, done_rows or 1)
    store.finish_run(conn, run_id, total_stocks=ok, ok_stocks=ok,
                     failed_stocks=0, bars_upserted=bars, failures=[],
                     aborted=False)
    return {"total": ok, "ok": ok, "failed": 0, "bars": bars,
            "failures": [], "aborted": False, "stopped": False,
            "elapsed_sec": round(time.perf_counter() - started, 1)}
