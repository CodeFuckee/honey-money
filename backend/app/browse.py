"""数据浏览查询：股票列表（含行情汇总）与单股日K。

只读查询，语义与 crawl_stats 一致——PG 不可达/表不存在是"状态"（down/空）
而不是错误，接口始终 200。
"""

import psycopg

_LIST_SQL = """
SELECT b.code, b.name,
       p.close, p.pct_chg, p.trade_date,
       COALESCE(s.bars, 0) AS bars,
       COALESCE(s.first_date, p.trade_date) AS first_date,
       COALESCE(s.sources, '') AS sources
FROM stock_basic b
LEFT JOIN LATERAL (
    SELECT close, pct_chg, trade_date
    FROM daily_bar d
    WHERE d.code = b.code
    ORDER BY trade_date DESC LIMIT 1
) p ON true
LEFT JOIN LATERAL (
    SELECT count(*) AS bars,
           min(trade_date) AS first_date,
           string_agg(DISTINCT d2.source, ',' ORDER BY d2.source) AS sources
    FROM daily_bar d2
    WHERE d2.code = b.code
) s ON true
WHERE (%s = '' OR b.code ILIKE %s OR b.name ILIKE %s)
ORDER BY b.code
LIMIT %s OFFSET %s
"""

_COUNT_SQL = """
SELECT count(*) FROM stock_basic
WHERE (%s = '' OR code ILIKE %s OR name ILIKE %s)
"""

_BARS_SQL = """
SELECT trade_date, open, close, high, low, volume, amount,
       amplitude, pct_chg, change, turnover, source
FROM daily_bar WHERE code = %s ORDER BY trade_date DESC LIMIT %s OFFSET %s
"""


def _safe(fn, default):
    """表不存在 → 空默认；其他异常（PG 不可达等）→ down + 原因，保持默认形状。"""
    try:
        return fn()
    except psycopg.errors.UndefinedTable:
        return default
    except Exception as exc:
        d = dict(default)
        d.update({"status": "down",
                  "detail": f"{type(exc).__name__}: {str(exc)[:120]}"})
        return d


def fetch_stock_list(dsn: str, *, q: str = "", limit: int = 50, offset: int = 0) -> dict:
    """股票列表 + 每股行情摘要（最新价/涨跌幅/K线数/区间/来源），code/name 模糊搜索。"""
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    like = f"%{q}%"

    def run():
        with psycopg.connect(dsn, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute(_LIST_SQL, (q, like, like, limit, offset))
                cols = ["code", "name", "close", "pct_chg", "last_date",
                        "bars", "first_date", "sources"]
                stocks = [dict(zip(cols, row)) for row in cur.fetchall()]
                cur.execute(_COUNT_SQL, (q, like, like))
                total = cur.fetchone()[0]
        for s in stocks:
            for k in ("close", "pct_chg"):
                s[k] = round(s[k], 2) if s[k] is not None else None
            if s["last_date"]:
                s["last_date"] = s["last_date"].isoformat()
            if s["first_date"]:
                s["first_date"] = s["first_date"].isoformat()
        return {"status": "up", "total": total, "limit": limit,
                "offset": offset, "stocks": stocks}
    return _safe(run, {"status": "down", "detail": "pg unreachable",
                       "total": 0, "limit": limit, "offset": offset, "stocks": []})


def fetch_stock_bars(dsn: str, code: str, *, limit: int = 250, offset: int = 0) -> dict:
    """单股日K（默认最新 250 根，倒序进正序出）。"""
    limit = max(1, min(limit, 5000))
    offset = max(0, offset)

    def run():
        with psycopg.connect(dsn, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute(_BARS_SQL, (code, limit, offset))
                rows = [
                    {"date": r[0].isoformat(), "open": r[1], "close": r[2],
                     "high": r[3], "low": r[4], "volume": r[5], "amount": r[6],
                     "amplitude": r[7], "pct_chg": r[8], "change": r[9],
                     "turnover": r[10], "source": r[11]}
                    for r in cur.fetchall()
                ]
        rows.reverse()
        return {"status": "up", "code": code, "count": len(rows), "bars": rows}
    return _safe(run, {"status": "down", "detail": "pg unreachable",
                       "code": code, "count": 0, "bars": []})
