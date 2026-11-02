"""全市场基金「定投 + 20% 止盈」回测——离线 CSV 驱动手动跑批。

计算核心已迁入 backend/app/dca_backtest.py（与调度器日更共用同一实现，
口径约定见该模块 docstring）。本脚本只负责离线驱动：fund_scale.csv 选
宇宙 → PG 拉净值 → run_backtest → 写 scripts/data/*.csv → app.dca_store
导入刷新页面（也可由调度器日更自动完成，见 crawl_task 预置任务）。

输出 scripts/data/fund_dca_backtest.csv（每基金一行，双窗口指标；
xirr 三列 *_xirr / *_xirr_disc / *_xirr_nom 对应三场景）+ fund_dca_rounds.csv。
用法：.venv/bin/python scripts/fund_dca_backtest.py [--min-age-years N] [--min-scale-yi N]
"""

import argparse
import csv
import io
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import psycopg  # noqa: E402

from app.dca_backtest import run_backtest  # noqa: E402

DATA = Path(__file__).parent / "data"
SCALE_CSV = DATA / "fund_scale.csv"
OUT_CSV = DATA / "fund_dca_backtest.csv"


def main():
    import os
    ap = argparse.ArgumentParser(description="全市场基金「定投+20%止盈」回测")
    ap.add_argument("--min-age-years", type=float, default=0.0,
                    help="仅回测运作 ≥N 年的基金（首条净值距数据集最新净值日，"
                         "成立日期的可用代理；0=不过滤）")
    ap.add_argument("--min-scale-yi", type=float, default=0.0,
                    help="仅回测资产规模 ≥N 亿的基金（0=全市场不限规模，"
                         "含无规模披露的基金）")
    ap.add_argument("--dsn", default=os.environ.get(
        "DATABASE_URL",
        "postgresql://honey_money:honey_money@host.docker.internal:5433/honey_money"))
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

    with psycopg.connect(args.dsn, connect_timeout=10) as conn:
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
    end_ts = pd.Timestamp(nav["nav_date"].max())
    scale_map = {r["code"]: {"scale_yi": r.get("scale_yi"),
                             "scale_date": r.get("scale_date")}
                 for _, r in scale.iterrows()}

    if min_age_years:
        # 运作年限：首条净值距数据集最新净值日（fund_basic 无成立日期列，
        # 首次净值披露是其可靠代理）；不足门槛整体剔除
        def old_enough(g):
            first = pd.Timestamp(g["nav_date"].iloc[0])
            return (end_ts - first).days >= min_age_years * 365.25
        nav = nav.groupby("code", group_keys=False).filter(old_enough)
        kept = set(nav["code"].astype(str).unique())
        print(f"运作 ≥{min_age_years:g} 年门槛剔除 {len(selected) - len(kept)} 只")

    rows, rounds_rows = run_backtest(nav, basic, scale_map,
                                     end_ts=end_ts, log=print)
    out = pd.DataFrame(rows)
    if out.empty:
        print("无可回测的基金（过滤条件过严或无净值数据），不写文件")
        return
    out = out.sort_values("w10_xirr", ascending=False, na_position="last")
    out.to_csv(OUT_CSV, index=False)
    with (DATA / "fund_dca_rounds.csv").open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["code", "fee", "seq", "tp_date", "round_days", "gap_days",
                    "profit", "tp_nav", "invested", "sub_fee", "redeem_fee",
                    "net_profit"])
        w.writerows(rounds_rows)
    print(f"写入 {OUT_CSV}：{len(out)} 只 / 轮次 {len(rounds_rows)} 行")

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
