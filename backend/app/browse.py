"""数据浏览查询：股票列表（含行情汇总）与单股日K。

只读查询，语义与 crawl_stats 一致——PG 不可达/表不存在是"状态"（down/空）
而不是错误，接口始终 200。
"""

import psycopg

from .adjust import ensure_factors, load_factors

# 两段式查询：先在 stock_basic 上分页（小表，毫秒级），再只对当页 ≤200 只
# 聚合 daily_bar（大表 1,600 万行——原写法 LATERAL 作用于全表分页前的每一行，
# 深分页时退化成对大表数千次聚合，实测 8~10s）。
# 有来源过滤时只列「该来源下有K线」的股票（用 EXISTS 而非聚合，可走 (code,trade_date,source) 主键）
_SRC_FILTER = "EXISTS (SELECT 1 FROM daily_bar d WHERE d.code = stock_basic.code AND d.source = {src!r})"

_LIST_IDS_SQL = """
SELECT code, name FROM stock_basic
WHERE (%s = '' OR code ILIKE %s OR name ILIKE %s)
  AND {src_clause}
  AND {delist_clause}
ORDER BY code
LIMIT %s OFFSET %s
"""

_LIST_SQL = """
SELECT b.code, b.name, b.delisted,
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
  AND {delist_clause}
"""

_BARS_SQL = """
SELECT trade_date, open, close, high, low, volume, amount,
       amplitude, pct_chg, change, turnover, source, outstanding_share
FROM daily_bar WHERE code = %s {src_clause}
ORDER BY trade_date DESC LIMIT %s OFFSET %s
"""

# 基金列表：与股票列表同款两段式（fund_basic 小表分页 → 只对当页聚合
# fund_nav 大表——爬满后 fund_nav 可达数千万行，LATERAL 必须作用于分页后）
_FUND_LIST_IDS_SQL = """
SELECT code, name FROM fund_basic
WHERE (%s = '' OR code ILIKE %s OR name ILIKE %s)
  AND {delist_clause}
  AND {backend_clause}
  AND {money_clause}
  AND {qdii_clause}
ORDER BY code
LIMIT %s OFFSET %s
"""

_FUND_LIST_SQL = """
SELECT b.code, b.name, b.fund_type,
       p.unit_nav, p.pct_chg, p.nav_date,
       COALESCE(s.navs, 0) AS navs,
       COALESCE(s.first_date, p.nav_date) AS first_date
FROM fund_basic b
LEFT JOIN LATERAL (
    SELECT unit_nav, pct_chg, nav_date
    FROM fund_nav f
    WHERE f.code = b.code
    ORDER BY nav_date DESC LIMIT 1
) p ON true
LEFT JOIN LATERAL (
    SELECT count(*) AS navs, min(nav_date) AS first_date
    FROM fund_nav f2
    WHERE f2.code = b.code
) s ON true
WHERE b.code = ANY(%s)
ORDER BY b.code
"""

_FUND_COUNT_SQL = """
SELECT count(*) FROM fund_basic
WHERE (%s = '' OR code ILIKE %s OR name ILIKE %s)
  AND {delist_clause}
  AND {backend_clause}
  AND {money_clause}
  AND {qdii_clause}
"""

_FUND_NAVS_SQL = """
SELECT nav_date, unit_nav, accum_nav, pct_chg, unit_money, source
FROM fund_nav WHERE code = %s
ORDER BY nav_date DESC LIMIT %s OFFSET %s
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
                     source: str = "", delisted: str = "") -> dict:
    """股票列表 + 每股行情摘要（最新价/涨跌幅/K线数/区间/来源），code/name 模糊搜索。

    source 非空时仅返回该来源下有K线的股票（数据源 Tab 过滤）；
    delisted="false"/"true" 仅在市/仅退市（数据页退市过滤），其余值不过滤。
    """
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    like = f"%{q}%"
    # 退市过滤：严格匹配 "true"/"false" 内联布尔谓词；其余值（含空串）按全部处理。
    # 这两段 SQL 的 stock_basic 无别名 → 谓词必须用全表名（用 b. 会 42P01 被 _safe 吞成 down）
    delist_clause = (f"stock_basic.delisted = {delisted.upper()}"
                     if delisted in ("true", "false") else "true")
    sql_ids = (_LIST_IDS_SQL.replace("{src_clause}", _SRC_FILTER.format(src=source))
                 .replace("{delist_clause}", delist_clause)
               if source else _LIST_IDS_SQL.replace("{src_clause}", "true")
                 .replace("{delist_clause}", delist_clause))
    count_sql = (_COUNT_SQL.replace("{src_clause}", _SRC_FILTER.format(src=source))
                   .replace("{delist_clause}", delist_clause)
                 if source else _COUNT_SQL.replace("{src_clause}", "true")
                   .replace("{delist_clause}", delist_clause))

    def run():
        with psycopg.connect(dsn, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute(sql_ids, (q, like, like, limit, offset))
                page_rows = cur.fetchall()
                codes = [r[0] for r in page_rows]
                stocks = []
                if codes:
                    cur.execute(_LIST_SQL, (codes,))
                    cols = ["code", "name", "delisted", "close", "pct_chg",
                            "last_date", "bars", "first_date", "sources"]
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
                     source: str = "", adjust: str = "") -> dict:
    """单股日K（默认最新 250 根，倒序进正序出）。

    source 非空时仅取该来源（个股详情页用——(code,trade_date,source) 主键下
    同一交易日两源各一行，不过滤会混返）；缺省不过滤，兼容旧行为。
    adjust="qfq"：前复权口径（因子懒加载入库；失败降级不复权，
    adjust_applied=false 供前端提示）。
    """
    limit = max(1, min(limit, 5000))
    offset = max(0, offset)
    source = source.strip().lower()
    sql = (_BARS_SQL.replace("{src_clause}", "AND source = %s")
           if source else _BARS_SQL.replace("{src_clause}", "AND true"))
    params = ((code, source, limit, offset) if source
              else (code, limit, offset))

    def run():
        with psycopg.connect(dsn, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()

        # 前复权：因子懒加载 + 逐行换算 OHLC + 重算涨跌衍生字段
        adjust_applied = False
        rows.sort(key=lambda r: r[0])                  # 升序处理（输出再倒序）
        if adjust == "qfq" and rows:
            try:
                ensure_factors(dsn, [code], latest_trade_dates={
                    code: rows[-1][0]})
                with psycopg.connect(dsn, connect_timeout=3) as fconn:
                    fac = load_factors(fconn, code)
                if not fac.empty:
                    fmap = {d.date() if hasattr(d, "date") else d: v
                            for d, v in fac.items()}
                    adj_rows, prev_factor, prev_adj_close = [], None, None
                    for r in rows:             # r: date, open, close, high, low, ...
                        factor = fmap.get(r[0]) or prev_factor
                        prev_factor = factor or prev_factor
                        if not factor:
                            adj_rows.append(r)
                            prev_adj_close = r[2]
                            continue
                        o, cl, h, l = (r[1] * factor, r[2] * factor,
                                       r[3] * factor, r[4] * factor)
                        pct = ((cl / prev_adj_close - 1) * 100
                               if prev_adj_close else r[8])
                        amp = ((h - l) / prev_adj_close * 100
                               if prev_adj_close else r[7])
                        chg = cl - prev_adj_close if prev_adj_close else r[9]
                        adj_rows.append((r[0], o, cl, h, l, r[5], r[6],
                                         amp, pct, chg, r[10], r[11], r[12]))
                        prev_adj_close = cl
                    rows = adj_rows
                    adjust_applied = True
            except Exception:                          # 因子不可用：降级不复权
                adjust_applied = False

        out = [
            {"date": r[0].isoformat(), "open": r[1], "close": r[2],
             "high": r[3], "low": r[4], "volume": r[5], "amount": r[6],
             "amplitude": r[7], "pct_chg": r[8], "change": r[9],
             "turnover": r[10], "source": r[11],
             "outstanding_share": r[12]}
            for r in rows
        ]
        out.reverse()
        return {"status": "up", "code": code, "count": len(out),
                "adjust": adjust, "adjust_applied": adjust_applied,
                "bars": out}
    return _safe(run, {"status": "down", "detail": "pg unreachable",
                       "code": code, "count": 0, "bars": [],
                       "adjust_applied": False})


def fetch_stock_basic(dsn: str) -> dict:
    """全清单 code+name（教学页代码下拉数据源，一次性拉取前端本地过滤）。

    与 fetch_stock_list 同一套 SQL，但放宽单页上限取全量、只回两列。
    """
    def run():
        with psycopg.connect(dsn, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT code, name FROM stock_basic ORDER BY code")
                rows = cur.fetchall()
        return {"status": "up", "total": len(rows),
                "stocks": [{"code": r[0], "name": r[1]} for r in rows]}
    return _safe(run, {"status": "down", "detail": "pg unreachable",
                       "total": 0, "stocks": []})


def fetch_fund_list(dsn: str, *, q: str = "", limit: int = 50,
                    offset: int = 0, delisted: str = "",
                    show_backend: str = "", show_money: str = "",
                    show_qdii: str = "") -> dict:
    """基金列表 + 每只净值摘要（最新单位净值/日增长率/净值条数/区间），
    code/name 模糊搜索。两段式同 fetch_stock_list（见其 docstring 的
    深分页性能教训）。

    delisted="false"/"true" 仅在市/仅退市（fund_basic.delisted 由爬虫按
    在市集合差集标记），其余值（含空串）不过滤；
    show_backend 非 "1"/"true"（默认）隐藏后端收费份额——东财此类份额
    名称固定带"(后端)"标注（全集 190 只，源上普遍无独立净值数据）。
    show_money 非 "1"/"true"（默认）隐藏货币基金（fund_type 含"货币"——
    摊余成本法货基净值恒为 1、无单位净值走势，列表常驻意义小）。
    show_qdii 非 "1"/"true"（默认）隐藏 QDII 基金（fund_type 含"QDII"，
    净值披露 T+1/T+2 滞后）。
    谓词用 position() 而非 LIKE '%..%'：psycopg3 会对带参 SQL 里的字面量
    % 做 %-格式化，中文首字节撞上直接 UnicodeDecodeError（真库实测）
    ——内联片段禁用裸 %，与全表名教训同源；COALESCE 使 NULL 类型
    （快照增量新见基金）不受隐藏影响。
    """
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    like = f"%{q}%"
    delist_clause = (f"fund_basic.delisted = {delisted.upper()}"
                     if delisted in ("true", "false") else "true")
    backend_clause = ("true" if show_backend in ("1", "true")
                      else "position('后端' in fund_basic.name) = 0")
    money_clause = ("true" if show_money in ("1", "true")
                    else "COALESCE(position('货币' in fund_basic.fund_type), 0) = 0")
    qdii_clause = ("true" if show_qdii in ("1", "true")
                   else "COALESCE(position('QDII' in fund_basic.fund_type), 0) = 0")
    sql_ids = (_FUND_LIST_IDS_SQL
               .replace("{delist_clause}", delist_clause)
               .replace("{backend_clause}", backend_clause)
               .replace("{money_clause}", money_clause)
               .replace("{qdii_clause}", qdii_clause))
    count_sql = (_FUND_COUNT_SQL
                 .replace("{delist_clause}", delist_clause)
                 .replace("{backend_clause}", backend_clause)
                 .replace("{money_clause}", money_clause)
                 .replace("{qdii_clause}", qdii_clause))

    def run():
        with psycopg.connect(dsn, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute(sql_ids, (q, like, like, limit, offset))
                codes = [r[0] for r in cur.fetchall()]
                funds = []
                if codes:
                    cur.execute(_FUND_LIST_SQL, (codes,))
                    cols = ["code", "name", "fund_type", "unit_nav", "pct_chg",
                            "last_date", "navs", "first_date"]
                    by_code = {r[0]: dict(zip(cols, r)) for r in cur.fetchall()}
                    funds = [by_code[c] for c in codes]   # 保持 code 序
                cur.execute(count_sql, (q, like, like))
                total = cur.fetchone()[0]
        for f in funds:
            f["unit_nav"] = round(f["unit_nav"], 4) if f["unit_nav"] is not None else None
            f["pct_chg"] = round(f["pct_chg"], 2) if f["pct_chg"] is not None else None
            for k in ("last_date", "first_date"):
                if f[k]:
                    f[k] = f[k].isoformat()
        return {"status": "up", "total": total, "limit": limit,
                "offset": offset, "funds": funds}
    return _safe(run, {"status": "down", "detail": "pg unreachable",
                       "total": 0, "limit": limit, "offset": offset, "funds": []})


def fetch_fund_navs(dsn: str, code: str, *, limit: int = 250,
                    offset: int = 0) -> dict:
    """单只基金净值历史（默认最新 250 个净值日，倒序进正序出，同 bars 约定：
    图表用升序，明细表自行倒序展示）。"""
    limit = max(1, min(limit, 5000))
    offset = max(0, offset)

    def run():
        with psycopg.connect(dsn, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute(_FUND_NAVS_SQL, (code, limit, offset))
                rows = cur.fetchall()
        out = [
            {"date": r[0].isoformat(), "unit_nav": r[1], "accum_nav": r[2],
             "pct_chg": r[3], "unit_money": r[4], "source": r[5]}
            for r in rows
        ]
        out.reverse()
        return {"status": "up", "code": code, "count": len(out), "navs": out}
    return _safe(run, {"status": "down", "detail": "pg unreachable",
                       "code": code, "count": 0, "navs": []})
