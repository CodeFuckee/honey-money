"""定投止盈回测结果查询 API（/api/funds/dca）。

数据来自 fund_dca_result 快照（scripts 离线回测导入，见 dca_store），
只读 + _safe 降级约定同 browse：PG 不可达/表不存在是"状态"（down/空）
而不是错误，接口始终 200。
"""

import datetime as dt

import psycopg

from . import config
from . import bear_pause                              # 断缴实验常量（案例码）
from .dca_backtest import (DCA_METHODS, TAKE_PROFIT,   # 阈值/定投方式事实源单一
                           TP_THRESHOLDS, BACKTEST_UNIVERSE_TYPES)

_DCA_META_SQL = """
SELECT generated_at, fund_count, params FROM fund_dca_meta
"""

# 参考日=tp 表全表 max(full_end)（2026-11-26 二十二轮：旧口径表退役，
# 单一数据源；旧口径行与新引擎 tp=0.20/amount 行同源同值）
_DCA_REF_SQL = "SELECT max(full_end) FROM fund_dca_tp_result"

# 排序白名单：前端传列名，只允许这些键进 ORDER BY（防注入）
_SORTABLE = {
    "full_xirr", "full_profit_rate", "full_profit_amt",
    "w10_rounds", "full_rounds", "scale_yi", "code", "full_start",
    "gap_avg_days", "age_years",   # 运作年限列 field 别名 → ORDER BY full_start
    "dd_current", "days_since_peak",   # 时机列（LEFT JOIN fund_dip_timing）
    "vol_all", "vol_down_all", "mdd_all", "sharpe_all", "roll_vol",
    "vol_pctile",   # 波动指标列（LEFT JOIN fund_risk_metrics，全史口径）
    "risk_score",   # 综合波动分位（三指标横截面等权，越低越稳）
    "osc_pctile",   # 震荡分位（zigzag ±10% 频率×规则度，越高越像余弦）
    "full_cur_xirr", "full_cur_profit_rate",   # 止盈后年化/收益率（全史当前轮）
}
# 费用口径后缀（gross=不含费沿用原列；disc=申购一折；nom=申购名义）。
# fee 只影响 ORDER BY 与统计卡的列替换；SELECT 恒返回全部口径列，
# 前端按行内后缀字段自取（与静态快照同一份行结构）
_LIST_FEE_SUFFIX = {"gross": "", "disc": "_disc", "nom": "_nom"}
# 这些排序字段区分口径；scale_yi / code / full_start 与费用无关
_FEE_SORTABLE = {"full_xirr", "full_profit_rate",
                 "w10_rounds", "full_rounds", "gap_avg_days",
                 "full_cur_xirr", "full_cur_profit_rate"}
# 排序落在时机表（t 别名）的字段；其余白名单字段在主表（r 别名）
_T_SORTABLE = {"dd_current", "days_since_peak"}
# 排序落在波动指标表（rm 别名）的字段（费用口径无关，同 scale_yi）
_RM_SORTABLE = {"vol_all", "vol_down_all", "mdd_all", "sharpe_all",
                "roll_vol", "vol_pctile", "risk_score",
                "osc_per_year", "osc_pctile"}


def _list_sort_col(sort_base: str, suffix: str) -> str:
    """排行列表 ORDER BY 列：fee 口径字段带后缀，时机字段落 t 表、波动指标
    字段落 rm 表，其余 r 表。full_profit_amt（总收益额）为派生排序：落
    总收益率{口径后缀}×总投入 表达式（总投入与费率无关，2026-11-27）。"""
    if sort_base == "full_profit_amt":
        return f"r.full_profit_rate{suffix} * r.full_invested"
    if sort_base in _FEE_SORTABLE:
        return f"r.{sort_base}{suffix}"
    if sort_base in _T_SORTABLE:
        return f"t.{sort_base}"
    if sort_base in _RM_SORTABLE:
        return f"rm.{sort_base}"
    return f"r.{sort_base}"


def _range_filters(*, ref, suffix, age_min=None, age_max=None,
                   gap_min=None, gap_max=None, xirr_min=None, xirr_max=None,
                   dd_min=None, dd_max=None, pr_min=None, pr_max=None):
    """五维度区间筛选 → (SQL 片段列表, 参数列表)；全部可选，None=不筛。

    age：运作年限边界 → 成立日距参考日偏移；gap/xirr/pr 按费用口径后缀列；
    dd：用户输入正数回撤幅度，后端取负与 dd_current（负数）比较
    （NULL=无时机行，不满足任何区间条件，如实排除）。
    """
    clauses, params = [], []

    def _between(col, lo, hi):
        if lo is not None and hi is not None:
            clauses.append(f"{col} BETWEEN %s AND %s")
            params.extend([lo, hi])
        elif lo is not None:
            clauses.append(f"{col} >= %s")
            params.append(lo)
        elif hi is not None:
            clauses.append(f"{col} <= %s")
            params.append(hi)

    if age_min is not None and ref:
        clauses.append("r.full_start <= %s")
        params.append(ref - dt.timedelta(days=365.25 * age_min))
    if age_max is not None and ref:
        clauses.append("r.full_start >= %s")
        params.append(ref - dt.timedelta(days=365.25 * age_max))
    _between(f"r.gap_avg_days{suffix}", gap_min, gap_max)
    _between(f"r.full_xirr{suffix}", xirr_min, xirr_max)
    if dd_min is not None:
        clauses.append("t.dd_current <= %s")
        params.append(-dd_min / 100.0)   # 25 → -0.25
    if dd_max is not None:
        clauses.append("t.dd_current >= %s")
        params.append(-dd_max / 100.0)
    _between(f"r.full_profit_rate{suffix}", pr_min, pr_max)
    return clauses, params

# 权益类白名单（东财 fund_type）：股票/偏股/灵活/平衡混合 + A股股票指数
# + 海外权益（海外股票指数、QDII 股票/QDII 偏股）。债券、FOF、货币、偏债、
# 商品、REITs 不在内。口径 2026-11-15 用户确认（全市场开关默认仅权益类）；
# 2026-11-25 十四轮起与回测宇宙白名单同一事实源（引擎
# BACKTEST_UNIVERSE_TYPES 的别名——非权益/年轻基金已不产生回测结果行）。
EQUITY_TYPES = BACKTEST_UNIVERSE_TYPES
_EQUITY_TRUTHY = {"true", "1", "yes", "on"}


def _parse_equity(raw) -> bool:
    """equity 查询参数 → bool；仅真值字面量为仅权益类，其余（含乱值）=全市场。"""
    return str(raw).strip().lower() in _EQUITY_TRUTHY
# 行映射列清单（与 _DCA_LIST_SQL 的 SELECT 列一一对应，dict(zip) 用）；
# 恒含三口径后缀列——fee 只切换排序/统计列，行结构全口径
_LIST_COLS = ["code", "name", "fund_type", "alive", "scale_yi",
              "scale_date", "w10_start", "w10_end", "w10_months",
              "w10_rounds", "w10_invested", "w10_final_value",
              "full_xirr",
              "full_rounds", "full_profit_rate", "full_invested",
              "full_months",
              "full_start", "gap_avg_days",
              "full_xirr_disc",
              "full_xirr_nom", "w10_rounds_disc", "w10_rounds_nom",
              "full_rounds_disc", "full_rounds_nom",
              "full_profit_rate_disc", "full_profit_rate_nom",
              "gap_avg_days_disc", "gap_avg_days_nom",
              # 时机列（LEFT JOIN fund_dip_timing；非在市/未刷新基金为 NULL）
              "dd_current", "days_since_peak", "dip_band",
              # 波动指标列（LEFT JOIN fund_risk_metrics；全史口径，缺行为 NULL）
              "vol_all", "vol_down_all", "mdd_all", "sharpe_all",
              "roll_vol", "vol_pctile", "risk_score",
              "osc_per_year", "osc_pctile",
              # 「最近一次止盈后」当前轮（全史窗口；未止盈基金全 NULL）
              "full_last_tp", "full_last_tp_disc", "full_last_tp_nom",
              "full_cur_invested",
              "full_cur_profit_rate", "full_cur_profit_rate_disc",
              "full_cur_profit_rate_nom",
              "full_cur_xirr", "full_cur_xirr_disc", "full_cur_xirr_nom"]
# 浮点保留 4 位的字段（口径变体与原字段同规则）
_LIST_ROUND4 = ("full_xirr",
                "full_profit_rate",
                "full_xirr_disc", "full_xirr_nom",
                "full_profit_rate_disc", "full_profit_rate_nom",
                "vol_all", "vol_down_all", "mdd_all", "sharpe_all",
                "roll_vol", "vol_pctile", "risk_score",
                "osc_per_year", "osc_pctile",
                "full_cur_profit_rate", "full_cur_profit_rate_disc",
                "full_cur_profit_rate_nom",
                "full_cur_xirr", "full_cur_xirr_disc", "full_cur_xirr_nom")


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


def register_fund_dca_routes(app):
    @app.get("/api/funds/dca/scatter")
    def funds_dca_scatter(fee: str = "nom", equity: str = "true",
                          method: str = "amount", tp: str = "0.2"):
        """三维散点：每只在市基金一点 (运作年限, 规模亿, 年化) + 年限段 ×
        规模段分箱统计（每基金只计入唯一格子）。fee 选年化口径：
        gross=不含费 / disc=含费·申购一折 / nom=含费·申购名义（默认），
        非白名单值回落 nom；equity=true 默认仅权益类（false/其他=全市场）；
        method/tp 跟随页面顶部总开关（定投方式/止盈点，白名单外各回落
        amount/0.2——amount 且 20% 走旧表零回归，其余走 tp 表按档过滤）。
        降级约定同 /api/funds/dca。
        """
        return fetch_dca_scatter(config.DATABASE_URL, fee=fee.strip().lower(),
                                 equity=_parse_equity(equity),
                                 method=_parse_method(method),
                                 tp=_parse_tp(tp.strip().lower()))

    @app.get("/api/funds/dca/tp")
    def funds_dca_tp(tp: str = "0.2", q: str = "", alive: str = "true",
                     sort: str = "full_xirr", order: str = "desc",
                     min_age: float = 0.0, min_scale: float = 0.0,
                     limit: int = 50, offset: int = 0, fee: str = "gross",
                     equity: str = "true", method: str = "amount",
                     age_min: float = None, age_max: float = None,
                     gap_min: float = None, gap_max: float = None,
                     xirr_min: float = None, xirr_max: float = None,
                     dd_min: float = None, dd_max: float = None,
                     pr_min: float = None, pr_max: float = None):
        """定投止盈回测排行（多阈值版，fund_dca_tp_result 快照）。

        tp 选止盈点（0.05~0.40，白名单外回落 0.2）；method 选定投方式
        （amount 等额 / share 等份额，白名单外回落 amount）；其余参数与
        降级约定同 /api/funds/dca（含五维度区间筛选，权益开关沿用
        EQUITY_TYPES 8 类口径，与旧排行一致）。统计卡跟随 tp/method 与
        各筛选、费用口径。
        """
        return fetch_dca_tp_list(config.DATABASE_URL,
                                 tp=_parse_tp(tp.strip().lower()),
                                 q=q, alive=alive.strip().lower(),
                                 sort=sort, order=order.strip().lower(),
                                 min_age=max(0.0, min_age),
                                 min_scale=max(0.0, min_scale),
                                 limit=limit, offset=offset,
                                 fee=fee.strip().lower(),
                                 equity=_parse_equity(equity),
                                 method=_parse_method(method),
                                 age_min=age_min, age_max=age_max,
                                 gap_min=gap_min, gap_max=gap_max,
                                 xirr_min=xirr_min, xirr_max=xirr_max,
                                 dd_min=dd_min, dd_max=dd_max,
                                 pr_min=pr_min, pr_max=pr_max)

    @app.get("/api/funds/dca/tp/compare")
    def funds_dca_tp_compare(fee: str = "nom", equity: str = "true",
                             method: str = "amount"):
        """八档止盈点市场级对比聚合（5%~40%）：每阈值样本数 / w10 年化
        P25~P90/均值 / 正收益占比 / 止盈成功率 / 平均轮次 / 全史中位均值 /
        平均止盈间隔。默认权益类+商品池（TP_COMPARE_EQUITY_TYPES，
        2026-11-15 用户确认口径），equity=false 全市场对照；fee 切换口径、
        method 切定投方式（跟随页面级 tab）。降级约定同 /api/funds/dca。
        """
        return fetch_dca_tp_compare(config.DATABASE_URL,
                                    fee=fee.strip().lower(),
                                    equity=_parse_equity(equity),
                                    method=_parse_method(method))

    @app.get("/api/funds/dca/tp/{code}/rounds")
    def funds_dca_tp_rounds(code: str, fee: str = "nom", tp: str = "0.2",
                            method: str = "amount"):
        """单基金单阈值每轮止盈明细（契约同 /api/funds/dca/{code}/rounds
        + tp/method 维度；白名单外各回落 0.2 / amount）。降级约定同
        /api/funds/dca。"""
        return fetch_dca_tp_rounds(config.DATABASE_URL, code,
                                   fee=fee.strip().lower(),
                                   tp=_parse_tp(tp.strip().lower()),
                                   method=_parse_method(method))

    @app.get("/api/funds/dca/tp/{code}/summary")
    def funds_dca_tp_summary(code: str, method: str = "amount"):
        """单基金全阈值总收益率/年化汇总（近 10 年窗口，每阈值一行恒带
        三口径字段——前端弹窗按当前费率口径取值；method 跟随页面级定投
        方式 tab）。降级约定同 /api/funds/dca。"""
        return fetch_dca_tp_summary(config.DATABASE_URL, code.strip(),
                                    method=_parse_method(method))

    @app.get("/api/funds/dca/timing")
    def funds_dca_timing(tp: str = "0.2", band: str = "25-30", q: str = "",
                         alive: str = "true", sort: str = "full_xirr",
                         order: str = "desc", min_age: float = 0.0,
                         min_scale: float = 0.0, limit: int = 50,
                         offset: int = 0, fee: str = "gross",
                         equity: str = "true",
                         age_min: float = None, age_max: float = None,
                         gap_min: float = None, gap_max: float = None,
                         xirr_min: float = None, xirr_max: float = None,
                         dd_min: float = None, dd_max: float = None,
                         pr_min: float = None, pr_max: float = None):
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
                                equity=_parse_equity(equity),
                                age_min=age_min, age_max=age_max,
                                gap_min=gap_min, gap_max=gap_max,
                                xirr_min=xirr_min, xirr_max=xirr_max,
                                dd_min=dd_min, dd_max=dd_max,
                                pr_min=pr_min, pr_max=pr_max)

    @app.get("/api/funds/dca/dip-report")
    def funds_dca_dip_report():
        """起点条件回测报告（假设验证统计：三组对照/回撤深度梯度/起点年份，
        各组首轮止盈天数分位、3 年成功率、首轮年化分位）。静态快照，验证
        重跑后刷新。历史统计不构成操作建议。降级约定同 /api/funds/dca。"""
        return fetch_dca_dip_report(config.DATABASE_URL)

    @app.get("/api/funds/dca/pause-experiment")
    def funds_dca_pause_experiment():
        """熊市断缴实验市场级统计（2026-11-19 scratch 实验产品化，导入
        scripts/bear_pause_import.py 落库）：子样本（全体/仅断缴/末回撤
        三分层）× 口径（止盈20%/无止盈）的 A/B/C/D 配对比较 + 案例基金 +
        实验参数 meta。静态快照，重跑导入脚本后刷新；历史统计不构成操作
        建议。降级约定同 /api/funds/dca。"""
        return fetch_dca_pause_experiment(config.DATABASE_URL)

    @app.get("/api/funds/dca/bear-pause/{code}")
    def funds_dca_bear_pause(code: str):
        """单基金熊市断缴实验四组对照（A 坚持/B 断缴闲置/C 断缴补投/
        D 熊市加倍；全史·gross 无费·双口径）。降级约定同 /api/funds/dca。"""
        return fetch_bear_pause_fund(config.DATABASE_URL, code.strip())



# ===== 三维散点（每基金一点）+ 年限段 × 规模段 分箱统计 =====
AGE_BANDS = [(0, 1), (1, 3), (3, 5), (5, 10), (10, None)]     # 运作年限段（年）
SCALE_BANDS = [(0, 1), (1, 10), (10, 100), (100, None)]       # 规模段（亿元）

# 年化口径 → 结果表列名（gross=不含费原口径；disc/nom 含费，赎回费一致）；
# 2026-11-25 十七轮起年化统一全史口径（w10 年化指标退役）
_FEE_XIRR_COL = {"gross": "full_xirr", "disc": "full_xirr_disc",
                 "nom": "full_xirr_nom"}


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


def fetch_dca_scatter(dsn: str, fee: str = "nom", equity: bool = True,
                      method: str = "amount", tp: float = TAKE_PROFIT) -> dict:
    """三维散点数据：每只在市基金一点 (运作年限, 规模亿, 年化) + 分箱统计。

    fee 选择年化口径：gross=不含费 / disc=含费·申购一折 / nom=含费·申购名义
    （赎回费三口径一致收取）；equity=True（默认）仅权益类白名单；
    method 切定投方式、tp 切止盈档（均跟随页面顶部总开关，2026-11-23 三轮
    需求：切换后散点/分箱跟随）——2026-11-26 二十二轮起恒走
    fund_dca_tp_result 按 tp+method 过滤（旧表退役，单一数据源）。
    响应带 fee / method / tp / equity 回显。点数组
    [age, scale, xirr, code, name, fund_type]，规模取对数轴定位由前端处理；
    降级约定同 /api/funds/dca。
    """
    xirr_col = _FEE_XIRR_COL.get(fee, "full_xirr_nom")
    method_val = method if method in DCA_METHODS else "amount"
    tp_val = next((t for t in TP_THRESHOLDS if abs(tp - t) < 1e-9),
                  TAKE_PROFIT)

    def run():
        with psycopg.connect(dsn, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute(_DCA_REF_SQL)
                ref = cur.fetchone()[0]
                sql = (f"SELECT code, name, fund_type, scale_yi, "
                       f"full_start, {xirr_col}, w10_rounds, full_end "
                       f"FROM fund_dca_tp_result "
                       f"WHERE alive AND tp = %s AND method = %s "
                       f"AND {xirr_col} IS NOT NULL "
                       f"AND full_start IS NOT NULL AND scale_yi IS NOT NULL")
                params = [tp_val, method_val]
                if equity:
                    sql += " AND fund_type = ANY(%s)"
                    params.append(list(EQUITY_TYPES))
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
                "method": method_val, "tp": tp_val, "equity": bool(equity),
                "ref_date": ref.isoformat() if ref else None,
                "points": points, **bands}
    return _safe(run, {"status": "down", "detail": "pg unreachable",
                       "method": method_val, "tp": tp_val,
                       "equity": bool(equity),
                       "points": [],
                       "age_bands": [], "scale_bands": [], "matrix": []})


# ===== 多止盈点市场级对比（fund_dca_tp_* 快照，2026-11-15 grilling 定案） =====
# 排行阈值切换 / 阈值轮次明细 / 八档对比聚合；降级约定与旧端点一致
# （始终 200，白名单外回落 20%）。散点/分箱走自己的端点：止盈点固定
# tp=0.2 档、定投方式 method 跟随页面级 tab（二十二轮起全部恒走 tp 表）。

_TP_LIST_SQL = """
SELECT r.tp, r.code, r.name, r.fund_type, r.alive, r.scale_yi, r.scale_date,
       r.w10_start, r.w10_end, r.w10_months, r.w10_rounds,
       r.w10_invested, r.w10_final_value,
       r.full_xirr, r.full_rounds, r.full_profit_rate, r.full_invested,
       r.full_months,
       r.full_start, r.gap_avg_days,
       r.full_xirr_disc, r.full_xirr_nom,
       r.w10_rounds_disc, r.w10_rounds_nom, r.full_rounds_disc, r.full_rounds_nom,
       r.full_profit_rate_disc, r.full_profit_rate_nom,
       r.gap_avg_days_disc, r.gap_avg_days_nom,
       t.dd_current, t.days_since_peak, t.dip_band,
       rm.vol_all, rm.vol_down_all, rm.mdd_all, rm.sharpe_all,
       rm.roll_vol, rm.vol_pctile, rm.risk_score,
       rm.osc_per_year, rm.osc_pctile,
       r.full_last_tp, r.full_last_tp_disc, r.full_last_tp_nom,
       r.full_cur_invested,
       r.full_cur_profit_rate, r.full_cur_profit_rate_disc,
       r.full_cur_profit_rate_nom,
       r.full_cur_xirr, r.full_cur_xirr_disc, r.full_cur_xirr_nom
FROM fund_dca_tp_result r
LEFT JOIN fund_dip_timing t ON t.code = r.code
LEFT JOIN fund_risk_metrics rm ON rm.code = r.code
WHERE r.tp = %s
  AND r.method = %s
  AND (%s = '' OR r.code ILIKE %s OR r.name ILIKE %s)
  AND {alive_clause}
  AND {age_clause}
  AND {scale_clause}
  AND {equity_clause}
  AND {range_clause}
ORDER BY {sort_col} {order_dir} NULLS LAST, r.code
LIMIT %s OFFSET %s
"""

_TP_COUNT_SQL = """
SELECT count(*) FROM fund_dca_tp_result r
LEFT JOIN fund_dip_timing t ON t.code = r.code
WHERE r.tp = %s
  AND r.method = %s
  AND (%s = '' OR r.code ILIKE %s OR r.name ILIKE %s)
  AND {alive_clause}
  AND {age_clause}
  AND {scale_clause}
  AND {equity_clause}
  AND {range_clause}
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
       avg(({rounds_col} > 0)::int)::float,
       percentile_cont(0.25) WITHIN GROUP (ORDER BY {pr_col}),
       percentile_cont(0.5)  WITHIN GROUP (ORDER BY {pr_col}),
       percentile_cont(0.75) WITHIN GROUP (ORDER BY {pr_col}),
       percentile_cont(0.9)  WITHIN GROUP (ORDER BY {pr_col}),
       avg(({pr_col} > 0)::int)::float
FROM fund_dca_tp_result r
WHERE r.alive AND r.tp = %s AND r.method = %s AND {xirr_col} IS NOT NULL
  AND {age_clause}
  AND {scale_clause}
  AND {equity_clause}
"""

_TP_LIST_COLS = ["tp"] + _LIST_COLS

_TP_COMPARE_SQL = """
SELECT tp, count(*),
       percentile_cont(0.5) WITHIN GROUP (ORDER BY {fx}),
       avg({fx}), avg(({fx} > 0)::int)::float,
       avg(({wr} > 0)::int)::float, avg({wr}),
       avg({gap}),
       percentile_cont(0.5) WITHIN GROUP (ORDER BY {pr}),
       avg({pr}),
       percentile_cont(0.5) WITHIN GROUP (ORDER BY {pr} * {inv}),
       avg({pr} * {inv})
FROM fund_dca_tp_result
WHERE alive AND method = %s AND {fx} IS NOT NULL{equity_clause}
GROUP BY tp ORDER BY tp
"""

# 对比池口径（2026-11-15 用户确认）：权益 8 类白名单 + QDII-商品 + 商品；
# 排行切换端点仍用 EQUITY_TYPES（与既有权益开关同口径）
TP_COMPARE_EQUITY_TYPES = EQUITY_TYPES + ("QDII-商品", "商品")

# 对比聚合快照（fund_dca_tp_compare，日更导入后 dca_store.rebuild_tp_compare
# 重建）：值已按现算同精度圆整，读出即返回；computed_at 随行取 max
_TP_COMPARE_SNAP_SQL = """
SELECT tp, sample_count, median, mean, positive_share, success_share,
       avg_rounds, avg_gap_days,
       median_profit_rate, mean_profit_rate,
       median_profit_amt, mean_profit_amt, computed_at
FROM fund_dca_tp_compare
WHERE method = %s AND equity = %s AND fee = %s ORDER BY tp
"""


def _parse_tp(raw) -> float:
    """tp 查询参数 → 白名单阈值；乱值/越界回落 20%（与 sort/fee 同约定）。"""
    try:
        v = round(float(str(raw).strip()), 2)
    except (TypeError, ValueError):
        return TAKE_PROFIT
    return next((t for t in TP_THRESHOLDS if abs(v - t) < 1e-9), TAKE_PROFIT)


def _parse_method(raw) -> str:
    """method 查询参数 → 定投方式白名单（DCA_METHODS）；乱值回落 amount。"""
    v = str(raw or "").strip().lower()
    return v if v in DCA_METHODS else "amount"


def fetch_dca_tp_list(dsn: str, *, tp: float = TAKE_PROFIT, q: str = "",
                      alive: str = "true", sort: str = "full_xirr",
                      order: str = "desc", min_age: float = 0.0,
                      min_scale: float = 0.0, limit: int = 50, offset: int = 0,
                      fee: str = "gross", equity: bool = True,
                      method: str = "amount",
                      age_min: float = None, age_max: float = None,
                      gap_min: float = None, gap_max: float = None,
                      xirr_min: float = None, xirr_max: float = None,
                      dd_min: float = None, dd_max: float = None,
                      pr_min: float = None, pr_max: float = None) -> dict:
    """多阈值排行：fetch_dca_list 同款契约 + tp/method 维度（fund_dca_tp_result）。

    tp 为白名单阈值（0.05~0.40）、method 为定投方式白名单（DCA_METHODS，
    amount 等额 / share 等份额）；排序/统计/筛选口径与旧端点逐项一致
    （含五维度区间筛选与时机列 dd_current/days_since_peak/dip_band），
    行结构多 tp 一列。响应回显 tp / method / fee / equity。
    """
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    like = f"%{q}%"
    fee_val = fee if fee in _LIST_FEE_SUFFIX else "gross"
    suffix = _LIST_FEE_SUFFIX[fee_val]
    sort_base = sort if sort in _SORTABLE else "full_xirr"
    sort_col = _list_sort_col(sort_base, suffix)
    order_dir = "ASC" if order.lower() == "asc" else "DESC"
    alive_clause = ("true" if alive not in ("true", "false")
                    else f"r.alive = {alive.upper()}")
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
                range_clauses, range_params = _range_filters(
                    ref=ref, suffix=suffix, age_min=age_min, age_max=age_max,
                    gap_min=gap_min, gap_max=gap_max, xirr_min=xirr_min,
                    xirr_max=xirr_max, dd_min=dd_min, dd_max=dd_max,
                    pr_min=pr_min, pr_max=pr_max)
                range_clause = (" AND ".join(range_clauses)
                                if range_clauses else "true")
                sql = (_TP_LIST_SQL.replace("{alive_clause}", alive_clause)
                       .replace("{age_clause}", age_clause)
                       .replace("{scale_clause}", scale_clause)
                       .replace("{equity_clause}", equity_clause)
                       .replace("{range_clause}", range_clause)
                       .replace("{sort_col}", sort_col)
                       .replace("{order_dir}", order_dir))
                count_sql = (_TP_COUNT_SQL
                             .replace("{alive_clause}", alive_clause)
                             .replace("{age_clause}", age_clause)
                             .replace("{scale_clause}", scale_clause)
                             .replace("{equity_clause}", equity_clause)
                             .replace("{range_clause}", range_clause))
                stats_sql = (_TP_STATS_SQL
                             .replace("{xirr_col}", f"r.full_xirr{suffix}")
                             .replace("{rounds_col}", f"r.w10_rounds{suffix}")
                             .replace("{pr_col}", f"r.full_profit_rate{suffix}")
                             .replace("{age_clause}", age_clause)
                             .replace("{scale_clause}", scale_clause)
                             .replace("{equity_clause}", equity_clause))
                base = ([tp, method, q, like, like]
                        + ([cutoff] if cutoff else [])
                        + ([scale_param] if scale_param else [])
                        + ([equity_param] if equity_param else [])
                        + range_params)
                cur.execute(sql, (*base, limit, offset))
                rows = [dict(zip(_TP_LIST_COLS, r)) for r in cur.fetchall()]
                cur.execute(count_sql, base)
                total = cur.fetchone()[0]
                cur.execute(stats_sql,
                            ([tp, method] + ([cutoff] if cutoff else [])
                             + ([scale_param] if scale_param else [])
                             + ([equity_param] if equity_param else [])))
                s = cur.fetchone()
                cur.execute(_DCA_META_SQL)
                m = cur.fetchone()
        for r in rows:
            for k in _LIST_ROUND4:
                r[k] = round(r[k], 4) if r[k] is not None else None
            r["scale_yi"] = round(r["scale_yi"], 2) if r["scale_yi"] is not None else None
            # 总收益额 = 总收益率×总投入（前端行内计算）的分母，2 位即可
            r["full_invested"] = (round(r["full_invested"], 2)
                                  if r["full_invested"] is not None else None)
            r["age_years"] = (round((ref - r["full_start"]).days / 365.25, 1)
                              if ref and r["full_start"] else None)
            r.pop("full_start", None)
            for k in ("scale_date", "w10_start", "w10_end",
                      "full_last_tp", "full_last_tp_disc", "full_last_tp_nom"):
                if r[k]:
                    r[k] = r[k].isoformat()
        stats = None
        if s and s[0]:
            stats = {"count": s[0], "p25": s[1], "median": s[2],
                     "p75": s[3], "p90": s[4],
                     "positive_share": s[5], "take_profit_share": s[6],
                     # 总收益率五分位（2026-11-27，全史窗口，与年化同宇宙）
                     "pr_p25": s[7], "pr_median": s[8], "pr_p75": s[9],
                     "pr_p90": s[10], "pr_positive_share": s[11]}
        meta = None
        if m:
            meta = {"generated_at": m[0].isoformat() if m[0] else None,
                    "fund_count": m[1], "params": m[2]}
        return {"status": "up", "tp": tp, "method": method, "total": total,
                "limit": limit, "offset": offset, "fee": fee_val,
                "equity": bool(equity),
                "rows": rows, "stats": stats, "meta": meta}
    return _safe(run, {"status": "down", "detail": "pg unreachable",
                       "tp": tp, "method": method,
                       "total": 0, "limit": limit, "offset": offset,
                       "equity": bool(equity),
                       "rows": [], "stats": None, "meta": None})


def fetch_dca_tp_compare(dsn: str, *, fee: str = "nom", equity: bool = True,
                         method: str = "amount") -> dict:
    """八档阈值市场级对比聚合（权益类+商品池，alive 且 w10 年化非空宇宙）。

    每阈值一行：样本数 / 年化中位与均值 / 正收益占比 / 止盈成功率
    （轮次>0 占比）/ 平均轮次 / 平均止盈间隔 / 全史总收益率中位与均值 /
    总收益额中位与均值（额=率×总投入，总投入与费率无关，2026-11-27）。
    fee 切换口径（gross/disc/nom），equity=False 切全市场对照，
    method 切定投方式（DCA_METHODS）。

    优先读 fund_dca_tp_compare 快照（日更导入后 dca_store.rebuild_tp_compare
    顺带重建，240 行小表毫秒级；生产百万行现算实测 ~2.2s）；快照缺表或该
    组合不满八档回落现算聚合（口径同前）。响应新增 source（snapshot/live）
    与 computed_at（快照重建时间 ISO 串，live 为 None）键，前端不读取，
    形状向后兼容。降级约定同 /api/funds/dca。
    """
    fee_val = fee if fee in _FEE_XIRR_COL else "nom"
    method_val = method if method in DCA_METHODS else "amount"
    suffix = _LIST_FEE_SUFFIX[fee_val]
    cols = {"fx": f"full_xirr{suffix}",
            "wr": f"w10_rounds{suffix}", "fr": f"full_rounds{suffix}",
            "gap": f"gap_avg_days{suffix}",
            "pr": f"full_profit_rate{suffix}", "inv": "full_invested"}

    def _snap_rows_to_out(snap):
        return [{"tp": r[0], "count": r[1],
                 "median": r[2], "mean": r[3],
                 "positive_share": r[4], "success_share": r[5],
                 "avg_rounds": r[6], "avg_gap_days": r[7],
                 "median_profit_rate": r[8], "mean_profit_rate": r[9],
                 "median_profit_amt": r[10], "mean_profit_amt": r[11]}
                for r in snap]

    def run():
        # 快照优先：缺表（部署早期）/库不可达一律回落现算，绝不 500
        snap = None
        try:
            with psycopg.connect(dsn, connect_timeout=3) as conn:
                with conn.cursor() as cur:
                    cur.execute(_TP_COMPARE_SNAP_SQL,
                                (method_val, bool(equity), fee_val))
                    snap = cur.fetchall()
        except psycopg.Error:
            snap = None
        if snap is not None and len(snap) == len(TP_THRESHOLDS):
            computed = max((r[12] for r in snap if r[12] is not None),
                           default=None)
            return {"status": "up", "fee": fee_val, "method": method_val,
                    "equity": bool(equity), "source": "snapshot",
                    "computed_at": computed.isoformat() if computed else None,
                    "thresholds": [float(t) for t in TP_THRESHOLDS],
                    "rows": _snap_rows_to_out(snap)}
        with psycopg.connect(dsn, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                params = [method_val]
                eq_clause = ""
                if equity:
                    eq_clause = " AND fund_type = ANY(%s)"
                    params.append(list(TP_COMPARE_EQUITY_TYPES))
                cur.execute(_TP_COMPARE_SQL.replace("{fx}", cols["fx"])
                            .replace("{wr}", cols["wr"])
                            .replace("{fr}", cols["fr"])
                            .replace("{gap}", cols["gap"])
                            .replace("{pr}", cols["pr"])
                            .replace("{inv}", cols["inv"])
                            .replace("{equity_clause}", eq_clause), params)
                rows = cur.fetchall()
        out = []
        for r in rows:
            out.append({
                "tp": r[0], "count": r[1],
                "median": round(r[2], 4) if r[2] is not None else None,
                "mean": round(r[3], 4) if r[3] is not None else None,
                "positive_share": round(r[4], 4) if r[4] is not None else None,
                "success_share": round(r[5], 4) if r[5] is not None else None,
                "avg_rounds": round(r[6], 2) if r[6] is not None else None,
                "avg_gap_days": round(r[7], 1) if r[7] is not None else None,
                "median_profit_rate":
                    round(r[8], 4) if r[8] is not None else None,
                "mean_profit_rate":
                    round(r[9], 4) if r[9] is not None else None,
                "median_profit_amt":
                    round(r[10], 2) if r[10] is not None else None,
                "mean_profit_amt":
                    round(r[11], 2) if r[11] is not None else None,
            })
        return {"status": "up", "fee": fee_val, "method": method_val,
                "equity": bool(equity), "source": "live", "computed_at": None,
                "thresholds": [float(t) for t in TP_THRESHOLDS], "rows": out}
    return _safe(run, {"status": "down", "detail": "pg unreachable",
                       "method": method, "equity": bool(equity),
                       "thresholds": [], "rows": [],
                       "source": None, "computed_at": None})


_TP_ROUNDS_SQL = """
SELECT seq, tp_date, round_days, gap_days, profit,
       tp_nav, invested, sub_fee, redeem_fee, net_profit
FROM fund_dca_tp_rounds
WHERE code = %s AND tp = %s AND fee = %s AND method = %s ORDER BY seq
"""


def fetch_dca_tp_rounds(dsn: str, code: str, fee: str = "nom",
                        tp: float = TAKE_PROFIT,
                        method: str = "amount") -> dict:
    """单基金单阈值每轮止盈明细（fund_dca_tp_rounds）。契约同 fetch_dca_rounds
    + tp/method 维度回显。"""
    fee_val = fee if fee in _FEE_XIRR_COL else "nom"
    method_val = method if method in DCA_METHODS else "amount"

    def run():
        with psycopg.connect(dsn, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute(_TP_ROUNDS_SQL, (code, tp, fee_val, method_val))
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
                "method": method_val, "rounds": out, "totals": totals}
    return _safe(run, {"status": "down", "detail": "pg unreachable",
                       "code": code, "tp": tp, "method": method_val,
                       "rounds": [], "totals": {}})


_TP_SUMMARY_SQL = """
SELECT tp, full_start,
       full_last_tp, full_last_tp_disc, full_last_tp_nom,
       full_cur_invested,
       full_cur_profit_rate, full_cur_profit_rate_disc,
       full_cur_profit_rate_nom,
       full_cur_xirr, full_cur_xirr_disc, full_cur_xirr_nom
FROM fund_dca_tp_result WHERE code = %s AND method = %s ORDER BY tp
"""


def fetch_dca_tp_summary(dsn: str, code: str,
                         method: str = "amount") -> dict:
    """单基金全阈值窗口信息汇总 +「最近一次止盈后」当前轮（全史窗口）。

    每阈值一行带窗口起始与运作年限；「最近一次止盈后」当前轮十二字段
    （2026-11-19；未止盈基金全 NULL）。年化/总收益率指标统一全史口径
    （2026-11-25 十七轮：w10 年化/总收益率指标退役，排序/统计/对比卡/
    弹窗指标卡取 full_cur_* 与排行 full_* 列）。method 切定投方式
    （DCA_METHODS，前端弹窗跟随页面级 tab）。降级约定同旧端点。
    """
    method_val = method if method in DCA_METHODS else "amount"

    def run():
        with psycopg.connect(dsn, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute(_DCA_REF_SQL)
                ref = cur.fetchone()[0]
                cur.execute(_TP_SUMMARY_SQL, (code, method_val))
                rows = cur.fetchall()
        out = [{"tp": r[0],
                "w10_start_iso": r[1].isoformat() if r[1] else None,
                "age_years": (round((ref - r[1]).days / 365.25, 1)
                              if ref and r[1] else None),
                "full_last_tp": r[2].isoformat() if r[2] else None,
                "full_last_tp_disc": r[3].isoformat() if r[3] else None,
                "full_last_tp_nom": r[4].isoformat() if r[4] else None,
                "full_cur_invested": r[5],
                "full_cur_profit_rate": (round(r[6], 4)
                                         if r[6] is not None else None),
                "full_cur_profit_rate_disc": (round(r[7], 4)
                                              if r[7] is not None else None),
                "full_cur_profit_rate_nom": (round(r[8], 4)
                                             if r[8] is not None else None),
                "full_cur_xirr": round(r[9], 4) if r[9] is not None else None,
                "full_cur_xirr_disc": (round(r[10], 4)
                                       if r[10] is not None else None),
                "full_cur_xirr_nom": (round(r[11], 4)
                                      if r[11] is not None else None)}
               for r in rows]
        return {"status": "up", "code": code, "method": method_val, "rows": out}
    return _safe(run, {"status": "down", "detail": "pg unreachable",
                       "code": code, "method": method_val, "rows": []})


_TIMING_SQL = """
SELECT t.code, r.name, r.fund_type, r.alive, t.dd_current,
       t.days_since_peak, t.dip_band, r.scale_yi, r.full_start,
       r.w10_start, r.w10_end, r.w10_rounds, r.gap_avg_days,
       r.full_xirr,
       r.w10_rounds_disc, r.w10_rounds_nom,
       r.full_xirr_disc, r.full_xirr_nom,
       r.gap_avg_days_disc, r.gap_avg_days_nom
FROM fund_dip_timing t
JOIN fund_dca_tp_result r ON r.code = t.code AND r.tp = %s
WHERE (%s = '' OR t.code ILIKE %s OR r.name ILIKE %s)
  AND {band_clause}
  AND {alive_clause}
  AND {age_clause}
  AND {scale_clause}
  AND {equity_clause}
  AND {range_clause}
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
  AND {range_clause}
"""

# 时机桶白名单（25-30 为验证结论甜蜜点；'' = 全部）
_TIMING_BANDS = ("20-25", "25-30", "30+")
_TIMING_COLS = ["code", "name", "fund_type", "alive", "dd_current",
                "days_since_peak", "dip_band", "scale_yi", "full_start",
                "w10_start", "w10_end", "w10_rounds", "gap_avg_days",
                "full_xirr",
                "w10_rounds_disc", "w10_rounds_nom",
                "full_xirr_disc", "full_xirr_nom",
                "gap_avg_days_disc", "gap_avg_days_nom"]
_TIMING_SORTABLE = {"full_xirr",
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
                     q: str = "", alive: str = "true", sort: str = "full_xirr",
                     order: str = "desc", min_age: float = 0.0,
                     min_scale: float = 0.0, limit: int = 50, offset: int = 0,
                     fee: str = "gross", equity: bool = True,
                     age_min: float = None, age_max: float = None,
                     gap_min: float = None, gap_max: float = None,
                     xirr_min: float = None, xirr_max: float = None,
                     dd_min: float = None, dd_max: float = None,
                     pr_min: float = None, pr_max: float = None) -> dict:
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
    sort_base = sort if sort in _TIMING_SORTABLE else "full_xirr"
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
                range_clauses, range_params = _range_filters(
                    ref=ref, suffix=suffix, age_min=age_min, age_max=age_max,
                    gap_min=gap_min, gap_max=gap_max, xirr_min=xirr_min,
                    xirr_max=xirr_max, dd_min=dd_min, dd_max=dd_max,
                    pr_min=pr_min, pr_max=pr_max)
                range_clause = (" AND ".join(range_clauses)
                                if range_clauses else "true")
                sql = (_TIMING_SQL.replace("{band_clause}", band_clause)
                       .replace("{alive_clause}", alive_clause)
                       .replace("{age_clause}", age_clause)
                       .replace("{scale_clause}", scale_clause)
                       .replace("{equity_clause}", equity_clause)
                       .replace("{range_clause}", range_clause)
                       .replace("{sort_col}", sort_col)
                       .replace("{order_dir}", order_dir))
                count_sql = (_TIMING_COUNT_SQL
                             .replace("{band_clause}", band_clause)
                             .replace("{alive_clause}", alive_clause)
                             .replace("{age_clause}", age_clause)
                             .replace("{scale_clause}", scale_clause)
                             .replace("{equity_clause}", equity_clause)
                             .replace("{range_clause}", range_clause))
                base = ([tp, q, like, like]
                        + ([band_val] if band_val else [])
                        + ([cutoff] if cutoff else [])
                        + ([scale_param] if scale_param else [])
                        + ([equity_param] if equity_param else [])
                        + range_params)
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


# ===== 熊市断缴实验（scratch 实验产品化，2026-11-19；表见 dca_store） =====

_PAUSE_MARKET_HEAD = ("subset", "mode", "n", "med_out_a", "med_wallet_a",
                      "xirr_a_med", "xirr_a_p25", "xirr_a_p75")
_PAUSE_ARM_STATS = ("xirr_med", "xirr_lower_pct", "wallet_delta_med",
                    "wallet_delta_p25", "wallet_delta_p75", "wallet_lower_pct")
_PAUSE_MARKET_SELECT = (", ".join(_PAUSE_MARKET_HEAD) + ", "
                        + ", ".join(f"{a}_{c}" for a in ("b", "c", "d")
                                    for c in _PAUSE_ARM_STATS))
_PAUSE_FUND_HEAD = ("code", "span_days", "months", "ever_paused", "episodes",
                    "pause_months", "dd_end", "dd_min", "in_universe")
_PAUSE_FUND_SELECT = ", ".join(
    _PAUSE_FUND_HEAD
    + tuple(f"{p}_{a}_{k}" for p in ("tp", "nt") for a in ("a", "b", "c", "d")
            for k in ("wallet", "xirr", "out", "rounds")))


def _pause_fund_dict(row) -> dict:
    """断缴实验基金行 → dict（金额两位/比率与回撤四位，同排行表约定）。"""
    d = dict(zip(_PAUSE_FUND_HEAD
                 + tuple(f"{p}_{a}_{k}" for p in ("tp", "nt")
                         for a in ("a", "b", "c", "d")
                         for k in ("wallet", "xirr", "out", "rounds")), row))
    for k, v in d.items():
        if isinstance(v, float):
            d[k] = (round(v, 2) if (k.endswith("_wallet") or k.endswith("_out"))
                    else round(v, 4))
    return d


def _pause_market_dict(row) -> dict:
    keys = _PAUSE_MARKET_HEAD + tuple(
        f"{a}_{c}" for a in ("b", "c", "d") for c in _PAUSE_ARM_STATS)
    d = dict(zip(keys, row))
    for k, v in d.items():
        if isinstance(v, float):
            d[k] = (round(v, 2) if ("wallet" in k or k.startswith("med_"))
                    else round(v, 4))
    return d


def fetch_dca_pause_experiment(dsn: str) -> dict:
    """熊市断缴实验市场级统计 + meta + 案例基金（静态快照，只读三表）。

    market 按 mode（tp/nt）→ subset（all/paused_only/near_high/mid/stuck）
    两层分拣；cases 按 meta.params.case_codes 读基金表（缺省回落引擎常量）。
    降级约定同旧端点。
    """

    def run():
        with psycopg.connect(dsn, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT fund_count, params "
                            "FROM fund_bear_pause_meta LIMIT 1")
                meta_row = cur.fetchone()
                cur.execute(f"SELECT {_PAUSE_MARKET_SELECT} "
                            "FROM fund_bear_pause_market")
                market_rows = cur.fetchall()
                case_codes = (((meta_row[1] or {}).get("case_codes"))
                              if meta_row else None) \
                    or list(bear_pause.CASE_CODES)
                cur.execute(f"SELECT {_PAUSE_FUND_SELECT} "
                            "FROM fund_bear_pause_fund "
                            "WHERE code = ANY(%s) ORDER BY code",
                            ([str(c) for c in case_codes],))
                case_rows = cur.fetchall()
        market = {"tp": {}, "nt": {}}
        for r in market_rows:
            block = _pause_market_dict(r)
            market[block.pop("mode")][block.pop("subset")] = block
        return {"status": "up",
                "meta": {"fund_count": meta_row[0] if meta_row else None,
                         "params": dict(meta_row[1]) if meta_row
                         and meta_row[1] else {}},
                "market": market,
                "cases": [_pause_fund_dict(r) for r in case_rows]}
    return _safe(run, {"status": "down", "detail": "pg unreachable",
                       "meta": {}, "market": {}, "cases": []})


def fetch_bear_pause_fund(dsn: str, code: str) -> dict:
    """单基金断缴实验四组对照（无数据 → fund=None，仍是 up 语义）。"""

    def run():
        with psycopg.connect(dsn, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT {_PAUSE_FUND_SELECT} "
                            "FROM fund_bear_pause_fund WHERE code = %s",
                            (code,))
                row = cur.fetchone()
        return {"status": "up",
                "fund": _pause_fund_dict(row) if row else None}
    return _safe(run, {"status": "down", "detail": "pg unreachable",
                       "fund": None})