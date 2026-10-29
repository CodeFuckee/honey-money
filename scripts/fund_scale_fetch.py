"""抓取全市场基金最新资产规模（东财 F10 规模变动接口，季度「期末净资产」）。

数据源：fundf10.eastmoney.com/FundArchivesDatas.aspx?type=gmbd&code={code}
响应为 JS 变量内嵌 HTML 表格，行按日期倒序，首行即最新季度；
期末净资产单位为亿元。输出 scripts/data/fund_scale.csv（code,scale_yi,scale_date,rows）。

用法：
    .venv/bin/python scripts/fund_scale_fetch.py [--concurrency 8]
数据库取自 DATABASE_URL（默认 NAS 生产库）。
"""

import argparse
import asyncio
import csv
import os
import re
import time
from pathlib import Path

import httpx
import psycopg

DB_URL = os.environ.get(
    "DATABASE_URL", "postgresql://honey_money:honey_money@host.docker.internal:5433/honey_money"
)
OUT = Path(__file__).parent / "data" / "fund_scale.csv"
URL = "https://fundf10.eastmoney.com/FundArchivesDatas.aspx?type=gmbd&code={code}"
HEADERS = {
    "Referer": "https://fundf10.eastmoney.com/",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
}
ROW_RE = re.compile(r"<tr><td>(\d{4}-\d{2}-\d{2})</td>((?:<td[^>]*>[^<]*</td>)+)</tr>")
CELL_RE = re.compile(r"<td[^>]*>([^<]*)</td>")


def parse_gmbd(text: str) -> tuple[float | None, str | None, int]:
    """返回 (最新期末净资产亿元, 对应季度日期, 有效行数)；无数据返回 (None, None, 0)。"""
    rows = []
    for m in ROW_RE.finditer(text):
        cells = [c.strip() for c in CELL_RE.findall(m.group(2))]
        # cells: 期间申购/期间赎回/期末总份额/期末净资产(亿元)/净资产变动率
        if len(cells) < 5:
            continue
        scale = cells[3].replace(",", "")
        try:
            rows.append((m.group(1), float(scale)))
        except ValueError:
            continue  # 单行缺数（如 '---'）→ 落到更早的完整季度
    if not rows:
        return None, None, 0
    date, scale = rows[0]  # 首行即最新季度（日期倒序）
    return scale, date, len(rows)


class Pacer:
    """全局匀速限流：fundf10 对突发并发回 HTTP 514（自定义限速码），匀速更稳。"""

    def __init__(self, rps: float):
        self.interval = 1.0 / rps
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self):
        async with self._lock:
            now = time.monotonic()
            slot = max(now, self._next)
            self._next = slot + self.interval
        await asyncio.sleep(slot - now)


async def fetch_one(client: httpx.AsyncClient, pacer: Pacer, code: str,
                    result: dict, progress: dict):
    for attempt in range(6):
        await pacer.wait()
        try:
            r = await client.get(URL.format(code=code))
            if r.status_code == 200:
                result[code] = parse_gmbd(r.text)
                break
            progress[f"http_{r.status_code}"] = progress.get(f"http_{r.status_code}", 0) + 1
            await asyncio.sleep(1.5 * (attempt + 1))  # 限速码也要让路，指数退避
        except httpx.HTTPError as e:
            progress[f"exc_{type(e).__name__}"] = progress.get(f"exc_{type(e).__name__}", 0) + 1
            await asyncio.sleep(1 + attempt)
    else:
        result[code] = (None, None, 0)
        progress["fail"] = progress.get("fail", 0) + 1
    progress["done"] += 1
    if progress["done"] % 2000 == 0:
        elapsed = time.time() - progress["t0"]
        remain = (progress["total"] - progress["done"]) / max(progress["done"] / elapsed, 1)
        errs = {k: v for k, v in progress.items()
                if k.startswith(("http_", "exc_", "fail"))}
        print(f"  {progress['done']}/{progress['total']} "
              f"(剩余~{remain/60:.0f}min, {errs})", flush=True)


async def main(codes: list[str], rps: float, merge_from: Path | None = None):
    """merge_from：已有结果 CSV，其中已有规模的代码跳过、结果合并写回。"""
    preloaded: dict[str, tuple] = {}
    if merge_from and merge_from.exists():
        with merge_from.open(newline="") as f:
            for row in csv.DictReader(f):
                if row["scale_yi"]:
                    preloaded[row["code"]] = (float(row["scale_yi"]),
                                              row["scale_date"], int(row["quarters"]))
        codes = [c for c in codes if c not in preloaded]
        print(f"已有规模 {len(preloaded)} 只，本次补抓 {len(codes)} 只")
    sem_rps = Pacer(rps)
    result: dict[str, tuple] = dict(preloaded)
    progress = {"done": 0, "total": len(codes), "t0": time.time()}
    async with httpx.AsyncClient(headers=HEADERS, timeout=15,
                                 limits=httpx.Limits(max_connections=64)) as client:
        await asyncio.gather(*(fetch_one(client, sem_rps, c, result, progress)
                               for c in codes))
    OUT.parent.mkdir(parents=True, exist_ok=True)
    all_codes = sorted(result)
    with OUT.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["code", "scale_yi", "scale_date", "quarters"])
        for code in all_codes:
            scale, date, n = result.get(code, (None, None, 0))
            w.writerow([code, scale or "", date or "", n])
    print(f"写入 {OUT}：{len(all_codes)} 只，其中无规模数据 "
          f"{sum(1 for v in result.values() if v[0] is None)} 只")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--rps", type=float, default=15.0, help="全局匀速限流（请求/秒）")
    ap.add_argument("--missing-only", action="store_true",
                    help="跳过输出 CSV 中已有规模的代码，结果合并写回")
    args = ap.parse_args()
    with psycopg.connect(DB_URL, connect_timeout=10) as conn:
        codes = [r[0] for r in conn.execute(
            "SELECT DISTINCT code FROM fund_nav ORDER BY code").fetchall()]
    print(f"待抓取 {len(codes)} 只（有净值历史）")
    asyncio.run(main(codes, args.rps,
                     merge_from=OUT if args.missing_only else None))
