"""止盈点对比市场级聚合快照手动重建（fund_dca_tp_compare）。

日更任务每次导入后自动重建（scheduler._run_backtest_job →
dca_store.rebuild_tp_compare）；本脚本用于手动补口——脚本路径改了
fund_dca_tp_result（如 dca_share_sample.py 样本回填）后立即刷新快照，
免等夜更，对比卡（/api/funds/dca/tp/compare）随即恢复毫秒级快照取数。

用法（本机）：
  python3 scripts/rebuild_tp_compare.py [--dsn ...]
  （DSN 缺省取 DATABASE_URL 环境变量，再退回 NAS 生产库只读口径常量）
"""

import argparse
import os
import sys

import psycopg

sys.path.insert(0, "backend")

from app.dca_api import TP_COMPARE_EQUITY_TYPES    # noqa: E402
from app.dca_store import rebuild_tp_compare       # noqa: E402

DSN = "postgresql://honey_money:honey_money@10.0.0.169:5433/honey_money"

ap = argparse.ArgumentParser(
    description="重建止盈点对比市场级聚合快照（method×equity×fee×tp 240 行）")
ap.add_argument("--dsn", default=os.environ.get("DATABASE_URL", DSN))
args = ap.parse_args()

with psycopg.connect(args.dsn, connect_timeout=10) as conn:
    n = rebuild_tp_compare(conn, equity_types=TP_COMPARE_EQUITY_TYPES)
    conn.commit()
print(f"对比快照重建完成：{n} 行（fund_dca_tp_compare）")
