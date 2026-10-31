"""全市场基金「定投 + 20% 止盈」回测。

口径约定：
- 范围：scripts/data/fund_scale.csv 中最新披露资产规模 ≥ 1 亿元的基金；
- 定投：每只基金每月首个净值日投入固定 1000 元（金额不影响收益率）；
- 复权：东财日增长率 pct_chg 已是分红再投资口径（已验证），链乘合成复权净值；
  pct_chg 缺失行用 (unit_nav+每份分红)/前日单位净值 兜底（分红由累计净值差推出）；
- 止盈：任一净值日持仓收益率 = 持仓市值/已投入 - 1 ≥ 20% 即全仓赎回并开启新一轮
  （触发线按市值口径，三场景共用同一条路径）；
- 费用：持有期费用（管理/托管/销售服务费）已在净值口径内；交易费用按
  申购费（外扣法，按买入折份额）+ 赎回费（按每笔份额持有天数套阶梯）建模，
  跑三场景——gross 不含费（原口径）/ disc 申购费一折 / nom 申购费名义
  （赎回费一律不折扣）。全市场无结构化费率源，费率按基金类型 + A/C 类
  估典型档位（精确的近似）；
- 年化：XIRR（资金加权，现金流 = 每月 -1000 / 止盈日 +净回款 / 期末清算 +净回款；
  含费场景的回款为扣赎回费后净额，投入现金流仍记全额）；
- 双窗口：full=基金全历史；10y=近 10 年（窗口内成立则从成立起）。

输出 scripts/data/fund_dca_backtest.csv（每基金一行，双窗口指标；
xirr 三列 *_xirr / *_xirr_disc / *_xirr_nom 对应三场景）。
用法：.venv/bin/python scripts/fund_dca_backtest.py
"""

import csv
import io
import os
from datetime import date
from pathlib import Path
from typing import NamedTuple

import numpy as np
import pandas as pd
import psycopg
from scipy.optimize import brentq

DB_URL = os.environ.get(
    "DATABASE_URL", "postgresql://honey_money:honey_money@host.docker.internal:5433/honey_money"
)
DATA = Path(__file__).parent / "data"
SCALE_CSV = DATA / "fund_scale.csv"
OUT_CSV = DATA / "fund_dca_backtest.csv"

AMOUNT = 1000.0
TAKE_PROFIT = 0.20
WINDOW_10Y = pd.DateOffset(years=10)
MIN_SPAN_DAYS = 90  # 窗口短于此天数的年化记 nan


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


def simulate(dates: pd.DatetimeIndex, adj: np.ndarray,
             fee: FeeSchedule | None = None) -> dict:
    """单基金单窗口：每月首个净值日买入，日频检查 20% 止盈。

    fee=None 为无费口径（原行为）；给定调度时申购按外扣法折份额
    （净申购 = 金额/(1+费率)，投入现金流仍记全额），止盈/期末清算的
    回款现金流为扣赎回费后净额。止盈触发线按市值口径，与用户在
    APP 里看到的一致，三场景共用。

    res["flows"]：XIRR 现金流 [(日期, 金额)]。
    res["round_events"]：每轮止盈事件 (tp_date, round_days, gap_days,
    profit)——profit 为市值口径收益率（≥20%，含跳空越过部分）。
    """
    months = dates.to_numpy().astype("datetime64[M]")
    seen_months = set()
    lots: list[tuple[int, float]] = []   # (买入日下标, 份额)
    basis_units = 0.0                    # Σ 份额，市值 = basis_units*adj(t)
    invested = 0.0
    rounds = 0
    realized = 0.0
    flows: list[tuple[pd.Timestamp, float]] = []
    round_events: list[tuple] = []
    round_start: pd.Timestamp | None = None
    last_tp: pd.Timestamp | None = None

    for i in range(len(dates)):
        a = adj[i]
        m = months[i]
        if m not in seen_months:
            seen_months.add(m)
            if basis_units == 0.0:
                round_start = dates[i]      # 新一轮从本轮首次买入起算
            net_amount = AMOUNT if fee is None else AMOUNT / (1.0 + fee.sub_rate)
            lots.append((i, net_amount / a))
            basis_units += net_amount / a
            invested += AMOUNT
            flows.append((dates[i], -AMOUNT))
        if basis_units > 0 and basis_units * a >= invested * (1.0 + TAKE_PROFIT):
            value = basis_units * a
            net = _net_redeem(lots, dates, i, a, fee)
            realized += net - invested
            rounds += 1
            flows.append((dates[i], net))
            gap = (dates[i] - last_tp).days if last_tp is not None else \
                (dates[i] - round_start).days
            round_events.append((
                str(dates[i].date()),
                (dates[i] - round_start).days if round_start is not None else None,
                gap,
                round(value / invested - 1.0, 4),
            ))
            last_tp = dates[i]
            lots = []
            basis_units = 0.0
            invested = 0.0

    if basis_units > 0:
        end_value = _net_redeem(lots, dates, len(dates) - 1, adj[-1], fee)
        flows.append((dates[-1], end_value))
    else:
        end_value = 0.0
    invested_total = -sum(a for _, a in flows if a < 0)  # 正数口径
    profit = realized + (end_value - invested) if basis_units > 0 else realized
    span = (dates[-1] - dates[0]).days
    res = {
        "start": str(dates[0].date()),
        "end": str(dates[-1].date()),
        "months": len(seen_months),
        "rounds": rounds,
        "round_events": round_events,
        "flows": flows,
        "invested": round(invested_total, 2),
        "final_value": round(invested_total + profit, 2),
        "profit_rate": profit / invested_total if invested_total > 0 else None,
        "xirr": xirr(flows) if span >= MIN_SPAN_DAYS else None,
    }
    return res


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


def run_window(df: pd.DataFrame, start: pd.Timestamp | None,
               fee: FeeSchedule | None = None) -> dict | None:
    prep = prep_window(df, start)
    if prep is None:
        return None
    return simulate(prep[0], prep[1], fee)


COLS_FULL = ["start", "end", "months", "rounds", "invested", "final_value",
             "profit_rate", "xirr"]


def main():
    import argparse
    ap = argparse.ArgumentParser(description="全市场基金「定投+20%止盈」回测")
    ap.add_argument("--min-age-years", type=float, default=0.0,
                    help="仅回测运作 ≥N 年的基金（首条净值距数据集最新净值日，"
                         "成立日期的可用代理；0=不过滤）")
    ap.add_argument("--min-scale-yi", type=float, default=0.0,
                    help="仅回测资产规模 ≥N 亿的基金（0=全市场不限规模，"
                         "含无规模披露的基金）")
    args = ap.parse_args()
    min_age_years = args.min_age_years
    min_scale_yi = args.min_scale_yi

    scale = pd.read_csv(SCALE_CSV, dtype={"code": str})
    if min_scale_yi > 0:
        scale = scale.dropna(subset=["scale_yi"])
        selected = scale[scale["scale_yi"] >= min_scale_yi]["code"].tolist()
        print(f"规模 ≥{min_scale_yi:g} 亿基金：{len(selected)} 只")
    else:
        selected = scale["code"].tolist()
        print(f"全市场（不限规模）：{len(selected)} 只")

    with psycopg.connect(DB_URL, connect_timeout=10) as conn:
        basic = pd.read_sql(
            "SELECT code, name, fund_type, delisted FROM fund_basic", conn,
            dtype={"code": str})
        codes_arr = "{" + ",".join(selected) + "}"
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
    nav = pd.read_csv(buf, dtype={"code": "category"}, parse_dates=["nav_date"])
    print(f"净值行：{len(nav):,}")

    basic = basic.set_index("code")
    scale_idx = scale.set_index("code")
    end_ts = pd.Timestamp(nav["nav_date"].max())

    rows = []
    rounds_rows = []
    skipped_age = 0
    for code, g in nav.groupby("code", sort=True):
        g = g.sort_values("nav_date")
        if min_age_years:
            # 运作年限：首条净值距数据集最新净值日（fund_basic 无成立日期列，
            # 首次净值披露是其可靠代理）；不足门槛整体剔除
            first = pd.Timestamp(g["nav_date"].iloc[0])
            if (end_ts - first).days < min_age_years * 365.25:
                skipped_age += 1
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
            "delisted": bool(basic["delisted"].get(code, False)),
            "scale_yi": scale_idx["scale_yi"].get(code),
            "scale_date": scale_idx["scale_date"].get(code),
        }
        for prefix, prep in (("full", full_p), ("w10", w10_p)):
            res = simulate(*prep, fee["gross"]) if prep else None
            for k in COLS_FULL:
                row[f"{prefix}_{k}"] = res[k] if res else None
            # 含费场景只多出年化列；轮次等事件沿用无费口径（触发线一致）
            row[f"{prefix}_xirr_disc"] = (
                simulate(*prep, fee["disc"])["xirr"] if prep else None)
            row[f"{prefix}_xirr_nom"] = (
                simulate(*prep, fee["nom"])["xirr"] if prep else None)
            if prefix == "w10":
                w10_res = res
        rows.append(row)
        if w10_res:
            for seq, ev in enumerate(w10_res["round_events"], 1):
                rounds_rows.append([code, seq, *ev])
        if len(rows) % 1000 == 0:
            print(f"  已回测 {len(rows)} 只", flush=True)

    out = pd.DataFrame(rows)
    out = out.sort_values("w10_xirr", ascending=False, na_position="last")
    out.to_csv(OUT_CSV, index=False)
    with (DATA / "fund_dca_rounds.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["code", "seq", "tp_date", "round_days", "gap_days", "profit"])
        w.writerows(rounds_rows)
    print(f"写入 {OUT_CSV}：{len(out)} 只"
          + (f"（运作 ≥{min_age_years:g} 年门槛剔除 {skipped_age} 只）"
             if min_age_years else ""))

    # 在市判定用净值新鲜度：fund_basic.delisted 按快照渠道差集计算，
    # 场内 ETF 不在该渠道会被误标（以 max(nav_date) 近 45 天为准）
    end_all = pd.Timestamp(nav["nav_date"].max())
    out["full_end"] = pd.to_datetime(out["full_end"])
    live = out[out["full_end"] >= end_all - pd.Timedelta(days=45)]
    for tag, df in (("在市", live), ("全部", out)):
        s = df["w10_xirr"].dropna()
        if len(s) == 0:
            continue
        q = s.quantile([0.1, 0.25, 0.5, 0.75, 0.9])
        print(f"\n[{tag} {len(df)} 只] 近10年 XIRR 分布："
              f"P10={q[0.1]:.1%} P25={q[0.25]:.1%} 中位={q[0.5]:.1%} "
              f"P75={q[0.75]:.1%} P90={q[0.9]:.1%} 均值={s.mean():.1%}")
        print(f"  其中止盈至少1次：{(df['w10_rounds'] > 0).sum()} 只"
              f"（{(df['w10_rounds'] > 0).mean():.0%}）")


if __name__ == "__main__":
    main()
