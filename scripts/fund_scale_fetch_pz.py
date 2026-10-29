"""补抓基金规模——pingzhongdata 通道（fundf10 gmbd 被限速时的备用源）。

Data_fluctuationScale = {categories:[季度], series:[{y:资产规模亿元, mom}, ...]}，
取最新一个非空 y。与 gmbd 的「期末净资产」同源同值（已抽样比对一致）。
单响应 ~300KB，8 并发 ~25 只/秒；合并写回 scripts/data/fund_scale.csv。

用法：.venv/bin/python scripts/fund_scale_fetch_pz.py
"""

import asyncio
import csv
import json
import re
import time
from pathlib import Path

import httpx
import psycopg

DB_URL = "postgresql://honey_money:honey_money@host.docker.internal:5433/honey_money"
OUT = Path(__file__).parent / "data" / "fund_scale.csv"
URL = "https://fund.eastmoney.com/pingzhongdata/{}.js"
HEADERS = {
    "Referer": "https://fund.eastmoney.com/",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
}
SCALE_RE = re.compile(r"Data_fluctuationScale\s*=\s*(\{.*?\});", re.S)


def parse_scale(text: str) -> tuple[float | None, str | None]:
    m = SCALE_RE.search(text)
    if not m:
        return None, None
    d = json.loads(m.group(1))
    cats, series = d["categories"], d["series"]
    for i in range(len(series) - 1, -1, -1):
        if series[i].get("y") is not None:
            return float(series[i]["y"]), cats[i]
    return None, None


async def fetch_one(client: httpx.AsyncClient, sem: asyncio.Semaphore, code: str,
                    result: dict, progress: dict):
    async with sem:
        for attempt in range(3):
            try:
                r = await client.get(URL.format(code))
                if r.status_code == 200:
                    result[code] = parse_scale(r.text)
                    break
                progress[f"http_{r.status_code}"] = progress.get(f"http_{r.status_code}", 0) + 1
                await asyncio.sleep(1 + attempt)
            except httpx.HTTPError as e:
                progress[f"exc_{type(e).__name__}"] = progress.get(f"exc_{type(e).__name__}", 0) + 1
                await asyncio.sleep(1 + attempt)
        else:
            result[code] = (None, None)
            progress["fail"] = progress.get("fail", 0) + 1
        progress["done"] += 1
        if progress["done"] % 2000 == 0:
            remain = (progress["total"] - progress["done"]) * (time.time() - progress["t0"]) / progress["done"]
            errs = {k: v for k, v in progress.items() if k.startswith(("http_", "exc_", "fail"))}
            print(f"  {progress['done']}/{progress['total']} (剩余~{remain/60:.0f}min, {errs})", flush=True)
        await asyncio.sleep(0.02)


async def main(codes: list[str], concurrency: int = 8):
    preloaded: dict[str, tuple] = {}
    if OUT.exists():
        with OUT.open(newline="") as f:
            for row in csv.DictReader(f):
                if row["scale_yi"]:
                    preloaded[row["code"]] = (float(row["scale_yi"]), row["scale_date"])
    codes = [c for c in codes if c not in preloaded]
    print(f"已有规模 {len(preloaded)} 只，pingzhongdata 补抓 {len(codes)} 只", flush=True)
    sem = asyncio.Semaphore(concurrency)
    result = dict(preloaded)
    progress = {"done": 0, "total": len(codes), "t0": time.time()}
    async with httpx.AsyncClient(headers=HEADERS, timeout=20, follow_redirects=True,
                                 limits=httpx.Limits(max_connections=concurrency + 4)) as client:
        await asyncio.gather(*(fetch_one(client, sem, c, result, progress) for c in codes))
    with OUT.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["code", "scale_yi", "scale_date", "quarters"])
        for code in sorted(result):
            scale, date = result[code]
            w.writerow([code, scale if scale is not None else "", date or "", ""])
    print(f"写入 {OUT}：{len(result)} 只，其中无规模 "
          f"{sum(1 for v in result.values() if v[0] is None)} 只", flush=True)


if __name__ == "__main__":
    with psycopg.connect(DB_URL, connect_timeout=10) as conn:
        codes = [r[0] for r in conn.execute(
            "SELECT DISTINCT code FROM fund_nav ORDER BY code").fetchall()]
    asyncio.run(main(codes))
