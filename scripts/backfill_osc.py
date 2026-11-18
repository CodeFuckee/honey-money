"""一次性回填 fund_risk_metrics 震荡指标（osc_per_year / osc_regularity）。

背景：震荡指标（zigzag ±10% 摆动，2026-11-18 接入）需逐基金重算全史
净值链，无法从既有 rm 列 SQL 推导——本脚本复用日更管线的取数与口径
（_load_nav / prep_window / zigzag_oscillation），只为存量行补 osc 两列；
osc_pctile 横截面百分位随后由 dca_store._RISK_SCORE_BACKFILL（幂等，
与 risk_score 同语句）统一重算。日更 import_risk 自新代码起已顺带写入
osc 两列，本脚本仅用于「代码上线早于日更窗口」时的即时回填。

用法（NAS 生产直连，只读 nav + 写 rm 两列）：
    DATABASE_URL=postgresql://honey_money:...@10.0.0.169:5433/honey_money \
        python scripts/backfill_osc.py
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

import psycopg  # noqa: E402

from app.dca_backtest import _load_nav, prep_window  # noqa: E402
from app.dca_store import _RISK_MIGRATE, _RISK_SCORE_BACKFILL  # noqa: E402
from app.risk_metrics import zigzag_oscillation  # noqa: E402


def main() -> None:
    dsn = os.environ.get("DATABASE_URL",
                         "postgresql://honey_money:honey_money"
                         "@host.docker.internal:5433/honey_money")
    with psycopg.connect(dsn, connect_timeout=10) as conn:
        with conn.cursor() as cur:
            cur.execute(_RISK_MIGRATE)   # 幂等补列（新列部署前先手工建）
            conn.commit()
            cur.execute("SELECT code FROM fund_risk_metrics")
            codes = [r[0] for r in cur.fetchall()]
        print(f"rm 存量行：{len(codes):,}")
        nav = _load_nav(conn, codes)
        print(f"净值行：{len(nav):,}")
        updates = []
        for code, g in nav.groupby("code", sort=True):
            prep = prep_window(g.sort_values("nav_date"), None)
            if prep is None:
                continue
            osc = zigzag_oscillation(prep[1])
            updates.append((osc["osc_per_year"], osc["osc_regularity"], code))
        with conn.cursor() as cur:
            cur.executemany(
                "UPDATE fund_risk_metrics SET osc_per_year = %s, "
                "osc_regularity = %s WHERE code = %s", updates)
            cur.execute(_RISK_SCORE_BACKFILL)   # 横截面：osc_pctile + risk_score
            n = cur.rowcount
            cur.execute("SELECT count(*), count(osc_pctile) "
                        "FROM fund_risk_metrics WHERE osc_per_year IS NOT NULL")
            total, scored = cur.fetchone()
        conn.commit()
    print(f"osc 回填 {len(updates):,} 行；横截面重算 {n:,} 行；"
          f"有震荡值 {scored}/{total} 只已评分")


if __name__ == "__main__":
    main()
