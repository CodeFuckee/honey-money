"""定投止盈回测结果查询 API（/api/funds/dca）。

数据来自 fund_dca_result 快照（scripts 离线回测导入，见 dca_store），
只读 + _safe 降级约定同 browse：PG 不可达/表不存在是"状态"（down/空）
而不是错误，接口始终 200。
"""

import datetime as dt

import psycopg

from . import config

_DCA_LIST_SQL = """
SELECT code, name, fund_type, alive, scale_yi, scale_date,
       w10_start, w10_end, w10_months, w10_rounds,
       w10_invested, w10_final_value, w10_profit_rate, w10_xirr,
       full_xirr, full_rounds, full_profit_rate, full_months,
       full_start, gap_avg_days
FROM fund_dca_result
WHERE (%s = '' OR code ILIKE %s OR name ILIKE %s)
  AND {alive_clause}
  AND {age_clause}
  AND {scale_clause}
ORDER BY {sort_col} {order_dir} NULLS LAST, code
LIMIT %s OFFSET %s
"""

_DCA_COUNT_SQL = """
SELECT count(*) FROM fund_dca_result
WHERE (%s = '' OR code ILIKE %s OR name ILIKE %s)
  AND {alive_clause}
  AND {age_clause}
  AND {scale_clause}
"""

# 统计卡口径跟随「在市 + 运作年限」筛选（不跟搜索词——描述筛选后的宇宙，
# 而非当页）；无年限筛选时与旧口径一致
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
  AND {age_clause}
  AND {scale_clause}
"""

_DCA_META_SQL = """
SELECT generated_at, fund_count, params FROM fund_dca_meta
"""

_DCA_REF_SQL = "SELECT max(full_end) FROM fund_dca_result"

# 排序白名单：前端传列名，只允许这些键进 ORDER BY（防注入）
_SORTABLE = {
    "w10_xirr", "full_xirr", "w10_profit_rate", "full_profit_rate",
    "w10_rounds", "full_rounds", "scale_yi", "code", "full_start",
    "gap_avg_days",
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
                   min_age: float = 0.0, min_scale: float = 0.0,
                   limit: int = 50, offset: int = 0) -> dict:
    """回测排行：分页 + 搜索 + 在市/年限过滤 + 服务端排序 + 宇宙统计与口径 meta。

    alive="true"（默认）仅近 45 天有净值的在市基金；"false" 仅清盘/退市；
    其余值（含空串）全部。min_age>0 只看运作超过 N 年的基金（严格大于：
    首条净值 < 参考日 - N*365.25 天，参考日=全表 max(full_end)）；
    min_scale>0 只看规模超过 N 亿的（scale_yi > N，无规模披露的自动排除）。
    统计卡同步跟随两个筛选。sort 非白名单回落 w10_xirr。
    """
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    like = f"%{q}%"
    sort_col = sort if sort in _SORTABLE else "w10_xirr"
    order_dir = "ASC" if order.lower() == "asc" else "DESC"
    alive_clause = ("true" if alive not in ("true", "false")
                    else f"alive = {alive.upper()}")

    def run():
        with psycopg.connect(dsn, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute(_DCA_REF_SQL)
                ref = cur.fetchone()[0]
                age_clause, cutoff = "true", None
                if min_age and ref:
                    cutoff = ref - dt.timedelta(days=365.25 * min_age)
                    age_clause = "full_start < %s"   # 严格大于 N 年
                scale_clause = "scale_yi > %s" if min_scale else "true"
                scale_param = min_scale if min_scale else None
                sql = (_DCA_LIST_SQL.replace("{alive_clause}", alive_clause)
                       .replace("{age_clause}", age_clause)
                       .replace("{scale_clause}", scale_clause)
                       .replace("{sort_col}", sort_col)
                       .replace("{order_dir}", order_dir))
                count_sql = (_DCA_COUNT_SQL
                             .replace("{alive_clause}", alive_clause)
                             .replace("{age_clause}", age_clause)
                             .replace("{scale_clause}", scale_clause))
                stats_sql = (_DCA_STATS_SQL
                             .replace("{age_clause}", age_clause)
                             .replace("{scale_clause}", scale_clause))
                base = ([q, like, like] + ([cutoff] if cutoff else [])
                        + ([scale_param] if scale_param else []))
                cur.execute(sql, (*base, limit, offset))
                cols = ["code", "name", "fund_type", "alive", "scale_yi",
                        "scale_date", "w10_start", "w10_end", "w10_months",
                        "w10_rounds", "w10_invested", "w10_final_value",
                        "w10_profit_rate", "w10_xirr", "full_xirr",
                        "full_rounds", "full_profit_rate", "full_months",
                        "full_start", "gap_avg_days"]
                rows = [dict(zip(cols, r)) for r in cur.fetchall()]
                cur.execute(count_sql, base)
                total = cur.fetchone()[0]
                cur.execute(stats_sql, ([cutoff] if cutoff else [])
                            + ([scale_param] if scale_param else []))
                s = cur.fetchone()
                cur.execute(_DCA_META_SQL)
                m = cur.fetchone()
        for r in rows:
            for k in ("w10_xirr", "full_xirr", "w10_profit_rate",
                      "full_profit_rate"):
                r[k] = round(r[k], 4) if r[k] is not None else None
            r["scale_yi"] = round(r["scale_yi"], 2) if r["scale_yi"] is not None else None
            r["age_years"] = (round((ref - r["full_start"]).days / 365.25, 1)
                              if ref and r["full_start"] else None)
            r.pop("full_start", None)
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


_ROUNDS_SQL = """
SELECT seq, tp_date, round_days, gap_days, profit
FROM fund_dca_rounds WHERE code = %s ORDER BY seq
"""


def fetch_dca_rounds(dsn: str, code: str) -> dict:
    def run():
        with psycopg.connect(dsn, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute(_ROUNDS_SQL, (code,))
                rows = cur.fetchall()
        out = [{"seq": r[0], "tp_date": r[1].isoformat() if r[1] else None,
                "round_days": r[2], "gap_days": r[3],
                "profit": round(r[4], 4) if r[4] is not None else None}
               for r in rows]
        return {"status": "up", "code": code, "rounds": out}
    return _safe(run, {"status": "down", "detail": "pg unreachable",
                       "code": code, "rounds": []})


def register_fund_dca_routes(app):
    @app.get("/api/funds/dca")
    def funds_dca(q: str = "", alive: str = "true", sort: str = "w10_xirr",
                  order: str = "desc", min_age: float = 0.0,
                  min_scale: float = 0.0, limit: int = 50, offset: int = 0):
        """定投止盈回测排行（快照数据，重跑回测导入后刷新）。

        降级约定同 /api/funds：始终 200；alive="true" 默认仅在市；
        min_age>0 只看运作 >N 年；min_scale>0 只看规模 >N 亿；
        sort 白名单排序；stats 跟随在市+年限+规模筛选（不随搜索词）。
        """
        return fetch_dca_list(config.DATABASE_URL, q=q,
                              alive=alive.strip().lower(), sort=sort,
                              order=order.strip().lower(),
                              min_age=max(0.0, min_age),
                              min_scale=max(0.0, min_scale),
                              limit=limit, offset=offset)

    @app.get("/api/funds/dca/{code}/rounds")
    def funds_dca_rounds(code: str):
        """单基金每次止盈明细：止盈日 / 距上次止盈天数 / 本轮持有天数 /
        实际止盈收益率（≥20%，含跳空越过）。降级约定同 /api/funds/dca。"""
        return fetch_dca_rounds(config.DATABASE_URL, code)

    @app.get("/api/funds/dca/scatter")
    def funds_dca_scatter(fee: str = "disc"):
        """三维散点：每只在市基金一点 (运作年限, 规模亿, 年化) + 年限段 ×
        规模段分箱统计（每基金只计入唯一格子）。fee 选年化口径：
        gross=不含费 / disc=含费·申购一折（默认）/ nom=含费·申购名义，
        非白名单值回落 disc。降级约定同 /api/funds/dca。
        """
        return fetch_dca_scatter(config.DATABASE_URL, fee=fee.strip().lower())



# ===== 三维散点（每基金一点）+ 年限段 × 规模段 分箱统计 =====
AGE_BANDS = [(0, 1), (1, 3), (3, 5), (5, 10), (10, None)]     # 运作年限段（年）
SCALE_BANDS = [(0, 1), (1, 10), (10, 100), (100, None)]       # 规模段（亿元）

# 年化口径 → 结果表列名（gross=不含费原口径；disc/nom 含费，赎回费一致）
_FEE_XIRR_COL = {"gross": "w10_xirr", "disc": "w10_xirr_disc",
                 "nom": "w10_xirr_nom"}


def _pct(sorted_vals, p):
    """线性插值分位数（sorted_vals 非空）。"""
    if not sorted_vals:
        return None
    k = (len(sorted_vals) - 1) * p
    f = int(k)
    c = min(f + 1, len(sorted_vals) - 1)
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def age_band_label(lo, hi):
    return f"{lo}-{hi} 年" if hi else f"≥{lo} 年"


def scale_band_label(lo, hi):
    if hi is None:
        return f"≥{lo} 亿"
    return f"<{hi} 亿" if lo == 0 else f"{lo}-{hi} 亿"


def compute_bands(rows, ref):
    """rows=[(scale, full_start, xirr, rounds)] → 年限段 × 规模段 分箱矩阵。

    每只基金按 (运作年限, 规模) 落入唯一一格；格统计只数/平均年化/中位/
    年化为正占比。
    """
    matrix = [[{"age_band": age_band_label(lo, hi if hi else None),
                "scale_band": scale_band_label(slo, shi if shi else None),
                "count": 0, "xirrs": [], "positive": 0}
               for (slo, shi) in SCALE_BANDS] for (lo, hi) in AGE_BANDS]
    for scale, full_start, xirr, rounds in rows:
        age = (ref - full_start).days / 365.25
        ai = next((i for i, (lo, hi) in enumerate(AGE_BANDS)
                   if age >= lo and (hi is None or age < hi)), None)
        si = next((i for i, (lo, hi) in enumerate(SCALE_BANDS)
                   if scale >= lo and (hi is None or scale < hi)), None)
        if ai is None or si is None:
            continue
        cell = matrix[ai][si]
        cell["count"] += 1
        cell["xirrs"].append(xirr)
        cell["positive"] += 1 if xirr > 0 else 0
    for row in matrix:
        for cell in row:
            n = cell.pop("count")
            xs = sorted(cell.pop("xirrs"))
            cell["count"] = n
            cell["mean_xirr"] = round(sum(xs) / n, 4) if n else None
            cell["median_xirr"] = round(_pct(xs, 0.5), 4) if n else None
            cell["positive_share"] = round(cell.pop("positive") / n, 4) if n else None
    return {
        "age_bands": [age_band_label(lo, hi) for lo, hi in AGE_BANDS],
        "scale_bands": [scale_band_label(lo, hi) for lo, hi in SCALE_BANDS],
        "matrix": matrix,
    }


def fetch_dca_scatter(dsn: str, fee: str = "disc") -> dict:
    """三维散点数据：每只在市基金一点 (运作年限, 规模亿, 年化) + 分箱统计。

    fee 选择年化口径：gross=不含费 / disc=含费·申购一折 / nom=含费·申购名义
    （赎回费三口径一致收取），响应带 fee 回显。点数组
    [age, scale, xirr, code, name, fund_type]，规模取对数轴定位由前端处理；
    降级约定同 /api/funds/dca。
    """
    xirr_col = _FEE_XIRR_COL.get(fee, "w10_xirr_disc")

    def run():
        with psycopg.connect(dsn, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute(_DCA_REF_SQL)
                ref = cur.fetchone()[0]
                cur.execute(f"SELECT code, name, fund_type, scale_yi, "
                            f"full_start, {xirr_col}, w10_rounds, full_end "
                            f"FROM fund_dca_result "
                            f"WHERE alive AND {xirr_col} IS NOT NULL "
                            f"AND full_start IS NOT NULL AND scale_yi IS NOT NULL")
                rows = cur.fetchall()
        points = [
            [round((ref - full_start).days / 365.25, 2), scale, xirr,
             code, name, fund_type]
            for code, name, fund_type, scale, full_start, xirr, rounds, _fe in rows
        ]
        bands = compute_bands(
            [(scale, full_start, xirr, rounds)
             for _, _, _, scale, full_start, xirr, rounds, _fe in rows], ref)
        return {"status": "up", "fee": fee if fee in _FEE_XIRR_COL else "disc",
                "ref_date": ref.isoformat() if ref else None,
                "points": points, **bands}
    return _safe(run, {"status": "down", "detail": "pg unreachable",
                       "points": [],
                       "age_bands": [], "scale_bands": [], "matrix": []})
