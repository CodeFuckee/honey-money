"""补齐 fund_basic 中从未抓过的基金（主要是场内 ETF/LOF）：一次 pingzhongdata
请求同时取「规模（Data_fluctuationScale）+ 全史净值」，分别并入 scale CSV 与
fund_nav 表。

背景：此前所有通道都只覆盖「有净值历史」的基金——ETF 的净值不走
Fund_JJJZ_Data 快照渠道，全量爬取期被整体遗漏（fund_nav 0 行且被误标
delisted）。本脚本按 fund_basic 全集补差。

用法：.venv/bin/python scripts/fund_full_sync_missing.py
"""

import asyncio
import csv
import sys
import time
from pathlib import Path

import httpx
import psycopg

sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
from app.crawler.fund import parse_pingzhongdata  # noqa: E402
from fund_scale_fetch_pz import parse_scale  # noqa: E402

DB_URL = "postgresql://honey_money:honey_money@host.docker.internal:5433/honey_money"
OUT = Path(__file__).parent / "data" / "fund_scale.csv"
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


async def sync_one(client, sem, code, cur, result, progress):
    async with sem:
        scale = nav_rows = None
        for attempt in range(3):
            try:
                r = await client.get(URL.format(code))
                if r.status_code != 200:
                    progress[f"http_{r.status_code}"] = progress.get(f"http_{r.status_code}", 0) + 1
                    await asyncio.sleep(1 + attempt)
                    continue
                m = parse_scale(r.text)
                scale = m if m[0] is not None else None
                try:
                    nav_rows = parse_pingzhongdata(r.text, code).rows
                except Exception:
                    nav_rows = None
                break
            except Exception as e:
                progress[type(e).__name__] = progress.get(type(e).__name__, 0) + 1
                await asyncio.sleep(1 + attempt)
        if scale:
            result[code] = scale
        if nav_rows:
            cur.executemany(UPSERT_NAV, [(code, *row) for row in nav_rows])
            progress["nav_rows"] = progress.get("nav_rows", 0) + len(nav_rows)
            progress["nav_ok"] = progress.get("nav_ok", 0) + 1
        else:
            progress["nav_nodata"] = progress.get("nav_nodata", 0) + 1
        progress["done"] += 1
        if progress["done"] % 1000 == 0:
            print(f"  {progress['done']}/{progress['total']} {progress}", flush=True)
        await asyncio.sleep(0.02)


async def main(concurrency: int = 8):
    with psycopg.connect(DB_URL, connect_timeout=10) as conn:
        all_codes = [r[0] for r in conn.execute("SELECT code FROM fund_basic").fetchall()]
    have = set()
    if OUT.exists():
        with OUT.open(newline="") as f:
            have = {row["code"] for row in csv.DictReader(f)}
    missing = [c for c in all_codes if c not in have]
    print(f"fund_basic {len(all_codes)} 只，已有规模 {len(have)}，本次同步 {len(missing)} 只", flush=True)

    result: dict[str, tuple] = {}
    progress = {"done": 0, "total": len(missing)}
    conn2 = psycopg.connect(DB_URL, connect_timeout=10)
    cur = conn2.cursor()
    sem = asyncio.Semaphore(concurrency)
    async with httpx.AsyncClient(headers=HEADERS, timeout=20, follow_redirects=True,
                                 limits=httpx.Limits(max_connections=concurrency + 4)) as client:
        await asyncio.gather(*(sync_one(client, sem, c, cur, result, progress) for c in missing))
    conn2.commit()

    # 合并写回 scale CSV
    merged: dict[str, tuple] = {}
    if OUT.exists():
        with OUT.open(newline="") as f:
            for row in csv.DictReader(f):
                if row["scale_yi"]:
                    merged[row["code"]] = (float(row["scale_yi"]), row["scale_date"])
    merged.update(result)
    with OUT.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["code", "scale_yi", "scale_date", "quarters"])
        for code in sorted(merged):
            scale, date = merged[code]
            w.writerow([code, scale, date, ""])
    print(f"完成：新增规模 {len(result)} 只 / 净值入库 {progress.get('nav_ok', 0)} 只 "
          f"{progress.get('nav_rows', 0)} 行；scale CSV 共 {len(merged)} 只", flush=True)


if __name__ == "__main__":
    t0 = time.time()
    asyncio.run(main())
    print(f"耗时 {time.time()-t0:.0f}s")
