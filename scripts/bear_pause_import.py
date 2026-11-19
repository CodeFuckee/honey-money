"""熊市断缴实验数据导入管线（权益池全量 → fund_bear_pause_* 三表）。

计算：app/bear_pause.py 纯函数引擎（口径与 scratch/bear_pause_experiment.py
2026-11-19 全市场实验逐字段一致，校验锚已转正单测）；
存储：app/dca_store.import_bear_pause（三表整表替换，事务由本脚本提交）。
宇宙 = 权益 8 类白名单（dca_api.EQUITY_TYPES）+ alive 45 天 + 窗口 ≥36 个月
∪ 案例基金（bear_pause.CASE_CODES，in_universe=False 时仅入基金表不进
市场级统计）。

用法（本机）：
  .venv/bin/python scripts/bear_pause_import.py [--limit N] [--dsn ...]
约 3 分钟（拉数 ~2 min + 计算 ~1.5 min）；只读 fund_nav/fund_basic，
写三张 fund_bear_pause_* 表。前端 /api/funds/dca/pause-experiment 与
/api/funds/dca/bear-pause/{code} 的数据源。
"""

import argparse
import io
import json
import sys
import time

import numpy as np
import pandas as pd
import psycopg

sys.path.insert(0, "backend")

from app import bear_pause                                # noqa: E402
from app.dca_api import EQUITY_TYPES                      # noqa: E402
from app.dca_backtest import prep_window                  # noqa: E402
from app.dca_store import (BEAR_PAUSE_FUND_COLS,          # noqa: E402
                           BEAR_PAUSE_MARKET_COLS,
                           ensure_bear_pause_schema, import_bear_pause)

DSN = "postgresql://honey_money:honey_money@10.0.0.169:5433/honey_money"
ALIVE_DAYS = 45

t0 = time.time()
ap = argparse.ArgumentParser(description="熊市断缴实验数据导入（只读净值，写三表）")
ap.add_argument("--dsn", default=DSN)
ap.add_argument("--limit", type=int, default=0, help="只处理前 N 只（冒烟）")
args = ap.parse_args()

# ---- 拉数：权益池 ∪ 案例基金 全量净值 ----
fetch_codes = list(bear_pause.CASE_CODES)
with psycopg.connect(args.dsn, connect_timeout=10) as conn:
    uni_codes = pd.read_sql(
        "SELECT DISTINCT n.code FROM fund_nav n "
        "JOIN fund_basic b ON b.code = n.code "
        "WHERE b.fund_type = ANY(%s)",
        conn, params=(list(EQUITY_TYPES),),
        dtype={"code": str})["code"].tolist()
    print(f"权益池基金：{len(uni_codes):,} 只", flush=True)
    fetch = list(dict.fromkeys(uni_codes + fetch_codes))
    arr = "{" + ",".join(fetch) + "}"
    buf = io.BytesIO()
    with conn.cursor() as cur:
        with cur.copy(
            "COPY (SELECT code, nav_date, unit_nav, accum_nav, pct_chg "
            f"FROM fund_nav WHERE code = ANY('{arr}'::text[]) "
            "ORDER BY code, nav_date) TO STDOUT (FORMAT csv, HEADER)"
        ) as cp:
            for chunk in cp:
                buf.write(chunk)
        names = pd.read_sql(
            "SELECT code, name FROM fund_basic "
            f"WHERE code = ANY('{arr}'::text[])",
            conn, dtype={"code": str}).set_index("code")["name"]
    buf.seek(0)
nav = pd.read_csv(buf, dtype={"code": "category"}, parse_dates=["nav_date"])
del buf
print(f"净值行：{len(nav):,}，拉数 {time.time()-t0:.0f}s", flush=True)

latest = nav["nav_date"].max()
cutoff = latest - pd.Timedelta(days=ALIVE_DAYS)
alive_codes = set(nav.groupby("code", observed=True)["nav_date"].max()
                  .loc[lambda x: x >= cutoff].index)
universe_set = set(uni_codes) & alive_codes
print(f"alive（{ALIVE_DAYS} 天新鲜度）∩ 权益池：{len(universe_set):,} 只",
      flush=True)

# ---- 逐基金模拟（引擎 run_fund） ----
funds = []
n_done = 0
t1 = time.time()
for code, g in nav.groupby("code", observed=True, sort=True):
    g = g.sort_values("nav_date").reset_index(drop=True)
    in_uni = code in universe_set
    if not in_uni and code not in bear_pause.CASE_CODES:
        continue
    prep = prep_window(g, None)
    if prep is None:
        continue
    dates, adj = prep
    if (dates[-1] - dates[0]).days < bear_pause.MIN_SPAN_DAYS:
        continue
    row = bear_pause.run_fund(dates, adj)
    row["code"] = code
    row["in_universe"] = in_uni
    funds.append(row)
    if in_uni:
        n_done += 1
        if n_done % 1000 == 0:
            print(f"  已回测 {n_done:,} 只 / {time.time()-t1:.0f}s", flush=True)
    if args.limit and n_done >= args.limit:
        break
print(f"回测完成：{n_done:,} 只（宇宙）/ 基金表 {len(funds)} 行 / "
      f"计算 {time.time()-t1:.0f}s / 总 {time.time()-t0:.0f}s", flush=True)

if n_done == 0:
    sys.exit("无有效样本，检查过滤条件")

# ---- 市场级聚合（仅宇宙内基金；案例基金不进统计） ----
uni_funds = [f for f in funds if f["in_universe"]]
market_blocks = bear_pause.aggregate_market(uni_funds)

ever = [f for f in uni_funds if f["ever_paused"]]
params = {
    "config": {"pause_level": bear_pause.PAUSE_LEVEL,
               "resume_level": bear_pause.RESUME_LEVEL,
               "amount": bear_pause.AMOUNT,
               "tp_main": bear_pause.TP_MAIN,
               "min_span_days": bear_pause.MIN_SPAN_DAYS,
               "alive_days": ALIVE_DAYS, "fee": "gross 无费",
               "wallet_note": "钱包=止盈累计回款+期末市值+期末囤现金(0%机会成本)",
               "d_wallet_note": "D 钱包差按多投入 0 收益折算同额"},
    "universe": {
        "equity_pool": len(uni_codes), "alive": len(universe_set),
        "backtested": len(uni_funds),
        "ever_paused_n": len(ever),
        "ever_paused_pct": round(len(ever) / len(uni_funds), 4),
        "pause_median_months": (float(np.median([f["pause_months"]
                                                 for f in ever]))
                                if ever else None),
        "pause_median_episodes": (float(np.median([f["episodes"]
                                                   for f in ever]))
                                  if ever else None),
        "pause_month_share_median": (float(np.median(
            [f["pause_months"] / f["months"] for f in ever]))
            if ever else None),
        "data_end": str(latest.date()),
    },
    "case_codes": list(bear_pause.CASE_CODES),
    "top5": sorted(
        ({"code": f["code"], "name": names.get(f["code"], ""),
          "wallet_delta_b": round(f["tp_b_wallet"] - f["tp_a_wallet"], 2)}
         for f in ever),
        key=lambda r: abs(r["wallet_delta_b"]), reverse=True)[:5],
}

# ---- 落库（三表整表替换，单事务） ----
fund_rows = [tuple(f[c] for c in BEAR_PAUSE_FUND_COLS) for f in funds]
market_rows = [tuple(b[c] for c in BEAR_PAUSE_MARKET_COLS)
               for b in market_blocks]
with psycopg.connect(args.dsn, connect_timeout=10) as conn:
    ensure_bear_pause_schema(conn)          # 幂等建表（启动清单外兜底）
    nf, nm = import_bear_pause(conn, fund_rows, market_rows, params=params)
    conn.commit()
print(f"落库 fund_bear_pause_fund {nf:,} 行 / fund_bear_pause_market {nm} 行"
      f" / meta params {len(json.dumps(params, ensure_ascii=False))}B"
      f" / 总耗时 {time.time()-t0:.0f}s", flush=True)
