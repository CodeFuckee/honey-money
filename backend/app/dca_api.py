"""定投止盈回测结果查询 API（/api/funds/dca）。

数据来自 fund_dca_result 快照（scripts 离线回测导入，见 dca_store），
只读 + _safe 降级约定同 browse：PG 不可达/表不存在是"状态"（down/空）
而不是错误，接口始终 200。
"""

import datetime as dt

import psycopg

from . import config
from .dca_backtest import TAKE_PROFIT, TP_THRESHOLDS   # 阈值事实源单一

_DCA_LIST_SQL = """
SELECT code, name, fund_type, alive, scale_yi, scale_date,
       w10_start, w10_end, w10_months, w10_rounds,
       w10_invested, w10_final_value, w10_profit_rate, w10_xirr,
       full_xirr, full_rounds, full_profit_rate, full_months,
       full_start, gap_avg_days,
       w10_xirr_disc, w10_xirr_nom, full_xirr_disc, full_xirr_nom,
       w10_rounds_disc, w10_rounds_nom, full_rounds_disc, full_rounds_nom,
       w10_profit_rate_disc, w10_profit_rate_nom,
       full_profit_rate_disc, full_profit_rate_nom,
       gap_avg_days_disc, gap_avg_days_nom
FROM fund_dca_result
WHERE (%s = '' OR code ILIKE %s OR name ILIKE %s)
  AND {alive_clause}
  AND {age_clause}
  AND {scale_clause}
  AND {equity_clause}
ORDER BY {sort_col} {order_dir} NULLS LAST, code
LIMIT %s OFFSET %s
"""

_DCA_COUNT_SQL = """
SELECT count(*) FROM fund_dca_result
WHERE (%s = '' OR code ILIKE %s OR name ILIKE %s)
  AND {alive_clause}
  AND {age_clause}
  AND {scale_clause}
  AND {equity_clause}
"""

# 统计卡口径跟随「在市 + 运作年限 + 权益类」筛选（不跟搜索词——描述筛选后
# 的宇宙，而非当页）与费用口径（{xirr_col}/{rounds_col} 按 fee 后缀替换）；
# 无年限筛选时与旧口径一致
_DCA_STATS_SQL = """
SELECT count(*),
       percentile_cont(0.25) WITHIN GROUP (ORDER BY {xirr_col}),
       percentile_cont(0.5)  WITHIN GROUP (ORDER BY {xirr_col}),
       percentile_cont(0.75) WITHIN GROUP (ORDER BY {xirr_col}),
       percentile_cont(0.9)  WITHIN GROUP (ORDER BY {xirr_col}),
       avg(({xirr_col} > 0)::int)::float,
       avg(({rounds_col} > 0)::int)::float
FROM fund_dca_result
WHERE alive AND {xirr_col} IS NOT NULL
  AND {age_clause}
  AND {scale_clause}
  AND {equity_clause}
"""

_DCA_META_SQL = """
SELECT generated_at, fund_count, params FROM fund_dca_meta
"""

_DCA_REF_SQL = "SELECT max(full_end) FROM fund_dca_result"

# 排序白名单：前端传列名，只允许这些键进 ORDER BY（防注入）
_SORTABLE = {
    "w10_xirr", "full_xirr", "w10_profit_rate", "full_profit_rate",
    "w10_rounds", "full_rounds", "scale_yi", "code", "full_start",
    "gap_avg_days", "age_years",   # 运作年限列 field 别名 → ORDER BY full_start
}
# 费用口径后缀（gross=不含费沿用原列；disc=申购一折；nom=申购名义）。
# fee 只影响 ORDER BY 与统计卡的列替换；SELECT 恒返回全部口径列，
# 前端按行内后缀字段自取（与静态快照同一份行结构）
_LIST_FEE_SUFFIX = {"gross": "", "disc": "_disc", "nom": "_nom"}
# 这些排序字段区分口径；scale_yi / code / full_start 与费用无关
_FEE_SORTABLE = {"w10_xirr", "full_xirr", "w10_profit_rate", "full_profit_rate",
                 "w10_rounds", "full_rounds", "gap_avg_days"}

# 权益类白名单（东财 fund_type）：股票/偏股/灵活/平衡混合 + A股股票指数
# + 海外权益（海外股票指数、QDII 股票/QDII 偏股）。债券、FOF、货币、偏债、
# 商品、REITs 不在内。口径 2026-11-15 用户确认（全市场开关默认仅权益类）。
EQUITY_TYPES = ("股票型", "混合型-偏股", "混合型-灵活", "混合型-平衡",
                "指数型-股票", "指数型-海外股票", "QDII-普通股票",
                "QDII-混合偏股")
_EQUITY_TRUTHY = {"true", "1", "yes", "on"}


def _parse_equity(raw) -> bool:
    """equity 查询参数 → bool；仅真值字面量为仅权益类，其余（含乱值）=全市场。"""
    return str(raw).strip().lower() in _EQUITY_TRUTHY
# 行映射列清单（与 _DCA_LIST_SQL 的 SELECT 列一一对应，dict(zip) 用）；
# 恒含三口径后缀列——fee 只切换排序/统计列，行结构全口径
_LIST_COLS = ["code", "name", "fund_type", "alive", "scale_yi",
              "scale_date", "w10_start", "w10_end", "w10_months",
              "w10_rounds", "w10_invested", "w10_final_value",
              "w10_profit_rate", "w10_xirr", "full_xirr",
              "full_rounds", "full_profit_rate", "full_months",
              "full_start", "gap_avg_days",
              "w10_xirr_disc", "w10_xirr_nom", "full_xirr_disc",
              "full_xirr_nom", "w10_rounds_disc", "w10_rounds_nom",
              "full_rounds_disc", "full_rounds_nom",
              "w10_profit_rate_disc", "w10_profit_rate_nom",
              "full_profit_rate_disc", "full_profit_rate_nom",
              "gap_avg_days_disc", "gap_avg_days_nom"]
# 浮点保留 4 位的字段（口径变体与原字段同规则）
_LIST_ROUND4 = ("w10_xirr", "full_xirr", "w10_profit_rate",
                "full_profit_rate", "w10_xirr_disc", "w10_xirr_nom",
                "full_xirr_disc", "full_xirr_nom",
                "w10_profit_rate_disc", "w10_profit_rate_nom",
                "full_profit_rate_disc", "full_profit_rate_nom")


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
                   limit: int = 50, offset: int = 0, fee: str = "gross",
                   equity: bool = True) -> dict:
    """回测排行：分页 + 搜索 + 在市/年限/权益过滤 + 服务端排序 + 宇宙统计与口径 meta。

    alive="true"（默认）仅近 45 天有净值的在市基金；"false" 仅清盘/退市；
    其余值（含空串）全部。min_age>0 只看运作超过 N 年的基金（严格大于：
    首条净值 < 参考日 - N*365.25 天，参考日=全表 max(full_end)）；
    min_scale>0 只看规模超过 N 亿的（scale_yi > N，无规模披露的自动排除）；
    equity=True（默认）仅权益类（EQUITY_TYPES 白名单 8 类），False=全市场。
    统计卡同步跟随各筛选与费用口径。sort 非白名单回落 w10_xirr；
    fee 选费用口径（gross=不含费 / disc=申购一折 / nom=申购名义，
    白名单外回落 gross），仅切换排序与统计列——行内恒带全部口径字段
    （w10_xirr / *_disc / *_nom 等），响应回显 fee 与 equity。
    """
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    like = f"%{q}%"
    fee_val = fee if fee in _LIST_FEE_SUFFIX else "gross"
    suffix = _LIST_FEE_SUFFIX[fee_val]
    if sort == "age_years":            # 运作年限列 field → 排序落库列成立日
        sort = "full_start"
    sort_base = sort if sort in _SORTABLE else "w10_xirr"
    sort_col = (f"{sort_base}{suffix}" if sort_base in _FEE_SORTABLE
                else sort_base)
    order_dir = "ASC" if order.lower() == "asc" else "DESC"
    alive_clause = ("true" if alive not in ("true", "false")
                    else f"alive = {alive.upper()}")
    equity_clause = "fund_type = ANY(%s)" if equity else "true"
    equity_param = list(EQUITY_TYPES) if equity else None

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
                       .replace("{equity_clause}", equity_clause)
                       .replace("{sort_col}", sort_col)
                       .replace("{order_dir}", order_dir))
                count_sql = (_DCA_COUNT_SQL
                             .replace("{alive_clause}", alive_clause)
                             .replace("{age_clause}", age_clause)
                             .replace("{scale_clause}", scale_clause)
                             .replace("{equity_clause}", equity_clause))
                stats_sql = (_DCA_STATS_SQL
                             .replace("{xirr_col}", f"w10_xirr{suffix}")
                             .replace("{rounds_col}", f"w10_rounds{suffix}")
                             .replace("{age_clause}", age_clause)
                             .replace("{scale_clause}", scale_clause)
                             .replace("{equity_clause}", equity_clause))
                base = ([q, like, like] + ([cutoff] if cutoff else [])
                        + ([scale_param] if scale_param else [])
                        + ([equity_param] if equity_param else []))
                cur.execute(sql, (*base, limit, offset))
                rows = [dict(zip(_LIST_COLS, r)) for r in cur.fetchall()]
                cur.execute(count_sql, base)
                total = cur.fetchone()[0]
                cur.execute(stats_sql, ([cutoff] if cutoff else [])
                            + ([scale_param] if scale_param else [])
                            + ([equity_param] if equity_param else []))
                s = cur.fetchone()
                cur.execute(_DCA_META_SQL)
                m = cur.fetchone()
        for r in rows:
            for k in _LIST_ROUND4:
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
                "offset": offset, "fee": fee_val, "equity": bool(equity),
                "rows": rows, "stats": stats, "meta": meta}
    return _safe(run, {"status": "down", "detail": "pg unreachable",
                       "total": 0, "limit": limit, "offset": offset,
                       "equity": bool(equity),
                       "rows": [], "stats": None, "meta": None})


_ROUNDS_SQL = """
SELECT seq, tp_date, round_days, gap_days, profit,
       tp_nav, invested, sub_fee, redeem_fee, net_profit
FROM fund_dca_rounds WHERE code = %s AND fee = %s ORDER BY seq
"""


def fetch_dca_rounds(dsn: str, code: str, fee: str = "nom") -> dict:
    """单基金每轮止盈明细（按费用口径）。fee 白名单外回落 nom。

    行字段：seq/tp_date/round_days/gap_days/profit（市值口径收益率）+
    tp_nav（止盈日复权净值）/invested（本轮投入）/sub_fee/redeem_fee
    （本轮申购/赎回费）/net_profit（本轮净收益，扣费后）；
    totals 为已了结轮次汇总（轮数/累计投入/累计净收益/两项费用合计）。
    """
    fee_val = fee if fee in _FEE_XIRR_COL else "nom"

    def run():
        with psycopg.connect(dsn, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute(_ROUNDS_SQL, (code, fee_val))
                rows = cur.fetchall()
        out = [{"seq": r[0], "tp_date": r[1].isoformat() if r[1] else None,
                "round_days": r[2], "gap_days": r[3],
                "profit": round(r[4], 4) if r[4] is not None else None,
                "tp_nav": r[5], "invested": r[6],
                "sub_fee": r[7], "redeem_fee": r[8], "net_profit": r[9]}
               for r in rows]
        totals = {
            "rounds": len(out),
            "invested": round(sum(r["invested"] or 0 for r in out), 2),
            "net_profit": round(sum(r["net_profit"] or 0 for r in out), 2),
            "sub_fee": round(sum(r["sub_fee"] or 0 for r in out), 2),
            "redeem_fee": round(sum(r["redeem_fee"] or 0 for r in out), 2),
        }
        return {"status": "up", "code": code, "fee": fee_val, "rounds": out,
                "totals": totals}
    return _safe(run, {"status": "down", "detail": "pg unreachable",
                       "code": code, "rounds": [], "totals": {}})


def register_fund_dca_routes(app):
    @app.get("/api/funds/dca")
    def funds_dca(q: str = "", alive: str = "true", sort: str = "w10_xirr",
                  order: str = "desc", min_age: float = 0.0,
                  min_scale: float = 0.0, limit: int = 50, offset: int = 0,
                  fee: str = "gross", equity: str = "true"):
        """定投止盈回测排行（快照数据，重跑回测导入后刷新）。

        降级约定同 /api/funds：始终 200；alive="true" 默认仅在市；
        min_age>0 只看运作 >N 年；min_scale>0 只看规模 >N 亿；
        equity=true 默认仅权益类（8 类白名单，全市场基金类型见
        EQUITY_TYPES；false/其他值=全市场）；sort 白名单排序；
        stats 跟随在市+年限+规模+权益筛选与费用口径
        （fee=gross 不含费默认 / disc 申购一折 / nom 申购名义，
        与散点/详情共用口径切换；白名单外回落 gross，
        行内恒带三口径字段）。
        """
        return fetch_dca_list(config.DATABASE_URL, q=q,
                              alive=alive.strip().lower(), sort=sort,
                              order=order.strip().lower(),
                              min_age=max(0.0, min_age),
                              min_scale=max(0.0, min_scale),
                              limit=limit, offset=offset,
                              fee=fee.strip().lower(),
                              equity=_parse_equity(equity))

    @app.get("/api/funds/dca/{code}/rounds")
    def funds_dca_rounds(code: str, fee: str = "nom"):
        """单基金每轮止盈明细：止盈日 / 复权净值 / 本轮投入 / 申购赎回费 /
        净收益（扣费后）/ 距上次止盈天数 / 本轮持有天数 / 市值口径收益率，
        外加 totals 已了结轮次汇总。fee 选费用口径（默认含费·申购名义，
        白名单外回落）。降级约定同 /api/funds/dca。"""
        return fetch_dca_rounds(config.DATABASE_URL, code,
                                fee=fee.strip().lower())

    @app.get("/api/funds/dca/scatter")
    def funds_dca_scatter(fee: str = "nom", equity: str = "true"):
        """三维散点：每只在市基金一点 (运作年限, 规模亿, 年化) + 年限段 ×
        规模段分箱统计（每基金只计入唯一格子）。fee 选年化口径：
        gross=不含费 / disc=含费·申购一折 / nom=含费·申购名义（默认），
        非白名单值回落 nom；equity=true 默认仅权益类（false/其他=全市场）。
        降级约定同 /api/funds/dca。
        """
        return fetch_dca_scatter(config.DATABASE_URL, fee=fee.strip().lower(),
                                 equity=_parse_equity(equity))

    @app.get("/api/funds/dca/tp")
    def funds_dca_tp(tp: str = "0.2", q: str = "", alive: str = "true",
                     sort: str = "w10_xirr", order: str = "desc",
                     min_age: float = 0.0, min_scale: float = 0.0,
                     limit: int = 50, offset: int = 0, fee: str = "gross",
                     equity: str = "true"):
        """定投止盈回测排行（多阈值版，fund_dca_tp_result 快照）。

        tp 选止盈点（0.05/0.1/0.15/0.2/0.25/0.3，白名单外回落 0.2）；
        其余参数与降级约定同 /api/funds/dca（权益开关沿用 EQUITY_TYPES
        8 类口径，与旧排行一致）。统计卡跟随 tp 与各筛选、费用口径。
        """
        return fetch_dca_tp_list(config.DATABASE_URL,
                                 tp=_parse_tp(tp.strip().lower()),
                                 q=q, alive=alive.strip().lower(),
                                 sort=sort, order=order.strip().lower(),
                                 min_age=max(0.0, min_age),
                                 min_scale=max(0.0, min_scale),
                                 limit=limit, offset=offset,
                                 fee=fee.strip().lower(),
                                 equity=_parse_equity(equity))

    @app.get("/api/funds/dca/tp/compare")
    def funds_dca_tp_compare(fee: str = "nom", equity: str = "true"):
        """六档止盈点市场级对比聚合（5%~30%）：每阈值样本数 / w10 年化
        P25~P90/均值 / 正收益占比 / 止盈成功率 / 平均轮次 / 全史中位均值 /
        平均止盈间隔。默认权益类+商品池（TP_COMPARE_EQUITY_TYPES，
        2026-11-15 用户确认口径），equity=false 全市场对照；fee 切换口径。
        降级约定同 /api/funds/dca。
        """
        return fetch_dca_tp_compare(config.DATABASE_URL,
                                    fee=fee.strip().lower(),
                                    equity=_parse_equity(equity))

    @app.get("/api/funds/dca/tp/{code}/rounds")
    def funds_dca_tp_rounds(code: str, fee: str = "nom", tp: str = "0.2"):
        """单基金单阈值每轮止盈明细（契约同 /api/funds/dca/{code}/rounds
        + tp 维度；tp 白名单外回落 0.2）。降级约定同 /api/funds/dca。"""
        return fetch_dca_tp_rounds(config.DATABASE_URL, code,
                                   fee=fee.strip().lower(),
                                   tp=_parse_tp(tp.strip().lower()))

    @app.get("/api/funds/dca/tp/{code}/summary")
    def funds_dca_tp_summary(code: str):
        """单基金全阈值总收益率/年化汇总（近 10 年窗口，每阈值一行恒带
        三口径字段——前端弹窗按当前费率口径取值）。降级约定同 /api/funds/dca。"""
        return fetch_dca_tp_summary(config.DATABASE_URL, code.strip())

    @app.get("/api/funds/dca/timing")
    def funds_dca_timing(tp: str = "0.2", band: str = "25-30", q: str = "",
                         alive: str = "true", sort: str = "w10_xirr",
                         order: str = "desc", min_age: float = 0.0,
                         min_scale: float = 0.0, limit: int = 50,
                         offset: int = 0, fee: str = "gross",
                         equity: str = "true"):
        """定投时机列表（当前回撤桶 × 历史止盈素质）。

        band 选回撤桶：25-30 默认（假设验证甜蜜点：该位置开始定投首轮止盈
        最快、成功率与年化最高）/ 20-25 / 30+（超深回撤警戒）/ 其他值=全部；
        tp/fee/equity 与排序口径同 /api/funds/dca/tp。历史统计，不构成操作
        建议。降级约定同 /api/funds/dca。
        """
        return fetch_dca_timing(config.DATABASE_URL,
                                tp=_parse_tp(tp.strip().lower()),
                                band=band.strip(), q=q,
                                alive=alive.strip().lower(),
                                sort=sort, order=order.strip().lower(),
                                min_age=max(0.0, min_age),
                                min_scale=max(0.0, min_scale),
                                limit=limit, offset=offset,
                                fee=fee.strip().lower(),
                                equity=_parse_equity(equity))

    @app.get("/api/funds/dca/dip-report")
    def funds_dca_dip_report():
        """起点条件回测报告（假设验证统计：三组对照/回撤深度梯度/起点年份，
        各组首轮止盈天数分位、3 年成功率、首轮年化分位）。静态快照，验证
        重跑后刷新。历史统计不构成操作建议。降级约定同 /api/funds/dca。"""
        return fetch_dca_dip_report(config.DATABASE_URL)



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


def fetch_dca_scatter(dsn: str, fee: str = "nom", equity: bool = True) -> dict:
    """三维散点数据：每只在市基金一点 (运作年限, 规模亿, 年化) + 分箱统计。

    fee 选择年化口径：gross=不含费 / disc=含费·申购一折 / nom=含费·申购名义
    （赎回费三口径一致收取）；equity=True（默认）仅权益类白名单。
    响应带 fee / equity 回显。点数组
    [age, scale, xirr, code, name, fund_type]，规模取对数轴定位由前端处理；
    降级约定同 /api/funds/dca。
    """
    xirr_col = _FEE_XIRR_COL.get(fee, "w10_xirr_nom")

    def run():
        with psycopg.connect(dsn, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute(_DCA_REF_SQL)
                ref = cur.fetchone()[0]
                sql = (f"SELECT code, name, fund_type, scale_yi, "
                       f"full_start, {xirr_col}, w10_rounds, full_end "
                       f"FROM fund_dca_result "
                       f"WHERE alive AND {xirr_col} IS NOT NULL "
                       f"AND full_start IS NOT NULL AND scale_yi IS NOT NULL")
                params = None
                if equity:
                    sql += " AND fund_type = ANY(%s)"
                    params = (list(EQUITY_TYPES),)
                cur.execute(sql, params)
                rows = cur.fetchall()
        points = [
            [round((ref - full_start).days / 365.25, 2), scale, xirr,
             code, name, fund_type]
            for code, name, fund_type, scale, full_start, xirr, rounds, _fe in rows
        ]
        bands = compute_bands(
            [(scale, full_start, xirr, rounds)
             for _, _, _, scale, full_start, xirr, rounds, _fe in rows], ref)
        return {"status": "up", "fee": fee if fee in _FEE_XIRR_COL else "nom",
                "equity": bool(equity),
                "ref_date": ref.isoformat() if ref else None,
                "points": points, **bands}
    return _safe(run, {"status": "down", "detail": "pg unreachable",
                       "equity": bool(equity),
                       "points": [],
                       "age_bands": [], "scale_bands": [], "matrix": []})


# ===== 多止盈点市场级对比（fund_dca_tp_* 快照，2026-11-15 grilling 定案） =====
# 排行阈值切换 / 阈值轮次明细 / 六档对比聚合；降级约定与旧端点一致
# （始终 200，白名单外回落 20%）。散点/分箱仍走旧端点（20% 口径）。

_TP_LIST_SQL = """
SELECT tp, code, name, fund_type, alive, scale_yi, scale_date,
       w10_start, w10_end, w10_months, w10_rounds,
       w10_invested, w10_final_value, w10_profit_rate, w10_xirr,
       full_xirr, full_rounds, full_profit_rate, full_months,
       full_start, gap_avg_days,
       w10_xirr_disc, w10_xirr_nom, full_xirr_disc, full_xirr_nom,
       w10_rounds_disc, w10_rounds_nom, full_rounds_disc, full_rounds_nom,
       w10_profit_rate_disc, w10_profit_rate_nom,
       full_profit_rate_disc, full_profit_rate_nom,
       gap_avg_days_disc, gap_avg_days_nom
FROM fund_dca_tp_result
WHERE tp = %s
  AND (%s = '' OR code ILIKE %s OR name ILIKE %s)
  AND {alive_clause}
  AND {age_clause}
  AND {scale_clause}
  AND {equity_clause}
ORDER BY {sort_col} {order_dir} NULLS LAST, code
LIMIT %s OFFSET %s
"""

_TP_COUNT_SQL = """
SELECT count(*) FROM fund_dca_tp_result
WHERE tp = %s
  AND (%s = '' OR code ILIKE %s OR name ILIKE %s)
  AND {alive_clause}
  AND {age_clause}
  AND {scale_clause}
  AND {equity_clause}
"""

# 统计卡口径跟随「在市+年限+规模+权益」筛选与费用口径（同旧端点约定，
# 不随搜索词），外加 tp 维度
_TP_STATS_SQL = """
SELECT count(*),
       percentile_cont(0.25) WITHIN GROUP (ORDER BY {xirr_col}),
       percentile_cont(0.5)  WITHIN GROUP (ORDER BY {xirr_col}),
       percentile_cont(0.75) WITHIN GROUP (ORDER BY {xirr_col}),
       percentile_cont(0.9)  WITHIN GROUP (ORDER BY {xirr_col}),
       avg(({xirr_col} > 0)::int)::float,
       avg(({rounds_col} > 0)::int)::float
FROM fund_dca_tp_result
WHERE alive AND tp = %s AND {xirr_col} IS NOT NULL
  AND {age_clause}
  AND {scale_clause}
  AND {equity_clause}
"""

_TP_LIST_COLS = ["tp"] + _LIST_COLS

_TP_COMPARE_SQL = """
SELECT tp, count(*),
       percentile_cont(0.25) WITHIN GROUP (ORDER BY {wx}),
       percentile_cont(0.5)  WITHIN GROUP (ORDER BY {wx}),
       percentile_cont(0.75) WITHIN GROUP (ORDER BY {wx}),
       percentile_cont(0.9)  WITHIN GROUP (ORDER BY {wx}),
       avg({wx}), avg(({wx} > 0)::int)::float,
       avg(({wr} > 0)::int)::float, avg({wr}),
       percentile_cont(0.5) WITHIN GROUP (ORDER BY {fx}),
       avg({fx}), avg(({fx} > 0)::int)::float,
       avg(({fr} > 0)::int)::float,
       avg({gap})
FROM fund_dca_tp_result
WHERE alive AND {wx} IS NOT NULL{equity_clause}
GROUP BY tp ORDER BY tp
"""

# 对比池口径（2026-11-15 用户确认）：权益 8 类白名单 + QDII-商品 + 商品；
# 排行切换端点仍用 EQUITY_TYPES（与既有权益开关同口径）
TP_COMPARE_EQUITY_TYPES = EQUITY_TYPES + ("QDII-商品", "商品")


def _parse_tp(raw) -> float:
    """tp 查询参数 → 白名单阈值；乱值/越界回落 20%（与 sort/fee 同约定）。"""
    try:
        v = round(float(str(raw).strip()), 2)
    except (TypeError, ValueError):
        return TAKE_PROFIT
    return next((t for t in TP_THRESHOLDS if abs(v - t) < 1e-9), TAKE_PROFIT)


def fetch_dca_tp_list(dsn: str, *, tp: float = TAKE_PROFIT, q: str = "",
                      alive: str = "true", sort: str = "w10_xirr",
                      order: str = "desc", min_age: float = 0.0,
                      min_scale: float = 0.0, limit: int = 50, offset: int = 0,
                      fee: str = "gross", equity: bool = True) -> dict:
    """多阈值排行：fetch_dca_list 同款契约 + tp 维度（fund_dca_tp_result）。

    tp 为白名单阈值（0.05~0.30）；排序/统计/筛选口径与旧端点逐项一致，
    行结构多 tp 一列。响应回显 tp / fee / equity。
    """
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    like = f"%{q}%"
    fee_val = fee if fee in _LIST_FEE_SUFFIX else "gross"
    suffix = _LIST_FEE_SUFFIX[fee_val]
    sort_base = sort if sort in _SORTABLE else "w10_xirr"
    sort_col = (f"{sort_base}{suffix}" if sort_base in _FEE_SORTABLE
                else sort_base)
    order_dir = "ASC" if order.lower() == "asc" else "DESC"
    alive_clause = ("true" if alive not in ("true", "false")
                    else f"alive = {alive.upper()}")
    equity_clause = "fund_type = ANY(%s)" if equity else "true"
    equity_param = list(EQUITY_TYPES) if equity else None

    def run():
        with psycopg.connect(dsn, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute(_DCA_REF_SQL)
                ref = cur.fetchone()[0]
                age_clause, cutoff = "true", None
                if min_age and ref:
                    cutoff = ref - dt.timedelta(days=365.25 * min_age)
                    age_clause = "full_start < %s"
                scale_clause = "scale_yi > %s" if min_scale else "true"
                scale_param = min_scale if min_scale else None
                sql = (_TP_LIST_SQL.replace("{alive_clause}", alive_clause)
                       .replace("{age_clause}", age_clause)
                       .replace("{scale_clause}", scale_clause)
                       .replace("{equity_clause}", equity_clause)
                       .replace("{sort_col}", sort_col)
                       .replace("{order_dir}", order_dir))
                count_sql = (_TP_COUNT_SQL
                             .replace("{alive_clause}", alive_clause)
                             .replace("{age_clause}", age_clause)
                             .replace("{scale_clause}", scale_clause)
                             .replace("{equity_clause}", equity_clause))
                stats_sql = (_TP_STATS_SQL
                             .replace("{xirr_col}", f"w10_xirr{suffix}")
                             .replace("{rounds_col}", f"w10_rounds{suffix}")
                             .replace("{age_clause}", age_clause)
                             .replace("{scale_clause}", scale_clause)
                             .replace("{equity_clause}", equity_clause))
                base = ([tp, q, like, like]
                        + ([cutoff] if cutoff else [])
                        + ([scale_param] if scale_param else [])
                        + ([equity_param] if equity_param else []))
                cur.execute(sql, (*base, limit, offset))
                rows = [dict(zip(_TP_LIST_COLS, r)) for r in cur.fetchall()]
                cur.execute(count_sql, base)
                total = cur.fetchone()[0]
                cur.execute(stats_sql, ([tp] + ([cutoff] if cutoff else [])
                                        + ([scale_param] if scale_param else [])
                                        + ([equity_param] if equity_param else [])))
                s = cur.fetchone()
                cur.execute(_DCA_META_SQL)
                m = cur.fetchone()
        for r in rows:
            for k in _LIST_ROUND4:
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
        return {"status": "up", "tp": tp, "total": total, "limit": limit,
                "offset": offset, "fee": fee_val, "equity": bool(equity),
                "rows": rows, "stats": stats, "meta": meta}
    return _safe(run, {"status": "down", "detail": "pg unreachable",
                       "tp": tp, "total": 0, "limit": limit, "offset": offset,
                       "equity": bool(equity),
                       "rows": [], "stats": None, "meta": None})


def fetch_dca_tp_compare(dsn: str, *, fee: str = "nom",
                         equity: bool = True) -> dict:
    """六档阈值市场级对比聚合（权益类+商品池，alive 且 w10 年化非空宇宙）。

    每阈值一行：样本数 / w10 年化 P25~P90 与均值 / 正收益占比 / 止盈成功率
    （轮次>0 占比）/ 平均轮次 / 全史中位与均值 / 平均止盈间隔。
    fee 切换口径（gross/discard/nom），equity=False 切全市场对照。
    """
    fee_val = fee if fee in _FEE_XIRR_COL else "nom"
    suffix = _LIST_FEE_SUFFIX[fee_val]
    cols = {"wx": f"w10_xirr{suffix}", "fx": f"full_xirr{suffix}",
            "wr": f"w10_rounds{suffix}", "fr": f"full_rounds{suffix}",
            "gap": f"gap_avg_days{suffix}"}

    def run():
        with psycopg.connect(dsn, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                params = None
                eq_clause = ""
                if equity:
                    eq_clause = " AND fund_type = ANY(%s)"
                    params = (list(TP_COMPARE_EQUITY_TYPES),)
                cur.execute(_TP_COMPARE_SQL.replace("{wx}", cols["wx"])
                            .replace("{fx}", cols["fx"])
                            .replace("{wr}", cols["wr"])
                            .replace("{fr}", cols["fr"])
                            .replace("{gap}", cols["gap"])
                            .replace("{equity_clause}", eq_clause), params)
                rows = cur.fetchall()
        out = []
        for r in rows:
            out.append({
                "tp": r[0], "count": r[1],
                "w10_p25": round(r[2], 4) if r[2] is not None else None,
                "w10_median": round(r[3], 4) if r[3] is not None else None,
                "w10_p75": round(r[4], 4) if r[4] is not None else None,
                "w10_p90": round(r[5], 4) if r[5] is not None else None,
                "w10_mean": round(r[6], 4) if r[6] is not None else None,
                "w10_positive_share": round(r[7], 4) if r[7] is not None else None,
                "w10_success_share": round(r[8], 4) if r[8] is not None else None,
                "w10_avg_rounds": round(r[9], 2) if r[9] is not None else None,
                "full_median": round(r[10], 4) if r[10] is not None else None,
                "full_mean": round(r[11], 4) if r[11] is not None else None,
                "full_positive_share": round(r[12], 4) if r[12] is not None else None,
                "full_success_share": round(r[13], 4) if r[13] is not None else None,
                "avg_gap_days": round(r[14], 1) if r[14] is not None else None,
            })
        return {"status": "up", "fee": fee_val, "equity": bool(equity),
                "thresholds": [float(t) for t in TP_THRESHOLDS], "rows": out}
    return _safe(run, {"status": "down", "detail": "pg unreachable",
                       "equity": bool(equity), "thresholds": [],
                       "rows": []})


_TP_ROUNDS_SQL = """
SELECT seq, tp_date, round_days, gap_days, profit,
       tp_nav, invested, sub_fee, redeem_fee, net_profit
FROM fund_dca_tp_rounds WHERE code = %s AND tp = %s AND fee = %s ORDER BY seq
"""


def fetch_dca_tp_rounds(dsn: str, code: str, fee: str = "nom",
                        tp: float = TAKE_PROFIT) -> dict:
    """单基金单阈值每轮止盈明细（fund_dca_tp_rounds）。契约同 fetch_dca_rounds
    + tp 维度回显。"""
    fee_val = fee if fee in _FEE_XIRR_COL else "nom"

    def run():
        with psycopg.connect(dsn, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute(_TP_ROUNDS_SQL, (code, tp, fee_val))
                rows = cur.fetchall()
        out = [{"seq": r[0], "tp_date": r[1].isoformat() if r[1] else None,
                "round_days": r[2], "gap_days": r[3],
                "profit": round(r[4], 4) if r[4] is not None else None,
                "tp_nav": r[5], "invested": r[6],
                "sub_fee": r[7], "redeem_fee": r[8], "net_profit": r[9]}
               for r in rows]
        totals = {
            "rounds": len(out),
            "invested": round(sum(r["invested"] or 0 for r in out), 2),
            "net_profit": round(sum(r["net_profit"] or 0 for r in out), 2),
            "sub_fee": round(sum(r["sub_fee"] or 0 for r in out), 2),
            "redeem_fee": round(sum(r["redeem_fee"] or 0 for r in out), 2),
        }
        return {"status": "up", "code": code, "fee": fee_val, "tp": tp,
                "rounds": out, "totals": totals}
    return _safe(run, {"status": "down", "detail": "pg unreachable",
                       "code": code, "tp": tp, "rounds": [], "totals": {}})


_TP_SUMMARY_SQL = """
SELECT tp, w10_profit_rate, w10_xirr,
       w10_profit_rate_disc, w10_xirr_disc,
       w10_profit_rate_nom, w10_xirr_nom, full_start
FROM fund_dca_tp_result WHERE code = %s ORDER BY tp
"""


def fetch_dca_tp_summary(dsn: str, code: str) -> dict:
    """单基金全阈值总收益率/年化汇总（fund_dca_tp_result，w10 窗口）。

    每阈值一行，恒带三口径字段（gross 原列 / _disc / _nom 后缀）——前端
    弹窗按当前费率口径取值，切口径无需重拉。降级约定同旧端点。
    """

    def run():
        with psycopg.connect(dsn, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute(_DCA_REF_SQL)
                ref = cur.fetchone()[0]
                cur.execute(_TP_SUMMARY_SQL, (code,))
                rows = cur.fetchall()
        out = [{"tp": r[0],
                "w10_profit_rate": round(r[1], 4) if r[1] is not None else None,
                "w10_xirr": round(r[2], 4) if r[2] is not None else None,
                "w10_profit_rate_disc": round(r[3], 4) if r[3] is not None else None,
                "w10_xirr_disc": round(r[4], 4) if r[4] is not None else None,
                "w10_profit_rate_nom": round(r[5], 4) if r[5] is not None else None,
                "w10_xirr_nom": round(r[6], 4) if r[6] is not None else None,
                "w10_start_iso": r[7].isoformat() if r[7] else None,
                "age_years": (round((ref - r[7]).days / 365.25, 1)
                              if ref and r[7] else None)}
               for r in rows]
        return {"status": "up", "code": code, "rows": out}
    return _safe(run, {"status": "down", "detail": "pg unreachable",
                       "code": code, "rows": []})


_TIMING_SQL = """
SELECT t.code, r.name, r.fund_type, r.alive, t.dd_current,
       t.days_since_peak, t.dip_band, r.scale_yi, r.full_start,
       r.w10_start, r.w10_end, r.w10_rounds, r.gap_avg_days,
       r.w10_xirr, r.full_xirr, r.w10_profit_rate,
       r.w10_rounds_disc, r.w10_rounds_nom,
       r.w10_xirr_disc, r.w10_xirr_nom,
       r.full_xirr_disc, r.full_xirr_nom,
       r.w10_profit_rate_disc, r.w10_profit_rate_nom,
       r.gap_avg_days_disc, r.gap_avg_days_nom
FROM fund_dip_timing t
JOIN fund_dca_tp_result r ON r.code = t.code AND r.tp = %s
WHERE (%s = '' OR t.code ILIKE %s OR r.name ILIKE %s)
  AND {band_clause}
  AND {alive_clause}
  AND {age_clause}
  AND {scale_clause}
  AND {equity_clause}
ORDER BY {sort_col} {order_dir} NULLS LAST, t.code
LIMIT %s OFFSET %s
"""

_TIMING_COUNT_SQL = """
SELECT count(*) FROM fund_dip_timing t
JOIN fund_dca_tp_result r ON r.code = t.code AND r.tp = %s
WHERE (%s = '' OR t.code ILIKE %s OR r.name ILIKE %s)
  AND {band_clause}
  AND {alive_clause}
  AND {age_clause}
  AND {scale_clause}
  AND {equity_clause}
"""

# 时机桶白名单（25-30 为验证结论甜蜜点；'' = 全部）
_TIMING_BANDS = ("20-25", "25-30", "30+")
_TIMING_COLS = ["code", "name", "fund_type", "alive", "dd_current",
                "days_since_peak", "dip_band", "scale_yi", "full_start",
                "w10_start", "w10_end", "w10_rounds", "gap_avg_days",
                "w10_xirr", "full_xirr", "w10_profit_rate",
                "w10_rounds_disc", "w10_rounds_nom",
                "w10_xirr_disc", "w10_xirr_nom",
                "full_xirr_disc", "full_xirr_nom",
                "w10_profit_rate_disc", "w10_profit_rate_nom",
                "gap_avg_days_disc", "gap_avg_days_nom"]
_TIMING_SORTABLE = {"w10_xirr", "full_xirr", "w10_profit_rate",
                    "w10_rounds", "gap_avg_days", "dd_current",
                    "days_since_peak", "scale_yi", "code",
                    "full_start", "age_years"}
# r 表（tp_result）上的排序字段（非 fee 口径）；其余白名单字段在 t 表；
# age_years 是运作年限列的展示字段名（列 field），排序落库列即成立日
_TIMING_R_SORTABLE = {"full_start", "age_years"}


def _timing_sort_col(sort_base: str, suffix: str) -> str:
    if sort_base in _FEE_SORTABLE:
        return f"r.{sort_base}{suffix}"
    if sort_base in _TIMING_R_SORTABLE:
        return "r.full_start" if sort_base == "age_years" \
            else f"r.{sort_base}"
    return f"t.{sort_base}"


def fetch_dca_timing(dsn: str, *, tp: float = TAKE_PROFIT, band: str = "25-30",
                     q: str = "", alive: str = "true", sort: str = "w10_xirr",
                     order: str = "desc", min_age: float = 0.0,
                     min_scale: float = 0.0, limit: int = 50, offset: int = 0,
                     fee: str = "gross", equity: bool = True) -> dict:
    """定投时机列表：fund_dip_timing（当前回撤桶）× fund_dca_tp_result（历史
    素质字段，tp/fee 口径同排行端点）。band 白名单（20-25/25-30/30+，'' = 全部，
    乱值回落全部）；排序白名单含 dd_current/days_since_peak（fee 口径字段随
    fee 切后缀）。降级约定同旧端点。

    band 语义（2026-11-16 假设验证）：25-30 = 历史上从该位置开始定投首轮
    止盈最快/成功率与年化最高的"甜蜜点"；30+ = 超深回撤警戒（起不来风险）。
    """
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    like = f"%{q}%"
    fee_val = fee if fee in _LIST_FEE_SUFFIX else "gross"
    suffix = _LIST_FEE_SUFFIX[fee_val]
    band_val = band if band in _TIMING_BANDS else ""
    sort_base = sort if sort in _TIMING_SORTABLE else "w10_xirr"
    sort_col = _timing_sort_col(sort_base, suffix)
    order_dir = "ASC" if order.lower() == "asc" else "DESC"
    alive_clause = ("true" if alive not in ("true", "false")
                    else f"r.alive = {alive.upper()}")
    band_clause = ("true" if not band_val else "t.dip_band = %s")
    equity_clause = "r.fund_type = ANY(%s)" if equity else "true"
    equity_param = list(EQUITY_TYPES) if equity else None

    def run():
        with psycopg.connect(dsn, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute(_DCA_REF_SQL)
                ref = cur.fetchone()[0]
                age_clause, cutoff = "true", None
                if min_age and ref:
                    cutoff = ref - dt.timedelta(days=365.25 * min_age)
                    age_clause = "r.full_start < %s"
                scale_clause = "r.scale_yi > %s" if min_scale else "true"
                scale_param = min_scale if min_scale else None
                sql = (_TIMING_SQL.replace("{band_clause}", band_clause)
                       .replace("{alive_clause}", alive_clause)
                       .replace("{age_clause}", age_clause)
                       .replace("{scale_clause}", scale_clause)
                       .replace("{equity_clause}", equity_clause)
                       .replace("{sort_col}", sort_col)
                       .replace("{order_dir}", order_dir))
                count_sql = (_TIMING_COUNT_SQL
                             .replace("{band_clause}", band_clause)
                             .replace("{alive_clause}", alive_clause)
                             .replace("{age_clause}", age_clause)
                             .replace("{scale_clause}", scale_clause)
                             .replace("{equity_clause}", equity_clause))
                base = ([tp, q, like, like]
                        + ([band_val] if band_val else [])
                        + ([cutoff] if cutoff else [])
                        + ([scale_param] if scale_param else [])
                        + ([equity_param] if equity_param else []))
                cur.execute(sql, (*base, limit, offset))
                rows = cur.fetchall()
                cur.execute(count_sql, base)
                total = cur.fetchone()[0]
        out = [dict(zip(_TIMING_COLS, r)) for r in rows]
        for r in out:
            r["tp"] = tp
            for k in _LIST_ROUND4:
                if k in r:
                    r[k] = round(r[k], 4) if r[k] is not None else None
            r["scale_yi"] = (round(r["scale_yi"], 2)
                             if r["scale_yi"] is not None else None)
            r["age_years"] = (round((ref - r["full_start"]).days / 365.25, 1)
                              if ref and r["full_start"] else None)
            r.pop("full_start")
            for k in ("w10_start", "w10_end"):
                if r.get(k):
                    r[k] = r[k].isoformat()
        return {"status": "up", "tp": tp, "band": band_val, "total": total,
                "limit": limit, "offset": offset, "fee": fee_val,
                "equity": bool(equity), "rows": out}
    return _safe(run, {"status": "down", "detail": "pg unreachable",
                       "tp": tp, "band": band, "total": 0, "limit": limit,
                       "offset": offset, "equity": bool(equity),
                       "rows": []})


_DIP_REPORT_SQL = """
SELECT grp, bucket, tp, n, days_median, days_p25, days_p75,
       success_rate, xirr_median, xirr_p25, xirr_p75
FROM fund_dip_validation ORDER BY grp, bucket, tp
"""


def fetch_dca_dip_report(dsn: str) -> dict:
    """起点条件回测报告（假设验证统计，scratch/dip_validation.py 落库）。

    按 grp+bucket 形态分拣：groups = 三组对照（SYS 随机基线 / NODIP 非深
    回撤 / DIP 深回撤，bucket='all'）；buckets = DIP 按事件最大回撤分桶；
    years = SYS 按起点年份。静态统计快照（验证重跑后刷新），只读全表。
    降级约定同旧端点。
    """

    def run():
        with psycopg.connect(dsn, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute(_DIP_REPORT_SQL)
                rows = cur.fetchall()
        groups, buckets, years, tp_list = [], [], [], []
        for (grp, bucket, tp, n, dm, dp, dp75, sr, xm, xp, xp75) in rows:
            item = {"grp": grp, "bucket": bucket, "tp": tp, "n": n,
                    "days_median": dm, "days_p25": dp, "days_p75": dp75,
                    "success_rate": round(sr, 4) if sr is not None else None,
                    "xirr_median": round(xm, 4) if xm is not None else None,
                    "xirr_p25": round(xp, 4) if xp is not None else None,
                    "xirr_p75": round(xp75, 4) if xp75 is not None else None}
            if bucket == "all" and grp in ("SYS", "NODIP", "DIP"):
                groups.append(item)
            elif grp == "DIP":
                buckets.append(item)
            elif grp == "SYS":
                years.append(item)
            if tp not in tp_list:
                tp_list.append(tp)
        return {"status": "up", "tp_list": sorted(tp_list),
                "groups": groups, "buckets": buckets, "years": years}
    return _safe(run, {"status": "down", "detail": "pg unreachable",
                       "tp_list": [], "groups": [], "buckets": [],
                       "years": []})