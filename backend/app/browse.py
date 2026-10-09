"""数据浏览查询：股票列表（含行情汇总）与单股日K。

只读查询，语义与 crawl_stats 一致——PG 不可达/表不存在是"状态"（down/空）
而不是错误，接口始终 200。
"""

import psycopg

# 两段式查询：先在 stock_basic 上分页（小表，毫秒级），再只对当页 ≤200 只
# 聚合 daily_bar（大表 1,600 万行——原写法 LATERAL 作用于全表分页前的每一行，
# 深分页时退化成对大表数千次聚合，实测 8~10s）。
# 有来源过滤时只列「该来源下有K线」的股票（用 EXISTS 而非聚合，可走 (code,trade_date,source) 主键）
_SRC_FILTER = "EXISTS (SELECT 1 FROM daily_bar d WHERE d.code = stock_basic.code AND d.source = {src!r})"

_LIST_IDS_SQL = """
SELECT code, name FROM stock_basic
WHERE (%s = '' OR code ILIKE %s OR name ILIKE %s)
  AND {src_clause}
ORDER BY code
LIMIT %s OFFSET %s
"""

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
WHERE b.code = ANY(%s)
ORDER BY b.code
"""

_COUNT_SQL = """
SELECT count(*) FROM stock_basic
WHERE (%s = '' OR code ILIKE %s OR name ILIKE %s)
  AND {src_clause}
"""

_BARS_SQL = """
SELECT trade_date, open, close, high, low, volume, amount,
       amplitude, pct_chg, change, turnover, source, outstanding_share
FROM daily_bar WHERE code = %s {src_clause}
ORDER BY trade_date DESC LIMIT %s OFFSET %s
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


def fetch_stock_list(dsn: str, *, q: str = "", limit: int = 50, offset: int = 0,
                     source: str = "") -> dict:
    """股票列表 + 每股行情摘要（最新价/涨跌幅/K线数/区间/来源），code/name 模糊搜索。

    source 非空时仅返回该来源下有K线的股票（数据源 Tab 过滤）。
    """
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    like = f"%{q}%"
    sql_ids = (_LIST_IDS_SQL.replace("{src_clause}", _SRC_FILTER.format(src=source))
               if source else _LIST_IDS_SQL.replace("{src_clause}", "true"))
    count_sql = (_COUNT_SQL.replace("{src_clause}", _SRC_FILTER.format(src=source))
                 if source else _COUNT_SQL.replace("{src_clause}", "true"))

    def run():
        with psycopg.connect(dsn, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute(sql_ids, (q, like, like, limit, offset))
                page_rows = cur.fetchall()
                codes = [r[0] for r in page_rows]
                stocks = []
                if codes:
                    cur.execute(_LIST_SQL, (codes,))
                    cols = ["code", "name", "close", "pct_chg", "last_date",
                            "bars", "first_date", "sources"]
                    by_code = {r[0]: dict(zip(cols, r)) for r in cur.fetchall()}
                    stocks = [by_code[c] for c in codes]   # 保持 code 序
                cur.execute(count_sql, (q, like, like))
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


def fetch_stock_bars(dsn: str, code: str, *, limit: int = 250, offset: int = 0,
                     source: str = "") -> dict:
    """单股日K（默认最新 250 根，倒序进正序出）。

    source 非空时仅取该来源（个股详情页用——(code,trade_date,source) 主键下
    同一交易日两源各一行，不过滤会混返）；缺省不过滤，兼容旧行为。
    """
    limit = max(1, min(limit, 5000))
    offset = max(0, offset)
    source = source.strip().lower()
    sql = (_BARS_SQL.replace("{src_clause}", "AND source = %s")
           if source else _BARS_SQL.replace("{src_clause}", "true"))
    params = ((code, source, limit, offset) if source
              else (code, limit, offset))

    def run():
        with psycopg.connect(dsn, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = [
                    {"date": r[0].isoformat(), "open": r[1], "close": r[2],
                     "high": r[3], "low": r[4], "volume": r[5], "amount": r[6],
                     "amplitude": r[7], "pct_chg": r[8], "change": r[9],
                     "turnover": r[10], "source": r[11],
                     "outstanding_share": r[12]}
                    for r in cur.fetchall()
                ]
        rows.reverse()
        return {"status": "up", "code": code, "count": len(rows), "bars": rows}
    return _safe(run, {"status": "down", "detail": "pg unreachable",
                       "code": code, "count": 0, "bars": []})
