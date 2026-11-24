"""等份额定投样本回填（过目用：只写 method='share' 的样本行，不动全量）。

计算：app.dca_backtest.run_backtest_tp(method='share')（与日更同引擎同口径）；
存储：**范围替换**——只 DELETE/INSERT `method='share' AND code = ANY(样本)`
的行（日更 import_tp_* 是整表替换，本脚本绝不调它，避免把等额全量数据
清掉）；间隔聚合同样按样本范围重算。alive 45 天口径与 run_full_backtest_tp
一致。当晚日更会整表重写全市场双 method 数据，样本行自然被全量覆盖。

用法（本机）：
  .venv/bin/python scripts/dca_share_sample.py [--limit 400] [--dsn ...]
"""

import argparse
import sys

import pandas as pd
import psycopg

sys.path.insert(0, "backend")

from app import dca_backtest                      # noqa: E402
from app.dca_store import (_TP_INSERT, _TP_UPSERT_ROUND, _TP_COLS,  # noqa: E402
                           _TP_ROUNDS_COLS, ensure_tp_schema)

DSN = "postgresql://honey_money:honey_money@10.0.0.169:5433/honey_money"

ap = argparse.ArgumentParser(description="非等额定投方式样本回填（只写指定 method 行）")
ap.add_argument("--dsn", default=DSN)
ap.add_argument("--limit", type=int, default=400, help="样本基金数")
ap.add_argument("--method", default="share",
                choices=["share", "dd_ladder", "dd_ladder_deep",
                         "profit_dd_ladder"],
                help="定投方式（share 等份额 / dd_ladder 回撤梯度加码 / "
                     "dd_ladder_deep 深回撤加码 / profit_dd_ladder 浮盈阶梯）")
args = ap.parse_args()

with psycopg.connect(args.dsn, connect_timeout=10) as conn:
    # 样本：在市基金里取运作最久的 N 只（长窗口最能看出等份额的差异面）
    with conn.cursor() as cur:
        cur.execute(
            "SELECT code FROM fund_dca_result WHERE alive "
            "ORDER BY full_months DESC NULLS LAST LIMIT %s", (args.limit,))
        codes = [r[0] for r in cur.fetchall()]
    print(f"样本基金：{len(codes):,} 只（在市 · 运作最久优先）", flush=True)
    basic = dca_backtest._load_basic(conn, codes)
    scale_map = dca_backtest._load_scale(conn)
    nav = dca_backtest._load_nav(conn, codes)
print(f"净值行：{len(nav):,}", flush=True)

rows, rounds_rows = dca_backtest.run_backtest_tp(
    nav, basic, scale_map, method=args.method,
    # 进度日志带 flush=True 关键字（dca_backtest 452/553），log 契约是
    # print 兼容 callable——单参数 lambda 在样本 ≥1000 只时 TypeError
    # （n_funds % 1000 触发点，2026-11-23 全量回放踩出）
    log=lambda m, **k: print(m, **k))
print(f"{args.method} 结果行：{len(rows):,} / 轮次行：{len(rounds_rows):,}", flush=True)

# alive 45 天口径（与 run_full_backtest_tp 同源逻辑，样本范围内）
all_end = max((r["full_end"] for r in rows if r["full_end"]), default=None)
cutoff = (pd.Timestamp(all_end) - pd.Timedelta(days=dca_backtest._ALIVE_WINDOW_DAYS)
          if all_end else None)
for r in rows:
    r["alive"] = bool(cutoff and r["full_end"]
                      and pd.Timestamp(r["full_end"]) >= cutoff)

arr = "{" + ",".join(codes) + "}"
with psycopg.connect(args.dsn) as conn:
    ensure_tp_schema(conn)          # 幂等迁移（含 method 列与 PK 重建）
    with conn.cursor() as cur:
        cur.execute("DELETE FROM fund_dca_tp_result "
                    "WHERE method = %s AND code = ANY(%s)", (args.method, arr))
        cur.execute("DELETE FROM fund_dca_tp_rounds "
                    "WHERE method = %s AND code = ANY(%s)", (args.method, arr))
        if rows:
            cur.executemany(_TP_INSERT,
                            [tuple(r[c] for c in _TP_COLS) for r in rows])
        if rounds_rows:
            cur.executemany(_TP_UPSERT_ROUND, rounds_rows)
        # 间隔聚合按样本范围重算（绝不动全表 RESET，防清掉等额全量列）
        cur.execute(
            "UPDATE fund_dca_tp_result SET gap_avg_days = NULL, "
            "gap_min_days = NULL, gap_max_days = NULL, "
            "gap_avg_days_disc = NULL, gap_avg_days_nom = NULL "
            "WHERE method = %s AND code = ANY(%s)", (args.method, arr))
        cur.execute(
            "UPDATE fund_dca_tp_result r SET "
            "gap_avg_days = t.g_avg, gap_min_days = t.g_min, "
            "gap_max_days = t.g_max, gap_avg_days_disc = t.d_avg, "
            "gap_avg_days_nom = t.n_avg "
            "FROM (SELECT code, tp, "
            "      avg(gap_days) FILTER (WHERE fee = 'gross') AS g_avg, "
            "      min(gap_days) FILTER (WHERE fee = 'gross') AS g_min, "
            "      max(gap_days) FILTER (WHERE fee = 'gross') AS g_max, "
            "      avg(gap_days) FILTER (WHERE fee = 'disc')  AS d_avg, "
            "      avg(gap_days) FILTER (WHERE fee = 'nom')   AS n_avg "
            "      FROM fund_dca_tp_rounds "
            "      WHERE method = %s AND code = ANY(%s) "
            "      GROUP BY code, tp, method) t "
            "WHERE r.code = t.code AND r.tp = t.tp AND r.method = %s",
            (args.method, arr, args.method))
    conn.commit()
print(f"样本回填完成（method={args.method} 范围替换；当晚日更整表重写为全量）",
      flush=True)
