"""全市场基金「定投 + 20% 止盈」回测核心（离线脚本与调度器日更共用）。

口径约定：
- 定投：每只基金每月首个净值日投入固定 1000 元（金额不影响收益率）；
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
"""

import io
from typing import NamedTuple

import numpy as np
import pandas as pd
import psycopg
from scipy.optimize import brentq

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
    profit, tp_nav, invested, sub_fee, redeem_fee, net_profit)——
    profit 为市值口径收益率（≥20%，含跳空越过部分）；tp_nav=止盈日
    复权净值；invested=本轮累计投入；sub_fee/redeem_fee=本轮申购费
    合计/赎回费（无费场景为 0）；net_profit=本轮净收益（回款-投入）。
    """
    months = dates.to_numpy().astype("datetime64[M]")
    seen_months = set()
    lots: list[tuple[int, float]] = []   # (买入日下标, 份额)
    basis_units = 0.0                    # Σ 份额，市值 = basis_units*adj(t)
    invested = 0.0
    round_sub_fee = 0.0                  # 本轮累计申购费（外扣法折损）
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
            round_sub_fee += AMOUNT - net_amount
            flows.append((dates[i], -AMOUNT))
        if basis_units > 0 and basis_units * a >= invested * (1.0 + TAKE_PROFIT):
            value = basis_units * a
            net = _net_redeem(lots, dates, i, a, fee)
            redeem_fee = value - net
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
                round(a, 4),                     # 止盈日复权净值
                round(invested, 2),              # 本轮累计投入
                round(round_sub_fee, 2),         # 本轮申购费合计
                round(redeem_fee, 2),            # 本轮赎回费
                round(net - invested, 2),        # 本轮净收益（回款-投入）
            ))
            last_tp = dates[i]
            lots = []
            basis_units = 0.0
            invested = 0.0
            round_sub_fee = 0.0

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
            row[f"{prefix}_xirr_disc"] = disc_res["xirr"] if disc_res else None
            row[f"{prefix}_xirr_nom"] = nom_res["xirr"] if nom_res else None
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
    return {r["code"]: {"scale_yi": r["scale_yi"],
                        "scale_date": (str(r["scale_date"].date())
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
