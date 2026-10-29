"""回补 fund_nav 历史缺口：净值行数过少的在市基金重新走 pingzhongdata 全史。

背景：全量爬取（2026-10-26）有少量基金失败/遗漏，但每日增量快照会给它们
落 1 行当日净值——表现为「只有快照单行、无历史」的假数据，回测前必须回补。
复用项目 parse_pingzhongdata + 保全型 upsert（source=fund_em，幂等）。

用法：.venv/bin/python scripts/fund_nav_backfill.py [--min-rows 5]
"""

import argparse
import asyncio
import sys
import time
from pathlib import Path

import httpx
import psycopg

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from app.crawler.fund import parse_pingzhongdata  # noqa: E402

DB_URL = "postgresql://honey_money:honey_money@host.docker.internal:5433/honey_money"
URL = "https://fund.eastmoney.com/pingzhongdata/{}.js"
HEADERS = {
    "Referer": "https://fund.eastmoney.com/",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
}
UPSERT_NAV = """
INSERT INTO fund_nav
  (code, nav_date, unit_nav, accum_nav, pct_chg, unit_money, source)
VALUES (%s, %s, %s, %s, %s, %s, 'fund_em')
ON CONFLICT (code, nav_date, source) DO UPDATE SET
  unit_nav = COALESCE(EXCLUDED.unit_nav, fund_nav.unit_nav),
  accum_nav = COALESCE(EXCLUDED.accum_nav, fund_nav.accum_nav),
  pct_chg = COALESCE(EXCLUDED.pct_chg, fund_nav.pct_chg),
  unit_money = COALESCE(EXCLUDED.unit_money, fund_nav.unit_money)
"""


async def fetch_one(client, sem, code, conn, progress):
    async with sem:
        rows = None
        for attempt in range(3):
            try:
                r = await client.get(URL.format(code))
                if r.status_code == 200:
                    bars = parse_pingzhongdata(r.text, code)
                    rows = bars.rows
                    break
                progress[f"http_{r.status_code}"] = progress.get(f"http_{r.status_code}", 0) + 1
                await asyncio.sleep(1 + attempt)
            except Exception as e:  # 解析失败(FundError)与网络错误都记数
                progress[type(e).__name__] = progress.get(type(e).__name__, 0) + 1
                await asyncio.sleep(1 + attempt)
        if rows:
            async with conn.lock:
                conn.cur.executemany(UPSERT_NAV, [(code, *row) for row in rows])
            progress["rows"] = progress.get("rows", 0) + len(rows)
            progress["fixed"] = progress.get("fixed", 0) + 1
        else:
            progress["nodata"] = progress.get("nodata", 0) + 1
        progress["done"] += 1
        if progress["done"] % 500 == 0:
            print(f"  {progress['done']}/{progress['total']} {progress}", flush=True)
        await asyncio.sleep(0.02)


class ConnWrap:
    """asyncio 下共享一个同步 psycopg 连接：executemany 在事件循环内串行执行。"""
    def __init__(self, conn):
        self.conn = conn
        self.lock = asyncio.Lock()
        self.cur = conn.cursor()


async def main(min_rows: int, concurrency: int = 8):
    with psycopg.connect(DB_URL, connect_timeout=10) as conn:
        codes = [r[0] for r in conn.execute(
            "SELECT n.code FROM (SELECT code, count(*) n FROM fund_nav GROUP BY code) n "
            "JOIN fund_basic b ON b.code = n.code "
            f"WHERE n.n < {min_rows} AND NOT b.delisted ORDER BY n.code").fetchall()]
    print(f"待回补 {len(codes)} 只（净值行数 < {min_rows} 的在市基金）", flush=True)
    progress = {"done": 0, "total": len(codes)}
    connwrap = ConnWrap(psycopg.connect(DB_URL, connect_timeout=10))
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(headers=HEADERS, timeout=20, follow_redirects=True,
                                 limits=httpx.Limits(max_connections=concurrency + 4)) as client:
        await asyncio.gather(*(fetch_one(client, sem, c, connwrap, progress) for c in codes))
    connwrap.conn.commit()
    print(f"完成：{progress}", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--min-rows", type=int, default=5)
    args = ap.parse_args()
    t0 = time.time()
    asyncio.run(main(args.min_rows))
    print(f"耗时 {time.time()-t0:.0f}s")
