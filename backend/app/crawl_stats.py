"""爬虫数据统计查询：前端「爬虫数据」卡片的数据源。

返回三类信息：已入库 K 线总数、股票覆盖进度（清单/已覆盖/待爬）、最近一次
运行汇总。语义与 infra 探测一致——PG 不可达是"状态"（down）而不是错误；
爬虫从未运行（表不存在）同样是正常状态（initialized=False，全 0）。
"""

import psycopg

_LAST_RUN_SQL = """
SELECT mode, started_at, finished_at, total_stocks, ok_stocks,
       failed_stocks, bars_upserted
FROM crawl_run ORDER BY id DESC LIMIT 1
"""


def _zero() -> dict:
    return {"status": "up", "initialized": False, "daily_bars": 0,
            "stocks_listed": 0, "stocks_covered": 0, "stocks_pending": 0,
            "last_run": None}


def fetch_crawl_stats(dsn: str) -> dict:
    try:
        with psycopg.connect(dsn, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                try:
                    cur.execute("SELECT count(*) FROM daily_bar")
                    daily_bars = cur.fetchone()[0]
                    cur.execute("SELECT count(*) FROM stock_basic")
                    stocks_listed = cur.fetchone()[0]
                    cur.execute("SELECT count(DISTINCT code) FROM daily_bar")
                    stocks_covered = cur.fetchone()[0]
                    cur.execute(_LAST_RUN_SQL)
                    row = cur.fetchone()
                except psycopg.errors.UndefinedTable:
                    return _zero()   # 爬虫未初始化：没有表 ≠ 服务故障
    except Exception as exc:
        return {"status": "down", "detail": f"{type(exc).__name__}: {str(exc)[:120]}"}

    last_run = None
    if row:
        last_run = {
            "mode": row[0], "started_at": row[1].isoformat() if row[1] else None,
            "finished_at": row[2].isoformat() if row[2] else None,
            "total_stocks": row[3], "ok_stocks": row[4],
            "failed_stocks": row[5], "bars_upserted": row[6],
        }
    return {"status": "up", "initialized": True, "daily_bars": daily_bars,
            "stocks_listed": stocks_listed, "stocks_covered": stocks_covered,
            "stocks_pending": max(0, stocks_listed - stocks_covered),
            "last_run": last_run}
