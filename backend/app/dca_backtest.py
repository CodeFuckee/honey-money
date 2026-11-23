"""全市场基金「定投 + 20% 止盈」回测核心（离线脚本与调度器日更共用）。

口径约定：
- 定投：每只基金每月首个净值日买入；定投方式三档（DCA_METHODS）——
  amount 等额定投（每期固定 1000 元，金额不影响收益率）/ share 等份额
  定投（每期固定到手份额 = 1000/窗口首日复权净值，现金随净值波动）/
  dd_ladder 回撤梯度加码（买入日按当日回撤分档加倍，见 DD_LADDER）；
- 复权：东财日增长率 pct_chg 已是分红再投资口径（已验证），链乘合成复权净值；
  pct_chg 缺失行用 (unit_nav+每份分红)/前日单位净值 兜底（分红由累计净值差推出）；
- 止盈：任一净值日持仓收益率 = 持仓市值/已投入 - 1 ≥ 20% 即全仓赎回并开启新一轮
  （触发线按市值口径，与用户在 APP 里看到的一致，三场景共用同一条路径）；
- 费用：持有期费用（管理/托管/销售服务费）已在净值口径内，不重复建模；交易
  费用按申购费（外扣法，按买入折份额）+ 赎回费（按每笔份额持有天数套阶梯）
  建模，跑三场景——gross 不含费 / disc 申购费一折 / nom 申购费名义
  （赎回费一律不折扣）。全市场无结构化费率源，按基金类型 + A/C 类估典型档位；
- 年化：XIRR（资金加权，现金流 = 每月 -1000 / 止盈日 +净回款 / 期末清算 +净回款；
  含费场景的回款为扣赎回费后净额，投入现金流仍记全额）；
- 双窗口：full=基金全历史；10y=近 10 年（窗口内成立则从成立起）。

两个入口共用同一套纯计算编排 `run_backtest()`：
- scripts/fund_dca_backtest.py：CSV 离线驱动（手动全量重跑 → CSV → 导入）；
- 本模块 `run_full_backtest()`：调度器日更（PG 拉数 → 算 → 内存行 → 直写
  fund_dca_result/fund_dca_rounds，见 scheduler 的 backtest 执行分支）。

多止盈点市场级对比（2026-11-15 grilling 定案）：`run_backtest_tp()` /
`run_full_backtest_tp()` 用 `simulate_multi()` 单遍扫六档阈值，产出带 tp
维度的行（fund_dca_tp_* 新表）；旧口径行由其中 tp=0.20 一档派生，与
`run_backtest()` 输出逐字段一致（测试锚定），日更同事务双写、旧表零改动。
"""

import gc
import io
from typing import NamedTuple

import numpy as np
import pandas as pd
import psycopg
from scipy.optimize import brentq

from .risk_metrics import compute_risk_metrics, risk_row

AMOUNT = 1000.0
TAKE_PROFIT = 0.20
WINDOW_10Y = pd.DateOffset(years=10)
MIN_SPAN_DAYS = 90  # 窗口短于此天数的年化记 nan
# 市场级阈值对比（2026-11-15 grilling 定案；同日追加 35%/40% → 八档）：
# 止盈点清单，API/排行切换与 fund_dca_tp_* 新表共用这一事实源
TP_THRESHOLDS = (0.05, 0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40)
# 定投方式清单（2026-11-23 定案，与 tp 同为 fund_dca_tp_* 的维度）：
# amount=等额定投（每期固定 1000 元）；share=等份额定投（每期固定到手
# 份额 = AMOUNT/窗口首日复权净值，首期恰好 1000 元——涨时现金花得多、
# 跌时花得少，成本摊平效果最差的反面基准；含费为外扣法对偶：现金 =
# 份额×净值×(1+申购费率)）。API method 白名单与前端 tab 共用此事实源。
DCA_METHODS = ("amount", "share", "dd_ladder")
# 回撤梯度加码（dd_ladder，2026-11-23 四轮）：月度买入日按当日回撤分档加倍
# ——回撤 = adj/历史最高（含当日）- 1，因果无前视；取满足 回撤 ≤ 阈值 的
# 最深一档的倍数（reversed 依序匹配，都不中为 1×——浅档在前直接 next 会
# 让深回撤错拿中间档，手算测试当场抓过）。≤-20% 投 2× 沿用熊市加倍实验
# D 组口径（2026-11-19 全市场 9,576 只：68% 基金比坚持等额定投更优），
# 中间档 -10% → 1.5× 为自然梯度；每期现金 = 1000 × 倍数，费用/止盈/XIRR 共用。
DD_LADDER = ((-0.10, 1.5), (-0.20, 2.0))


# ===== 费用模型（典型档位近似，见模块 docstring） =====

SUB_DISCOUNT = 0.1   # 三方代销平台申购费一折（disc 场景）
# 申购名义费率（A 类按 fund_type 前缀归档；C 类与货币类为 0）
SUB_RATE_A = {"股票": 0.015, "混合": 0.015, "指数": 0.015, "QDII": 0.015,
              "FOF": 0.012, "商品": 0.015, "债券": 0.008, "货币": 0.0}
_SUB_RATE_DEFAULT = 0.015   # 未知类型按权益类顶格
# 赎回费阶梯 (持有天数上限, 费率)：天数 < 上限命中该档，超出末档为 0
REDEEM_EQUITY = ((7, 0.015), (365, 0.005), (730, 0.0025))   # 权益类 A
REDEEM_BOND_C = ((7, 0.015),)                                # 债券 A ≥7 天免、一切 C 类、货币


class FeeSchedule(NamedTuple):
    sub_rate: float
    redeem_tiers: tuple


def sub_rate_for(fund_type) -> float:
    ft = fund_type or ""
    for prefix, rate in SUB_RATE_A.items():
        if ft.startswith(prefix):
            return rate
    return _SUB_RATE_DEFAULT


def is_c_class(name) -> bool:
    return bool(name) and name.rstrip().endswith("C")


def redeem_rate(days: int, tiers: tuple) -> float:
    for cap, rate in tiers:
        if days < cap:
            return rate
    return 0.0


def fee_schedules_for(name, fund_type) -> dict:
    """三场景费率调度：gross 无费；disc=申购一折；nom=申购名义。

    赎回费不折扣。C 类/货币类申购费为 0；债券 A 与 C 类/货币类用短赎回阶梯。
    """
    c_or_money = is_c_class(name) or (fund_type or "").startswith("货币")
    sub_nom = 0.0 if c_or_money else sub_rate_for(fund_type)
    tiers = (REDEEM_BOND_C if c_or_money or (fund_type or "").startswith("债券")
             else REDEEM_EQUITY)
    return {"gross": None,
            "disc": FeeSchedule(sub_nom * SUB_DISCOUNT, tiers),
            "nom": FeeSchedule(sub_nom, tiers)}


def build_growth(df: pd.DataFrame) -> np.ndarray:
    """每行日增长率（小数）。优先 pct_chg；缺失/离谱时用 单位净值+分红兜底。"""
    unit = df["unit_nav"].to_numpy(dtype=float)
    accum = df["accum_nav"].to_numpy(dtype=float)
    pct = df["pct_chg"].to_numpy(dtype=float)

    div = np.zeros(len(df))
    div[1:] = (accum[1:] - unit[1:]) - (accum[:-1] - unit[:-1])
    div = np.maximum(div, 0.0)

    g_fallback = np.zeros(len(df))
    with np.errstate(divide="ignore", invalid="ignore"):
        g_fallback[1:] = (unit[1:] + div[1:]) / unit[:-1] - 1.0
    g_fallback = np.nan_to_num(g_fallback, nan=0.0)

    g = np.where(np.isfinite(pct), pct / 100.0, g_fallback)
    g = np.where((g > 0.5) | (g < -0.5), g_fallback, g)  # 日增>50%必异常
    return np.clip(g, -0.95, 0.5)


def xirr(flows: list[tuple[pd.Timestamp, float]]) -> float | None:
    """资金加权年化。flows=[(date, cf)]，投入为负、回收为正。

    定投现金流符号非常规（-…-++…-…+），IRR 可能多根：盈利（cf.sum>0）时
    f(0)>0 且 f(∞)→首笔负现金流<0，正根必存在于 (0,∞)；亏损时同理在
    (-1,0) 找负根。只扫对应半边，取第一个变号区间做 brentq。
    """
    if len(flows) < 2:
        return None
    t0 = flows[0][0]
    t = np.array([(d - t0).days / 365.25 for d, _ in flows])
    cf = np.array([a for _, a in flows])
    profit = float(cf.sum())
    if abs(profit) < 1e-9:
        return 0.0

    def npv(r):
        return float(np.sum(cf / (1.0 + r) ** t))

    if profit > 0:
        grid = [0.0, 0.05, 0.1, 0.2, 0.3, 0.5, 0.75, 1.0, 1.5, 2.0,
                3.0, 5.0, 10.0, 100.0, 1000.0, 1e4, 1e5, 1e6]
    else:
        grid = [0.0, -0.05, -0.1, -0.2, -0.3, -0.4, -0.5, -0.6, -0.7,
                -0.8, -0.9, -0.95]
    f_prev = npv(grid[0])
    for i in range(1, len(grid)):
        f_cur = npv(grid[i])
        if f_prev * f_cur < 0:
            return float(brentq(npv, grid[i - 1], grid[i], xtol=1e-6))
        if f_prev == 0:
            return grid[i - 1]
        f_prev = f_cur
    return grid[-1] if f_prev == 0 else None


def _net_redeem(lots: list[tuple[int, float]], dates: pd.DatetimeIndex,
                i: int, a: float, fee: "FeeSchedule | None") -> float:
    """按笔清仓：Σ 份额市值×(1-赎回费率(持有天数))；无费调度直通总市值。"""
    if fee is None:
        return sum(sh for _, sh in lots) * a
    return sum(sh * a * (1.0 - redeem_rate((dates[i] - dates[bi]).days,
                                           fee.redeem_tiers))
               for bi, sh in lots)


def _new_tp_state(tp: float) -> dict:
    """单阈值策略的独立状态机（每档止盈线一套，互不干扰）。"""
    return {"tp": tp, "months": set(),
            "lots": [],            # (买入日下标, 份额)
            "basis_units": 0.0,    # Σ 份额，市值 = basis_units*adj(t)
            "invested": 0.0,
            "round_sub_fee": 0.0,  # 本轮累计申购费（外扣法折损）
            "rounds": 0, "realized": 0.0,
            "flows": [], "round_events": [],
            "round_start": None, "last_tp": None}


def _finalize_tp_state(st: dict, dates: pd.DatetimeIndex, adj: np.ndarray,
                       fee: FeeSchedule | None) -> dict:
    """期末清算 + 结果组装（与原 simulate 尾部逐字段一致）。"""
    if st["basis_units"] > 0:
        end_value = _net_redeem(st["lots"], dates, len(dates) - 1, adj[-1], fee)
        st["flows"].append((dates[-1], end_value))
    else:
        end_value = 0.0
    invested_total = -sum(a for _, a in st["flows"] if a < 0)  # 正数口径
    profit = (st["realized"] + (end_value - st["invested"])
              if st["basis_units"] > 0 else st["realized"])
    span = (dates[-1] - dates[0]).days
    # 「最近一次止盈后」当前轮（进行中，2026-11-19 用户需求）：止盈重新
    # 开始定投至窗口末日的收益。rounds=0 时前提不存在 → 全 None；
    # end_value 即当前轮期末市值（净赎回口径，与窗口 profit_rate 一致）
    last_tp = st["last_tp"]
    cur_invested = cur_pr = cur_xirr = None
    if st["rounds"] > 0 and last_tp is not None:
        cur_invested = st["invested"]
        if st["invested"] > 0:
            cur_pr = end_value / st["invested"] - 1.0
            cur_flows = [(d, a) for d, a in st["flows"] if d > last_tp]
            cur_flows.append((dates[-1], end_value))
            cur_xirr = (xirr(cur_flows)
                        if (dates[-1] - last_tp).days >= MIN_SPAN_DAYS
                        else None)
    return {
        "start": str(dates[0].date()),
        "end": str(dates[-1].date()),
        "months": len(st["months"]),
        "rounds": st["rounds"],
        "round_events": st["round_events"],
        "flows": st["flows"],
        "invested": round(invested_total, 2),
        "final_value": round(invested_total + profit, 2),
        "profit_rate": profit / invested_total if invested_total > 0 else None,
        "xirr": xirr(st["flows"]) if span >= MIN_SPAN_DAYS else None,
        "last_tp": str(last_tp.date()) if last_tp is not None else None,
        "cur_invested": cur_invested,
        "cur_profit_rate": cur_pr,
        "cur_xirr": cur_xirr,
    }


def simulate_multi(dates: pd.DatetimeIndex, adj: np.ndarray,
                   fee: FeeSchedule | None = None,
                   take_profits=TP_THRESHOLDS,
                   method: str = "amount") -> dict:
    """单基金单窗口单遍多阈值：同一净值序列上同时跑多档止盈。

    每档阈值一个独立状态机（月度买入/日频触发线/轮次事件与 simulate
    完全同逻辑，仅状态分离），返回 {tp: simulate 同构结果}——单档结果
    与 simulate(take_profit=tp) 逐字段一致。相对跑 N 遍全量模拟，省掉的
    只是窗口 prep/月检等共享开销；状态机本体仍是 N 倍工作量（日更多阈值
    总计算量 ≈ 单阈值日更 ×N，见 run_backtest_tp docstring）。

    method 选定投方式（DCA_METHODS，止盈/清算逻辑各方式共用）：
    - amount（等额，缺省）：每期现金固定 AMOUNT，外扣法折份额；
    - share（等份额）：每期到手份额固定 = AMOUNT/窗口首日复权净值，
      现金 = 份额×净值×(1+申购费率)（无费时 ×1），invested 每期可变；
    - dd_ladder（回撤梯度加码）：每期现金 = AMOUNT×档位倍数（DD_LADDER，
      按买入日回撤 = adj/历史最高（含当日）-1 分档），invested 每期可变。
    """
    if method not in DCA_METHODS:
        raise ValueError(f"unknown dca method: {method!r}")
    # 等份额标定：窗口首日净值折基准金额（首期恰好 1000 元；跨基金可比，
    # 无前视——adj[0] 即首个月度买入价）
    unit_shares = AMOUNT / adj[0]
    states = [_new_tp_state(tp) for tp in take_profits]
    months = dates.to_numpy().astype("datetime64[M]")
    peak = -np.inf          # dd_ladder 用：历史最高（含当日）的运行峰值
    for i in range(len(dates)):
        a = adj[i]
        m = months[i]
        if a > peak:
            peak = a
        dd = a / peak - 1.0
        for st in states:
            if m not in st["months"]:
                st["months"].add(m)
                if st["basis_units"] == 0.0:
                    st["round_start"] = dates[i]   # 新一轮从本轮首次买入起算
                if method == "share":
                    cash = unit_shares * a * (1.0 if fee is None
                                              else 1.0 + fee.sub_rate)
                    sh = unit_shares
                    sub_fee = 0.0 if fee is None else cash - sh * a
                else:
                    mult = (next((mv for th, mv in reversed(DD_LADDER)
                                  if dd <= th), 1.0)
                            if method == "dd_ladder" else 1.0)
                    cash = AMOUNT * mult
                    net_amount = (cash if fee is None
                                  else cash / (1.0 + fee.sub_rate))
                    sh = net_amount / a
                    sub_fee = cash - net_amount
                st["lots"].append((i, sh))
                st["basis_units"] += sh
                st["invested"] += cash
                st["round_sub_fee"] += sub_fee
                st["flows"].append((dates[i], -cash))
            if (st["basis_units"] > 0
                    and st["basis_units"] * a
                    >= st["invested"] * (1.0 + st["tp"])):
                value = st["basis_units"] * a
                net = _net_redeem(st["lots"], dates, i, a, fee)
                redeem_fee = value - net
                st["realized"] += net - st["invested"]
                st["rounds"] += 1
                st["flows"].append((dates[i], net))
                gap = ((dates[i] - st["last_tp"]).days
                       if st["last_tp"] is not None
                       else (dates[i] - st["round_start"]).days)
                st["round_events"].append((
                    str(dates[i].date()),
                    ((dates[i] - st["round_start"]).days
                     if st["round_start"] is not None else None),
                    gap,
                    round(value / st["invested"] - 1.0, 4),
                    round(a, 4),                     # 止盈日复权净值
                    round(st["invested"], 2),        # 本轮累计投入
                    round(st["round_sub_fee"], 2),   # 本轮申购费合计
                    round(redeem_fee, 2),            # 本轮赎回费
                    round(net - st["invested"], 2),  # 本轮净收益（回款-投入）
                ))
                st["last_tp"] = dates[i]
                st["lots"] = []
                st["basis_units"] = 0.0
                st["invested"] = 0.0
                st["round_sub_fee"] = 0.0
    return {st["tp"]: _finalize_tp_state(st, dates, adj, fee)
            for st in states}


def simulate(dates: pd.DatetimeIndex, adj: np.ndarray,
             fee: FeeSchedule | None = None,
             take_profit: float = TAKE_PROFIT,
             method: str = "amount") -> dict:
    """单基金单窗口单阈值：每月首个净值日买入，日频检查止盈。

    fee=None 为无费口径（原行为）；给定调度时申购按外扣法折份额
    （净申购 = 金额/(1+费率)，投入现金流仍记全额），止盈/期末清算的
    回款现金流为扣赎回费后净额。止盈触发线按市值口径（持仓市值/已投入
    -1 ≥ take_profit 即全仓赎回并开启新一轮），与用户在 APP 里看到的
    一致。method 选定投方式（amount 等额/ share 等份额，口径见
    simulate_multi 与 DCA_METHODS 注释）。实现为 simulate_multi 单档
    委托，行为逐字段一致。

    res["flows"]：XIRR 现金流 [(日期, 金额)]。
    res["round_events"]：每轮止盈事件 (tp_date, round_days, gap_days,
    profit, tp_nav, invested, sub_fee, redeem_fee, net_profit)——
    profit 为市值口径收益率（≥阈值，含跳空越过部分）；tp_nav=止盈日
    复权净值；invested=本轮累计投入；sub_fee/redeem_fee=本轮申购费
    合计/赎回费（无费场景为 0）；net_profit=本轮净收益（回款-投入）。
    res["last_tp"]/["cur_invested"]/["cur_profit_rate"]/["cur_xirr"]：
    「最近一次止盈后」当前轮（进行中）四字段——止盈日/本轮已投入/
    本轮市值口径收益率/本轮 XIRR（<90 天置 None）；rounds=0 全 None
    （cur_profit_rate 用净赎回口径期末市值，与窗口 profit_rate 一致）。
    """
    return simulate_multi(dates, adj, fee, [take_profit],
                          method=method)[take_profit]


def prep_window(df: pd.DataFrame, start: pd.Timestamp | None):
    """窗口切片 + 复权净值链，返回 (dates, adj)；数据不足返回 None。"""
    d = df["nav_date"]
    if start is not None:
        df = df[d >= start]
    if len(df) < 2:
        return None
    df = df.reset_index(drop=True)
    dates = pd.DatetimeIndex(pd.to_datetime(df["nav_date"]))
    return dates, np.cumprod(1.0 + build_growth(df))


COLS_FULL = ["start", "end", "months", "rounds", "invested", "final_value",
             "profit_rate", "xirr"]


def run_backtest(nav: pd.DataFrame, basic: pd.DataFrame,
                 scale_map: dict, *, end_ts=None, log=print) -> tuple[list[dict], list[tuple]]:
    """纯计算编排：净值/基金基础信息/规模映射 → （结果行， 轮次行）。

    nav：code,nav_date,unit_nav,accum_nav,pct_chg（已按 code,nav_date 排序）；
    basic：以 code 为索引的 name/fund_type/delisted；
    scale_map：code → {scale_yi, scale_date}；
    end_ts：10y 窗口右端（缺省=数据集最新净值日）。

    结果行不含 alive（离线 CSV 路径由导入期计算，日更路径在
    run_full_backtest 里按同一 45 天新鲜度规则回填）。
    """
    if end_ts is None:
        end_ts = pd.Timestamp(nav["nav_date"].max())
    rows, rounds_rows = [], []
    for code, g in nav.groupby("code", sort=True):
        g = g.sort_values("nav_date")
        full_p = prep_window(g, None)
        w10_p = prep_window(g, end_ts - WINDOW_10Y)
        if full_p is None and w10_p is None:
            continue
        fee = fee_schedules_for(basic["name"].get(code, ""),
                                basic["fund_type"].get(code, ""))
        row = {
            "code": code,
            "name": basic["name"].get(code, ""),
            "fund_type": basic["fund_type"].get(code, ""),
            "delisted": bool(basic["delisted"].get(code, False)) if "delisted" in basic else False,
            "scale_yi": scale_map.get(code, {}).get("scale_yi"),
            "scale_date": scale_map.get(code, {}).get("scale_date"),
        }
        w10_res_by_fee = {}
        for prefix, prep in (("full", full_p), ("w10", w10_p)):
            res = simulate(*prep, fee["gross"]) if prep else None
            for k in COLS_FULL:
                row[f"{prefix}_{k}"] = res[k] if res else None
            # 含费场景多出年化列；w10 的三场景轮次明细全量入库（止盈日
            # 随场景份额略有差异，各自自洽）
            disc_res = simulate(*prep, fee["disc"]) if prep else None
            nom_res = simulate(*prep, fee["nom"]) if prep else None
            if prefix == "full":
                # 全史「最近一次止盈后」当前轮三口径字段（止盈日随费用
                # 口径位移，invested 与费用无关记一份）
                row["full_last_tp"] = res["last_tp"] if res else None
                row["full_cur_invested"] = (res["cur_invested"]
                                            if res else None)
                row["full_cur_profit_rate"] = (res["cur_profit_rate"]
                                               if res else None)
                row["full_cur_xirr"] = res["cur_xirr"] if res else None
                row["full_last_tp_disc"] = (disc_res["last_tp"]
                                            if disc_res else None)
                row["full_cur_profit_rate_disc"] = (disc_res["cur_profit_rate"]
                                                    if disc_res else None)
                row["full_cur_xirr_disc"] = (disc_res["cur_xirr"]
                                             if disc_res else None)
                row["full_last_tp_nom"] = (nom_res["last_tp"]
                                           if nom_res else None)
                row["full_cur_profit_rate_nom"] = (nom_res["cur_profit_rate"]
                                                   if nom_res else None)
                row["full_cur_xirr_nom"] = (nom_res["cur_xirr"]
                                            if nom_res else None)
            row[f"{prefix}_xirr_disc"] = disc_res["xirr"] if disc_res else None
            row[f"{prefix}_xirr_nom"] = nom_res["xirr"] if nom_res else None
            # 排行表格口径切换不止年化：轮次/总收益率也随口径入库
            # （份额随申购费折损，止盈日与触发轮次可能位移）
            row[f"{prefix}_rounds_disc"] = disc_res["rounds"] if disc_res else None
            row[f"{prefix}_rounds_nom"] = nom_res["rounds"] if nom_res else None
            row[f"{prefix}_profit_rate_disc"] = (disc_res["profit_rate"]
                                                 if disc_res else None)
            row[f"{prefix}_profit_rate_nom"] = (nom_res["profit_rate"]
                                                if nom_res else None)
            if prefix == "w10":
                w10_res = res
                w10_res_by_fee = {"gross": res, "disc": disc_res, "nom": nom_res}
        rows.append(row)
        for fee_name in ("gross", "disc", "nom"):
            res_w = w10_res_by_fee.get(fee_name)
            if res_w:
                for seq, ev in enumerate(res_w["round_events"], 1):
                    rounds_rows.append((code, fee_name, seq, *ev))
        if len(rows) % 1000 == 0:
            log(f"  已回测 {len(rows):,} 只", flush=True)
    return rows, rounds_rows


def _or_none(res: dict | None, key: str):
    return res[key] if res else None


def run_backtest_tp(nav: pd.DataFrame, basic: pd.DataFrame,
                    scale_map: dict, thresholds=TP_THRESHOLDS, *,
                    end_ts=None, method: str = "amount",
                    log=print) -> tuple[list[dict], list[tuple]]:
    """多阈值纯计算编排：每基金每窗口每口径一次 simulate_multi 扫描。

    nav/basic/scale_map 同 run_backtest；end_ts：10y 窗口右端（缺省=数据集
    最新净值日）；method 选定投方式（DCA_METHODS，缺省等额）。返回
    (result_rows, rounds_rows)：
    - 结果行 = run_backtest 行结构 + "tp"/"method" 字段
      （每基金 × 每阈值一行）；
    - 轮次行 = (code, method, tp, fee, seq, *ev)，w10 窗口口径与旧表同一约定；
    - method=amount 且 tp=TAKE_PROFIT 的行/轮次与 run_backtest 输出逐字段
      一致（测试锚定），旧表写入即由该档派生。
    """
    if end_ts is None:
        end_ts = pd.Timestamp(nav["nav_date"].max())
    rows, rounds_rows = [], []
    n_funds = 0
    for code, g in nav.groupby("code", sort=True):
        g = g.sort_values("nav_date")
        full_p = prep_window(g, None)
        w10_p = prep_window(g, end_ts - WINDOW_10Y)
        if full_p is None and w10_p is None:
            continue
        fee = fee_schedules_for(basic["name"].get(code, ""),
                                basic["fund_type"].get(code, ""))
        base = {
            "code": code,
            "name": basic["name"].get(code, ""),
            "fund_type": basic["fund_type"].get(code, ""),
            "delisted": (bool(basic["delisted"].get(code, False))
                         if "delisted" in basic else False),
            "scale_yi": scale_map.get(code, {}).get("scale_yi"),
            "scale_date": scale_map.get(code, {}).get("scale_date"),
        }
        # per_tp_fee[tp][prefix][fee_name] → 该组合的 simulate 结果
        per_tp_fee = {tp: {"full": {}, "w10": {}} for tp in thresholds}
        for prefix, prep in (("full", full_p), ("w10", w10_p)):
            for fee_name in ("gross", "disc", "nom"):
                multi = (simulate_multi(*prep, fee[fee_name], thresholds,
                                        method=method)
                         if prep else None)
                for tp in thresholds:
                    per_tp_fee[tp][prefix][fee_name] = (
                        multi[tp] if multi else None)
        for tp in thresholds:
            res = per_tp_fee[tp]
            row = dict(base)
            row["tp"] = tp
            row["method"] = method
            for prefix in ("full", "w10"):
                p_res = res[prefix]
                for k in COLS_FULL:
                    row[f"{prefix}_{k}"] = _or_none(p_res["gross"], k)
                row[f"{prefix}_xirr_disc"] = _or_none(p_res["disc"], "xirr")
                row[f"{prefix}_xirr_nom"] = _or_none(p_res["nom"], "xirr")
                row[f"{prefix}_rounds_disc"] = _or_none(p_res["disc"], "rounds")
                row[f"{prefix}_rounds_nom"] = _or_none(p_res["nom"], "rounds")
                row[f"{prefix}_profit_rate_disc"] = _or_none(p_res["disc"],
                                                             "profit_rate")
                row[f"{prefix}_profit_rate_nom"] = _or_none(p_res["nom"],
                                                            "profit_rate")
                if prefix == "full":
                    # 全史「最近一次止盈后」当前轮三口径字段（与旧引擎
                    # run_backtest 行键集合一致——tp=0.20 锚点测试依赖）
                    row["full_last_tp"] = _or_none(p_res["gross"], "last_tp")
                    row["full_cur_invested"] = _or_none(p_res["gross"],
                                                        "cur_invested")
                    row["full_cur_profit_rate"] = _or_none(p_res["gross"],
                                                           "cur_profit_rate")
                    row["full_cur_xirr"] = _or_none(p_res["gross"], "cur_xirr")
                    row["full_last_tp_disc"] = _or_none(p_res["disc"],
                                                        "last_tp")
                    row["full_cur_profit_rate_disc"] = _or_none(
                        p_res["disc"], "cur_profit_rate")
                    row["full_cur_xirr_disc"] = _or_none(p_res["disc"],
                                                         "cur_xirr")
                    row["full_last_tp_nom"] = _or_none(p_res["nom"], "last_tp")
                    row["full_cur_profit_rate_nom"] = _or_none(
                        p_res["nom"], "cur_profit_rate")
                    row["full_cur_xirr_nom"] = _or_none(p_res["nom"],
                                                        "cur_xirr")
            rows.append(row)
            # w10 三口径轮次明细全量入库（与旧表同一窗口口径）
            for fee_name in ("gross", "disc", "nom"):
                res_w = res["w10"].get(fee_name)
                if res_w:
                    for seq, ev in enumerate(res_w["round_events"], 1):
                        rounds_rows.append((code, method, tp, fee_name, seq,
                                            *ev))
        n_funds += 1
        if n_funds % 1000 == 0:
            log(f"  已回测 {n_funds:,} 只 × {len(thresholds)} 档阈值"
                f"（{method}）", flush=True)
    return rows, rounds_rows


# ===== 调度器日更：PG 拉数 → run_backtest → 内存结果行（直写 PG 由调用方做） =====

_ALIVE_WINDOW_DAYS = 45   # 最后净值日距数据集最新净值日的在市窗口


def _load_codes(conn) -> list[str]:
    return pd.read_sql("SELECT DISTINCT code FROM fund_nav", conn,
                       dtype={"code": str})["code"].tolist()


def _load_basic(conn, codes: list[str]) -> pd.DataFrame:
    codes_arr = "{" + ",".join(codes) + "}"
    return pd.read_sql(
        "SELECT code, name, fund_type, delisted FROM fund_basic "
        f"WHERE code = ANY('{codes_arr}'::text[])",
        conn, dtype={"code": str}).set_index("code")


def _load_scale(conn) -> dict:
    rows = pd.read_sql("SELECT code, scale_yi, scale_date FROM fund_scale",
                       conn, dtype={"code": str})
    # PG DATE 列返回 datetime.date（非 Timestamp），统一转 ISO 字符串
    return {r["code"]: {"scale_yi": r["scale_yi"],
                        "scale_date": (str(r["scale_date"])[:10]
                                       if pd.notna(r["scale_date"]) else None)}
            for _, r in rows.iterrows()}


def _load_nav(conn, codes: list[str]) -> pd.DataFrame:
    codes_arr = "{" + ",".join(codes) + "}"
    buf = io.BytesIO()
    with conn.cursor() as cur:
        with cur.copy(
            "COPY (SELECT code, nav_date, unit_nav, accum_nav, pct_chg "
            f"FROM fund_nav WHERE code = ANY('{codes_arr}'::text[]) "
            "ORDER BY code, nav_date) TO STDOUT (FORMAT csv, HEADER)"
        ) as cp:
            for chunk in cp:
                buf.write(chunk)
    buf.seek(0)
    return pd.read_csv(buf, dtype={"code": "category"}, parse_dates=["nav_date"])


def run_full_backtest(dsn: str, *, log=print) -> tuple[list[dict], list[tuple], dict]:
    """日更全量回测：PG 拉数（fund_nav 全量 + basic + fund_scale 表）→ 计算
    → 内存结果行（含 alive 45 天新鲜度）与三口径轮次行。纯 CPU，调用方放
    子进程跑；直写 fund_dca_result / fund_dca_rounds 由调用方事务完成。

    返回 (result_rows, rounds_rows, stats)；stats 含 fund_count/round_count。
    """
    with psycopg.connect(dsn, connect_timeout=10) as conn:
        codes = _load_codes(conn)
        log(f"有净值的基金：{len(codes):,} 只")
        if not codes:
            raise SystemExit("fund_nav 为空，无从回测")
        basic = _load_basic(conn, codes)
        scale_map = _load_scale(conn)
        nav = _load_nav(conn, codes)
    log(f"净值行：{len(nav):,}")
    rows, rounds_rows = run_backtest(nav, basic, scale_map, log=log)
    # alive：full_end 距数据集最新净值日 45 天内（场内 ETF 的 delisted 标记
    # 不可直接用，见 dca_store 同款口径）
    all_end = max((r["full_end"] for r in rows if r["full_end"]), default=None)
    cutoff = (pd.Timestamp(all_end) - pd.Timedelta(days=_ALIVE_WINDOW_DAYS)) \
        if all_end else None
    for r in rows:
        r["alive"] = bool(cutoff and r["full_end"]
                          and pd.Timestamp(r["full_end"]) >= cutoff)
    stats = {"fund_count": len(rows), "round_count": len(rounds_rows),
             "window_end": str(all_end) if all_end else None}
    log(f"回测完成：{stats['fund_count']:,} 只 / 轮次 {stats['round_count']:,} 行")
    return rows, rounds_rows, stats


def compute_dip_timing(dates: pd.DatetimeIndex, adj: np.ndarray) -> dict:
    """单基金当前时机指标（定投时机视图，2026-11-16 假设验证结论）。

    dd_current：复权口径当前回撤 = adj[-1]/历史最高 - 1（负数）；
    dip_band 回撤桶（-25~-30% 为验证结论中的"甜蜜点"）：none（>-20%）/
    20-25（-0.25 < dd ≤ -0.20）/ 25-30（-0.30 < dd ≤ -0.25）/ 30+（≤ -0.30）；
    days_since_peak：最后触及历史最高的自然日到末日；净值不足返回空字段。
    """
    if len(adj) < 2:
        return {"dd_current": None, "dip_band": "none",
                "days_since_peak": None}
    cummax = np.maximum.accumulate(adj)
    dd = adj[-1] / cummax[-1] - 1.0
    if not np.isfinite(dd):
        return {"dd_current": None, "dip_band": "none",
                "days_since_peak": None}
    dd = round(float(dd), 4)          # 分桶与存储同精度，消浮点边界
    if dd > -0.20:
        band = "none"
    elif dd > -0.25:
        band = "20-25"
    elif dd > -0.30:
        band = "25-30"
    else:
        band = "30+"
    peak_idx = int(np.where(adj == cummax[-1])[0][-1])
    return {"dd_current": dd, "dip_band": band,
            "days_since_peak": int((dates[-1] - dates[peak_idx]).days)}


def run_full_backtest_tp(dsn: str, *, thresholds=TP_THRESHOLDS,
                         log=print) -> tuple[list[dict], list[tuple],
                                             list[dict], list[tuple],
                                             list[tuple], list[tuple], dict]:
    """日更全量回测（多阈值版）：PG 拉数一次 → 双 method × run_backtest_tp。

    返回 (old_rows, old_rounds, tp_rows, tp_rounds, timing_rows, risk_rows,
    stats)：
    - old_rows/old_rounds：amount 且 tp=TAKE_PROFIT 派生的旧口径行（写
      fund_dca_result / fund_dca_rounds，与旧引擎 run_backtest 输出一致）；
    - tp_rows/tp_rounds：全阈值 × 全定投方式行（写 fund_dca_tp_* 新表，
      method 维度见 DCA_METHODS；计算量 ≈ 单方法 ×N）；
    - timing_rows：当前回撤时机行（写 fund_dip_timing，定投时机视图）；
    - risk_rows：风险/波动指标行（写 fund_risk_metrics，risk_row 产码）；
    - alive 45 天新鲜度规则对时机行与各套行同一份（基金级，与阈值/方式无关）。
    纯 CPU，调用方放子进程跑；直写由调用方事务完成。
    """
    with psycopg.connect(dsn, connect_timeout=10) as conn:
        codes = _load_codes(conn)
        log(f"有净值的基金：{len(codes):,} 只")
        if not codes:
            raise SystemExit("fund_nav 为空，无从回测")
        basic = _load_basic(conn, codes)
        scale_map = _load_scale(conn)
        nav = _load_nav(conn, codes)
    log(f"净值行：{len(nav):,}")
    tp_rows, tp_rounds = [], []
    for method in DCA_METHODS:
        m_rows, m_rounds = run_backtest_tp(nav, basic, scale_map, thresholds,
                                           method=method, log=log)
        tp_rows.extend(m_rows)
        tp_rounds.extend(m_rounds)
    # 旧口径行 = amount 且 tp=TAKE_PROFIT 派生（去掉 tp/method 维度），
    # 与 run_backtest 输出一致
    old_rows = [{k: v for k, v in r.items() if k not in ("tp", "method")}
                for r in tp_rows
                if r["tp"] == TAKE_PROFIT and r["method"] == "amount"]
    old_rounds = [t[:1] + t[3:] for t in tp_rounds
                  if t[1] == "amount" and t[2] == TAKE_PROFIT]
    # alive：full_end 距数据集最新净值日 45 天内（场内 ETF 的 delisted 标记
    # 不可直接用，见 dca_store 同款口径）——必须先于时机/风险行计算（此前
    # 时机块误置于 alive/all_end 定义之前，引用未定义变量 NameError 使日更
    # 整轮崩溃；2026-11-17 随波动指标接入一并修正）
    all_end = max((r["full_end"] for r in old_rows if r["full_end"]),
                  default=None)
    cutoff = (pd.Timestamp(all_end) - pd.Timedelta(days=_ALIVE_WINDOW_DAYS)) \
        if all_end else None
    alive_by_code = {}
    for r in old_rows:
        r["alive"] = bool(cutoff and r["full_end"]
                          and pd.Timestamp(r["full_end"]) >= cutoff)
        alive_by_code[r["code"]] = r["alive"]
    for r in tp_rows:
        r["alive"] = alive_by_code.get(r["code"], False)
    # 时机行（定投时机视图，见 compute_dip_timing）+ 风险指标行
    # （risk_metrics，列表波动列/详情指标卡共用）：一次 groupby 双产出；
    # 时机行只收 alive 基金（"当前时机"语义），风险行不限（退市基金波动
    # 回顾仍有意义）
    timing_rows = []
    risk_rows = []
    for code, g in nav.groupby("code", sort=True):
        g = g.sort_values("nav_date")
        prep = prep_window(g, None)
        if prep is None:
            continue
        if alive_by_code.get(code):
            t = compute_dip_timing(*prep)
            timing_rows.append((code, t["dd_current"], t["days_since_peak"],
                                t["dip_band"],
                                str(all_end)[:10] if all_end else None))
        risk_rows.append(risk_row(code, compute_risk_metrics(*prep)))
    stats = {"fund_count": len(old_rows), "round_count": len(old_rounds),
             "tp_round_count": len(tp_rounds),
             "timing_count": len(timing_rows),
             "risk_count": len(risk_rows),
             "thresholds": [float(t) for t in thresholds],
             "methods": list(DCA_METHODS),
             "window_end": str(all_end) if all_end else None}
    log(f"回测完成：{stats['fund_count']:,} 只 / 旧口径轮次 "
        f"{stats['round_count']:,} 行 / 多阈值轮次 {stats['tp_round_count']:,} 行"
        f" / 时机行 {stats['timing_count']:,} / 风险行 {stats['risk_count']:,}")
    # 结果组装完即释放 NAV 大头（~1.2GB）：返回值要经 pickle 传回父进程
    # （buffer 与结果集同时在内存），不释放会把子进程 RSS 顶到内存悬崖
    # （2026-11-17 run 122 swap 抖动 72 分钟的共因之一）
    del nav
    gc.collect()
    return old_rows, old_rounds, tp_rows, tp_rounds, timing_rows, risk_rows, stats


def import_scale_csv(csv_path: str, dsn: str, *, log=print) -> int:
    """fund_scale.csv（code,scale_yi,scale_date）→ fund_scale 表整体替换。

    一次性迁移/季度规模刷新用：scripts/fund_scale_fetch*.py 产出 CSV 后
    执行本命令，日更回测即可从 PG 读规模（后端容器内无 scripts/data）。
    """
    import csv as _csv
    rows = []
    with open(csv_path, newline="", encoding="utf-8") as f:
        for r in _csv.DictReader(f):
            if not r.get("code"):
                continue
            rows.append((r["code"].strip(),
                         float(r["scale_yi"]) if r.get("scale_yi") else None,
                         r.get("scale_date") or None))
    with psycopg.connect(dsn, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute("CREATE TABLE IF NOT EXISTS fund_scale ("
                        "code VARCHAR(10) PRIMARY KEY, scale_yi DOUBLE PRECISION,"
                        " scale_date DATE)")
            cur.execute("DELETE FROM fund_scale")
            cur.executemany("INSERT INTO fund_scale (code, scale_yi, scale_date) "
                            "VALUES (%s, %s, %s::date)", rows)
        conn.commit()
    log(f"fund_scale 导入 {len(rows):,} 行")
    return len(rows)


if __name__ == "__main__":
    import argparse
    import os
    ap = argparse.ArgumentParser(description="定投回测辅助：规模 CSV → PG")
    ap.add_argument("--import-scale", metavar="CSV",
                    help="fund_scale.csv 导入 fund_scale 表（整体替换）")
    ap.add_argument("--dsn", default=os.environ.get(
        "DATABASE_URL",
        "postgresql://honey_money:honey_money@host.docker.internal:5433/honey_money"))
    args = ap.parse_args()
    if args.import_scale:
        import_scale_csv(args.import_scale, args.dsn)
    else:
        ap.print_help()
