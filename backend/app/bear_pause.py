"""熊市断缴定投实验引擎（纯函数，scripts/bear_pause_import.py 与单测共用）。

口径约定（与 scratch/bear_pause_experiment.py 2026-11-19 实验逐字段一致，
校验锚已转正单测）：
- 熊市判定（因果、无前视）：复权净值 dd(t)=adj/cummax-1 迟滞带：
  dd ≤ -20% 进入熊市态、恢复 dd > -10% 才退出（防边缘反复横跳）；
- 四组（同基金同窗口配对，gross 无费）：
  A 坚持定投（每月首个净值日 1000，基准）
  B 断缴闲置（熊市态停投，钱囤现金 0 收益，恢复后继续月投）
  C 断缴补投（同 B，恢复转出日把囤积现金一次性买入）
  D 熊市加倍（熊市态每月投 2000，反向参照；钱包比较按多投入 0 收益折算同额）
- 双口径：tp=0.20 止盈（日频市值≥投入×1.2 全清重开）与无止盈（躺平）；
- 钱包 = 止盈累计回款 + 期末市值 + 期末囤现金（全部留存，0% 机会成本——
  最保守口径）；out = 出钱包总额（B 的囤现金也算出钱包）；
  XIRR = 全部出钱包现金流的资金加权年化（复用 dca_backtest.xirr 多根处理）；
- 市场级聚合：子样本（全体/仅断缴/末回撤三分层）× 两口径的配对差分布
  （钱包差中位/四分位 + 「比坚持定投更薄」占比）。

结论速览（2026-11-19 全市场 9,576 只，止盈 20% 口径）：断缴闲置中位薄
~7 千元 / 82% 基金更差；恢复补投基本追平（+68 元）；熊市加倍 68% 基金更好。
「起不来保护」只在躺平不止盈 + 末日期深套场景近似成立。不构成投资建议。
"""

import numpy as np

from .dca_backtest import xirr

PAUSE_LEVEL = 0.20        # dd ≤ -20% 进入熊市态（断缴/加倍触发线）
RESUME_LEVEL = 0.10       # 恢复 dd > -10% 退出（迟滞带下沿）
AMOUNT = 1000.0
TP_MAIN = 0.20            # 主线止盈点（与现有体系一致）
MIN_SPAN_DAYS = 1095      # 窗口 ≥36 个月（XIRR 可比性，既有口径）
ARMS = ("a", "b", "c", "d")
ARM_DESC = {"a": "坚持定投", "b": "断缴闲置", "c": "断缴补投", "d": "熊市加倍"}
MODES = ("tp", "nt")
MODE_DESC = {"tp": "止盈20%", "nt": "无止盈"}
SUBSETS = ("all", "paused_only", "near_high", "mid", "stuck")
SUBSET_DESC = {"all": "全体", "paused_only": "仅断缴过的基金",
               "near_high": "末回撤 >-10%（接近前高）",
               "mid": "末回撤 -30%~-10%（中间地带）",
               "stuck": "末回撤 ≤-30%（起不来）"}
CASE_CODES = ("161725", "012414", "005827", "110022", "510300")


def bear_state(dd: np.ndarray, enter: float, resume: float):
    """迟滞带熊市态：dd ≤ -enter 进入、恢复 dd > -resume 退出。

    返回 (bear 布尔序列, 恢复转出日下标列表, 进入次数)。enter/resume 传
    1.01/0.0 可构造"永不入熊"（单测锚②：此时 B/C/D 必须退化为 A）。
    """
    n = len(dd)
    bear = np.zeros(n, dtype=bool)
    trans = []
    in_bear = False
    episodes = 0
    for i in range(n):
        if not in_bear:
            if dd[i] <= -enter:
                in_bear = True
                episodes += 1
        elif dd[i] > -resume:
            in_bear = False
            trans.append(i)
        bear[i] = in_bear
    return bear, trans, episodes


def simulate_arm(bear: np.ndarray, flush_days: set, newm: np.ndarray,
                 adj: np.ndarray, dates, tp: "float | None",
                 mode: str) -> dict:
    """单基金单组单口径模拟（mode ∈ 'A'|'B'|'C'|'D'）。

    日序：月投判断（熊市态 B/C 跳过并囤现金、D 投 2×）→ C 恢复日 flush
    → 止盈检查（与 dca_backtest.simulate_multi 同序，A 组与其逐字段一致）。
    XIRR 现金流 = 每笔出钱包为负（含 B 囤现金）、止盈回款/期末清算为正
    （含囤现金按面值回钱包，0% 机会成本）。
    """
    n = len(adj)
    lots_x = 0.0          # 当轮 Σ 份额
    inv_round = 0.0       # 当轮投入
    realized_cash = 0.0   # 止盈累计回款（留存钱包）
    idle_cash = 0.0       # 断缴囤现金（B/C）
    ft, fa = [], []       # 现金流（日下标, 金额）
    out = 0.0
    rounds = 0
    pause_months = 0
    flushed = 0.0
    tp_active = tp is not None
    for i in range(n):
        if newm[i]:
            if mode == "A":
                buy = AMOUNT
            elif mode == "D":
                buy = 2.0 * AMOUNT if bear[i] else AMOUNT
            elif bear[i]:                      # B/C 熊市态 → 断缴囤现金
                idle_cash += AMOUNT
                pause_months += 1
                ft.append(i); fa.append(-AMOUNT)
                out += AMOUNT
                buy = 0.0
            else:
                buy = AMOUNT
            if buy > 0.0:
                lots_x += buy / adj[i]
                inv_round += buy
                ft.append(i); fa.append(-buy)
                out += buy
        if mode == "C" and idle_cash > 0.0 and i in flush_days:
            u = idle_cash / adj[i]
            lots_x += u
            inv_round += idle_cash
            flushed += idle_cash
            idle_cash = 0.0
        if tp_active and lots_x > 0.0 and lots_x * adj[i] >= inv_round * (1.0 + tp):
            v = lots_x * adj[i]
            realized_cash += v
            rounds += 1
            ft.append(i); fa.append(v)
            lots_x = 0.0
            inv_round = 0.0
    end_v = lots_x * adj[-1]
    wallet = realized_cash + end_v + idle_cash
    if end_v > 0.0:
        ft.append(n - 1); fa.append(end_v)
    if idle_cash > 0.0:
        ft.append(n - 1); fa.append(idle_cash)
    flows = [(dates[t], a) for t, a in zip(ft, fa)]
    return {"out": round(out, 2), "wallet": round(wallet, 2),
            "xirr": xirr(flows), "rounds": rounds,
            "pause_months": pause_months, "flushed": round(flushed, 2),
            "idle_cash": round(idle_cash, 2)}


def run_fund(dates, adj: np.ndarray) -> dict:
    """单基金全量模拟：两口径 × 四组（8 次模拟）+ 断缴行为与回撤元信息。

    返回扁平 dict，键与 fund_bear_pause_fund 列一致（code/in_universe 由
    调用方补齐）。pause_months 取 B 组口径（与 C 相同——断缴行为只由
    熊市态决定，与止盈口径无关）。
    """
    n = len(adj)
    cummax = np.maximum.accumulate(adj)
    dd = adj / cummax - 1.0
    bear, trans, episodes = bear_state(dd, PAUSE_LEVEL, RESUME_LEVEL)
    mo = dates.to_numpy().astype("datetime64[M]").astype(np.int64)
    newm = np.empty(n, dtype=bool)
    newm[0] = True
    newm[1:] = mo[1:] != mo[:-1]
    flush_days = set(trans)
    row = {"span_days": int((dates[-1] - dates[0]).days),
           "months": int(newm.sum()),
           "ever_paused": bool(episodes > 0), "episodes": episodes,
           "dd_end": round(float(dd[-1]), 4), "dd_min": round(float(dd.min()), 4)}
    for prefix, tp in (("tp", TP_MAIN), ("nt", None)):
        for arm in ARMS:
            r = simulate_arm(bear, flush_days, newm, adj, dates, tp,
                             arm.upper())
            row[f"{prefix}_{arm}_wallet"] = r["wallet"]
            row[f"{prefix}_{arm}_xirr"] = r["xirr"]
            row[f"{prefix}_{arm}_out"] = r["out"]
            row[f"{prefix}_{arm}_rounds"] = r["rounds"]
            if arm == "b":
                row["pause_months"] = r["pause_months"]
    return row


# ===== 市场级聚合：子样本 × 口径的 A/B/C/D 配对差分布 =====

def _subset(funds: list[dict], key: str) -> list[dict]:
    if key == "all":
        return funds
    if key == "paused_only":
        return [f for f in funds if f["ever_paused"]]
    if key == "near_high":
        return [f for f in funds if f["dd_end"] > -RESUME_LEVEL]
    if key == "mid":
        return [f for f in funds
                if -0.30 < f["dd_end"] <= -RESUME_LEVEL]
    return [f for f in funds if f["dd_end"] <= -0.30]


def _arr(rows: list[dict], key: str) -> np.ndarray:
    return np.asarray([np.nan if r[key] is None else r[key] for r in rows],
                      dtype=float)


def _stat(a: np.ndarray, q: float):
    """nan 安全分位数（全 nan → None）。"""
    if len(a) == 0 or np.all(np.isnan(a)):
        return None
    return round(float(np.nanquantile(a, q)), 4)


def _ratio(mask: np.ndarray):
    if len(mask) == 0 or np.all(np.isnan(mask.astype(float))):
        return None
    return round(float(np.nanmean(mask)), 4)


def aggregate_market(funds: list[dict]) -> list[dict]:
    """市场级聚合行（键与 fund_bear_pause_market 列一致，subset 顺序固定）。

    钱包差：B/C 出钱包节奏与 A 完全一致 → wallet_m − wallet_a 直接可比；
    D 多投的钱按 0 收益扣回（wallet_adj = wallet − (out − out_A)）。
    """
    rows = []
    for mode in MODES:
        for key in SUBSETS:
            sub = _subset(funds, key)
            n = len(sub)
            if n == 0:
                continue
            wA = _arr(sub, f"{mode}_a_wallet")
            xA = _arr(sub, f"{mode}_a_xirr")
            block = {"subset": key, "mode": mode, "n": n,
                     "med_out_a": _stat(_arr(sub, f"{mode}_a_out"), 0.5),
                     "med_wallet_a": _stat(wA, 0.5),
                     "xirr_a_med": _stat(xA, 0.5),
                     "xirr_a_p25": _stat(xA, 0.25),
                     "xirr_a_p75": _stat(xA, 0.75)}
            for arm in ("b", "c", "d"):
                wm = _arr(sub, f"{mode}_{arm}_wallet")
                if arm == "d":
                    wm = wm - (_arr(sub, f"{mode}_d_out")
                               - _arr(sub, f"{mode}_a_out"))
                xm = _arr(sub, f"{mode}_{arm}_xirr")
                wd = wm - wA
                xd = xm - xA
                block[f"{arm}_xirr_med"] = _stat(xm, 0.5)
                block[f"{arm}_xirr_lower_pct"] = _ratio(xd < 0)
                block[f"{arm}_wallet_delta_med"] = _stat(wd, 0.5)
                block[f"{arm}_wallet_delta_p25"] = _stat(wd, 0.25)
                block[f"{arm}_wallet_delta_p75"] = _stat(wd, 0.75)
                block[f"{arm}_wallet_lower_pct"] = _ratio(wd < 0)
            rows.append(block)
    return rows
