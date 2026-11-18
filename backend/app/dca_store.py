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
  fee        VARCHAR(8)  NOT NULL DEFAULT 'gross',
  seq        INTEGER NOT NULL,
  tp_date    DATE NOT NULL,
  round_days INTEGER,
  gap_days   INTEGER,
  profit     DOUBLE PRECISION,
  tp_nav     DOUBLE PRECISION,
  invested   DOUBLE PRECISION,
  sub_fee    DOUBLE PRECISION,
  redeem_fee DOUBLE PRECISION,
  net_profit DOUBLE PRECISION,
  PRIMARY KEY (code, fee, seq)
);
CREATE INDEX IF NOT EXISTS idx_fund_dca_rounds_code ON fund_dca_rounds (code);

ALTER TABLE fund_dca_result ADD COLUMN IF NOT EXISTS gap_avg_days DOUBLE PRECISION;
ALTER TABLE fund_dca_result ADD COLUMN IF NOT EXISTS gap_min_days INTEGER;
ALTER TABLE fund_dca_result ADD COLUMN IF NOT EXISTS gap_max_days INTEGER;
-- 费用三口径年化（gross 沿用原 *_xirr 列；disc=申购一折、nom=名义，赎回费一致）
ALTER TABLE fund_dca_result ADD COLUMN IF NOT EXISTS w10_xirr_disc DOUBLE PRECISION;
ALTER TABLE fund_dca_result ADD COLUMN IF NOT EXISTS w10_xirr_nom DOUBLE PRECISION;
ALTER TABLE fund_dca_result ADD COLUMN IF NOT EXISTS full_xirr_disc DOUBLE PRECISION;
ALTER TABLE fund_dca_result ADD COLUMN IF NOT EXISTS full_xirr_nom DOUBLE PRECISION;
-- 排行表格口径切换：轮次/总收益率也随口径入库（份额随申购费折损，
-- 止盈日与触发轮次可能位移）；间隔均值按口径由轮次表聚合（下方 _APPLY_GAP）
ALTER TABLE fund_dca_result ADD COLUMN IF NOT EXISTS w10_rounds_disc INTEGER;
ALTER TABLE fund_dca_result ADD COLUMN IF NOT EXISTS w10_rounds_nom INTEGER;
ALTER TABLE fund_dca_result ADD COLUMN IF NOT EXISTS full_rounds_disc INTEGER;
ALTER TABLE fund_dca_result ADD COLUMN IF NOT EXISTS full_rounds_nom INTEGER;
ALTER TABLE fund_dca_result ADD COLUMN IF NOT EXISTS w10_profit_rate_disc DOUBLE PRECISION;
ALTER TABLE fund_dca_result ADD COLUMN IF NOT EXISTS w10_profit_rate_nom DOUBLE PRECISION;
ALTER TABLE fund_dca_result ADD COLUMN IF NOT EXISTS full_profit_rate_disc DOUBLE PRECISION;
ALTER TABLE fund_dca_result ADD COLUMN IF NOT EXISTS full_profit_rate_nom DOUBLE PRECISION;
ALTER TABLE fund_dca_result ADD COLUMN IF NOT EXISTS gap_avg_days_disc DOUBLE PRECISION;
ALTER TABLE fund_dca_result ADD COLUMN IF NOT EXISTS gap_avg_days_nom DOUBLE PRECISION;

CREATE TABLE IF NOT EXISTS fund_dca_meta (
  id           BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id),
  generated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  fund_count   INTEGER,
  params       JSONB
);
"""

# 一次性迁移：v1 轮次表无 fee 列（PK code,seq），数据可由 CSV 全量重导，
# 检测到旧结构直接弃表重建（导入前 ensure_schema 兜底，生产由重导填充）
_MIGRATE_ROUNDS_V2 = """
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.tables
             WHERE table_name = 'fund_dca_rounds')
     AND NOT EXISTS (SELECT 1 FROM information_schema.columns
                     WHERE table_name = 'fund_dca_rounds'
                       AND column_name = 'fee') THEN
    DROP TABLE fund_dca_rounds;
  END IF;
END $$;
"""

# 列顺序即 INSERT 占位符顺序
_COLS = ["code", "name", "fund_type", "alive", "scale_yi", "scale_date",
         "w10_start", "w10_end", "w10_months", "w10_rounds",
         "w10_invested", "w10_final_value", "w10_profit_rate", "w10_xirr",
         "full_start", "full_end", "full_months", "full_rounds",
         "full_invested", "full_final_value", "full_profit_rate", "full_xirr",
         "w10_xirr_disc", "w10_xirr_nom", "full_xirr_disc", "full_xirr_nom",
         "w10_rounds_disc", "w10_rounds_nom", "full_rounds_disc",
         "full_rounds_nom", "w10_profit_rate_disc", "w10_profit_rate_nom",
         "full_profit_rate_disc", "full_profit_rate_nom"]

_DATE_COLS = {"scale_date", "w10_start", "w10_end", "full_start", "full_end"}
_INT_COLS = {"w10_months", "w10_rounds", "full_months", "full_rounds",
             "w10_rounds_disc", "w10_rounds_nom",
             "full_rounds_disc", "full_rounds_nom"}
_FLOAT_COLS = {"scale_yi", "w10_invested", "w10_final_value", "w10_profit_rate",
               "w10_xirr", "full_invested", "full_final_value",
               "full_profit_rate", "full_xirr",
               "w10_xirr_disc", "w10_xirr_nom", "full_xirr_disc", "full_xirr_nom",
               "w10_profit_rate_disc", "w10_profit_rate_nom",
               "full_profit_rate_disc", "full_profit_rate_nom"}

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


_ROUNDS_COLS = ["code", "fee", "seq", "tp_date", "round_days", "gap_days",
                "profit", "tp_nav", "invested", "sub_fee", "redeem_fee",
                "net_profit"]
_ROUNDS_DATE = {"tp_date"}
_ROUNDS_INT = {"seq", "round_days", "gap_days"}
_ROUNDS_FLOAT = {"profit", "tp_nav", "invested", "sub_fee", "redeem_fee",
                 "net_profit"}

_UPSERT_ROUND = f"""
INSERT INTO fund_dca_rounds ({", ".join(_ROUNDS_COLS)})
VALUES ({", ".join(["%s"] * len(_ROUNDS_COLS))})
"""

_RESET_GAP = """
UPDATE fund_dca_result SET gap_avg_days = NULL, gap_min_days = NULL,
  gap_max_days = NULL, gap_avg_days_disc = NULL, gap_avg_days_nom = NULL
"""

_APPLY_GAP = """
UPDATE fund_dca_result r SET
  gap_avg_days = t.g_avg, gap_min_days = t.g_min, gap_max_days = t.g_max,
  gap_avg_days_disc = t.d_avg, gap_avg_days_nom = t.n_avg
FROM (SELECT code,
             avg(gap_days) FILTER (WHERE fee = 'gross') AS g_avg,
             min(gap_days) FILTER (WHERE fee = 'gross') AS g_min,
             max(gap_days) FILTER (WHERE fee = 'gross') AS g_max,
             avg(gap_days) FILTER (WHERE fee = 'disc')  AS d_avg,
             avg(gap_days) FILTER (WHERE fee = 'nom')   AS n_avg
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



# ===== 多止盈点回测存储（市场级阈值对比，与旧表完全隔离；2026-11-15 定案） =====
# 列结构与旧表逐列一致 + tp 维度；旧表/旧导出/旧测试零改动，静态通道可复活。

TP_DDL = """
CREATE TABLE IF NOT EXISTS fund_dca_tp_result (
  tp              DOUBLE PRECISION NOT NULL,
  code            VARCHAR(10) NOT NULL,
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
  full_profit_rate DOUBLE PRECISION, full_xirr        DOUBLE PRECISION,
  w10_xirr_disc DOUBLE PRECISION, w10_xirr_nom DOUBLE PRECISION,
  full_xirr_disc DOUBLE PRECISION, full_xirr_nom DOUBLE PRECISION,
  w10_rounds_disc INTEGER, w10_rounds_nom INTEGER,
  full_rounds_disc INTEGER, full_rounds_nom INTEGER,
  w10_profit_rate_disc DOUBLE PRECISION, w10_profit_rate_nom DOUBLE PRECISION,
  full_profit_rate_disc DOUBLE PRECISION, full_profit_rate_nom DOUBLE PRECISION,
  gap_avg_days DOUBLE PRECISION, gap_min_days INTEGER, gap_max_days INTEGER,
  gap_avg_days_disc DOUBLE PRECISION, gap_avg_days_nom DOUBLE PRECISION,
  PRIMARY KEY (code, tp)
);
CREATE INDEX IF NOT EXISTS idx_fund_dca_tp_alive ON fund_dca_tp_result (alive);
CREATE INDEX IF NOT EXISTS idx_fund_dca_tp_tp ON fund_dca_tp_result (tp, w10_xirr);

CREATE TABLE IF NOT EXISTS fund_dca_tp_rounds (
  tp         DOUBLE PRECISION NOT NULL,
  code       VARCHAR(10) NOT NULL,
  fee        VARCHAR(8)  NOT NULL DEFAULT 'gross',
  seq        INTEGER NOT NULL,
  tp_date    DATE NOT NULL,
  round_days INTEGER,
  gap_days   INTEGER,
  profit     DOUBLE PRECISION,
  tp_nav     DOUBLE PRECISION,
  invested   DOUBLE PRECISION,
  sub_fee    DOUBLE PRECISION,
  redeem_fee DOUBLE PRECISION,
  net_profit DOUBLE PRECISION,
  PRIMARY KEY (code, tp, fee, seq)
);
CREATE INDEX IF NOT EXISTS idx_fund_dca_tp_rounds_code
  ON fund_dca_tp_rounds (code, tp);

CREATE TABLE IF NOT EXISTS fund_dca_tp_meta (
  id           BOOLEAN PRIMARY KEY DEFAULT TRUE CHECK (id),
  generated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  fund_count   INTEGER,
  params       JSONB
);

-- 定投时机（当前回撤桶，2026-11-16 假设验证：-25~-30% 为甜蜜点、30%+ 警戒）
CREATE TABLE IF NOT EXISTS fund_dip_timing (
  code            VARCHAR(10) PRIMARY KEY,
  dd_current      DOUBLE PRECISION,
  days_since_peak INTEGER,
  dip_band        VARCHAR(16),
  computed_at     DATE
);
CREATE INDEX IF NOT EXISTS idx_fund_dip_band ON fund_dip_timing (dip_band);

-- 起点条件回测报告（假设验证统计落库：三组对照/深度桶/起点年份 × 止盈线）
CREATE TABLE IF NOT EXISTS fund_dip_validation (
  grp             VARCHAR(8) NOT NULL,
  bucket          VARCHAR(16) NOT NULL,
  tp              DOUBLE PRECISION NOT NULL,
  n               INTEGER,
  days_median     INTEGER,
  days_p25        INTEGER,
  days_p75        INTEGER,
  success_rate    DOUBLE PRECISION,
  xirr_median     DOUBLE PRECISION,
  xirr_p25        DOUBLE PRECISION,
  xirr_p75        DOUBLE PRECISION,
  PRIMARY KEY (grp, bucket, tp)
);
"""

_TP_COLS = ["tp"] + _COLS
_TP_INSERT = f"""
INSERT INTO fund_dca_tp_result ({", ".join(_TP_COLS)})
VALUES ({", ".join(["%s"] * len(_TP_COLS))})
"""

# 列序必须与引擎元组 (code, tp, fee, seq, *ev) 逐位对齐——PG 隐式类型
# 转换会把错位写入"成功"（'000001'→tp 列 1.0），只能靠列清单对齐防错
_TP_ROUNDS_COLS = _ROUNDS_COLS[:1] + ["tp"] + _ROUNDS_COLS[1:]
_TP_UPSERT_ROUND = f"""
INSERT INTO fund_dca_tp_rounds ({", ".join(_TP_ROUNDS_COLS)})
VALUES ({", ".join(["%s"] * len(_TP_ROUNDS_COLS))})
"""

_TP_META_UPSERT = """
INSERT INTO fund_dca_tp_meta (id, generated_at, fund_count, params)
VALUES (TRUE, now(), %s, %s::jsonb)
ON CONFLICT (id) DO UPDATE SET
  generated_at = now(), fund_count = EXCLUDED.fund_count,
  params = EXCLUDED.params
"""

_RESET_GAP_TP = """
UPDATE fund_dca_tp_result SET gap_avg_days = NULL, gap_min_days = NULL,
  gap_max_days = NULL, gap_avg_days_disc = NULL, gap_avg_days_nom = NULL
"""

_APPLY_GAP_TP = """
UPDATE fund_dca_tp_result r SET
  gap_avg_days = t.g_avg, gap_min_days = t.g_min, gap_max_days = t.g_max,
  gap_avg_days_disc = t.d_avg, gap_avg_days_nom = t.n_avg
FROM (SELECT code, tp,
             avg(gap_days) FILTER (WHERE fee = 'gross') AS g_avg,
             min(gap_days) FILTER (WHERE fee = 'gross') AS g_min,
             max(gap_days) FILTER (WHERE fee = 'gross') AS g_max,
             avg(gap_days) FILTER (WHERE fee = 'disc')  AS d_avg,
             avg(gap_days) FILTER (WHERE fee = 'nom')   AS n_avg
      FROM fund_dca_tp_rounds GROUP BY code, tp) t
WHERE r.code = t.code AND r.tp = t.tp
"""


# 基金风险/波动指标（复权口径，risk_metrics.compute_risk_metrics 日更全量
# 重算整表替换；列表排序/详情指标卡/排行展示共用——独立 DDL 便于启动清单
# 兜底建表，不必等首轮日更）
RISK_DDL = """
CREATE TABLE IF NOT EXISTS fund_risk_metrics (
  code            VARCHAR(10) PRIMARY KEY,
  vol_1y DOUBLE PRECISION, vol_down_1y DOUBLE PRECISION,
  mdd_1y DOUBLE PRECISION, sharpe_1y DOUBLE PRECISION,
  vol_3y DOUBLE PRECISION, vol_down_3y DOUBLE PRECISION,
  mdd_3y DOUBLE PRECISION, sharpe_3y DOUBLE PRECISION,
  vol_5y DOUBLE PRECISION, vol_down_5y DOUBLE PRECISION,
  mdd_5y DOUBLE PRECISION, sharpe_5y DOUBLE PRECISION,
  vol_all DOUBLE PRECISION, vol_down_all DOUBLE PRECISION,
  mdd_all DOUBLE PRECISION, sharpe_all DOUBLE PRECISION,
  roll_vol        DOUBLE PRECISION,
  vol_pctile      DOUBLE PRECISION,
  risk_score      DOUBLE PRECISION,
  osc_per_year    DOUBLE PRECISION, osc_regularity DOUBLE PRECISION,
  osc_pctile      DOUBLE PRECISION,
  as_of           DATE,
  nav_count       INTEGER,
  computed_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

# 已有部署的表补列（CREATE TABLE IF NOT EXISTS 对已存在表不生效）
_RISK_MIGRATE = (
    "ALTER TABLE fund_risk_metrics "
    "ADD COLUMN IF NOT EXISTS risk_score DOUBLE PRECISION;\n"
    "ALTER TABLE fund_risk_metrics "
    "ADD COLUMN IF NOT EXISTS osc_per_year DOUBLE PRECISION;\n"
    "ALTER TABLE fund_risk_metrics "
    "ADD COLUMN IF NOT EXISTS osc_regularity DOUBLE PRECISION;\n"
    "ALTER TABLE fund_risk_metrics "
    "ADD COLUMN IF NOT EXISTS osc_pctile DOUBLE PRECISION;"
)

# 综合波动分位回填（幂等，整表重算）：年化波动率/下行波动率/最大回撤三项
# **全史口径**（*_all，2026-11-18 用户需求「波动指标统计整个历史」）在全样本
# （非 NULL 行）中的 percent_rank 横截面等权平均，round(4)，越低越稳
# （三项都偏低=波动小回撤浅）；三指标全缺 → NULL。全史窗口只需 ≥20 个收益
# 样本（MIN_RETURNS），比 1y 窗口覆盖更多新基金。
# 日更 import_risk 同事务调用 + 启动清单兜底调用（部署即得历史数据）。
_RISK_SCORE_BACKFILL = """
UPDATE fund_risk_metrics rm
SET risk_score = t.score, osc_pctile = t.opct
FROM (
  SELECT code,
         round(((coalesce(pv, 0) + coalesce(pd, 0) + coalesce(pm, 0))
               / NULLIF((CASE WHEN pv IS NOT NULL THEN 1 ELSE 0 END
                         + CASE WHEN pd IS NOT NULL THEN 1 ELSE 0 END
                         + CASE WHEN pm IS NOT NULL THEN 1 ELSE 0 END), 0))
               ::numeric, 4) AS score,
         CASE WHEN osc_per_year IS NULL THEN NULL
              ELSE round(percent_rank() OVER (PARTITION BY osc_per_year IS NULL
                                              ORDER BY osc_per_year)::numeric, 4)
         END AS opct
  FROM (
    SELECT code,
           CASE WHEN vol_all IS NULL THEN NULL
                ELSE percent_rank() OVER (PARTITION BY vol_all IS NULL
                                          ORDER BY vol_all) END AS pv,
           CASE WHEN vol_down_all IS NULL THEN NULL
                ELSE percent_rank() OVER (PARTITION BY vol_down_all IS NULL
                                          ORDER BY vol_down_all) END AS pd,
           CASE WHEN mdd_all IS NULL THEN NULL
                ELSE percent_rank() OVER (PARTITION BY mdd_all IS NULL
                                          ORDER BY mdd_all) END AS pm,
           osc_per_year
    FROM fund_risk_metrics
  ) x
) t
WHERE rm.code = t.code
"""


def backfill_risk_scores(conn) -> None:
    """综合波动分位横截面回填（幂等整表重算；事务由调用方提交）。"""
    with conn.cursor() as cur:
        cur.execute(_RISK_SCORE_BACKFILL)


def ensure_tp_schema(conn) -> None:
    """多阈值新表 + 波动指标表幂等建表（调度器日更首跑前调用）。"""
    with conn.cursor() as cur:
        cur.execute(TP_DDL)
        cur.execute(RISK_DDL)
        cur.execute(_RISK_MIGRATE)
        cur.execute(_RISK_SCORE_BACKFILL)


def ensure_risk_schema(conn) -> None:
    """波动指标表幂等建表（启动清单兜底，缺表时浏览接口不至于整页 down）。"""
    with conn.cursor() as cur:
        cur.execute(RISK_DDL)
        cur.execute(_RISK_MIGRATE)
        cur.execute(_RISK_SCORE_BACKFILL)


def import_tp_rows(conn, rows: list[dict], *, params: dict | None = None) -> int:
    """多阈值结果快照整体替换（DELETE+INSERT）+ meta 单行 upsert。

    meta.fund_count 为去重基金数（行数 = 基金 × 阈值）；事务由调用方提交。
    """
    with conn.cursor() as cur:
        cur.execute("DELETE FROM fund_dca_tp_result")
        if rows:
            cur.executemany(_TP_INSERT,
                            [tuple(r[c] for c in _TP_COLS) for r in rows])
        n_funds = len({r["code"] for r in rows})
        cur.execute(_TP_META_UPSERT,
                    (n_funds, json.dumps(params or {}, ensure_ascii=False)))
    return len(rows)


def import_tp_rounds(conn, rows: list[tuple]) -> int:
    """多阈值轮次整表替换 + 按 (code, tp) 刷新结果表间隔汇总列。"""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM fund_dca_tp_rounds")
        if rows:
            cur.executemany(_TP_UPSERT_ROUND, rows)
        cur.execute(_RESET_GAP_TP)
        cur.execute(_APPLY_GAP_TP)
    return len(rows)

_TIMING_COLS = ["code", "dd_current", "days_since_peak", "dip_band",
                "computed_at"]
_TIMING_INSERT = f"""
INSERT INTO fund_dip_timing ({", ".join(_TIMING_COLS)})
VALUES ({", ".join(["%s"] * len(_TIMING_COLS))})
"""


def import_timing(conn, rows: list[tuple]) -> int:
    """时机表整体替换（DELETE+INSERT，日更顺带刷新）；事务由调用方提交。"""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM fund_dip_timing")
        if rows:
            cur.executemany(_TIMING_INSERT, rows)
    return len(rows)


# 列序与 risk_metrics.ROW_FIELDS 逐位对齐（risk_row 产码，code 前缀 +
# computed_at 由库端 now() 填充）；str 日期入 DATE 列同 import_timing 先例
_RISK_COLS = ["code", "vol_1y", "vol_down_1y", "mdd_1y", "sharpe_1y",
              "vol_3y", "vol_down_3y", "mdd_3y", "sharpe_3y",
              "vol_5y", "vol_down_5y", "mdd_5y", "sharpe_5y",
              "vol_all", "vol_down_all", "mdd_all", "sharpe_all",
              "roll_vol", "vol_pctile", "osc_per_year", "osc_regularity",
              "as_of", "nav_count"]
_RISK_INSERT = f"""
INSERT INTO fund_risk_metrics ({", ".join(_RISK_COLS)})
VALUES ({", ".join(["%s"] * len(_RISK_COLS))})
"""


def import_risk(conn, rows: list[tuple]) -> int:
    """波动指标整表替换（DELETE+INSERT，日更顺带刷新）；事务由调用方提交。

    替换后同事务回填综合波动分位（横截面指标须全样本落库后统一计算）。"""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM fund_risk_metrics")
        if rows:
            cur.executemany(_RISK_INSERT, rows)
        cur.execute(_RISK_SCORE_BACKFILL)
    return len(rows)


_VALIDATION_COLS = ["grp", "bucket", "tp", "n", "days_median", "days_p25",
                    "days_p75", "success_rate", "xirr_median", "xirr_p25",
                    "xirr_p75"]
_VALIDATION_INSERT = f"""
INSERT INTO fund_dip_validation ({", ".join(_VALIDATION_COLS)})
VALUES ({", ".join(["%s"] * len(_VALIDATION_COLS))})
"""


def import_dip_validation(conn, rows: list[tuple]) -> int:
    """验证报告整表替换（三维度共用一张表，重跑后旧数据不得残留）；
    事务由调用方提交。"""
    with conn.cursor() as cur:
        cur.execute("DELETE FROM fund_dip_validation")
        if rows:
            cur.executemany(_VALIDATION_INSERT, rows)
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
        cur.execute(_MIGRATE_ROUNDS_V2)   # v1 轮次表（无 fee 列）一次性弃表重建
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
