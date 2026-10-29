"""定投止盈回测结果查询 API（/api/funds/dca）。

数据来自 fund_dca_result 快照（scripts 离线回测导入，见 dca_store），
只读 + _safe 降级约定同 browse：PG 不可达/表不存在是"状态"（down/空）
而不是错误，接口始终 200。
"""

import psycopg

from . import config

_DCA_LIST_SQL = """
SELECT code, name, fund_type, alive, scale_yi, scale_date,
       w10_start, w10_end, w10_months, w10_rounds,
       w10_invested, w10_final_value, w10_profit_rate, w10_xirr,
       full_xirr, full_rounds, full_profit_rate, full_months
FROM fund_dca_result
WHERE (%s = '' OR code ILIKE %s OR name ILIKE %s)
  AND {alive_clause}
ORDER BY {sort_col} {order_dir} NULLS LAST, code
LIMIT %s OFFSET %s
"""

_DCA_COUNT_SQL = """
SELECT count(*) FROM fund_dca_result
WHERE (%s = '' OR code ILIKE %s OR name ILIKE %s)
  AND {alive_clause}
"""

# 统计卡口径固定为「在市全集」（不受当前搜索词影响，描述宇宙而非当页）
_DCA_STATS_SQL = """
SELECT count(*),
       percentile_cont(0.25) WITHIN GROUP (ORDER BY w10_xirr),
       percentile_cont(0.5)  WITHIN GROUP (ORDER BY w10_xirr),
       percentile_cont(0.75) WITHIN GROUP (ORDER BY w10_xirr),
       percentile_cont(0.9)  WITHIN GROUP (ORDER BY w10_xirr),
       avg((w10_xirr > 0)::int)::float,
       avg((w10_rounds > 0)::int)::float
FROM fund_dca_result
WHERE alive AND w10_xirr IS NOT NULL
"""

_DCA_META_SQL = """
SELECT generated_at, fund_count, params FROM fund_dca_meta
"""

# 排序白名单：前端传列名，只允许这些键进 ORDER BY（防注入）
_SORTABLE = {
    "w10_xirr", "full_xirr", "w10_profit_rate", "full_profit_rate",
    "w10_rounds", "full_rounds", "scale_yi", "code",
}


def _safe(fn, default):
    try:
        return fn()
    except psycopg.errors.UndefinedTable:
        return default
    except Exception as exc:
        d = dict(default)
        d.update({"status": "down",
                  "detail": f"{type(exc).__name__}: {str(exc)[:120]}"})
        return d


def fetch_dca_list(dsn: str, *, q: str = "", alive: str = "true",
                   sort: str = "w10_xirr", order: str = "desc",
                   limit: int = 50, offset: int = 0) -> dict:
    """回测排行：分页 + 搜索 + 在市过滤 + 服务端排序 + 宇宙统计与口径 meta。

    alive="true"（默认）仅近 45 天有净值的在市基金；"false" 仅清盘/退市；
    其余值（含空串）全部。sort 非白名单回落 w10_xirr。
    """
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    like = f"%{q}%"
    sort_col = sort if sort in _SORTABLE else "w10_xirr"
    order_dir = "ASC" if order.lower() == "asc" else "DESC"
    alive_clause = ("true" if alive not in ("true", "false")
                    else f"alive = {alive.upper()}")
    sql = (_DCA_LIST_SQL.replace("{alive_clause}", alive_clause)
           .replace("{sort_col}", sort_col).replace("{order_dir}", order_dir))
    count_sql = _DCA_COUNT_SQL.replace("{alive_clause}", alive_clause)

    def run():
        with psycopg.connect(dsn, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, (q, like, like, limit, offset))
                cols = ["code", "name", "fund_type", "alive", "scale_yi",
                        "scale_date", "w10_start", "w10_end", "w10_months",
                        "w10_rounds", "w10_invested", "w10_final_value",
                        "w10_profit_rate", "w10_xirr", "full_xirr",
                        "full_rounds", "full_profit_rate", "full_months"]
                rows = [dict(zip(cols, r)) for r in cur.fetchall()]
                cur.execute(count_sql, (q, like, like))
                total = cur.fetchone()[0]
                cur.execute(_DCA_STATS_SQL)
                s = cur.fetchone()
                cur.execute(_DCA_META_SQL)
                m = cur.fetchone()
        for r in rows:
            for k in ("w10_xirr", "full_xirr", "w10_profit_rate",
                      "full_profit_rate"):
                r[k] = round(r[k], 4) if r[k] is not None else None
            r["scale_yi"] = round(r["scale_yi"], 2) if r["scale_yi"] is not None else None
            for k in ("scale_date", "w10_start", "w10_end"):
                if r[k]:
                    r[k] = r[k].isoformat()
        stats = None
        if s and s[0]:
            stats = {"count": s[0], "p25": s[1], "median": s[2],
                     "p75": s[3], "p90": s[4],
                     "positive_share": s[5], "take_profit_share": s[6]}
        meta = None
        if m:
            meta = {"generated_at": m[0].isoformat() if m[0] else None,
                    "fund_count": m[1], "params": m[2]}
        return {"status": "up", "total": total, "limit": limit,
                "offset": offset, "rows": rows, "stats": stats, "meta": meta}
    return _safe(run, {"status": "down", "detail": "pg unreachable",
                       "total": 0, "limit": limit, "offset": offset,
                       "rows": [], "stats": None, "meta": None})


def register_fund_dca_routes(app):
    @app.get("/api/funds/dca")
    def funds_dca(q: str = "", alive: str = "true", sort: str = "w10_xirr",
                  order: str = "desc", limit: int = 50, offset: int = 0):
        """定投止盈回测排行（快照数据，重跑回测导入后刷新）。

        降级约定同 /api/funds：始终 200；alive="true" 默认仅在市；
        sort 白名单排序；stats 为在市全集分位（不受搜索影响）。
        """
        return fetch_dca_list(config.DATABASE_URL, q=q,
                              alive=alive.strip().lower(), sort=sort,
                              order=order.strip().lower(), limit=limit,
                              offset=offset)
