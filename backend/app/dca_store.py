"""定投止盈回测结果存储：fund_dca_result / fund_dca_meta 两表 + CSV 导入。

回测是一次性离线产物（scripts/fund_dca_backtest.py 全市场逐基金模拟，
输出 scripts/data/fund_dca_backtest.csv），本模块把快照导入库中供
/api/funds/dca 查询。重新回测后重跑导入即刷新（DELETE+INSERT 幂等）。

alive（在市）口径在导入时计算：full_end（最后净值日）距数据集最新净值日
45 天内——fund_basic.delisted 按"不在披露渠道"差集标记，场内 ETF 会被
误标（见 fund-scale-and-dca-backtest 笔记），不能直接用。

CLI：.venv/bin/python -m app.dca_store <csv路径> [--amount 1000] [--take-profit 0.2]
"""

import argparse
import csv
import datetime as dt
import json
import os
from pathlib import Path

import psycopg

DDL = """
CREATE TABLE IF NOT EXISTS fund_dca_result (
  code            VARCHAR(10) PRIMARY KEY,
  name            VARCHAR(128) NOT NULL,
  fund_type       VARCHAR(64),
  alive           BOOLEAN NOT NULL DEFAULT FALSE,
  scale_yi        DOUBLE PRECISION,
  scale_date      DATE,
  w10_start       DATE, w10_end       DATE,
  w10_months      INTEGER, w10_rounds INTEGER,
  w10_invested    DOUBLE PRECISION, w10_final_value DOUBLE PRECISION,
  w10_profit_rate DOUBLE PRECISION, w10_xirr        DOUBLE PRECISION,
  full_start      DATE, full_end      DATE,
  full_months     INTEGER, full_rounds INTEGER,
  full_invested   DOUBLE PRECISION, full_final_value DOUBLE PRECISION,
  full_profit_rate DOUBLE PRECISION, full_xirr        DOUBLE PRECISION
);
CREATE INDEX IF NOT EXISTS idx_fund_dca_alive ON fund_dca_result (alive);
CREATE INDEX IF NOT EXISTS idx_fund_dca_w10_xirr ON fund_dca_result (w10_xirr);

CREATE TABLE IF NOT EXISTS fund_dca_rounds (
  code       VARCHAR(10) NOT NULL,
  seq        INTEGER NOT NULL,
  tp_date    DATE NOT NULL,
  round_days INTEGER,
  gap_days   INTEGER,
  profit     DOUBLE PRECISION,
  PRIMARY KEY (code, seq)
);
CREATE INDEX IF NOT EXISTS idx_fund_dca_rounds_code ON fund_dca_rounds (code);

ALTER TABLE fund_dca_result ADD COLUMN IF NOT EXISTS gap_avg_days DOUBLE PRECISION;
ALTER TABLE fund_dca_result ADD COLUMN IF NOT EXISTS gap_min_days INTEGER;
ALTER TABLE fund_dca_result ADD COLUMN IF NOT EXISTS gap_max_days INTEGER;

CREATE TABLE IF NOT EXISTS fund_dca_meta (
  id           BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id),
  generated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  fund_count   INTEGER,
  params       JSONB
);
"""

# 列顺序即 INSERT 占位符顺序
_COLS = ["code", "name", "fund_type", "alive", "scale_yi", "scale_date",
         "w10_start", "w10_end", "w10_months", "w10_rounds",
         "w10_invested", "w10_final_value", "w10_profit_rate", "w10_xirr",
         "full_start", "full_end", "full_months", "full_rounds",
         "full_invested", "full_final_value", "full_profit_rate", "full_xirr"]

_DATE_COLS = {"scale_date", "w10_start", "w10_end", "full_start", "full_end"}
_INT_COLS = {"w10_months", "w10_rounds", "full_months", "full_rounds"}
_FLOAT_COLS = {"scale_yi", "w10_invested", "w10_final_value", "w10_profit_rate",
               "w10_xirr", "full_invested", "full_final_value",
               "full_profit_rate", "full_xirr"}

_ALIVE_WINDOW_DAYS = 45   # 最后净值日距数据集最新净值日的在市窗口

_INSERT = f"""
INSERT INTO fund_dca_result ({", ".join(_COLS)})
VALUES ({", ".join(["%s"] * len(_COLS))})
"""

_META_UPSERT = """
INSERT INTO fund_dca_meta (id, generated_at, fund_count, params)
VALUES (TRUE, now(), %s, %s::jsonb)
ON CONFLICT (id) DO UPDATE SET
  generated_at = now(), fund_count = EXCLUDED.fund_count,
  params = EXCLUDED.params
"""


def _cell(col: str, raw: str):
    """CSV 文本 → 目标类型；空串/坏值 → None（坏日期记 None 而非炸导入）。"""
    raw = (raw or "").strip()
    if raw == "":
        return None
    try:
        if col in _DATE_COLS:
            return dt.date.fromisoformat(raw)
        if col in _INT_COLS:
            return int(float(raw))
        if col in _FLOAT_COLS:
            return float(raw)
    except ValueError:
        return None
    return raw


def parse_csv(path: str | Path) -> list[dict]:
    """fund_dca_backtest.csv → 行字典列表（含导入期计算的 alive）。"""
    with open(path, newline="") as f:
        raw_rows = list(csv.DictReader(f))
    rows = []
    for r in raw_rows:
        rows.append({col: _cell(col, r.get(col, "")) for col in _COLS})
    latest = max((r["full_end"] for r in rows if r["full_end"]), default=None)
    cutoff = (latest - dt.timedelta(days=_ALIVE_WINDOW_DAYS)) if latest else None
    for r in rows:
        r["alive"] = bool(cutoff and r["full_end"] and r["full_end"] >= cutoff)
    return rows


_ROUNDS_COLS = ["code", "seq", "tp_date", "round_days", "gap_days", "profit"]
_ROUNDS_DATE = {"tp_date"}
_ROUNDS_INT = {"seq", "round_days", "gap_days"}
_ROUNDS_FLOAT = {"profit"}

_UPSERT_ROUND = f"""
INSERT INTO fund_dca_rounds ({", ".join(_ROUNDS_COLS)})
VALUES ({", ".join(["%s"] * len(_ROUNDS_COLS))})
"""

_RESET_GAP = """
UPDATE fund_dca_result SET gap_avg_days = NULL, gap_min_days = NULL, gap_max_days = NULL
"""

_APPLY_GAP = """
UPDATE fund_dca_result r SET
  gap_avg_days = t.gap_avg, gap_min_days = t.gap_min, gap_max_days = t.gap_max
FROM (SELECT code, avg(gap_days) AS gap_avg,
             min(gap_days) AS gap_min, max(gap_days) AS gap_max
      FROM fund_dca_rounds GROUP BY code) t
WHERE r.code = t.code
"""


def parse_rounds_csv(path: str | Path) -> list[tuple]:
    """fund_dca_rounds.csv → (code, seq, tp_date, round_days, gap_days, profit) 元组列表。"""
    with open(path, newline="") as f:
        raw = list(csv.DictReader(f))
    rows = []
    for r in raw:
        vals = []
        for col in _ROUNDS_COLS:
            v = (r.get(col) or "").strip()
            try:
                if col in _ROUNDS_DATE:
                    v = dt.date.fromisoformat(v) if v else None
                elif col in _ROUNDS_INT:
                    v = int(float(v)) if v else None
                elif col in _ROUNDS_FLOAT:
                    v = float(v) if v else None
            except ValueError:
                v = None
            vals.append(v)
        rows.append(tuple(vals))
    return rows


def import_rounds(conn, rows: list[tuple]) -> int:
    """整表替换止盈轮次 + 刷新 fund_dca_result 间隔汇总列；事务由调用方提交。"""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM fund_dca_rounds")
        if rows:
            cur.executemany(_UPSERT_ROUND, rows)
        cur.execute(_RESET_GAP)
        cur.execute(_APPLY_GAP)
    return len(rows)


def import_rows(conn, rows: list[dict], *, params: dict | None = None) -> int:
    """快照整体替换（DELETE+INSERT）+ meta 单行 upsert；单事务由调用方提交。"""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM fund_dca_result")
        cur.executemany(_INSERT, [tuple(r[c] for c in _COLS) for r in rows])
        cur.execute(_META_UPSERT, (len(rows), json.dumps(params or {}, ensure_ascii=False)))
    return len(rows)


def ensure_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute(DDL)


def main():
    ap = argparse.ArgumentParser(description="导入定投止盈回测 CSV 到 fund_dca_result")
    ap.add_argument("csv", help="fund_dca_backtest.csv 路径")
    ap.add_argument("--dsn", default=os.environ.get("DATABASE_URL",
                     "postgresql://honey_money:honey_money@host.docker.internal:5433/honey_money"))
    ap.add_argument("--amount", type=float, default=1000.0, help="月定投金额（元）")
    ap.add_argument("--take-profit", type=float, default=0.20, help="止盈线（0.2=20%%）")
    ap.add_argument("--min-age-years", type=float, default=0.0,
                    help="运作年限门槛（年，0=不过滤；仅写入 meta 供页面展示口径）")
    ap.add_argument("--min-scale-yi", type=float, default=None,
                    help="规模下限（亿元；不传=全市场，仅写入 meta 供页面展示口径）")
    ap.add_argument("--rounds-csv", default=None,
                    help="止盈轮次 CSV（缺省=结果 CSV 同目录 fund_dca_rounds.csv，无则跳过）")
    args = ap.parse_args()

    rows = parse_csv(args.csv)
    rounds_path = (Path(args.rounds_csv) if args.rounds_csv else
                   Path(args.csv).parent / "fund_dca_rounds.csv")
    rounds = parse_rounds_csv(rounds_path) if rounds_path.exists() else []
    latest = max((r["full_end"] for r in rows if r["full_end"]), default=None)
    params = {"amount": args.amount, "take_profit": args.take_profit,
              "scale_min_yi": args.min_scale_yi, "frequency": "monthly",
              "min_age_years": args.min_age_years or None,
              "window_end": latest.isoformat() if latest else None,
              "source": "scripts/fund_dca_backtest.py"}
    with psycopg.connect(args.dsn, connect_timeout=10) as conn:
        ensure_schema(conn)                    # 幂等：本地/新库首导直接可用
        n = import_rows(conn, rows, params=params)
        nr = import_rounds(conn, rounds) if rounds else 0
        conn.commit()
    alive = sum(1 for r in rows if r["alive"])
    print(f"导入 {n} 行（在市 {alive}）→ fund_dca_result，窗口截至 {latest}；"
          f"止盈轮次 {nr} 行")


if __name__ == "__main__":
    main()
