"""全市场基金「定投 + 20% 止盈」回测核心（离线脚本与调度器日更共用）。

口径约定：
- 定投：每只基金每月首个净值日买入；定投方式六档（DCA_METHODS）——
  amount 等额定投（每期固定 1000 元，金额不影响收益率）/ share 等份额
  定投（每期固定到手份额 = 1000/窗口首日复权净值，现金随净值波动）/
  dd_ladder 回撤梯度加码（买入日按当日回撤分档加倍，见 DD_LADDER）/
  dd_ladder_deep 深回撤加码（更深的回撤才加码）/ profit_dd_ladder 浮盈
  阶梯（浮盈减码+深撤加码）/ pl_ladder 盈亏阶梯（持仓浮亏加码+浮盈减码
  停投，两半边均持仓口径）；
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
import hashlib
import io
import os
from typing import NamedTuple

import numpy as np
import pandas as pd
import psycopg
from scipy.optimize import brentq

from .risk_metrics import compute_risk_metrics, risk_row
from . import settings_store

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
DCA_METHODS = ("amount", "share", "dd_ladder", "dd_ladder_deep",
               "profit_dd_ladder", "pl_ladder", "pl_ladder_deep")
# 回撤梯度加码（dd_ladder，2026-11-23 四轮）：月度买入日按当日回撤分档加倍
# ——回撤 = adj/历史最高（含当日）- 1，因果无前视；取满足 回撤 ≤ 阈值 的
# 最深一档的倍数（reversed 依序匹配，都不中为 1×——浅档在前直接 next 会
# 让深回撤错拿中间档，手算测试当场抓过）。≤-20% 投 2× 沿用熊市加倍实验
# D 组口径（2026-11-19 全市场 9,576 只：68% 基金比坚持等额定投更优），
# 中间档 -10% → 1.5× 为自然梯度；每期现金 = 1000 × 倍数，费用/止盈/XIRR 共用。
DD_LADDER = ((-0.10, 1.5), (-0.20, 2.0))
# 深梯度变体（dd_ladder_deep，2026-11-23 用户定案）：更深的回撤才加码
# ——回撤 ≥20% 投 1.5×、≥30% 投 2×（余者 1×）。比标准梯度保守：浅跌
# （-10%~-20%）不追投，回撤期现金压力更小、加码次数更少；分派/状态机/
# 费用/止盈与 dd_ladder 完全共用，仅档位表不同。
DD_LADDER_DEEP = ((-0.20, 1.5), (-0.30, 2.0))
# method → 梯度表（simulate_multi 按此分派；新增梯度方式即扩此映射 +
# DCA_METHODS + 白名单测试钉值）。profit_dd_ladder 的回撤半边复用深梯度表。
DD_LADDERS = {"dd_ladder": DD_LADDER, "dd_ladder_deep": DD_LADDER_DEEP,
              "profit_dd_ladder": DD_LADDER_DEEP}
# 浮盈阶梯（profit_dd_ladder，2026-11-24 七轮用户定案）：贵时少买、深跌
# 加码的双向调节——当轮浮盈（= 持仓市值/已投入 - 1，与止盈触发线同口径）
# ≥10% 投 0.5×、≥20% 投 0.25×（浮盈档优先，取满足条件的最高浮盈档，
# 与回撤 ≤ 取最深档方向相反）；未触发浮盈档落回撤档（深梯度表
# ≥20% → 1.5×、≥30% → 2×），余者 1×。新轮首买 invested=0 浮盈无定义
# → 直接看回撤档；止盈清算即重置，浮盈档随轮次呼吸。
PROFIT_LADDER = ((0.10, 0.5), (0.20, 0.25))
PROFIT_LADDERS = {"profit_dd_ladder": PROFIT_LADDER,
                  "pl_ladder": ((0.10, 0.5), (0.20, 0.0))}
# 盈亏阶梯（pl_ladder，2026-11-24 八轮用户定案）：持仓浮盈亏双向调节
# ——浮盈/浮亏均用持仓口径（pr = 市值/已投入 - 1，与止盈触发线同口径，
# 与浮盈阶梯亏损半边用价格回撤不同）：浮亏 ≥10% 投 1.5×、≥20% 投 2×
# （≤ 阈值取最深档，越亏越买）；浮盈 ≥10% 投 0.5×、≥20% 投 0×（0× =
# 当期完全停投，无份额无现金流——恰好也是 tp=0.20 的触发区，止盈检查
# 照常运行，停投即避免止盈前追高）；余者与浮盈无定义（invested=0：
# 新轮首买/停投后期）投 1×。无回撤分量（DD_LADDERS 无此键）。
PL_LOSS_LADDER = ((-0.10, 1.5), (-0.20, 2.0))
PL_PROFIT_LADDER = ((0.10, 0.5), (0.20, 0.0))
PL_LOSS_LADDERS = {"pl_ladder": PL_LOSS_LADDER}
# 盈亏阶梯深档（pl_ladder_deep，2026-11-26 十九轮用户定案）：pl_ladder 的
# 加码半边阈值加深变体——浮亏（持仓口径）≥20% 投 1.5×、≥30% 投 2×，
# 减码半边与 pl_ladder 完全同表（浮盈 ≥10% → 0.5×、≥20% → 0× 停投）。
# 比 pl_ladder 只在「账户真的亏了 20% 以上」才出手：加码次数更少、回撤期
# 现金压力更小（阈值与 dd_ladder_deep 的价格回撤档数值相同，但口径不同
# ——持仓浮亏按均价摊薄恒不深于价格回撤，触发更晚更温和）。
PL_LOSS_LADDER_DEEP = ((-0.20, 1.5), (-0.30, 2.0))
PL_LOSS_LADDERS["pl_ladder_deep"] = PL_LOSS_LADDER_DEEP
PROFIT_LADDERS["pl_ladder_deep"] = PL_PROFIT_LADDER
# 回测宇宙白名单（2026-11-25 十四轮用户定案）：仅权益类 8 类产生回测结果
# 行——与排行默认筛选同口径（dca_api.EQUITY_TYPES 别名引用本常量，事实源
# 单一）；债券/FOF/货币/商品/Reits/偏债/未知类型与年轻基金（全史窗口 <
# 运作期限下限）一律不参与回测（此前年轻基金仅年化置空，非权益全量参与）。
# 时机表/波动指标表不受影响（行情派生视图，维持全市场口径）。
BACKTEST_UNIVERSE_TYPES = ("股票型", "混合型-偏股", "混合型-灵活", "混合型-平衡",
                           "指数型-股票", "指数型-海外股票", "QDII-普通股票",
                           "QDII-混合偏股")


def fund_excluded(fund_type, first_date, last_date,
                  min_span_days: int | None = None) -> bool:
    """回测宇宙排除规则（十四轮）：非权益类或年轻基金（全史窗口 <
    运作期限下限，日期闭区间差 ≥ 下限才保留——与年化门槛同 ≥ 约定）
    返回 True；日期缺失一律排除。fund_type 为 None/未知类型视为非权益。"""
    if (fund_type or "") not in BACKTEST_UNIVERSE_TYPES:
        return True
    if first_date is None or last_date is None:
        return True
    if min_span_days is None:
        min_span_days = MIN_SPAN_DAYS
    return pd.Timestamp(last_date) - pd.Timestamp(first_date) \
        < pd.Timedelta(days=min_span_days)


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
    亏损栅格延伸到 -0.99（2026-11-24 用户定案）：根在 -0.95~-0.99 的超深
    亏个例也解出——极端年化正是选股要避开的信号，空值反而掩盖风险；
    NPV 全程不变号（数学无根，如末笔正回收相对投入过小）仍返回 None。
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
                -0.8, -0.9, -0.95, -0.98, -0.99]
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
                       fee: FeeSchedule | None,
                       min_span_days: int | None = None) -> dict:
    """期末清算 + 结果组装（与原 simulate 尾部逐字段一致）。

    min_span_days：年化运作期限下限（天），None=读模块常量 MIN_SPAN_DAYS
    （晚绑定，monkeypatch 兼容）；同时管全窗口年化与止盈后当前轮年化。
    """
    if st["basis_units"] > 0:
        end_value = _net_redeem(st["lots"], dates, len(dates) - 1, adj[-1], fee)
        st["flows"].append((dates[-1], end_value))
    else:
        end_value = 0.0
    invested_total = -sum(a for _, a in st["flows"] if a < 0)  # 正数口径
    profit = (st["realized"] + (end_value - st["invested"])
              if st["basis_units"] > 0 else st["realized"])
    span = (dates[-1] - dates[0]).days
    if min_span_days is None:
        min_span_days = MIN_SPAN_DAYS
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
                        if (dates[-1] - last_tp).days >= min_span_days
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
        "xirr": xirr(st["flows"]) if span >= min_span_days else None,
        "last_tp": str(last_tp.date()) if last_tp is not None else None,
        "cur_invested": cur_invested,
        "cur_profit_rate": cur_pr,
        "cur_xirr": cur_xirr,
    }


def simulate_multi(dates: pd.DatetimeIndex, adj: np.ndarray,
                   fee: FeeSchedule | None = None,
                   take_profits=TP_THRESHOLDS,
                   method: str = "amount",
                   min_span_days: int | None = None) -> dict:
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
      按买入日回撤 = adj/历史最高（含当日）-1 分档），invested 每期可变；
    - dd_ladder_deep（深回撤加码）：同 dd_ladder 换深梯度表（DD_LADDER_DEEP，
      ≥20% → 1.5×、≥30% → 2×），invested 每期可变；
    - profit_dd_ladder（浮盈阶梯）：浮盈档优先——当轮浮盈 = 持仓市值/
      已投入 - 1（与止盈触发线同口径）≥10% → 0.5×、≥20% → 0.25×；
      未触发浮盈档落深梯度回撤档（≥20% → 1.5×、≥30% → 2×），余者 1×。
      新轮首买 invested=0 浮盈无定义直接看回撤档，invested 每期可变；
    - pl_ladder（盈亏阶梯，2026-11-24 八轮）：持仓浮盈亏双向调节（两半边
      均为持仓口径 pr = 市值/已投入 - 1，无回撤分量）——浮亏 ≥10% 投
      1.5×、≥20% 投 2×（≤ 阈值取最深档）；浮盈 ≥10% 投 0.5×、≥20% 投
      0×（当期完全停投：无份额无现金流，止盈检查照常运行）；余者与
      浮盈无定义（invested=0）投 1×，invested 每期可变；
    - pl_ladder_deep（盈亏阶梯深档，2026-11-26 十九轮）：同 pl_ladder 换深
      浮亏档位表（PL_LOSS_LADDER_DEEP，≥20% → 1.5×、≥30% → 2×），减码
      半边与 pl_ladder 同表，invested 每期可变。

    min_span_days：年化运作期限下限（天），None=读模块常量 MIN_SPAN_DAYS
    （设置页 backtest_min_span_days 经调用方下传；全窗口年化与止盈后当前
    轮年化同用，窗口 <90 天的次新基金年化置空即出自此规则）。
    """
    if method not in DCA_METHODS:
        raise ValueError(f"unknown dca method: {method!r}")
    # 等份额标定：窗口首日净值折基准金额（首期恰好 1000 元；跨基金可比，
    # 无前视——adj[0] 即首个月度买入价）
    unit_shares = AMOUNT / adj[0]
    ladder = DD_LADDERS.get(method)   # 梯度加码方式的档位表（amount/share 为 None）
    pr_ladder = PROFIT_LADDERS.get(method)   # 浮盈档位表（profit_dd_ladder/pl_ladder 系）
    pl_loss = PL_LOSS_LADDERS.get(method)   # 盈亏阶梯的浮亏半边（pl_ladder 系）
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
                sh = None   # 本期是否实际买入（pl_ladder 0× 停投期无买入）
                if method == "share":
                    cash = unit_shares * a * (1.0 if fee is None
                                              else 1.0 + fee.sub_rate)
                    sh = unit_shares
                    sub_fee = 0.0 if fee is None else cash - sh * a
                else:
                    # 浮盈档优先（profit_dd_ladder / pl_ladder）：当轮浮盈 =
                    # 市值/已投入 - 1（与止盈触发线同口径）取最高浮盈档；
                    # invested=0 新轮首买浮盈无定义、或浮盈未触发 →
                    # pl_ladder 落浮亏半边（pr ≤ 阈值取最深档），其余方式
                    # 落回撤档（无 pr_ladder 者行为与原回撤分派逐位一致）
                    mult = None
                    if pr_ladder and st["invested"] > 0:
                        pr = st["basis_units"] * a / st["invested"] - 1.0
                        mult = next((mv for th, mv in reversed(pr_ladder)
                                     if pr >= th), None)
                        if mult is None and pl_loss:
                            mult = next((mv for th, mv in reversed(pl_loss)
                                         if pr <= th), None)
                    if mult is None:
                        mult = (next((mv for th, mv in reversed(ladder)
                                      if dd <= th), 1.0) if ladder else 1.0)
                    if mult > 0:   # pl_ladder 0×（浮盈 ≥20%）：当期完全停投
                        cash = AMOUNT * mult
                        net_amount = (cash if fee is None
                                      else cash / (1.0 + fee.sub_rate))
                        sh = net_amount / a
                        sub_fee = cash - net_amount
                if sh is not None:
                    if st["basis_units"] == 0.0:
                        st["round_start"] = dates[i]   # 新一轮从本轮首次买入起算
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
    return {st["tp"]: _finalize_tp_state(st, dates, adj, fee,
                                         min_span_days=min_span_days)
            for st in states}


def simulate(dates: pd.DatetimeIndex, adj: np.ndarray,
             fee: FeeSchedule | None = None,
             take_profit: float = TAKE_PROFIT,
             method: str = "amount",
             min_span_days: int | None = None) -> dict:
    """单基金单窗口单阈值：每月首个净值日买入，日频检查止盈。

    fee=None 为无费口径（原行为）；给定调度时申购按外扣法折份额
    （净申购 = 金额/(1+费率)，投入现金流仍记全额），止盈/期末清算的
    回款现金流为扣赎回费后净额。止盈触发线按市值口径（持仓市值/已投入
    -1 ≥ take_profit 即全仓赎回并开启新一轮），与用户在 APP 里看到的
    一致。method 选定投方式（amount/share/dd_ladder/dd_ladder_deep/
    profit_dd_ladder，口径见
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
    本轮市值口径收益率/本轮 XIRR（窗口不足运作期限 min_span_days 置
    None，缺省 90 天）；rounds=0 全 None
    （cur_profit_rate 用净赎回口径期末市值，与窗口 profit_rate 一致）。
    """
    return simulate_multi(dates, adj, fee, [take_profit],
                          method=method,
                          min_span_days=min_span_days)[take_profit]


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
                 scale_map: dict, *, end_ts=None, log=print,
                 min_span_days: int | None = None) -> tuple[list[dict], list[tuple]]:
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
        # 回测宇宙排除（十四轮）：非权益/年轻基金不产生结果行（脚本离线
        # 路径与 worker 路径经此处共享同一规则）
        fd = g["nav_date"]
        if fund_excluded(basic["fund_type"].get(code, ""), fd.iloc[0],
                         fd.iloc[-1], min_span_days):
            continue
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
            res = (simulate(*prep, fee["gross"], min_span_days=min_span_days)
                   if prep else None)
            for k in COLS_FULL:
                row[f"{prefix}_{k}"] = res[k] if res else None
            # 含费场景多出年化列；w10 的三场景轮次明细全量入库（止盈日
            # 随场景份额略有差异，各自自洽）
            disc_res = (simulate(*prep, fee["disc"], min_span_days=min_span_days)
                        if prep else None)
            nom_res = (simulate(*prep, fee["nom"], min_span_days=min_span_days)
                       if prep else None)
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
                    log=print,
                    min_span_days: int | None = None) -> tuple[list[dict], list[tuple]]:
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
        # 回测宇宙排除（十四轮）：非权益/年轻基金不产生结果行（脚本离线
        # 路径与 worker 路径经此处共享同一规则）
        fd = g["nav_date"]
        if fund_excluded(basic["fund_type"].get(code, ""), fd.iloc[0],
                         fd.iloc[-1], min_span_days):
            continue
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
                                        method=method,
                                        min_span_days=min_span_days)
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


def run_full_backtest_tp_method(dsn: str, method: str, *, log=print,
                                progress=None,
                                with_side_tables: bool = False,
                                cutoff_iso: str | None = None
                                ) -> tuple[list[dict],
                                           list[tuple], dict]:
    """单策略全市场重算（设置页「运行单策略全量」按钮的执行体，2026-11-25
    九轮）：复用脏检测版的分片并行机制，但只算选定 method——新策略上线后
    不必等日更全量即可单独出数（其余方法结果不受影响）。

    progress：基金级进度回调（2026-11-25 十轮，与脏检测版同通道）——全市场
    基金集确定后先报 (0, 总数)，每块完成报 (累计基金数, 总数)。

    与 run_full_backtest_tp_dirty 的刻意差异（正确性锚）：
    - 不读/不写输入指纹：fund_dca_fp 的引擎串归日更所有，局部运行改写会让
      日更误判「引擎未变」而跳过其余方法的陈旧行——引擎陈旧判定留给日更
      兜底全量（正确性优先，接受一次重复计算）；
    - 不产旧口径行/时机行/风险行：三者与 method 无关，日更每晚统一维护；
    - end_ts = 全市场 max(nav_date)（索引尾扫，秒级；分片 worker 仍须显式
      下发全局锚，理由同脏检测版）；
    - alive 由本结果集窗口末派生 45 天 cutoff 重盖（合并后调用方再做
      method 域集合刷新兜底）。

    with_side_tables（2026-11-26 二十轮，日更逐方法分阶段根治项）：True 时
    保留 worker 顺带算好的旧口径/时机/风险行挂 stats["side"] 返回——三者
    与 method 无关，分阶段日更由 amount 首班次搭车产出（worker 本来就算、
    原实现丢弃，零额外计算）；旧口径行仅 method="amount" 非空（派生自
    amount+tp=TAKE_PROFIT），调用方负责在**同一事务**内合并（时机/风险
    表的新鲜度与 amount 方法戳绑定，中断不留半套）。缺省 False 不含
    "side" 键——单策略按钮 job 兼容零改动。
    cutoff_iso：alive 45 天锚（ISO 日期）。None（缺省，原行为）= worker
    按全死标注、父进程以本结果集窗口末重盖；提供时下传 worker 让 alive
    门控在计算期真实生效（**时机行=仅 alive 基金**，不给 cutoff 时机行
    恒空——搭车产时机行必须给），父进程重盖同源基准。

    返回 (tp_rows, tp_rounds, stats)；合并导入（merge_tp_*_method method
    域替换 + refresh_alive_method + 对比快照重建）由调用方完成。
    """
    if method not in DCA_METHODS:
        raise ValueError(f"unknown dca method: {method!r}")
    with psycopg.connect(dsn, connect_timeout=10) as conn:
        codes = _load_codes(conn)
        log(f"有净值的基金：{len(codes):,} 只")
        if not codes:
            raise SystemExit("fund_nav 为空，无从回测")
        with conn.cursor() as cur:
            cur.execute("SELECT max(nav_date) FROM fund_nav")
            row = cur.fetchone()
        end_ts = pd.Timestamp(row[0]) if row and row[0] else None
        # 运作期限下限随设置页（与日更同款读取与回退，读失败不崩）
        try:
            min_span_days = settings_store.get_min_span_days(
                conn, default=MIN_SPAN_DAYS)
        except Exception as exc:
            log(f"  设置表读取失败，回退运作期限下限 {MIN_SPAN_DAYS} 天：{exc}")
            min_span_days = MIN_SPAN_DAYS
        # 回测宇宙过滤（十五轮）：fp 表存有上轮逐基金指纹（fund_type+全史
        # 窗口），读出即可把被排除基金从分片与进度分母中剔除（允许一天内
        # 陈旧——worker 层 run_backtest_tp 的权威规则兜底）；fp 未收录的
        # 新基金保留；fp 表缺失/读取失败回落全 codes（不因口径丢基金）
        prev_fps = None
        try:
            from . import dca_store
            prev_fps, _prev_engine = dca_store.load_fingerprints(conn)
        except Exception as exc:
            log(f"  指纹表读取失败：{exc}")
        if prev_fps:
            universe_codes = {
                c for c, fp in prev_fps.items()
                if not fund_excluded(fp[7],
                                     pd.Timestamp(fp[1]) if fp[1] else None,
                                     pd.Timestamp(fp[2]) if fp[2] else None,
                                     min_span_days)}
            codes = [c for c in codes
                     if c in universe_codes or c not in prev_fps]
            log(f"宇宙过滤：{len(codes):,} 只（按上轮指纹剔除 "
                f"{len(prev_fps) - len(universe_codes):,} 只已排除）")
        else:
            # fp 表空（首跑/近次全量被中断未落指纹，2026-11-25 实测）→
            # 直接查库判定宇宙（权益白名单+全史窗口 ≥ 下限，与 worker 规则
            # 同语义；索引扫描约数分钟，仅过渡期发生——日更落指纹后走 fp
            # 快路径）；查库也失败 → 回落全 codes（行级排除兜底）
            try:
                with conn.cursor() as cur:
                    # nav_date 是 DATE 列：max-min 直取整数天数（PG 无
                    # integer >= interval 操作符——旧写法 make_interval
                    # 比较在真实库必抛 UndefinedFunction 被吞掉回落全
                    # 市场，二十一轮用户三次反馈的 26832 分母真凶；
                    # ::date 显式 cast 固定整数天语义，不依赖列类型）
                    cur.execute(
                        "SELECT n.code FROM fund_nav n "
                        "JOIN fund_basic b USING (code) "
                        "WHERE b.fund_type = ANY(%s) GROUP BY n.code HAVING "
                        "(max(n.nav_date)::date - min(n.nav_date)::date) >= %s",
                        (list(BACKTEST_UNIVERSE_TYPES), min_span_days))
                    universe = {r[0] for r in cur.fetchall()}
                codes_all = len(codes)
                codes = [c for c in codes if c in universe]
                log(f"宇宙过滤（直查）：{len(codes):,} 只（fp 表空，全市场 "
                    f"{codes_all:,} 只中剔除 {codes_all - len(codes):,} 只）")
            except Exception as exc:
                log(f"  宇宙直查失败，回落全量分母：{exc}")
    log(f"单策略全量：{method} / 净值截至 {end_ts} / 运作期限下限 "
        f"{min_span_days} 天")
    chunks = _chunk_codes(codes, _chunk_size())
    workers = min(_worker_count(), len(chunks)) or 1
    log(f"并行重算：{len(chunks)} 块 × ≤{_chunk_size()} 只 / {workers} worker")
    args = [(dsn, tuple(c), min_span_days, cutoff_iso, None,
             str(end_ts) if end_ts else None, (method,)) for c in chunks]
    # 基金级进度（2026-11-25 十轮，与脏检测版同通道）
    on_done = None
    if progress:
        progress(0, len(codes))
        sizes = [len(c) for c in chunks]
        done = {"n": 0}

        def on_done(idx):
            done["n"] += sizes[idx]
            progress(done["n"], len(codes))
    results = (_run_chunk_tasks(args, workers, on_done=on_done)
               if on_done else _run_chunk_tasks(args, workers))
    tp_rows: list[dict] = []
    tp_rounds: list[tuple] = []
    old_rows: list[dict] = []
    old_rounds: list[tuple] = []
    timing_rows: list[tuple] = []
    risk_rows: list[tuple] = []
    for c_old, c_old_rounds, c_tp, c_tp_rounds, c_timing, c_risk in results:
        tp_rows.extend(c_tp)
        tp_rounds.extend(c_tp_rounds)
        old_rows.extend(c_old)
        old_rounds.extend(c_old_rounds)
        timing_rows.extend(c_timing)
        risk_rows.extend(c_risk)
    # alive：本结果集窗口末（max full_end）派生 45 天 cutoff 重盖（worker
    # 传 cutoff None 已标全死，此处为权威值）；调用方下发 cutoff_iso 时
    # 同源基准（worker 计算期门控与父进程重盖逐位一致，见 docstring）
    all_end = max((r["full_end"] for r in tp_rows if r["full_end"]),
                  default=None)
    if cutoff_iso:
        cutoff = pd.Timestamp(cutoff_iso)
    else:
        cutoff = (pd.Timestamp(all_end) - pd.Timedelta(days=_ALIVE_WINDOW_DAYS)) \
            if all_end else None
    for r in tp_rows:
        r["alive"] = bool(cutoff and r["full_end"]
                          and pd.Timestamp(r["full_end"]) >= cutoff)
    stats = {"method": method, "fund_count": len(codes),
             "tp_round_count": len(tp_rounds),
             "thresholds": [float(t) for t in TP_THRESHOLDS],
             "window_end": str(all_end) if all_end else None,
             "engine": engine_version(min_span_days),
             "workers": workers, "chunks": len(chunks)}
    if with_side_tables:
        # 旧口径行 alive 同款重盖（worker 端 cutoff=None 时标全死）；时机
        # 行由 worker 按 alive 门控产出——cutoff_iso 缺省 None 时恒空，
        # 搭车产时机行必须下发 cutoff_iso（docstring 契约）
        for r in old_rows:
            r["alive"] = bool(cutoff and r["full_end"]
                              and pd.Timestamp(r["full_end"]) >= cutoff)
        stats["side"] = {"old_rows": old_rows, "old_rounds": old_rounds,
                         "timing_rows": timing_rows, "risk_rows": risk_rows}
    log(f"单策略重算完成：{method} / {stats['fund_count']:,} 只 / "
        f"轮次 {stats['tp_round_count']:,} 行 / {workers} worker × "
        f"{len(chunks)} 块，窗口截至 {all_end}")
    return tp_rows, tp_rounds, stats


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
        # 年化运作期限下限随设置页（backtest_min_span_days，缺省 90）：
        # 整轮统一一份值，日更/手动回放各自启动时读取；批量任务不许因
        # 设置表异常整轮崩——读失败记日志回落默认值
        try:
            min_span_days = settings_store.get_min_span_days(
                conn, default=MIN_SPAN_DAYS)
        except Exception as exc:
            log(f"  设置表读取失败，回退运作期限下限 {MIN_SPAN_DAYS} 天：{exc}")
            min_span_days = MIN_SPAN_DAYS
    log(f"净值行：{len(nav):,}；运作期限下限：{min_span_days} 天")
    tp_rows, tp_rounds = [], []
    for method in DCA_METHODS:
        m_rows, m_rounds = run_backtest_tp(nav, basic, scale_map, thresholds,
                                           method=method, log=log,
                                           min_span_days=min_span_days)
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
    # 回顾仍有意义）——循环体抽为 _timing_risk_rows 供脏检测日更共用
    timing_rows, risk_rows = _timing_risk_rows(nav, alive_by_code, all_end)
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


def _timing_risk_rows(nav: pd.DataFrame, alive_by_code: dict,
                      all_end) -> tuple[list[tuple], list[tuple]]:
    """时机行（定投时机视图，见 compute_dip_timing）+ 风险指标行
    （risk_metrics，列表波动列/详情指标卡共用）：一次 groupby 双产出；
    时机行只收 alive 基金（"当前时机"语义），风险行不限（退市基金波动
    回顾仍有意义）。全量与脏检测两路编排共用（脏检测只喂 dirty 基金子集）。"""
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
    return timing_rows, risk_rows


# ===== 脏检测日更（2026-11-24）：基金级输入指纹 + 引擎版本 → 只重算有变的基金 =====
# 动机：周末/节假日净值零更新，全市场重算产出与前夜完全相同的结果（5 方法
# 全量本机 ~6h）；按基金比对输入指纹，没变的复用库中上次结果行，周末夜更
# 从小时级降到分钟级。口径安全性：复权链 = cumprod(日增长率)（build_growth
# 逐行只依赖当天与前一天），追加新净值不改写历史复权点 → 已关闭轮次与
# 未变基金的全部结果行可安全复用；指纹含精确和值，数据源回头修正历史净值
# 也能抓住并强制重算。

# 引擎版本 base——**改算法/口径逻辑（simulate 状态机、XIRR 求解、窗口口径等
# 代码）时手动 bump**；参数常量（方法清单/止盈档位/费率表/回撤梯度等）由
# engine_version() 自动纳入哈希（调用时读模块全局，改源码即变），改动即
# 自动全量，不依赖人的记性。
ENGINE_VERSION_BASE = "2026-11-24.1"


def engine_version(*extra) -> str:
    """引擎版本串 = base + 参与计算的全部常量与 extra 的 sha1 前 8 位。

    常量清单在函数体内调用时取模块全局——凡参与逐基金计算的口径常量都在
    此（新增口径常量须同步补进，漏掉的改了不会触发自动全量 → 脏数据风险）。
    extra 携带运行时配置（如设置页的运作期限下限 min_span_days——DB 值
    不进代码常量，须显式传入），任何一项变化都让版本串变化 → 次日日更
    判定全部指纹失效 → 自动全量重算，杜绝表内混版本。"""
    material = repr((AMOUNT, TAKE_PROFIT, MIN_SPAN_DAYS, TP_THRESHOLDS,
                     DCA_METHODS, DD_LADDER, DD_LADDER_DEEP, DD_LADDERS,
                     PROFIT_LADDER, PROFIT_LADDERS, PL_LOSS_LADDER,
                     PL_PROFIT_LADDER, PL_LOSS_LADDERS,
                     PL_LOSS_LADDER_DEEP,
                     BACKTEST_UNIVERSE_TYPES, SUB_DISCOUNT,
                     SUB_RATE_A, _SUB_RATE_DEFAULT, REDEEM_EQUITY,
                     REDEEM_BOND_C)
                    + tuple(extra))
    digest = hashlib.sha1(material.encode()).hexdigest()[:8]
    return f"{ENGINE_VERSION_BASE}+{digest}"


# 指纹 SQL：一条 GROUP BY 在 PG 侧聚合全市场（~3200 万行扫描，实测 NAS
# ~10 分钟），不拉明细进内存。sum 用 ::numeric 精确累加——顺序无关且确定，
# 历史净值被源修正（count/max_date 不变）时和值必变；带 fund_basic
# （名称→C 类判定/类型→费率档/退市标记）与 fund_scale（规模/日期，
# 季度更新）字段，基础信息变更同样触发重算。nav_count<2 的基金不产生
# 回测结果行，不影响全量口径。
_FP_SQL = """
SELECT n.code,
       count(*)                                  AS nav_count,
       min(nav_date)::date::text                 AS min_date,
       max(nav_date)::date::text                 AS max_date,
       sum(unit_nav::numeric)::text              AS s_unit,
       sum(accum_nav::numeric)::text             AS s_accum,
       sum(coalesce(pct_chg, 0)::numeric)::text  AS s_pct,
       b.name                                    AS name,
       b.fund_type                               AS fund_type,
       b.delisted                                AS delisted,
       s.scale_yi::text                          AS scale_yi,
       s.scale_date::date::text                  AS scale_date
FROM fund_nav n
LEFT JOIN fund_basic b USING (code)
LEFT JOIN fund_scale s USING (code)
GROUP BY n.code, b.name, b.fund_type, b.delisted, s.scale_yi, s.scale_date
"""


def load_input_fingerprints(conn) -> dict[str, tuple]:
    """每基金回测输入指纹（元组，逐位可比）：nav 聚合 + basic + scale。
    只读，不落库；落库由 dca_store.replace_fingerprints 完成。"""
    out: dict[str, tuple] = {}
    with conn.cursor() as cur:
        cur.execute(_FP_SQL)
        for (code, nav_count, min_date, max_date, s_unit, s_accum, s_pct,
             name, fund_type, delisted, scale_yi, scale_date) in cur.fetchall():
            out[str(code)] = (int(nav_count), min_date, max_date,
                              s_unit, s_accum, s_pct,
                              name, fund_type,
                              None if delisted is None else bool(delisted),
                              scale_yi, scale_date)
    return out


def compute_dirty(cur_fps: dict[str, tuple], cur_engine: str,
                  prev_fps: dict[str, tuple] | None,
                  prev_engine: str | None) -> tuple[set[str], set[str]]:
    """脏集合推导（纯函数）：引擎版本变化或上次指纹缺失（首跑/表被清）→
    全量（dirty=全部，gone=∅）；否则指纹变化的基金 + 新基金为脏，消失的
    基金单列 gone（结果表清行用）。指纹值必须是同构元组（两路加载都已
    规范化为 tuple）。"""
    if not prev_fps or prev_engine != cur_engine:
        return set(cur_fps), set()
    dirty = {c for c, fp in cur_fps.items() if prev_fps.get(c) != fp}
    gone = set(prev_fps) - set(cur_fps)
    return dirty, gone


# ===== 单机并行重算（2026-11-24）：脏基金按基金分片 → 进程池动态负载均衡 =====
# 块小（缺省 250 只 ≈ 5 方法 ~3.5 分钟）+ 池按空闲领取下一块 → 历史长短
# 不同的基金天然均衡，无需预排序。worker 自开 PG 连接只加载自己分片的
# 净值（IO 随分片摊掉，父进程不再全量加载）。

def _chunk_codes(codes: list[str], size: int) -> list[list[str]]:
    """基金分片（保持顺序；负载均衡由池的空闲领取语义保证）。"""
    if size <= 0:
        size = 250
    return [list(codes[i:i + size]) for i in range(0, len(codes), size)]


def _chunk_size() -> int:
    """分片大小：BACKTEST_CHUNK_SIZE env 覆盖，缺省 250 只（NAS 内存吃紧
    时可调小，如 100，配合降 worker）。"""
    raw = os.environ.get("BACKTEST_CHUNK_SIZE", "")
    return int(raw) if raw.isdigit() and int(raw) >= 1 else 250


def _worker_count() -> int:
    """worker 进程数：BACKTEST_WORKERS env 覆盖；缺省 min(4, cpu)——NAS
    4 核 8GB 的安全缺省，本机手动全量可设 8~12。=1 串行内联（不拉池，
    单测亦走此路）。"""
    raw = os.environ.get("BACKTEST_WORKERS", "")
    if raw.isdigit() and int(raw) >= 1:
        return int(raw)
    return max(1, min(4, os.cpu_count() or 1))


def _run_chunk_tasks(args_list: list[tuple], workers: int,
                     on_done=None) -> list:
    """分片派发缝：workers≤1 或只有一块 → 串行内联（退化路径/单测）；
    否则 spawn 进程池。worker 异常原样抛回（调度器记 aborted，与顺序版
    一致）。on_done：每块完成回调（传完成块的 args 下标；2026-11-25 十轮
    基金级进度回传缝——闭包仅在本进程内使用不跨 pickle，串行/池两路同
    语义，池路按 as_completed 逐块回调、结果顺序仍与输入一致）。"""
    if workers <= 1 or len(args_list) <= 1:
        results = []
        for i, a in enumerate(args_list):
            results.append(_backtest_chunk_worker(*a))
            if on_done:
                on_done(i)
        return results
    import concurrent.futures
    import multiprocessing
    ctx = multiprocessing.get_context("spawn")
    with concurrent.futures.ProcessPoolExecutor(max_workers=workers,
                                                mp_context=ctx) as pool:
        futures = {pool.submit(_backtest_chunk_worker, *a): i
                   for i, a in enumerate(args_list)}
        results: list = [None] * len(args_list)
        for fut in concurrent.futures.as_completed(futures):
            i = futures[fut]
            results[i] = fut.result()   # 异常原样抛回（原 map 同语义）
            if on_done:
                on_done(i)
        return results


def _backtest_chunk_worker(dsn: str, codes, min_span_days: int,
                           cutoff_iso: str | None, computed_at_iso: str | None,
                           end_ts_iso: str | None,
                           methods: tuple = DCA_METHODS) -> tuple[
        list[dict], list[tuple], list[dict], list[tuple],
        list[tuple], list[tuple]]:
    """并行分片 worker（模块级函数可被 spawn pickle，教训②）。自加载本块
    basic/scale/nav → methods（缺省全部定投方式；单策略全量运行传单方法
    元组）× 8 阈值全算（end_ts 必须用父进程下发的全局值——缺省会取本块
    nav 的最大净值日，各块窗口右端漂移）→ 时机/风险行（按 cutoff 门控
    alive）。返回 (old_rows, old_rounds, tp_rows, tp_rounds, timing_rows,
    risk_rows)；alive 交由父进程统一重盖。"""
    with psycopg.connect(dsn, connect_timeout=10) as conn:
        basic = _load_basic(conn, list(codes))
        scale_map = _load_scale(conn)
        nav = _load_nav(conn, list(codes))
    end_ts = pd.Timestamp(end_ts_iso) if end_ts_iso else None
    thresholds = TP_THRESHOLDS
    tp_rows: list[dict] = []
    tp_rounds: list[tuple] = []
    for method in methods:
        m_rows, m_rounds = run_backtest_tp(nav, basic, scale_map, thresholds,
                                           end_ts=end_ts, method=method,
                                           log=print,
                                           min_span_days=min_span_days)
        tp_rows.extend(m_rows)
        tp_rounds.extend(m_rounds)
        print(f"[chunk {codes[0]}~{codes[-1]}] {method} 完成 "
              f"{len(m_rows):,} 行", flush=True)
    # 旧口径行 = amount 且 tp=TAKE_PROFIT 派生（同顺序版）
    old_rows = [{k: v for k, v in r.items() if k not in ("tp", "method")}
                for r in tp_rows
                if r["tp"] == TAKE_PROFIT and r["method"] == "amount"]
    old_rounds = [t[:1] + t[3:] for t in tp_rounds
                  if t[1] == "amount" and t[2] == TAKE_PROFIT]
    cutoff = pd.Timestamp(cutoff_iso) if cutoff_iso else None
    alive_by_code = {}
    for r in old_rows:
        r["alive"] = bool(cutoff is not None and r["full_end"]
                          and pd.Timestamp(r["full_end"]) >= cutoff)
        alive_by_code[r["code"]] = r["alive"]
    for r in tp_rows:
        r["alive"] = alive_by_code.get(r["code"], False)
    timing_rows, risk_rows = _timing_risk_rows(nav, alive_by_code,
                                               computed_at_iso)
    # 本块算完即释放净值（worker 进程会被池复用跑下一块）
    del nav, basic, scale_map
    gc.collect()
    return old_rows, old_rounds, tp_rows, tp_rounds, timing_rows, risk_rows


def run_full_backtest_tp_dirty(dsn: str, *, log=print,
                               progress=None,
                               cur_fps: dict[str, tuple] | None = None
                               ) -> tuple[
        list[dict], list[tuple], list[dict], list[tuple], list[tuple],
        list[tuple], dict, dict[str, tuple]]:
    """日更全量回测（脏检测版）：逐基金比对输入指纹，只重算"净值或基础
    信息有变"的基金，其余复用库中上次结果行；引擎版本变化（含设置页口径
    变更）或首跑自动退化为全量；BACKTEST_FORCE_FULL=1 强制全量。

    progress：基金级进度回调（2026-11-25 十轮）——dirty 集算出后先报
    (0, 需重算总数)，此后每块完成报 (累计基金数, 总数)；分母=本次需要
    回测的基金数（dirty 集，含强制全量的全市场）。调度器传
    functools.partial(task_runtime.write_progress, dsn, task_id) 跨 spawn
    池落库 task_runtime.progress。

    cur_fps：调用方预计算的全市场指纹（2026-11-26 二十轮——日更 job 为
    分支判定/收尾落库已先做过 ~10 分钟全市场 GROUP BY 聚合，传入复用，
    避免同一夜晚重复聚合两次；None（缺省）引擎内部计算，既有调用方
    （脚本/旧测试）零改动。

    返回 (old_rows, old_rounds, tp_rows, tp_rounds, timing_rows, risk_rows,
    stats, fingerprints)——结果行只含 dirty 基金；stats 同
    run_full_backtest_tp 并增补 engine/dirty_count/skipped_count/gone_count/
    full_run/dirty/gone 键；fingerprints 为本轮全市场指纹（调用方经
    dca_store.replace_fingerprints 落库）。合并导入（按 code 范围替换 +
    alive 全表刷新 + 时机死行清理 + 分位回填）由调用方用 dca_store.merge_*
    在单事务内完成；纯 CPU，调用方放子进程跑。"""
    from . import dca_store          # 惰性导入：引擎不依赖存储的依赖方向
    with psycopg.connect(dsn, connect_timeout=10) as conn:
        if cur_fps is None:
            cur_fps = load_input_fingerprints(conn)
        prev_fps, prev_engine = dca_store.load_fingerprints(conn)
        # 运作期限下限随设置页（与 run_full_backtest_tp 同款读取与回退）：
        # DB 值不进代码常量哈希，须作为 extra 纳入版本串
        try:
            min_span_days = settings_store.get_min_span_days(
                conn, default=MIN_SPAN_DAYS)
        except Exception as exc:
            log(f"  设置表读取失败，回退运作期限下限 {MIN_SPAN_DAYS} 天：{exc}")
            min_span_days = MIN_SPAN_DAYS
    engine = engine_version(min_span_days)
    if os.environ.get("BACKTEST_FORCE_FULL") == "1":
        prev_fps, prev_engine = None, None
        log("BACKTEST_FORCE_FULL=1，强制全量重算")
    dirty, gone = compute_dirty(cur_fps, engine, prev_fps, prev_engine)
    # 回测宇宙排除（十四轮）：指纹即可判定（fund_type + 全史窗口），无需
    # 加载明细；被排除基金的历史行由「版本变化→全量→merge 全删重插」自动
    # 清理（规则变化必 bump 版本 → 必走全量），无需专门清理机制
    excluded = {code for code, fp in cur_fps.items()
                if fund_excluded(fp[7],
                                 pd.Timestamp(fp[1]) if fp[1] else None,
                                 pd.Timestamp(fp[2]) if fp[2] else None,
                                 min_span_days)}
    log(f"回测宇宙：全市场 {len(cur_fps):,} 只，权益类且窗口 ≥ "
        f"{min_span_days} 天 {len(cur_fps) - len(excluded):,} 只（排除 "
        f"{len(excluded):,} 只）")
    # 全局窗口末日 = 全市场 max(末次净值日)（nav_count≥2 的基金——单行基金
    # 不产生结果行，排除以与旧口径 all_end=max(full_end) 逐位一致）
    all_end = max((fp[2] for fp in cur_fps.values() if fp[2] and fp[0] >= 2),
                  default=None)
    total = len(cur_fps)
    thresholds = TP_THRESHOLDS
    stats = {"fund_count": 0, "round_count": 0, "tp_round_count": 0,
             "timing_count": 0, "risk_count": 0,
             "thresholds": [float(t) for t in thresholds],
             "methods": list(DCA_METHODS),
             "window_end": str(all_end) if all_end else None,
             "engine": engine, "dirty_count": len(dirty),
             "skipped_count": total - len(dirty), "gone_count": len(gone),
             "universe_count": total - len(excluded),
             "excluded_count": len(excluded),
             "full_run": not prev_fps or prev_engine != engine,
             "dirty": sorted(dirty), "gone": sorted(gone)}

    if not dirty:
        # 周末/节假日快路径：无任何输入变化，零计算——alive 刷新/时机时间戳/
        # 分位回填/meta 由调用方的 merge_* 集合 SQL 完成
        log(f"指纹无变化：基金 {total:,} 只全部跳过重算"
            f"（清理 gone {len(gone):,} 只），仅刷新派生列")
        stats["fund_count"] = total
        return [], [], [], [], [], [], stats, cur_fps

    codes_dirty = sorted(dirty - excluded)   # 排除基金不进分片/分母（十五轮）；
    # stats["dirty"] 仍含它们——merge 按 code 范围 DELETE 清历史行
    log(f"脏检测：基金 {total:,} 只，重算 {len(dirty):,} 只 / 跳过 "
        f"{total - len(dirty):,} 只（清理 gone {len(gone):,} 只），引擎 {engine}")
    # 两个全局锚点由父进程从指纹派生后下发 worker（分片后不能各自取局部
    # max）：end_ts=全市场 max 净值日（含单行基金，与全量 nav.max() 逐位
    # 一致——最新净值日基金必然有新指纹=必然在重算集内）；computed_at/
    # all_end=按 nav_count≥2 过滤（单行基金无结果行，同旧口径）；cutoff=
    # alive 45 天窗口
    end_ts = max((fp[2] for fp in cur_fps.values() if fp[2]), default=None)
    computed_at = str(all_end)[:10] if all_end else None
    cutoff_iso = (str((pd.Timestamp(all_end)
                       - pd.Timedelta(days=_ALIVE_WINDOW_DAYS)).date())
                  if all_end else None)
    chunks = _chunk_codes(codes_dirty, _chunk_size())
    workers = min(_worker_count(), len(chunks)) or 1
    log(f"并行重算：{len(chunks)} 块 × ≤{_chunk_size()} 只 / {workers} worker")
    args = [(dsn, tuple(c), min_span_days, cutoff_iso, computed_at,
             str(end_ts) if end_ts else None) for c in chunks]
    # 基金级进度（2026-11-25 十轮）：先报 (0, 重算总数)，每块完成累加；
    # on_done 闭包仅在本进程内使用（外层池只 pickle worker fn+args），分母
    # =本次需要回测的基金数
    on_done = None
    if progress:
        dirty_total = len(codes_dirty)
        progress(0, dirty_total)
        sizes = [len(c) for c in chunks]
        done = {"n": 0}

        def on_done(idx):
            done["n"] += sizes[idx]
            progress(done["n"], dirty_total)
    results = (_run_chunk_tasks(args, workers, on_done=on_done)
               if on_done else _run_chunk_tasks(args, workers))
    tp_rows: list[dict] = []
    tp_rounds: list[tuple] = []
    old_rows: list[dict] = []
    old_rounds: list[tuple] = []
    timing_rows: list[tuple] = []
    risk_rows: list[tuple] = []
    for c_old, c_old_rounds, c_tp, c_tp_rounds, c_timing, c_risk in results:
        tp_rows.extend(c_tp)
        tp_rounds.extend(c_tp_rounds)
        old_rows.extend(c_old)
        old_rounds.extend(c_old_rounds)
        timing_rows.extend(c_timing)
        risk_rows.extend(c_risk)
    # alive 统一重盖（worker 已按同谓词标过，父进程再盖一次保证与顺序版
    # 逐位一致；跳过基金的 alive 由 merge 后的全表刷新 SQL 统一翻转）
    cutoff = (pd.Timestamp(all_end) - pd.Timedelta(days=_ALIVE_WINDOW_DAYS)) \
        if all_end else None
    alive_by_code = {}
    for r in old_rows:
        r["alive"] = bool(cutoff and r["full_end"]
                          and pd.Timestamp(r["full_end"]) >= cutoff)
        alive_by_code[r["code"]] = r["alive"]
    for r in tp_rows:
        r["alive"] = alive_by_code.get(r["code"], False)
    stats.update({"fund_count": total, "round_count": len(old_rounds),
                  "tp_round_count": len(tp_rounds),
                  "timing_count": len(timing_rows),
                  "risk_count": len(risk_rows),
                  "workers": workers, "chunks": len(chunks)})
    log(f"重算完成：{len(dirty):,} 只 / {workers} worker × {len(chunks)} 块 / "
        f"旧口径轮次 {len(old_rounds):,} 行 / "
        f"多阈值轮次 {len(tp_rounds):,} 行 / 时机行 {len(timing_rows):,} / "
        f"风险行 {len(risk_rows):,}，窗口截至 {all_end}")
    return (old_rows, old_rounds, tp_rows, tp_rounds, timing_rows, risk_rows,
            stats, cur_fps)


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
